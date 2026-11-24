"""gateway 経由の走行が telemetry record になる（2026-09-07）。"""
import json, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from laps import gateway_record as R


def _lines():
    # ★fixture は serve の実物の線から写す（/root/bw-eval/_results/*.events.jsonl・2026-09-08）。
    #   旧: `{"type":"run_start"}` という**存在しない型**を書いていた＝本番で一度も活性化しない配線を緑にしていた。
    yield '{"v":1,"seq":2,"type":"notice","id":"r1","kind":"run_start","session":"s1","model":"blackwall-coder"}'
    yield '{"v":1,"seq":2,"type":"iter_start","id":"r1"}'
    yield '{"v":1,"seq":3,"type":"tool_call","id":"r1","call":"c1","name":"Glob","input":{"pattern":"**"}}'
    yield '{"v":1,"seq":4,"type":"tool_result","id":"r1","call":"c1","name":"Glob","ok":true,"result":"a.py\\nb.py"}'
    yield '{"v":1,"seq":5,"type":"token","id":"r1","text":"1+1 は"}'
    yield '{"v":1,"seq":6,"type":"token","id":"r1","text":" 2 です。"}'
    yield '{"v":1,"seq":7,"type":"notice","id":"r1","kind":"heartbeat","stage":"推論機","quiet_secs":15}'
    yield '{"v":1,"seq":8,"type":"done","id":"r1","outcome":"accepted","text":"1+1 は 2 です。"}'


def test_依頼からdoneまでで実機と同じ形のrecordになる():
    rec = R.RunRecorder(sid="s1", version="0.1.202", peer="192.0.2.254", workspace="/root/bw-mobile",
                        backend="http://192.0.2.160:8741/v1", model="blackwall-coder")
    rec.note_client({"type": "send", "id": "r1", "prompt": "1+1 は？一言で。"})
    out = None
    for l in _lines():
        got = rec.feed(l)
        if got: out = got
    assert out and out["client"]["kind"] == "gateway" and out["client"]["host_label"] == "192.0.2.254"
    assert out["engine"]["backend"] == "http://192.0.2.160:8741/v1" and out["engine"]["model"] == "blackwall-coder"
    assert out["input"]["text"] == "1+1 は？一言で。" and out["output"]["text"] == "1+1 は 2 です。"
    assert out["tools"] == [dict(out["tools"][0], **{"name": "Glob", "ok": True})] and out["tools"][0]["result_chars"] == 9
    assert out["cost"]["iters"] == 1 and out["cost"]["answer_tokens"] == 2
    assert out["stop"] == {"reason": "accepted", "error": None} and out["counters"]["notices"] == {"heartbeat": 1}
    assert out["run"] == {"request_id": "r1", "session": "s1"}
    # 走行が終わったら次の依頼まで何も出さない
    assert rec.feed('{"v":1,"seq":9,"type":"token","id":"r1","text":"x"}') is None


def test_backend_errorはstop_errorに残る():
    rec = R.RunRecorder(sid="s2", version="x")
    rec.note_client({"type": "send", "id": "r2", "messages": [{"role": "user", "content": "hi"}]})
    rec.feed('{"type":"error","id":"r2","kind":"backend","message":"ConnectError: refused 192.0.2.160:8741"}')
    out = rec.feed('{"type":"done","id":"r2","outcome":"backend_error","text":""}')
    assert out["stop"]["reason"] == "backend_error" and "refused" in out["stop"]["error"]
    assert out["input"]["text"] == "hi"


def test_postは1行のNDJSONで届く():
    got = {}
    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0); got["body"] = self.rfile.read(n)
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), H); port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    try:
        R.post_record({"rec": 1, "id": "t"}, f"http://127.0.0.1:{port}/ingest")
        for _ in range(50):
            if "body" in got: break
            time.sleep(0.05)
        assert got["body"].endswith(b"\n") and json.loads(got["body"])["id"] == "t"
    finally:
        srv.shutdown()


def test_urlの既定と無効化(monkeypatch):
    monkeypatch.delenv("LAPS_GATEWAY_TELEMETRY_URL", raising=False)
    assert R.telemetry_url() is None          # 公開版の既定は送らない
    monkeypatch.setenv("LAPS_GATEWAY_TELEMETRY_URL", "")
    assert R.telemetry_url() is None


def test_Webとandroidの実物の依頼型runで記録が活性化する(monkeypatch):
    """★2026-09-08 監査 C1。webui/app.js は `{type:"run", id, prompt}`、Android ServeCommand.kt も
    `put("type","run")` を送る。旧実装は "send" しか受けず、本番の Web/Android 走行は一度も記録されなかった。"""
    from laps.gateway_record import RunRecorder
    rec = RunRecorder(sid="s", version="t", peer="web", workspace="/w", backend="b", model="m")
    rec.note_client({"type": "run", "id": "r9", "prompt": "1+1 は？"})
    assert rec.active and rec.request_id == "r9" and rec.input_text == "1+1 は？"
    assert rec.feed('{"v":1,"seq":1,"type":"notice","id":"r9","kind":"run_start","session":"s"}') is None
    assert rec.feed('{"v":1,"seq":2,"type":"token","id":"r9","text":"2"}') is None
    out = rec.feed('{"v":1,"seq":3,"type":"done","id":"r9","outcome":"accepted","text":"2"}')
    assert out is not None  # record が返る


def test_serve側の実物run_startだけでも活性化する():
    """依頼を見逃しても（再接続後など）、serve の `notice/run_start` で記録が始まる。"""
    from laps.gateway_record import RunRecorder
    rec = RunRecorder(sid="s", version="t", peer="web", workspace="/w", backend="b", model="m")
    assert rec.feed('{"v":1,"seq":1,"type":"notice","id":"r5","kind":"run_start","session":"s"}') is None
    assert rec.active and rec.request_id == "r5"
    assert rec.feed('{"v":1,"seq":2,"type":"done","id":"r5","outcome":"accepted","text":"x"}') is not None
