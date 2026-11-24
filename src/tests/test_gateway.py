"""WebSocket ゲートウェイ（2026-09-03）。本物の serve を子に起動し、線の向こうから ping→pong が返ることで中継を確かめる
（推論機は要らない: serve は ping に pong で答える）。合言葉・上限・切断で子が消えること。"""
from __future__ import annotations

import asyncio
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import websockets
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve as ws_serve

from laps import gateway as gwmod


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


async def _with_gateway(tmp_path: Path, token, max_conn, fn):
    gw = gwmod.Gateway(token=token, workspace=tmp_path / "ws", home=tmp_path / "home", max_conn=max_conn,
                       backend="http://127.0.0.1:1/v1", model="x", policy="trusted", toolset="focused")
    port = _free_port()
    async with ws_serve(gw.handler, "127.0.0.1", port, process_request=gw.process_request):
        await fn(gw, port)


@pytest.mark.timeout(60) if hasattr(pytest.mark, "timeout") else (lambda f: f)
def test_health_token_relay_and_teardown(tmp_path):
    async def body(gw, port):
        # /health は素の HTTP
        raw = await asyncio.get_running_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).read())
        h = json.loads(raw); assert h["ok"] is True and h["auth"] is True and h["connections"] == 0
        # 合言葉なし → 401
        with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
            async with connect(f"ws://127.0.0.1:{port}/ws"):
                pass
        assert ei.value.response.status_code == 401
        # 合言葉あり → serve が起動し ready が来て、ping に pong が返る（本物の serve・推論機不要）
        async with connect(f"ws://127.0.0.1:{port}/ws?token=s3cret") as c:
            first = json.loads(await asyncio.wait_for(c.recv(), 20))
            assert first.get("type") == "notice" and first.get("kind") == "ready", first
            assert len(gw.live) == 1
            await c.send(json.dumps({"type": "ping", "id": "p1"}))
            for _ in range(20):
                m = json.loads(await asyncio.wait_for(c.recv(), 20))
                if m.get("kind") == "pong":
                    assert m.get("id") == "p1"; break
            else:
                raise AssertionError("pong が来ない")
            proc = list(gw.live.values())[0]
        # 切断 → serve が消える（PID を待つ・名前撃ちしない）
        for _ in range(60):
            if proc.returncode is not None and not gw.live:
                break
            await asyncio.sleep(0.1)
        assert proc.returncode is not None and not gw.live
    asyncio.run(_with_gateway(tmp_path, "s3cret", 4, body))


def test_connection_limit_and_no_auth_mode(tmp_path):
    async def body(gw, port):
        async with connect(f"ws://127.0.0.1:{port}/ws") as c1:      # 認証なし（token=None）
            await asyncio.wait_for(c1.recv(), 20)
            with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
                async with connect(f"ws://127.0.0.1:{port}/ws"):
                    pass
            assert ei.value.response.status_code == 503
    asyncio.run(_with_gateway(tmp_path, None, 1, body))


def test_sessions_listing_reads_first_user_line(tmp_path):
    gw = gwmod.Gateway(token=None, workspace=tmp_path / "ws", home=tmp_path / "home", max_conn=1,
                       backend="", model="", policy="trusted", toolset="focused")
    d = tmp_path / "home" / "sessions" / "20260903_070000_abc"; d.mkdir(parents=True)
    (d / "messages.jsonl").write_text(json.dumps({"role": "user", "content": "README を書いて"}) + "\n", encoding="utf-8")
    s = gw.sessions(); assert s and s[0]["id"] == "20260903_070000_abc" and s[0]["first"] == "README を書いて"


def test_token_resolution(monkeypatch, tmp_path):
    monkeypatch.setattr(gwmod, "TOKEN_FILE", tmp_path / "gateway.token")
    monkeypatch.delenv("LAPS_GATEWAY_TOKEN", raising=False)
    t1 = gwmod.resolve_token(); assert t1 and (tmp_path / "gateway.token").read_text().strip() == t1
    assert gwmod.resolve_token() == t1                               # 2 回目は同じ
    monkeypatch.setenv("LAPS_GATEWAY_TOKEN", "")
    assert gwmod.resolve_token() is None                             # 空＝認証なし
    monkeypatch.setenv("LAPS_GATEWAY_TOKEN", "abc")
    assert gwmod.resolve_token() == "abc"


def test_session_resume_replays_since_seq_and_grace_reaps(tmp_path):
    """切断しても猶予のあいだ serve は生き、同じ session で戻れば since_seq より後の行が再送される。
    猶予を過ぎたら serve が止まる（PID を待つ）。本物の serve・推論機不要（ping→pong）。"""
    async def body(gw, port):
        gw.grace = 1.5
        async with connect(f"ws://127.0.0.1:{port}/ws?session=A") as c:
            first = json.loads(await asyncio.wait_for(c.recv(), 20)); assert first.get("kind") == "ready"
            await c.send(json.dumps({"type": "ping", "id": "p1"}))
            pong = None
            for _ in range(20):
                m = json.loads(await asyncio.wait_for(c.recv(), 20))
                if m.get("kind") == "pong":
                    pong = m; break
            assert pong is not None
            ses = gw.sessions_live["A"]; pid = ses.proc.pid; last = ses.last_seq
        # 切断直後: serve は生きている（猶予）
        await asyncio.sleep(0.3)
        assert "A" in gw.sessions_live and gw.sessions_live["A"].proc.returncode is None
        # 同じ session で戻る: since_seq=0 なら ready と pong が再送される／since_seq=last なら再送 0
        async with connect(f"ws://127.0.0.1:{port}/ws?session=A&since_seq=0") as c2:
            got = []
            for _ in range(10):
                try:
                    got.append(json.loads(await asyncio.wait_for(c2.recv(), 1.0)))
                except asyncio.TimeoutError:
                    break
            kinds = [g.get("kind") for g in got]
            assert "ready" in kinds and "pong" in kinds, kinds
            assert gw.sessions_live["A"].proc.pid == pid           # 同じ serve に戻った
            await c2.send(json.dumps({"type": "ping", "id": "p2"}))  # 続きも流れる
            for _ in range(20):
                m = json.loads(await asyncio.wait_for(c2.recv(), 20))
                if m.get("kind") == "pong" and m.get("id") == "p2":
                    break
            else:
                raise AssertionError("再接続後の pong が来ない")
        # 猶予を過ぎる → serve が止まる
        for _ in range(60):
            if "A" not in gw.sessions_live:
                break
            await asyncio.sleep(0.1)
        assert "A" not in gw.sessions_live
    asyncio.run(_with_gateway(tmp_path, None, 4, body))


def test_wrong_token_is_401_not_just_missing_token(tmp_path):
    """合言葉「あり」だが**違う**ときも 401（無しの 401 としか試験していなかった）。"""
    async def body(gw, port):
        with pytest.raises(websockets.exceptions.InvalidStatus) as ei:
            async with connect(f"ws://127.0.0.1:{port}/ws?token=wrong"):
                pass
        assert ei.value.response.status_code == 401
    asyncio.run(_with_gateway(tmp_path, "s3cret", 4, body))


def test_takeover_closes_old_connection_with_4000(tmp_path):
    """同じ session に 2 本目が繋ぐと、1 本目は code=4000 で閉じられる（★従来は目視のみ・未試験だった）。"""
    async def body(gw, port):
        async with connect(f"ws://127.0.0.1:{port}/ws?session=A") as c1:
            first = json.loads(await asyncio.wait_for(c1.recv(), 20)); assert first.get("kind") == "ready"
            async with connect(f"ws://127.0.0.1:{port}/ws?session=A") as c2:
                second = json.loads(await asyncio.wait_for(c2.recv(), 20))
                # 2 本目には ready ではなく再送（無ければ何も来ないこともある）が先に来ることがあるので、
                # ready が来るまで読み進める。
                assert isinstance(second, dict)
                # 1 本目は引き継ぎで閉じられる
                with pytest.raises(websockets.exceptions.ConnectionClosed):
                    await asyncio.wait_for(c1.recv(), 20)
                assert c1.close_code == 4000, (c1.close_code, c1.close_reason)
    asyncio.run(_with_gateway(tmp_path, None, 4, body))


def test_serve_death_closes_client_instead_of_hanging_forever(tmp_path):
    """★2026-09-03 に見つけた壊れ方: serve が（gateway 経由でなく）自分で quit すると、
    子は死ぬが**誰にも伝わらず接続は開いたまま**——クライアントは新しい行を永久に待つ
    （「done が出ない＝画面が永久に考え中」）。直した後: 死んだら WS を code=4001 で閉じ、
    session をすぐに手放す（max_conn の枠を占有し続けない）。"""
    async def body(gw, port):
        async with connect(f"ws://127.0.0.1:{port}/ws?session=B") as c:
            first = json.loads(await asyncio.wait_for(c.recv(), 20)); assert first.get("kind") == "ready"
            assert "B" in gw.sessions_live
            # serve 自身に quit させる（gateway の _shutdown を経由しない＝「勝手に終わった」を再現）
            await c.send(json.dumps({"type": "quit", "id": "q1"}))
            # quit の返事（notice kind=shutdown）が先に 1 行来ることがある。それを読み飛ばしてから閉じるのを待つ。
            with pytest.raises(websockets.exceptions.ConnectionClosed):
                for _ in range(5):
                    await asyncio.wait_for(c.recv(), 20)
            assert c.close_code == 4001, (c.close_code, c.close_reason)
        # session の枠もすぐ手放している（子が死んだのに sessions_live に残らない）
        for _ in range(50):
            if "B" not in gw.sessions_live and not gw.live:
                break
            await asyncio.sleep(0.1)
        assert "B" not in gw.sessions_live
        assert not gw.live
    asyncio.run(_with_gateway(tmp_path, None, 4, body))


def test_stale_since_seq_beyond_buffer_is_logged_not_silently_gapped(tmp_path):
    """控え（4,000 行）より前を指す since_seq は埋められない。★以前は何のログも出さず、
    再送件数だけを見ると「取りこぼし無し」に見えた。いまは差が分かるログが 1 行出る。"""
    async def body(gw, port):
        async with connect(f"ws://127.0.0.1:{port}/ws?session=C") as c:
            await asyncio.wait_for(c.recv(), 20)
        ses = gw.sessions_live["C"]
        ses.buffer.clear()
        for i in range(100, 110):
            ses.buffer.append((i, json.dumps({"seq": i, "type": "token", "text": "x"})))
        import io
        import sys
        buf = io.StringIO()
        old = sys.stderr
        sys.stderr = buf
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws?session=C&since_seq=1") as c2:
                for _ in range(5):
                    try:
                        await asyncio.wait_for(c2.recv(), 1.0)
                    except asyncio.TimeoutError:
                        break
        finally:
            sys.stderr = old
        assert "控えの外" in buf.getvalue(), buf.getvalue()
    asyncio.run(_with_gateway(tmp_path, None, 4, body))


def test_sessions_filters_events_endpoint_and_token_on_http(tmp_path):
    """画面担当の要望: 一覧の絞り込み・過去走行の中身・一覧にも合言葉。"""
    async def body(gw, port):
        home = gw.home / "sessions"
        a = home / "20260903_010000_aaa"; a.mkdir(parents=True)
        (a / "messages.jsonl").write_text(json.dumps({"role": "user", "content": "A の依頼"}) + "\n", encoding="utf-8")
        (a / "events.jsonl").write_text("\n".join(json.dumps({"v": 1, "seq": i, "type": "token", "text": f"t{i}"}) for i in range(1, 6)) + "\n", encoding="utf-8")
        b = home / "20260903_020000_bbb"; b.mkdir(parents=True)          # events 無し（空の走行）
        (b / "messages.jsonl").write_text(json.dumps({"role": "user", "content": "B"}) + "\n", encoding="utf-8")
        loop = asyncio.get_running_loop()
        def get(path, with_auth=True):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"X-Blackwall-Token": "s3cret"} if with_auth else {})
                return urllib.request.urlopen(req, timeout=5).read().decode()
            except urllib.error.HTTPError as e:
                return f"HTTP {e.code}"
        assert (await loop.run_in_executor(None, lambda: get("/sessions", False))) == "HTTP 401"          # 合言葉が要る
        rows = json.loads(await loop.run_in_executor(None, get, "/sessions"))
        assert [r["id"] for r in rows] == ["20260903_020000_bbb", "20260903_010000_aaa"]
        rows = json.loads(await loop.run_in_executor(None, get, "/sessions?has_events=1"))
        assert [r["id"] for r in rows] == ["20260903_010000_aaa"] and rows[0]["events_bytes"] > 0
        rows = json.loads(await loop.run_in_executor(None, get, "/sessions?order=asc&limit=1"))
        assert [r["id"] for r in rows] == ["20260903_010000_aaa"]
        ev = await loop.run_in_executor(None, get, "/sessions/20260903_010000_aaa/events?since_seq=3")
        seqs = [json.loads(l)["seq"] for l in ev.splitlines()]
        assert seqs == [4, 5]
        ev = await loop.run_in_executor(None, get, "/sessions/20260903_010000_aaa/events?limit=2")
        assert [json.loads(l)["seq"] for l in ev.splitlines()] == [1, 2]
        assert (await loop.run_in_executor(None, get, "/sessions/nope/events")) == "HTTP 404"
        assert (await loop.run_in_executor(None, get, "/sessions/../etc/events")) in ("HTTP 404",)
    asyncio.run(_with_gateway(tmp_path, "s3cret", 4, body))
