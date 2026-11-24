"""中断は**飛行中の HTTP stream を切る**（2026-08-23 レビュー high）。

`httpx.Client.close()` は別スレッドで読み途中の stream を中断しないので、Ctrl-C しても
最初の生成が終わるまで（実測 15〜100 秒）done が来なかった。Response 自身を閉じれば
iter_lines がその場で例外になる。ここでは 1 トークン/秒でだらだら流す偽サーバを相手に、
close() から 2 秒以内に呼び出しが戻ることを見る（ローカル LLM は起こさない・ただの HTTP）。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from laps.backend import Backend, BackendConfig


class _Slow(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静かに
        pass

    silent_for = 0.0          # 最初に黙る秒数（P40 の slot 待ちを模す）

    def do_GET(self):
        # /health（生存確認つき再試行が見る）
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        time.sleep(self.silent_for)
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for i in range(60):                      # 60 秒かけて流す
            chunk = {"choices": [{"delta": {"content": f"t{i} "}}]}
            try:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode()); self.wfile.flush()
            except Exception:
                return
            time.sleep(1.0)
        self.wfile.write(b"data: [DONE]\n\n")


@pytest.fixture
def slow_server():
    srv = HTTPServer(("127.0.0.1", 0), _Slow)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_close_aborts_a_stream_being_read_in_another_thread(slow_server):
    b = Backend(BackendConfig(base_url=slow_server, api_key="x", model="m", max_retries=0))
    got: list[str] = []
    err: list[BaseException] = []
    ended = threading.Event()

    def run():
        try:
            b._chat_stream([{"role": "user", "content": "x"}], temperature=0.0, max_tokens=10,
                           stop=None, on_token=got.append)
        except BaseException as e:   # 中断は例外で戻る
            err.append(e)
        finally:
            ended.set()

    th = threading.Thread(target=run, daemon=True); th.start()
    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        time.sleep(0.05)
    assert got, "stream が始まっていない"
    t0 = time.monotonic()
    b.close()                                   # ← request_cancel が呼ぶもの
    assert ended.wait(timeout=2.0), "close() しても読み取りが戻らない（旧挙動: 生成が終わるまで待つ）"
    assert time.monotonic() - t0 < 2.0
    assert err, "中断が例外として伝わっていない"


def test_close_wakes_a_read_blocked_on_a_silent_peer():
    """★本命: 相手が黙っている間の中断。socket.close では起きない・shutdown なら起きる。"""
    class _Silent(_Slow):
        silent_for = 6.0
    srv = HTTPServer(("127.0.0.1", 0), _Silent)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        b = Backend(BackendConfig(base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1", api_key="x", model="m", max_retries=0))
        ended = threading.Event(); err: list[BaseException] = []

        def run():
            try:
                b._chat_stream([{"role": "user", "content": "x"}], temperature=0.0, max_tokens=10, stop=None, on_token=lambda t: None)
            except BaseException as e:
                err.append(e)
            finally:
                ended.set()
        threading.Thread(target=run, daemon=True).start()
        time.sleep(1.0)                               # 要求は送られ、応答を待って blocked
        t0 = time.monotonic()
        b.close()
        assert ended.wait(timeout=3.0), "黙っている相手への読みが中断で起きない（旧挙動: 90 秒の read timeout まで待つ）"
        assert time.monotonic() - t0 < 3.0 and err
    finally:
        srv.shutdown()


def test_abort_inflight_is_idempotent():
    b = Backend(BackendConfig(base_url="http://127.0.0.1:9/v1", api_key="x", model="m", max_retries=0))
    assert b.abort_inflight() == 0
    b.close(); b.close()


def test_no_retry_backoff_after_close(slow_server):
    """閉じた client へ 3 回の後退つき再試行（10.5 秒）をしない。"""
    b = Backend(BackendConfig(base_url=slow_server, api_key="x", model="m", max_retries=3, retry_base=1.5))
    b.close()
    t0 = time.monotonic()
    with pytest.raises(Exception):
        b.chat([{"role": "user", "content": "x"}], temperature=0.0, max_tokens=5)
    assert time.monotonic() - t0 < 1.0, "中断後に後退つきで再試行している"


def test_readtimeout_retries_once_only_when_peer_is_alive(slow_server, monkeypatch):
    """★2026-08-24 VDIR run7: 一過性の断で 240 秒待って落ちたが P40 自身は健全だった。
    /health が 2 秒で応えるときだけ 1 回やり直す。死んでいれば従来どおり即諦める。"""
    from laps.backend import Backend, BackendConfig
    import httpx as _hx
    b = Backend(BackendConfig(base_url=slow_server, api_key="x", model="m", max_retries=3))
    calls = {"n": 0}
    orig = b._client.stream
    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _hx.ReadTimeout("silence")
        return orig(*a, **kw)
    b._client.stream = flaky
    out = b.chat([{"role": "user", "content": "x"}], temperature=0.0, max_tokens=3,
                 on_token=lambda t: None)
    assert calls["n"] == 2 and out            # 1 回目 timeout → 生存確認 → 2 回目で成功


def test_readtimeout_no_retry_when_peer_is_dead(monkeypatch):
    from laps.backend import Backend, BackendConfig
    import httpx as _hx, time as _t
    b = Backend(BackendConfig(base_url="http://127.0.0.1:9/v1", api_key="x", model="m", max_retries=3))
    def always_timeout(*a, **kw): raise _hx.ReadTimeout("silence")
    b._client.stream = always_timeout
    t0 = _t.monotonic()
    with pytest.raises(RuntimeError, match="応答しません"):
        b.chat([{"role": "user", "content": "x"}], temperature=0.0, max_tokens=3,
               on_token=lambda t: None)
    assert _t.monotonic() - t0 < 5.0          # 死んだ相手に再試行の後退で待たない
