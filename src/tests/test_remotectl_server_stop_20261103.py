"""操作の線の server.stop（2026-11-03 オーナー「Blackwallのアプリや CLI 版から推論機を空けたり（止めて GPU を返す）
推論機を起動したりできるようにしたい」）。

Android などほかの機械の画面は、この機械のゲートウェイの server.stop で モデル切り替えの口 の POST /v1/stop に頼む。
ここで固定する: stoppable の無い宛先（Claude の中継・古い モデル切り替えの口）には頼まない・busy / lease の断りを code で分ける・
止める言葉（already を含む）をそのまま返す。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from laps import remotectl as rc
from tests.test_remotectl_server_models_20261019 import _serve


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("LAPS_MODELCTL_URL", raising=False)
    monkeypatch.setenv("LAPS_HTTP_TRUST_ENV", "0")


def _one(monkeypatch, routes):
    srv, got = _serve(routes)
    monkeypatch.setattr(rc, "_modelctl_bases", lambda gw: [f"http://127.0.0.1:{srv.server_port}/"])
    return srv, got


VIEW = {"active": "katcoder", "stoppable": True, "units": [{"id": "katcoder"}], "switch": {"state": "idle"}}


def test_stop_is_asked_and_its_words_come_back(monkeypatch):
    srv, got = _one(monkeypatch, {("GET", "/v1/models"): (200, VIEW),
                                  ("POST", "/v1/stop"): (202, {"ok": True, "message": "katcoder を止めます"})})
    try:
        r = rc.server_stop(SimpleNamespace(backend="x"), {})
        assert r == {"ok": True, "already": False, "message": "katcoder を止めます"}
        assert got == [{"force": False}]
    finally:
        srv.shutdown()


def test_already_stopped(monkeypatch):
    srv, _ = _one(monkeypatch, {("GET", "/v1/models"): (200, {**VIEW, "active": None}),
                                ("POST", "/v1/stop"): (200, {"ok": True, "already": True, "message": "止まっています"})})
    try:
        assert rc.server_stop(SimpleNamespace(backend="x"), {})["already"] is True
    finally:
        srv.shutdown()


@pytest.mark.parametrize("body,code", [({"error": "推論中です", "busy": True}, "busy"),
                                       ({"error": "GPU の仕事が動いています", "lease": True}, "lease"),
                                       ({"error": "ほかの切り替えが進んでいます"}, "refused")])
def test_refusals_are_told_apart(monkeypatch, body, code):
    srv, _ = _one(monkeypatch, {("GET", "/v1/models"): (200, VIEW), ("POST", "/v1/stop"): (409, body)})
    try:
        with pytest.raises(rc.CtlError) as e:
            rc.server_stop(SimpleNamespace(backend="x"), {})
        assert e.value.code == code and e.value.message == body["error"]
    finally:
        srv.shutdown()


def test_claude_relay_has_no_stop(monkeypatch):
    view = {"kind": "claude", "active": "claude-haiku-5-5", "units": [{"id": "claude-haiku-5-5"}]}
    srv, got = _one(monkeypatch, {("GET", "/v1/models"): (200, view)})
    try:
        with pytest.raises(rc.CtlError) as e:
            rc.server_stop(SimpleNamespace(backend="x"), {})
        assert e.value.code == "unsupported" and got == []          # 頼んでいない
    finally:
        srv.shutdown()


def test_op_is_advertised():
    assert rc.NATIVE["server.stop"] is rc.server_stop
