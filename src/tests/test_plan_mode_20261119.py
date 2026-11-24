"""計画モード（2026-11-19）: 見るだけで調べて計画を出す。書く道具は断り、Agent の前置きのまま走る。"""
from __future__ import annotations

import json

from laps.engine import loop as L
from laps.engine import server as S
from laps.engine.tools import base_tools
from tests.test_engine_20261030 import env  # noqa: F401  （台）


def test_plan_is_a_known_toolset_with_only_looking_tools():
    assert "plan" in S.TOOLSETS
    assert set(base_tools("plan")) == set(base_tools("chat"))
    assert "Write" not in base_tools("plan") and "Edit" not in base_tools("plan")


def test_plan_mode_refuses_changes_and_asks_for_a_plan(env):
    a, fake, rec, ws = env([{"calls": [("Write", {"file_path": "x.txt", "content": "y"})]},
                            {"calls": [("Read", {"file_path": "x.txt"})]},
                            {"text": "計画: 1. x.txt を作る 2. 中身を確かめる"}], tools=base_tools("full"))
    a.ask_allowed = frozenset(base_tools("chat"))
    a.plan_mode = True
    a.ctx.answer_only = True
    out = a.run("x.txt を作る計画を立てて")
    assert out.startswith("計画:") and not (ws / "x.txt").exists()
    sent = json.dumps(fake.bodies[0]["messages"], ensure_ascii=False)
    assert "Plan mode" in sent and "Ask mode" not in sent
    tool_msgs = [m for m in fake.bodies[1]["messages"] if m["role"] == "tool"]
    assert "Not run in Plan mode" in tool_msgs[-1]["content"]
    assert set(fake.bodies[0]["tools"] and [t["function"]["name"] for t in fake.bodies[0]["tools"]]) >= {"Write", "Edit"}


def test_plan_mode_bash_only_looks(env):
    a, fake, rec, ws = env([{"calls": [("Bash", {"command": "touch made.txt"})]}, {"text": "計画です。"}], tools=base_tools("full"))
    a.ask_allowed = frozenset(base_tools("chat"))
    a.plan_mode = True
    a.ctx.answer_only = True
    a.run("計画を立てて")
    assert not (ws / "made.txt").exists()


def test_plan_keeps_the_agent_prefix():
    class T:
        root = None
    spec = S.Spec.__new__(S.Spec)
    spec.toolset = "plan"
    assert S.Server._ask_keeps_prefix(None, T(), spec) is True
    assert L.PLAN_NOTE.startswith("[Harness] Plan mode")
