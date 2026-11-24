"""serve 自身の待ち行列の出来事（queued / queue_cleared）も events.jsonl に残る（2026-09-03・W8 監査の残件）。

W1 は heartbeat/cancel/cancel_applied/run_start/run_spec/result を記録に足したが、queued・queue_cleared・
request・crash 等 15 種は線にしか出ていなかった。実機で「同じ依頼が 3 回待ち行列に溜まり、止めた瞬間に
3 回走り出す」を踏んだのに、その競合が記録から読めない。判定の写しは書かない——読むのは serve が書いた events.jsonl。
"""
from __future__ import annotations

import http.server
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

_w1 = importlib.util.spec_from_file_location("_w1rec", Path(__file__).parent / "test_serve_records_notices.py")
_mod = importlib.util.module_from_spec(_w1); _w1.loader.exec_module(_mod)      # type: ignore[union-attr]
_SlowEngine, _recs = _mod._SlowEngine, _mod._recs


def test_queued_and_queue_cleared_are_recorded(tmp_path):
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

    def send(obj):
        p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n"); p.stdin.flush()

    try:
        assert wait_for(lambda l: l.get("kind") == "ready"), "ready が来ない"
        send({"type": "send", "id": "r1", "messages": [{"role": "user", "content": "1 つ目"}],
              "workspace": str(tmp_path)})
        assert wait_for(lambda l: l.get("kind") == "run_start" and l.get("id") == "r1"), "run_start が来ない"
        send({"type": "send", "id": "r2", "messages": [{"role": "user", "content": "2 つ目（待ち行列）"}],
              "workspace": str(tmp_path)})
        assert wait_for(lambda l: l.get("kind") == "queued" and l.get("id") == "r2"), "queued が線に出ない"
        send({"type": "cancel", "id": "r1"})
        assert wait_for(lambda l: l.get("kind") == "queue_cleared"), "queue_cleared が線に出ない"
        assert wait_for(lambda l: l.get("type") == "done" and l.get("id") == "r2"), "待ち行列の r2 に done が出ない"
        assert wait_for(lambda l: l.get("type") == "done" and l.get("id") == "r1"), "r1 に done が出ない"
        send({"type": "quit"})
        p.wait(timeout=10)
    finally:
        httpd.shutdown()
        if p.poll() is None:
            p.kill()

    sessions = sorted((home / "sessions").glob("*/events.jsonl"))
    assert sessions, f"記録が無い: {home}"
    kinds: dict[str, dict] = {}
    for ev in sessions:
        for r in _recs(ev):
            if r.get("type") == "notice":
                kinds[str((r.get("payload") or {}).get("kind"))] = r.get("payload") or {}
    for k in ("queued", "queue_cleared"):
        assert k in kinds, f"notice kind={k} が記録に無い（記録された: {sorted(kinds)}）"
    assert kinds["queue_cleared"].get("dropped") == ["r2"], kinds["queue_cleared"]
