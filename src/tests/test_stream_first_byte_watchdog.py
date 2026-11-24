"""逐次配信の「最初の 1 byte」番犬＋一括フォールバック（2026-09-06 P0）。

★実機で起きた形:
  道具付き逐次配信は llama.cpp の --jinja + tools のもとで prefill が終わるまで
  1 byte も返さない（実測: 38k tok の接頭辞で first_line が 21.6s）。prefill が
  read timeout(90s) を超える状況（モデル入替・巨大プロンプト・P40 混雑）だと、
  以前は 90s×4 の逐次再試行＋一括 90s ＝ 最悪 450s、利用者は「推論中」のまま。

  番犬は「最初の 1 byte」だけを別枠で縛り、超えたら stream を切って **相手の
  生存を見てから一括で1回だけ**取りに行く。相手が死んでいれば二重待ちしない。
"""
from __future__ import annotations

import json
import threading
import time
import http.server

import httpx
import pytest

from laps.backend import Backend, BackendConfig


class _Handler(http.server.BaseHTTPRequestHandler):
    # クラス変数で挙動を制御する
    prefill_delay = 5.0        # 逐次のとき本文を返すまでの沈黙（＝長い prefill）
    nonstream_answer = "答え"  # 一括のとき返す本文
    nonstream_delay = 0.05     # 一括は速い（＝番犬が切った後、温まった KV で速攻）

    def log_message(self, *a):  # 静かに
        pass

    def do_GET(self):
        if self.path.rstrip("/").endswith("/health"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

    def _read_body(self):
        n = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_POST(self):
        body = self._read_body()
        streaming = bool(body.get("stream"))
        if streaming:
            # ヘッダだけ返して沈黙する＝prefill が長い状況の再現。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.flush()
            except Exception:
                pass
            time.sleep(self.prefill_delay)     # 本文を返さないまま待つ
            # ここまで来たら（＝切られなければ）一応 [DONE] を返す
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
        else:
            time.sleep(self.nonstream_delay)
            payload = {
                "choices": [{"message": {"content": self.nonstream_answer,
                                         "tool_calls": []}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


@pytest.fixture
def slow_stream_server():
    """/health は速い（相手は生きている）／逐次は沈黙し続ける／一括は速く答える。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield port
    srv.shutdown()


def _tools():
    return [{"type": "function", "function": {"name": "Bash",
             "parameters": {"type": "object", "properties": {}}}}]


def test_watchdog_falls_back_to_nonstream_and_returns_answer(slow_stream_server, monkeypatch):
    """番犬が逐次を切り、相手が生きているので一括で答えを返す。"""
    monkeypatch.setenv("LAPS_STREAM_FIRST_BYTE", "1")   # 1 秒で番犬
    monkeypatch.setenv("LAPS_READ_TIMEOUT", "30")       # read timeout より内側で鳴る
    b = Backend(BackendConfig(base_url=f"http://127.0.0.1:{slow_stream_server}/v1", model="x"))
    tokens = []
    t = time.time()
    content, calls = b.chat_with_tools(
        [{"role": "user", "content": "hi"}], _tools(),
        max_tokens=5, on_token=lambda s: tokens.append(s),
    )
    dt = time.time() - t
    assert content == "答え", f"一括の答えが返っていない: {content!r}"
    assert dt < 8, f"番犬が効かず待ちすぎ: {dt:.1f}s"
    assert b.usage.get("stream_first_byte_stall", 0) >= 1, "番犬が鳴っていない"
    assert b.usage.get("stream_fallback_nonstream", 0) >= 1, "一括へ落ちていない"


def test_no_retry_amplification(slow_stream_server, monkeypatch):
    """再試行の乗算が無い＝番犬(1s)＋一括(速い)だけ。以前は 4×read_to になっていた。"""
    monkeypatch.setenv("LAPS_STREAM_FIRST_BYTE", "1")
    monkeypatch.setenv("LAPS_READ_TIMEOUT", "30")
    b = Backend(BackendConfig(base_url=f"http://127.0.0.1:{slow_stream_server}/v1", model="x"))
    waits = []
    b.on_wait = lambda n, to: waits.append(n)
    t = time.time()
    b.chat_with_tools([{"role": "user", "content": "hi"}], _tools(),
                      max_tokens=5, on_token=lambda s: None)
    dt = time.time() - t
    # 4×30s の逐次再試行が消えている＝番犬 1s ＋ 一括 ≪1s で終わる。
    assert dt < 5, f"乗算が残っている: {dt:.1f}s"
    assert waits == [], f"逐次を再試行している（on_wait が鳴った）: {waits}"


def test_prompt_streams_normally_when_first_byte_is_fast(monkeypatch):
    """最初の 1 byte が速く来る通常時は、番犬は解除され逐次でトークンが流れる。"""
    class Fast(_Handler):
        pass

    tokens_sent = ["Hel", "lo", "!"]

    def do_POST(self):  # noqa: N802
        body = self._read_body()
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for tk in tokens_sent:
                chunk = {"choices": [{"delta": {"content": tk}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.02)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_response(500)
            self.end_headers()

    Fast.do_POST = do_POST
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Fast)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("LAPS_STREAM_FIRST_BYTE", "1")
        monkeypatch.setenv("LAPS_READ_TIMEOUT", "10")
        b = Backend(BackendConfig(base_url=f"http://127.0.0.1:{port}/v1", model="x"))
        got = []
        content, calls = b.chat_with_tools(
            [{"role": "user", "content": "hi"}], _tools(),
            max_tokens=5, on_token=lambda s: got.append(s),
        )
        assert content == "Hello!", f"逐次の本文が組み上がっていない: {content!r}"
        assert got == tokens_sent, f"トークンが逐次で流れていない: {got}"
        assert b.usage.get("stream_first_byte_stall", 0) == 0, "通常時に番犬が誤発火"
        assert b.usage.get("stream_fallback_nonstream", 0) == 0, "通常時に一括へ落ちた"
    finally:
        srv.shutdown()
