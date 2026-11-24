"""子のエージェントの作り込み（2026-11-18）: 文脈の目安・答えの形・長い答えの置き場・子の記録。"""
from __future__ import annotations

from pathlib import Path

from laps.engine import loop
from laps.engine.prompt import CHILD
from laps.engine.tools.task import TASK
from tests.test_engine_20261030 import env  # noqa: F401


def test_child_is_nudged_once_past_the_soft_budget_and_keeps_going(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAPS_CHILD_CTX_SOFT", "1000")
    a, fake, rec, ws = env([{"calls": [("Glob", {"pattern": "*"})], "prompt": 1500},
                            {"calls": [("Glob", {"pattern": "*.py"})], "prompt": 1600},
                            {"text": "Answer: 何も無い", "prompt": 1700}])
    out = a.spawn("探して", "explore", "下見")
    assert "何も無い" in out
    sent = "\n".join(str(m.get("content")) for m in fake.bodies[-1]["messages"])
    assert sent.count("This sub-task has used about") == 1   # 一度だけ・止めない（3 手番目まで走った）
    assert a.children and a.children[0]["nudged"] == 1 and a.children[0]["iters"] == 3


def test_soft_budget_off(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAPS_CHILD_CTX_SOFT", "0")
    a, fake, *_ = env([{"calls": [("Glob", {"pattern": "*"})], "prompt": 900000}, {"text": "done"}])
    a.spawn("探して", "explore", "下見")
    assert "This sub-task has used" not in str(fake.bodies[-1]["messages"])


def test_long_answer_goes_to_a_file_and_the_parent_gets_the_head(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAPS_CHILD_RESULT_MAX", "200")
    long = "\n".join(f"line {i} " + "x" * 40 for i in range(50))
    a, *_ = env([{"text": long}])
    out = a.spawn("まとめて", "explore", "下見")
    assert "line 0" in out and "line 49" not in out.split("[The rest")[0]
    path = out.split(" is in ")[1].split(". Read it")[0]
    assert Path(path).read_text(encoding="utf-8") == long
    assert "Sub-agent: 1 steps" in out and "Treat its claims as leads" in out


def test_child_record_is_in_done_fields(env):  # noqa: F811
    a, *_ = env([{"text": "Answer: ok"}])
    a.spawn("見て", "explore", "下見")
    d = a.done_fields("x")
    assert d["children"][0]["kind"] == "explore" and d["children"][0]["result_chars"] > 0
    assert d["counters"].get("child_iters") == 1


def test_brief_and_answer_shape_are_asked_for():
    assert "what to return" in str(TASK.schema())
    for k in ("Answer:", "Evidence:", "Unsure:", "Stop as soon as you can answer"):
        assert k in CHILD
    assert loop.CHILD_CTX_SOFT == 60000
