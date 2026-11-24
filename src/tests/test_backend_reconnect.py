"""走行の途中で推論機が落ちて戻ったとき、走行を失わない（2026-09-20・敵対レビュー F2-1）。

★1 通目の接続失敗は今までどおり**すぐ**失敗する（`test_backend_unreachable.py` の約束）。
  待つのは、一度でも応答を受けた**後**だけ。
"""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from laps.backend import Backend, BackendConfig


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):          # 静かに
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _start(port: int) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _backend(port: int) -> Backend:
    return Backend(BackendConfig(base_url=f"http://127.0.0.1:{port}/v1", connect_timeout=1.0, max_retries=1))


def test_途中で落ちて戻った推論機を待って送り直す(monkeypatch):
    monkeypatch.setenv("LAPS_RECONNECT_SECS", "20")
    port = _free_port()
    srv = _start(port)
    b = _backend(port)
    waits: list = []
    b.on_wait = lambda n, secs: waits.append((n, secs))
    assert b.chat([{"role": "user", "content": "ping"}]) == "pong"          # 1 通目は通る
    srv.shutdown(); srv.server_close()                                         # 推論機を落とす（再起動の形）
    threading.Timer(3.0, lambda: _start(port)).start()                         # 3 秒後に戻る
    t0 = time.time()
    assert b.chat([{"role": "user", "content": "ping"}]) == "pong"          # ★走行は死なない
    assert 2.0 < time.time() - t0 < 15
    assert b.usage.get("reconnected") == 1
    assert waits, "待っている間に知らせていない（沈黙にしない）"


def test_一度も応答を受けていなければ待たずにすぐ失敗する(monkeypatch):
    monkeypatch.setenv("LAPS_RECONNECT_SECS", "20")
    b = _backend(_free_port())                                                 # 誰も居ない宛先
    t0 = time.time()
    with pytest.raises(RuntimeError) as e:
        b.chat([{"role": "user", "content": "x"}])
    assert time.time() - t0 < 6, "1 通目から待っている（居ない相手を待たない約束を壊した）"
    assert "接続できません" in str(e.value)


def test_戻ってこなければ上限で諦めて理由を言う(monkeypatch):
    monkeypatch.setenv("LAPS_RECONNECT_SECS", "4")
    port = _free_port()
    srv = _start(port)
    b = _backend(port)
    assert b.chat([{"role": "user", "content": "ping"}]) == "pong"
    srv.shutdown(); srv.server_close()
    t0 = time.time()
    with pytest.raises(RuntimeError) as e:
        b.chat([{"role": "user", "content": "ping"}])
    assert 3.0 < time.time() - t0 < 12
    assert "接続できません" in str(e.value)


def test_中断されたら待たない(monkeypatch):
    monkeypatch.setenv("LAPS_RECONNECT_SECS", "30")
    port = _free_port()
    srv = _start(port)
    b = _backend(port)
    assert b.chat([{"role": "user", "content": "ping"}]) == "pong"
    srv.shutdown(); srv.server_close()
    b._hard_closed = True                                                      # 利用者が止めた
    t0 = time.time()
    with pytest.raises(Exception):
        b.chat([{"role": "user", "content": "ping"}])
    assert time.time() - t0 < 5, "中断したのに推論機の復帰を待っている"


def test_0を渡せば待たない_従来どおり(monkeypatch):
    monkeypatch.setenv("LAPS_RECONNECT_SECS", "0")
    port = _free_port()
    srv = _start(port)
    b = _backend(port)
    assert b.chat([{"role": "user", "content": "ping"}]) == "pong"
    srv.shutdown(); srv.server_close()
    t0 = time.time()
    with pytest.raises(RuntimeError):
        b.chat([{"role": "user", "content": "ping"}])
    assert time.time() - t0 < 5
