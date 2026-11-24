"""serve は終わり方（result）を記録にも残す（2026-09-03 dogfood: events.jsonl に result が無く outcome/counters が取れなかった）。
本物の serve プロセス＋偽の推論機（本文だけ返す）で端から端まで。"""
from __future__ import annotations

import http.server, json, os, subprocess, sys, threading, time
from pathlib import Path


class _Engine(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = b'{"status":"ok"}' if self.path.startswith("/health") else b'{"data":[{"id":"x"}]}'
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        if not self.path.startswith("/v1/chat"):
            b = b'{"tokens":[1,2,3]}'; self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'); self.wfile.flush()


def test_result_is_recorded_in_session_events(tmp_path):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Engine); port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    env = dict(os.environ, LAPS_BACKEND=f"http://127.0.0.1:{port}/v1", LAPS_HOME=str(home), LAPS_TRUST="trusted", LAPS_READ_TIMEOUT="20")
    p = subprocess.Popen([sys.executable, "-u", "-m", "laps.serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))), text=True, encoding="utf-8")
    lines = []
    threading.Thread(target=lambda: [lines.append(json.loads(l)) for l in p.stdout if l.strip().startswith("{")], daemon=True).start()

    # ★2026-09-05・r4/serve-gateway: 固定 `time.sleep(1.5)` は起動が重い CI ホストで
    #   ready より先にタイムアウトしうる（兄弟の test_serve_records_notices.py /
    #   test_serve_records_queue.py は "ready" が来るまでポーリングしている——揃える）。
    t_ready = time.time()
    while time.time() - t_ready < 40 and not any(l.get("kind") == "ready" for l in lines):
        time.sleep(0.1)
    assert any(l.get("kind") == "ready" for l in lines), "ready が来ない"
    # ★`cwd` は `parse_send` が知らない鍵で黙って無視される（正しい鍵は `workspace`）。
    #   作業場が conftest の `LAPS_WORKSPACE=/tmp` のままだと走行が推論機の段階から
    #   進まず、この試験は 40 秒待って必ず落ちる——第4回が「負荷で揺れる既知フレーク」と
    #   呼んでいたものの正体（2026-09-05・r4c。環境変数を 1 つずつ与えた再現で確定。
    #   修正前の serve.py でも同じで、この差分とは無関係）。
    p.stdin.write(json.dumps({"type": "run", "id": "r1", "prompt": "say hi",
                              "workspace": str(tmp_path)}) + "\n"); p.stdin.flush()
    t0 = time.time(); done = None
    while time.time() - t0 < 40 and done is None:
        time.sleep(0.2); done = next((l for l in lines if l.get("type") == "done" and l.get("id") == "r1"), None)
    try:
        p.stdin.write(json.dumps({"type": "shutdown"}) + "\n"); p.stdin.flush()
    except Exception: pass
    p.wait(timeout=10); httpd.shutdown()
    assert done is not None, "done が来ない"
    sid = done.get("session"); ev = home / "sessions" / sid / "events.jsonl"
    assert ev.exists(), f"記録が無い: {ev}"
    recs = [json.loads(l) for l in ev.read_text(encoding="utf-8").splitlines() if l.strip().startswith("{")]
    res = [r for r in recs if r.get("type") == "result"]
    assert res, "記録に result が無い（done は線にしか出ていない）"
    pl = res[-1].get("payload") or {}
    assert pl.get("outcome") == done.get("outcome") and "counters" in pl and pl.get("session_id") == sid
