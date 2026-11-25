"""子のエージェントを別の推論機で動かす（2026-11-25 オーナー「Gyro-M が子に命令を送って作業をさせて結果を受け取れるようにする
って感じです。Claude の仕組みに近い感じにしたい」）。親の推論機の席は子に使われず、親は子の答えだけを受け取る。
子の推論機が止まっていれば（GPU を別の仕事に貸している間など）、今までどおり親と同じ推論機で動かす。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from laps.engine import childengine
from tests.test_engine_20261030 import Fake, env  # noqa: F401


def _fresh():
    childengine._cache.clear()


def test_the_child_runs_on_its_own_engine_and_the_parent_gets_only_the_answer(env, monkeypatch):  # noqa: F811
    _fresh()
    child = Fake([{"text": "Answer: 42"}])
    monkeypatch.setenv("LAPS_CHILD_BACKEND", child.url)
    a, fake, *_ = env([])
    n0 = len(fake.bodies)
    out = a.spawn("答えを探して", "explore", "下見")
    assert "42" in out
    assert len(child.bodies) == 1 and len(fake.bodies) == n0, "親の推論機は子に使われない（席の保存と戻しも要らない）"
    assert "答えを探して" in json.dumps(child.bodies[0]["messages"], ensure_ascii=False)
    assert a.children[0]["engine"] == "other" and a.counters.get("child_other_engine") == 1
    assert not a._slot_restore and not a._other_llms


def test_a_stopped_child_engine_means_the_child_runs_on_the_parents_engine(env, monkeypatch):  # noqa: F811
    _fresh()
    monkeypatch.setenv("LAPS_CHILD_BACKEND", "http://127.0.0.1:9/v1")      # 誰も聞いていない
    a, fake, *_ = env([{"text": "Answer: same"}])
    out = a.spawn("見て", "explore", "下見")
    assert "same" in out and a.children[0]["engine"] == "same"
    assert not a.counters.get("child_other_engine")


def test_off_and_unset_keep_the_old_way(env, monkeypatch):  # noqa: F811
    _fresh()
    for v in ("off", ""):
        monkeypatch.setenv("LAPS_CHILD_BACKEND", v)
        a, fake, *_ = env([{"text": "Answer: same"}])
        a.spawn("見て", "explore", "下見")
        assert a.children[0]["engine"] == "same"


class _Broken(BaseHTTPRequestHandler):
    """/health には応えるが、生成の要求には 500 を返す推論機（読み込みの途中で GPU を取り上げられた形）。"""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"model_path": "/m/child.gguf", "default_generation_settings": {"n_ctx": 65536}, "modalities": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = b'{"error":{"message":"unavailable"}}'
        self.send_response(500)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_a_child_engine_that_fails_at_once_is_retried_on_the_parents_engine(env, monkeypatch):  # noqa: F811
    _fresh()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Broken)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/v1"
        monkeypatch.setenv("LAPS_CHILD_BACKEND", url)
        a, fake, *_ = env([{"text": "Answer: on the parent"}])
        out = a.spawn("見て", "explore", "下見")
        assert "on the parent" in out, "何もしないうちに落ちたので、親の推論機でやり直す"
        assert a.counters.get("child_other_engine_fallback") == 1
        assert a.children[-1]["engine"] == "same"
        assert childengine.alive(url) is False, "落ちた推論機はしばらく使わない"
    finally:
        srv.shutdown()
