"""Remote Control（2026-10-31）: QR・/rc の窓の中身・頼める操作の範囲。実際の中継を通す確かめは試験の外（本物のゲートウェイが要る）。"""
from __future__ import annotations

import json
import os
import time

import pytest

from laps import qr, relayauth


def test_qr_size_grows_with_the_text_and_has_finder_patterns():
    small, big = qr.encode("x"), qr.encode("y" * 300)
    assert len(small) == 21 and all(len(r) == 21 for r in small)
    assert len(big) > len(small)
    assert small[0][:7] == "1111111" and small[0][-7:] == "1111111" and small[-1][:7] == "1111111"


def test_qr_refuses_what_does_not_fit():
    with pytest.raises(ValueError):
        qr.encode("z" * 5000)


def test_halfblocks_keeps_every_row_pair():
    rows = qr.encode("x")
    text = qr.halfblocks(rows)
    assert len(text.splitlines()) == (len(rows) + 4 + 1) // 2


def test_rc_info_reports_connection_and_links(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_AUTH_FILE", str(tmp_path / "auth.json"))
    (tmp_path / "device-id").write_text("devA\n", encoding="utf-8")
    (tmp_path / "remote").mkdir()
    st = tmp_path / "remote" / "status.json"
    st.write_text(json.dumps({"connected": True, "since": 5}), encoding="utf-8")
    r = relayauth.rc_info("s_1", "ws://h:18400", tmp_path)
    assert r["connected"] is True and r["app"] == "blackwall://open?d=devA&s=s_1"
    assert r["web"] == "https://h:18400/#dev=devA&sid=s_1" and len(r["qr"]) >= 21


def test_rc_info_points_at_the_other_machine_when_asked(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_AUTH_FILE", str(tmp_path / "auth.json"))
    (tmp_path / "device-id").write_text("devA\n", encoding="utf-8")
    r = relayauth.rc_info("s_1", "ws://h:18400", tmp_path, device="devB")
    assert r["app"] == "blackwall://open?d=devB&s=s_1"


def test_rc_info_stale_status_means_not_connected(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_AUTH_FILE", str(tmp_path / "auth.json"))
    (tmp_path / "device-id").write_text("devA\n", encoding="utf-8")
    (tmp_path / "remote").mkdir()
    st = tmp_path / "remote" / "status.json"
    st.write_text(json.dumps({"connected": True}), encoding="utf-8")
    old = time.time() - relayauth.RC_STATUS_STALE - 5
    os.utime(st, (old, old))
    r = relayauth.rc_info("", "ws://h:18400", tmp_path)
    assert r["connected"] is False and r["error"]


def test_ctl_refuses_operations_outside_the_short_list():
    for op in ("fs.list", "desktop", "apikeys.list", "mcp.set"):
        with pytest.raises(relayauth.AuthError) as e:
            relayauth.ctl_call("devA", op, relay="ws://127.0.0.1:9")
        assert e.value.code == "bad_request"


def test_a_401_from_another_relay_keeps_the_sign_in(tmp_path, monkeypatch):
    """2026-10-31 試験用の中継の 401 で本物のサインインを消した。消してよいのは、それを出した中継に断られたときだけ。"""
    monkeypatch.setenv("LAPS_AUTH_FILE", str(tmp_path / "auth.json"))
    relayauth.save({"token": "t", "relay": "https://192.0.2.160:18400"})
    assert relayauth.issued_by("wss://192.0.2.160:18400")
    assert not relayauth.issued_by("ws://127.0.0.1:5555")
    relayauth.save({"token": "t"})                         # 出した中継の記録が無い控えは従来どおり
    assert relayauth.issued_by("ws://127.0.0.1:5555")
