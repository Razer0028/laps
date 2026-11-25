"""全画面の表示（term/tui）で、子（Explore・Task）の道具の中継を、その行の「対象」に数えて出す（2026-10-18）。

★それまでは子の中継（notice kind=subagent・「子: ▶ Read」）が表のどこにも出ず、子が動く 1〜2 分の間、
  Explore・Task の行は「-」のまま止まって見えた。"""
import asyncio

import pytest


def test_child_step_counts_calls_and_failures_20261018():
    from laps.term.tui import child_label, child_step
    st: dict = {}
    assert child_step(st, "子: ▶ Read")
    assert not child_step(st, "子: ✓ Read")              # 成功は数を増やさない（呼んだ時に数えた）
    assert child_step(st, "子: ▶ Grep")
    assert child_step(st, "子: ✗ Grep 失敗: no match")
    assert not child_step(st, "子のツールが止まりました")     # 形の違う行は数えない
    assert st["n"] == 2 and st["bad"] == 1 and st["by"] == {"Read": 1, "Grep": 1}
    assert child_label(st, "where is it") == "サブエージェント 2 ✗1 · where is it"
    assert child_label({"n": 3}, "") == "サブエージェント 3"


def test_tui_row_shows_child_progress_20261018(tmp_path, monkeypatch):
    """本物の画面を中で動かし、走っている Explore の行だけが変わり、終わった後の中継は数えないことを見る。"""
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
    app = tui.BlackwallTui(_Ctx(), cfg={}, config_path=None)

    async def main():
        async with app.run_test(size=(180, 40)) as pilot:
            app._tool_event({"type": "tool_call", "name": "Read", "input": {"file_path": "/a/b/c.py"}, "call": "r1"})
            app._tool_event({"type": "tool_call", "name": "Explore", "input": {"question": "where is the parser"},
                             "call": "c1"})
            for m in ("子: ▶ Read", "子: ✓ Read", "子: ▶ Grep", "子: ✗ Grep 失敗: no match"):
                app._child_event(m)
            await pilot.pause()
            t = app.query_one("#tools", tui.ToolsTable)
            assert str(t.get_cell("c1", "target")) == "サブエージェント 2 ✗1 · where is the parser"
            assert str(t.get_cell("r1", "target")) == "b/c.py"            # 子を走らせないツールの行は変えない
            app._tool_event({"type": "tool_result", "name": "Explore", "call": "c1", "ok": True, "result": "x"})
            app._child_event("子: ▶ Read")
            await pilot.pause()
            assert str(t.get_cell("c1", "target")) == "サブエージェント 2 ✗1 · where is the parser"
    asyncio.run(main())
