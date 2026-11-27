"""AskUserQuestion の強化（2026-11-27 オーナー「ASK ツールって強化できます？」→ 1・2・5）。"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from laps.engine.tools import misc


def _ctx(replies):
    seen = []

    def ask(q, labels, descs):
        seen.append((q, labels, descs))
        return replies.pop(0)
    return SimpleNamespace(ask=ask, cancel=threading.Event()), seen


def test_options_carry_descriptions():
    ctx, seen = _ctx(["A (Recommended)"])
    r = misc.ask({"question": "どちら？", "options": [{"label": "A (Recommended)", "description": "速い"}, {"label": "B", "description": "安全"}]}, ctx)
    assert seen[0] == ("どちら？", ["A (Recommended)", "B"], ["速い", "安全"])
    assert r.output == "The user answered: A (Recommended)"


def test_several_questions_are_asked_in_turn_and_answered_together():
    ctx, seen = _ctx(["Postgres", "自分で書いた答え"])
    r = misc.ask({"questions": [
        {"question": "DB は？", "header": "DB", "options": [{"label": "Postgres"}, {"label": "SQLite"}]},
        {"question": "置き場は？", "header": "置き場", "options": ["/srv", "/opt"]}]}, ctx)
    assert [s[0] for s in seen] == ["（1/2）【DB】DB は？", "（2/2）【置き場】置き場は？"]
    assert "- DB: Postgres\n" in r.output + "\n"
    assert "- 置き場: 自分で書いた答え (typed, not one of the options)" in r.output


def test_old_string_options_still_work_and_empty_is_refused():
    ctx, seen = _ctx(["x"])
    misc.ask({"question": "q", "options": ["x", "y"]}, ctx)
    assert seen[0][1] == ["x", "y"]
    assert not misc.ask({}, ctx).ok
    assert not misc.ask({"questions": [{"header": "h"}]}, ctx).ok
