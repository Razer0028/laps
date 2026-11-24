"""操作の線の server.models・server.switch（2026-10-19 オーナー「Android 版アプリの UI/UX を Claude のスマホ版アプリに寄せてほしい」）。

Android の上のモデル選びは、この機械のゲートウェイが推論機の切り替え口（モデル切り替えの口 :8743）か Claude の中継（/modelctl/）に頼む。
ここで固定する: 候補の順（宛先の /modelctl/ → 同じ機械の :8743）・llama-server の 404 は飛ばす・切り替えの言葉と断りの理由。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from laps import remotectl as rc


def _serve(routes):
    """routes: {(method, path): (code, body)}。受けた POST の本文を got に積む。"""
    got = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, method):
            code, body = routes.get((method, self.path.split("?")[0]), (404, {"error": "not found"}))
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._reply("GET")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            got.append(json.loads(self.rfile.read(n) or b"{}"))
            self._reply("POST")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, got


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("LAPS_MODELCTL_URL", raising=False)
    monkeypatch.setenv("LAPS_HTTP_TRUST_ENV", "0")


def test_bases_try_the_relay_then_the_same_host_8743():
    gw = SimpleNamespace(backend="http://192.0.2.160:8741/v1")
    assert rc._modelctl_bases(gw) == ["http://192.0.2.160:8741/modelctl/", "http://192.0.2.160:8743/"]


def test_local_inference_has_no_server_models():
    with pytest.raises(rc.CtlError) as e:
        rc._modelctl_bases(SimpleNamespace(backend=""))
    assert e.value.code == "no_server"


def test_forced_url_wins(monkeypatch):
    monkeypatch.setenv("LAPS_MODELCTL_URL", "http://10.0.0.5:9000")
    assert rc._modelctl_bases(SimpleNamespace(backend="http://192.0.2.160:8741/v1")) == ["http://10.0.0.5:9000/"]


def test_claude_relay_answers_at_modelctl_and_switch_returns_its_words():
    view = {"kind": "claude", "active": "claude-haiku-5-5", "units": [{"id": "claude-haiku-5-5"}, {"id": "claude-sonnet-5-5"}]}
    srv, got = _serve({("GET", "/modelctl/v1/models"): (200, view),
                       ("POST", "/modelctl/v1/switch"): (202, {"ok": True, "message": "haiku → sonnet に切り替えました"})})
    try:
        gw = SimpleNamespace(backend=f"http://127.0.0.1:{srv.server_port}/v1")
        d = rc.server_models(gw, {})
        assert d["kind"] == "claude" and d["base"].endswith("/modelctl/") and len(d["units"]) == 2
        r = rc.server_switch(gw, {"id": "claude-sonnet-5-5"})
        assert r == {"ok": True, "message": "haiku → sonnet に切り替えました"}
        assert got == [{"id": "claude-sonnet-5-5", "force": False}]
    finally:
        srv.shutdown()


def test_llama_404_falls_through_to_the_next_base_and_refusal_is_explained(monkeypatch):
    llama, _ = _serve({})                                          # llama-server は知らない道に 404
    ctl, got = _serve({("GET", "/v1/models"): (200, {"active": "katcoder", "units": [{"id": "katcoder"}]}),
                       ("POST", "/v1/switch"): (409, {"error": "推論中なので切り替えません"})})
    try:
        monkeypatch.setattr(rc, "_modelctl_bases", lambda gw: [f"http://127.0.0.1:{llama.server_port}/modelctl/",
                                                               f"http://127.0.0.1:{ctl.server_port}/"])
        d = rc.server_models(SimpleNamespace(backend="x"), {})
        assert d["active"] == "katcoder" and d["base"].endswith(f":{ctl.server_port}/")
        with pytest.raises(rc.CtlError) as e:
            rc.server_switch(SimpleNamespace(backend="x"), {"id": "qwen36x2"})
        assert e.value.code == "refused" and "推論中" in e.value.message
        with pytest.raises(rc.CtlError) as e2:
            rc.server_switch(SimpleNamespace(backend="x"), {})
        assert e2.value.code == "bad_args"
    finally:
        llama.shutdown(); ctl.shutdown()


def test_nothing_answers_is_unreachable(monkeypatch):
    monkeypatch.setattr(rc, "_modelctl_bases", lambda gw: ["http://127.0.0.1:9/modelctl/"])
    with pytest.raises(rc.CtlError) as e:
        rc.server_models(SimpleNamespace(backend="x"), {})
    assert e.value.code == "unreachable"


def test_ops_are_advertised():
    assert "server.models" in rc.NATIVE and "server.switch" in rc.NATIVE
