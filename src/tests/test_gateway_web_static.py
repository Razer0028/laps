"""ゲートウェイの静的配信（`/web/`）— 2026-09-07。

★なぜ生の HTTP で書くか: `urllib` も `httpx` も、送る前に経路の `..` を**自分で畳む**。
  それでは「畳まずに送られてきたとき、こちらが断るか」を一度も試していないことになる
  （動作確認のつもりで、実際には自分の client を試験していた——という型を過去に踏んでいる）。
  ここは socket に生の 1 行を書いて、`GET /web/../gateway.py` がそのまま届く形で確かめる。

見るもの:
  - `/web/` と `/web` が index.html を返す（Content-Type つき）
  - **合言葉が要らない**（画面のコードだけ。要求すると合言葉を入れる画面すら出せない）
  - それ以外の口（`/sessions`）は従来どおり合言葉が要る
  - `..`・`%2e%2e`・`//`・symlink で webui/ の外へ出られない
  - 表に無い拡張子は出さない
  - `/health` が `web: true` を名乗る
"""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest
from websockets.asyncio.server import serve as ws_serve

from laps import gateway as gwmod


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _raw_get(port: int, raw_path: str, timeout: float = 5.0) -> tuple[int, dict, bytes]:
    """経路を**畳まずに**そのまま送る。返りは (status, headers, body)。"""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(f"GET {raw_path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
                  .encode("latin-1"))
        chunks = []
        while True:
            b = s.recv(65536)
            if not b:
                break
            chunks.append(b)
    data = b"".join(chunks)
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, body


async def _with_gateway(tmp_path: Path, token, fn):
    gw = gwmod.Gateway(token=token, workspace=tmp_path / "ws", home=tmp_path / "home", max_conn=2,
                       backend="http://127.0.0.1:1/v1", model="x", policy="trusted", toolset="focused")
    port = _free_port()
    async with ws_serve(gw.handler, "127.0.0.1", port, process_request=gw.process_request):
        await asyncio.get_running_loop().run_in_executor(None, fn, port)


def test_web_is_served_without_a_token_and_stays_inside_webui(tmp_path):
    def body(port: int) -> None:
        # ① 画面は合言葉なしで取れる（合言葉を入れる画面そのものだから）
        for path in ("/web/", "/web", "/web/index.html"):
            st, h, b = _raw_get(port, path)
            assert st == 200, (path, st)
            assert h["content-type"] == "text/html; charset=utf-8", (path, h)
            assert b"L.A.P.S" in b and b"app.js" in b, path
        st, h, b = _raw_get(port, "/web/app.js")
        assert st == 200 and h["content-type"] == "text/javascript; charset=utf-8"
        assert b"bwReduce" in b
        st, h, b = _raw_get(port, "/web/styles.css")
        assert st == 200 and h["content-type"] == "text/css; charset=utf-8"

        # ② それ以外の口は従来どおり合言葉が要る（画面だけを開けた＝全部開けた、ではない）
        st, _, _ = _raw_get(port, "/sessions")
        assert st == 401
        st, _, _ = _raw_get(port, "/ws")
        assert st == 401

        # ③ webui/ の外へは出られない。**畳まずに**送る（client が畳むと何も試していない）
        for path in ("/web/../gateway.py", "/web/%2e%2e/gateway.py", "/web/..%2fgateway.py",
                     "/web/../../deploy/gateway.token", "/web//../gateway.py",
                     "/web/subdir/../../gateway.py"):
            st, _, _ = _raw_get(port, path)
            assert st in (400, 404), (path, st)

        # ④ 無い物・表に無い拡張子は 404（webui/ に何かが紛れ込んでも出て行かない）
        for path in ("/web/nope.js", "/web/run_smoke.py", "/web/../webui/app.js"):
            st, _, _ = _raw_get(port, path)
            assert st in (400, 404), (path, st)

        # ⑤ /health は合言葉なしで、画面が在ることを名乗る
        st, _, b = _raw_get(port, "/health")
        assert st == 200
        h = json.loads(b)
        assert h["web"] is True and h["auth"] is True

    asyncio.run(_with_gateway(tmp_path, "s3cret", body))


def test_a_symlink_out_of_webui_is_not_served(tmp_path, monkeypatch):
    """symlink は「経路の文字」では見つからない。resolve した先で判定していることを確かめる。"""
    web = tmp_path / "webui"; web.mkdir()
    (web / "index.html").write_text("<b>ok</b>", encoding="utf-8")
    secret = tmp_path / "secret.txt"; secret.write_text("token=xyz", encoding="utf-8")
    (web / "leak.txt").symlink_to(secret)
    monkeypatch.setattr(gwmod, "WEB_DIR", web)

    def body(port: int) -> None:
        st, _, b = _raw_get(port, "/web/")
        assert st == 200 and b"ok" in b
        st, _, b = _raw_get(port, "/web/leak.txt")
        assert st == 404, (st, b)

    asyncio.run(_with_gateway(tmp_path, None, body))


def test_health_says_no_web_when_the_files_are_absent(tmp_path, monkeypatch):
    """画面を置いていない 開発機 で「404 なのは合言葉のせいか、置いていないのか」を分けられること。"""
    monkeypatch.setattr(gwmod, "WEB_DIR", tmp_path / "nothing-here")

    def body(port: int) -> None:
        st, _, b = _raw_get(port, "/health")
        assert st == 200 and json.loads(b)["web"] is False
        st, _, _ = _raw_get(port, "/web/")
        assert st == 404

    asyncio.run(_with_gateway(tmp_path, None, body))


@pytest.mark.parametrize("path", ["/webfoo", "/webui/app.js"])
def test_only_the_web_prefix_is_static(tmp_path, path):
    """`/web` で**始まる**だけの経路を静的配信に吸わせない（`/webfoo` は従来どおりの扱い）。"""
    def body(port: int) -> None:
        st, _, _ = _raw_get(port, path)
        assert st == 401          # 合言葉の門が先に立つ＝静的配信には入っていない

    asyncio.run(_with_gateway(tmp_path, "s3cret", body))
