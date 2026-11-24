"""全画面の表示（term/tui）の直し（2026-10-27 SNS の撮影・オーナー「実演部分の動画を blackwall に送って、TUI を直してもらって」）。

①--privacy で server の箱の「…に届きません」も伏せる ②推論機の数が取れない間は server の箱をたたむ
③CTX は推論機の枠の古い数を出さない ④子の道具の中継を会話の欄で 1 行にまとめる ⑤道具の一覧の対象は宛先を出す。"""
import asyncio
import io

import pytest


def test_cmd_target_shows_the_destination():
    from laps.term.tui import cmd_target, tool_target
    assert cmd_target("curl -s -X POST http://127.0.0.1:8000/admin/login -d 'u=a'") == "curl POST 127.0.0.1:8000/admin/login"
    assert cmd_target("curl -s https://example.com/a?b=1 | jq .") == "curl example.com/a?b=1"
    assert cmd_target("curl -s --data-raw x http://h/api") == "curl POST h/api"
    assert cmd_target("cd /srv/shop && grep -rn SECRET_KEY app/") == "grep -rn SECRET_KEY app/"
    assert cmd_target("ls  -la\n /srv") == "ls -la /srv"
    assert cmd_target("cd /x && echo hi | wget -qO- http://h:1/p") == "wget h:1/p"
    assert tool_target("Bash", {"command": "cd /w && curl -X PUT http://h/x"}) == "curl PUT h/x"
    assert tool_target("Read", {"file_path": "/a/b/c.py"}) == "b/c.py"


def test_priv_masks_only_with_privacy(monkeypatch):
    from laps.term.tui import _priv
    msg = "192.168.0.160:8742 に届きません（ConnectError）"
    monkeypatch.delenv("LAPS_PRIVACY", raising=False)
    assert _priv(msg) == msg
    monkeypatch.setenv("LAPS_PRIVACY", "1")
    assert "192.168.0.160" not in _priv(msg) and "<ip>" in _priv(msg)


def test_shown_ctx_ignores_the_slot_left_by_an_earlier_conversation():
    from laps.term.tui import Telemetry, shown_ctx
    tel = Telemetry()
    tel.slots_ok, tel.processing, tel.ctx_used, tel.agent_ctx = True, False, 85_000, None
    assert shown_ctx(tel, busy=False) is None                 # 起動の直後: 枠に残った 85K は出さない
    tel.agent_ctx = 9_900
    assert shown_ctx(tel, busy=True) == 9_900                 # 走っていても推論機を使っていない間（道具）はこの会話の数
    tel.processing, tel.ctx_used = True, 10_400
    assert shown_ctx(tel, busy=True) == 10_400                # 推論機を使っている間は枠の生の数


def _renderer(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.setenv("HOME", str(tmp_path))
    from rich.console import Console
    from laps.term.tui import TuiRenderer
    buf = io.StringIO()
    con = Console(file=buf, force_terminal=False, width=100, color_system=None)
    return TuiRenderer(con), buf


def test_child_relay_lines_fold_into_one_line(tmp_path, monkeypatch):
    r, buf = _renderer(tmp_path, monkeypatch)
    r.on_event({"type": "tool_call", "name": "Task", "input": {"description": "audit"}, "call": "t1"})
    for i in range(9):
        r.on_event({"type": "notice", "kind": "subagent", "message": "子: ▶ Bash"})
        r.on_event({"type": "notice", "kind": "subagent", "message": "子: ✓ Bash"})
    r.on_event({"type": "notice", "kind": "subagent", "message": "子: ▶ Read"})
    r.on_event({"type": "notice", "kind": "subagent", "message": "子: ✗ Read 失敗: no such file"})
    r.on_event({"type": "tool_result", "name": "Task", "call": "t1", "ok": True, "result": "done"})
    out = buf.getvalue()
    assert "子: ▶" not in out and "子: ✓" not in out               # 1 件ずつの行は出さない
    assert "子: ✗ Read 失敗" in out                               # 失敗はその場で出す
    assert out.count("子の道具:") == 1 and "Bash ×9" in out and "Read ×1" in out and "✗ 1" in out
    r.on_event({"type": "tool_result", "name": "Task", "call": "t2", "ok": True, "result": "done"})
    assert buf.getvalue().count("子の道具:") == 1                  # 数えた物が無ければ出さない


def _app(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    monkeypatch.setenv("HOME", str(tmp_path))
    from laps.term import tui

    class _Client:
        workspace = str(tmp_path)

        def __getattr__(self, k):
            return None

    class _Ctx:
        exit_requested = False
        client = _Client()

        def __getattr__(self, k):
            return None

    monkeypatch.setattr(tui.Poller, "start", lambda self: None)
    monkeypatch.setattr(tui.BlackwallTui, "_boot", lambda self: None)
    return tui, tui.BlackwallTui(_Ctx(), cfg={}, config_path=None)


def test_server_box_folds_while_metrics_never_came(tmp_path, monkeypatch):
    tui, app = _app(tmp_path, monkeypatch)

    async def main():
        async with app.run_test(size=(160, 40)) as pilot:
            srv = app.query_one("#srv")
            with app.tel.lock:
                app.tel.srv_ok, app.tel.srv_err, app.tel.last = False, "192.0.2.160:8742 に届きません", {}
            app._tick_slow()
            await pilot.pause()
            assert srv.display is False and app._srv_auto_hidden                 # 数が一度も取れない＝たたむ
            assert "server 不通 F2" in str(app.query_one("#hdr").render())
            assert app.query_one("#top").display                                 # agent の箱は残る
            app.action_toggle("srv")                                             # F2 で開いたら出したまま
            app._tick_slow()
            await pilot.pause()
            assert srv.display is True and app._srv_pinned
            app.action_toggle("srv")                                             # F2 で閉じる→自分で閉じた物は触らない
            with app.tel.lock:
                app.tel.srv_ok, app.tel.last = True, {"gpus": []}
            app._tick_slow()
            await pilot.pause()
            assert srv.display is False
            app.action_toggle("srv")
            await pilot.pause()
            assert srv.display is True
    asyncio.run(main())


def test_server_box_comes_back_when_metrics_arrive(tmp_path, monkeypatch):
    tui, app = _app(tmp_path, monkeypatch)

    async def main():
        async with app.run_test(size=(160, 40)) as pilot:
            srv = app.query_one("#srv")
            with app.tel.lock:
                app.tel.srv_ok, app.tel.srv_err, app.tel.last = False, "x に届きません", {}
            app._tick_slow()
            await pilot.pause()
            assert srv.display is False
            with app.tel.lock:
                app.tel.srv_ok, app.tel.last = True, {"gpus": [{"vram_total": 24000, "vram_used": 1000}]}
            app._tick_slow()
            await pilot.pause()
            assert srv.display is True and not app._srv_auto_hidden
            assert "server 不通" not in str(app.query_one("#hdr").render())
    asyncio.run(main())


def test_done_line_names_the_failed_tools():
    """★締めの行の「失敗 N」に、何の道具が失敗したかを添える（tools の箱を閉じて撮ると分からなかった）。"""
    from laps.term.render import Renderer
    from tests.test_term_render import _call, _console, _done, _result, _turn
    c = _console()
    bad = {"head": "false", "tail": "exit 1", "status": "error"}
    _turn(Renderer(c), [_call("Bash", {"command": "false"}, 1), _result("Bash", 1, "exit 1", ok=False, summary=bad),
                        _call("Bash", {"command": "false"}, 2), _result("Bash", 2, "exit 1", ok=False, summary=bad),
                        _call("Edit", {"file_path": "/a"}, 3), _result("Edit", 3, "Error: not found", ok=False),
                        _done(secs=12.0, tok_s=40.0, tool_calls={"Bash": 2, "Edit": 1})])
    line = [ln for ln in c.export_text(clear=False).splitlines() if "done" in ln][-1]
    assert "失敗  3（Bash ×2・Edit）" in line, line
