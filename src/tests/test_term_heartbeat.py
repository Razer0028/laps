"""端末版の心拍表示（0.1.165）。0.1.164 では message 無しの notice が `kind` の文字で
刷られ、沈黙 15 秒ごとに dim の "heartbeat" が並んだ。本物の Renderer と本物の _LiveView で確かめる。"""
from __future__ import annotations

import io

from rich.console import Console

from laps.term.render import Renderer, _LiveView


def _console() -> Console:
    return Console(record=True, width=80, force_terminal=True, file=io.StringIO())


def _hb(stage="推論機", q=45):
    return {"v": 1, "type": "notice", "id": "r1", "kind": "heartbeat", "stage": stage, "quiet_secs": q}


def test_heartbeat_is_not_printed_as_a_line():
    c = _console(); r = Renderer(c)
    r.on_event(_hb())
    r.on_event(_hb(q=60))
    out = c.export_text()
    assert "heartbeat" not in out and "待っています" not in out, out


def test_heartbeat_shows_stage_in_live_view_and_clears_on_real_event():
    c = _console(); r = Renderer(c)
    r.on_event(_hb("推論機", 45))
    c.print(_LiveView(r))
    assert "推論機の応答を待っています（45 秒）" in c.export_text(clear=True)
    r.on_event(_hb("道具 Bash", 120))
    c.print(_LiveView(r))
    assert "道具 Bash を実行中（120 秒 応答なし）" in c.export_text(clear=True)
    r.on_event({"type": "token", "id": "r1", "text": "alpha"})
    c.print(_LiveView(r))
    assert "待っています" not in c.export_text(clear=True)
    assert r._waiting == ""


def test_tests_do_not_write_into_real_home(tmp_path):
    import os
    from laps.session import state_dir
    assert os.environ["LAPS_HOME"].startswith(str(tmp_path))
    assert str(state_dir()).startswith(str(tmp_path))
