"""名乗るだけの認証情報（中継の MACHINE_SCOPE）を持つ機械から、MCP の desktop が使えなかった（2026-11-14 model セッションの報告）。"""
import io
import urllib.error

import pytest

from laps import ctlclient as cc
from laps import relayauth


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(cc, "_machine_only", False)
    monkeypatch.setattr(relayauth, "headers", lambda: {"X-Blackwall-Token": "t"})


def _refuse(code):
    return urllib.error.HTTPError("https://relay/devices", code, "x", {}, io.BytesIO(b"this credential can only connect the machine\n"))


def test_devices_retries_without_the_credential_after_a_403(monkeypatch):
    seen = []

    def get(timeout):
        seen.append(dict(cc._headers()))
        if cc._headers():
            raise _refuse(403)
        return [{"id": "a", "name": "pc"}]
    monkeypatch.setattr(cc, "_get_devices", get)
    assert cc.devices() == [{"id": "a", "name": "pc"}]
    assert seen == [{"X-Blackwall-Token": "t"}, {}] and cc._machine_only is True
    cc.devices()
    assert seen[-1] == {}                      # 以後は付けない（毎回 2 度頼まない）


def test_a_refusal_without_the_credential_says_to_sign_in(monkeypatch):
    monkeypatch.setattr(cc, "_get_devices", lambda timeout: (_ for _ in ()).throw(_refuse(401)))
    with pytest.raises(cc.CtlClientError, match="blackwall login"):
        cc.devices()


def test_the_line_is_opened_again_without_the_credential(monkeypatch):
    seen = []

    def open_with(device_id, timeout, h):
        seen.append(dict(h))
        if h:
            raise cc.CtlClientError("the relay refused the line: HTTP/1.1 403 Forbidden")
        return "sock"
    monkeypatch.setattr(cc, "_open_with", open_with)
    assert cc._open("dev", 5.0) == "sock" and seen == [{"X-Blackwall-Token": "t"}, {}]
