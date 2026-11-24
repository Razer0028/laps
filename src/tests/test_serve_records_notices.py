"""serve 自身の通知と利用者の依頼文を events.jsonl に残す（2026-09-03・item C）。

実測（40 セッション / 56 走行）: events.jsonl に残っていた notice は **1 件だけ**で、
heartbeat / cancel / cancel_applied / run_start は 0 件、利用者の依頼文も 0 件だった。
＝「止めたのに 73 秒止まらなかった」等を後から追う材料が記録に無い。

本物の serve プロセス＋偽の推論機（1 トークン出して黙る）で端から端まで確かめる。
判定の写しは書かない——読むのは serve が実際に書いた events.jsonl。
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


class _SlowEngine(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):                           # /health
        b = b'{"status":"ok"}'
        self.send_response(200); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if not self.path.startswith("/v1/chat"):
            b = b'{"tokens":[1,2,3]}'
            self.send_response(200); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        # SSE: 1 トークンだけ出して長く黙る（利用者が「止める」を押せる窓を作る）。
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"working "}}]}\n\n')
            self.wfile.flush()
            time.sleep(20.0)                    # 中断されると write が失敗して抜ける
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        except OSError:
            pass


def _recs(ev: Path) -> list[dict]:
    return [json.loads(l) for l in ev.read_text(encoding="utf-8").splitlines()
            if l.strip().startswith("{")]


def test_prompt_and_serve_notices_are_recorded(tmp_path):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowEngine)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    home = tmp_path / "home"
    env = dict(os.environ, LAPS_BACKEND=f"http://127.0.0.1:{port}/v1",
               LAPS_HOME=str(home), LAPS_TRUST="trusted",
               LAPS_READ_TIMEOUT="30", LAPS_HEARTBEAT_SECS="0.5")
    p = subprocess.Popen([sys.executable, "-u", "-m", "laps.serve"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         text=True, encoding="utf-8")
    lines: list[dict] = []
    threading.Thread(target=lambda: [lines.append(json.loads(l)) for l in p.stdout
                                     if l.strip().startswith("{")], daemon=True).start()

    def wait_for(pred, secs=40):
        t0 = time.time()
        while time.time() - t0 < secs:
            hit = next((l for l in list(lines) if pred(l)), None)
            if hit is not None:
                return hit
            time.sleep(0.1)
        return None

    try:
        assert wait_for(lambda l: l.get("kind") == "ready"), "ready が来ない"
        prompt = "書庫のファイル一覧を出して"
        p.stdin.write(json.dumps({"type": "run", "id": "r1", "prompt": prompt,
                                  "cwd": str(tmp_path)}) + "\n")
        p.stdin.flush()
        assert wait_for(lambda l: l.get("kind") == "run_start"), "run_start が来ない"
        time.sleep(1.2)                         # 心拍が数回鳴る（0.5s 間隔・推論機は黙っている）
        p.stdin.write(json.dumps({"type": "cancel", "id": "r1"}) + "\n"); p.stdin.flush()
        done = wait_for(lambda l: l.get("type") == "done" and l.get("id") == "r1")
        assert done is not None, "done が来ない"
        assert done["outcome"] == "cancelled"
    finally:
        try:
            p.stdin.write(json.dumps({"type": "shutdown"}) + "\n"); p.stdin.flush()
        except Exception:
            pass
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()
        httpd.shutdown()

    sid = done.get("session")
    ev = home / "sessions" / sid / "events.jsonl"
    assert ev.exists(), f"記録が無い: {ev}"
    recs = _recs(ev)
    notices = {(r.get("payload") or {}).get("kind"): (r.get("payload") or {})
               for r in recs if r.get("type") == "notice"}

    # 1) 利用者の依頼文が run_spec として残っている（包む前の素の依頼）。
    spec = [r for r in recs if r.get("type") == "run_spec"]
    assert spec, "run_spec（依頼文）が記録に無い"
    assert spec[-1]["payload"].get("prompt") == prompt, "依頼文が違う/切れている"

    # 2) serve 自身の通知（今まで stdout にしか無かった 4 種）が記録に残る。
    for kind in ("run_start", "heartbeat", "cancel", "cancel_applied"):
        assert kind in notices, f"notice kind={kind} が記録に無い（記録された: {sorted(notices)}）"

    # 3) cancel_applied は「何を切ったか」の数字を持つ（item B の主目的）。
    ca = notices["cancel_applied"]
    assert ca.get("applied") == "running"
    assert "request_cancel_ms" in ca and isinstance(ca["request_cancel_ms"], (int, float))
    assert "sockets" in ca and "inflight" in ca      # None でも欄がある＝欠測と区別できる
