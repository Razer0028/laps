"""沈黙の見張り（心拍）— 2026-09-02。

実利用者の記録: 「600 秒のあいだ Python から 1 バイトも届きませんでした」×3。
再現: 推論機が黙ると `iter_start` の後 400 秒以上 1 行も出ない。
ここでは **本物の `_heartbeat_loop` と本物の serve プロセス**で確かめる（判定の写しは書かない）。
"""
from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from laps import serve as srv


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]




class _Silent(http.server.BaseHTTPRequestHandler):
    """/health・/tokenize は即答、本文だけ HANG 秒黙ってから 1 語返す＝遅い推論機。"""
    HANG = 2.5

    def log_message(self, *a):  # noqa: D401
        pass

    def do_GET(self):
        b = b'{"status":"ok"}' if self.path.startswith("/health") else b'{"data":[{"id":"x"}]}'
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        if not self.path.startswith("/v1/chat"):
            b = b'{"tokens":[1,2,3]}'
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.end_headers(); self.wfile.flush()
        time.sleep(self.HANG)
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"late"},"finish_reason":"stop"}]}\n\n'
                         b'data: [DONE]\n\n')
        self.wfile.flush()


def test_serve_process_emits_heartbeats_while_engine_is_silent(tmp_path):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Silent)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    env = dict(os.environ, LAPS_BACKEND=f"http://127.0.0.1:{port}/v1",
               LAPS_HOME=str(tmp_path / "home"), LAPS_TRUST="trusted",
               LAPS_HEARTBEAT_SECS="0.5", LAPS_READ_TIMEOUT="20")
    p = subprocess.Popen([sys.executable, "-u", "-m", "laps.serve"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                         cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         text=True, encoding="utf-8")
    lines: list[dict] = []
    def rd():
        for line in p.stdout:
            try: lines.append(json.loads(line))
            except Exception: pass
    threading.Thread(target=rd, daemon=True).start()
    time.sleep(1.5)
    # ★`cwd` は `parse_send` が知らない鍵で、**黙って無視される**（正しい鍵は `workspace`）。
    #   そのため作業場は conftest の `_policy_workspace` が入れる `LAPS_WORKSPACE=/tmp`
    #   のままになり、この走行は /tmp を作業場として動いて **推論機の段階から先へ進まない**
    #   （2026-09-05・r4c で切り分け: 環境変数を 1 つずつ与えた再現で、
    #   `LAPS_WORKSPACE=/tmp` だけで 100% 再現・`LAPS_POLICY=workspace` だけなら
    #   2.8 秒で done。修正前の serve.py でも同じ）。第4回の総括が挙げた
    #   「負荷で揺れる既知フレーク」はこれ＝**負荷ではなく試験自身の鍵の綴り**だった。
    p.stdin.write(json.dumps({"type": "run", "id": "r1", "prompt": "say hi",
                              "workspace": str(tmp_path)}) + "\n")
    p.stdin.flush()
    t0 = time.time()
    while time.time() - t0 < 30 and not any(l.get("type") == "done" and l.get("id") == "r1" for l in lines):
        time.sleep(0.2)
    try:
        p.stdin.write(json.dumps({"type": "shutdown"}) + "\n"); p.stdin.flush()
    except Exception:
        pass
    p.wait(timeout=10)
    httpd.shutdown()
    hb = [l for l in lines if l.get("kind") == "heartbeat" and l.get("id") == "r1"]
    assert hb, "推論機が黙っている間に心拍が 1 行も出ていない:\n" + "\n".join(json.dumps(l, ensure_ascii=False)[:120] for l in lines)
    assert all(l["stage"] == "推論機" for l in hb), hb
    # 沈黙は本物の事象（iter_start）から数えられている
    assert max(l["quiet_secs"] for l in hb) >= 1
    assert any(l.get("type") == "done" and l.get("id") == "r1" for l in lines), \
        "遅い推論機の答え（late）まで届いていない"


def test_tokenizer_default_timeout_is_short(monkeypatch):
    from laps.context_budget import TokenCounter
    monkeypatch.delenv("LAPS_TOKENIZE_TIMEOUT", raising=False)
    assert TokenCounter("http://127.0.0.1:1/v1")._timeout == 10.0
    monkeypatch.setenv("LAPS_TOKENIZE_TIMEOUT", "3")
    assert TokenCounter("http://127.0.0.1:1/v1")._timeout == 3.0
    assert TokenCounter("http://127.0.0.1:1/v1", timeout=7)._timeout == 7.0






def _recs_file(path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip().startswith("{")]
