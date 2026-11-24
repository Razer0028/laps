"""道具の一覧は会話の間変えない（2026-11-16）。

0.1.685 と 0.1.686 の更新で Skill の説明が変わり、20 万トークンの会話を頭から読み直していた（1 回 5〜7 分）。
道具の一覧は前置きのすぐ後ろに描かれるので、説明の 1 字の違いで推論機の控えが前置きの後ろから全部効かなくなる。
"""
from __future__ import annotations

import dataclasses
import json

from laps.engine.loop import Agent, _tools_shape
from laps.engine.session import Transcript
from tests.test_engine_20261030 import env  # noqa: F401


def _redo(a: Agent) -> list[dict]:
    a._schemas = None
    return a._tool_schemas()


def test_same_shape_keeps_the_descriptions_already_sent(env):  # noqa: F811
    a, *_ = env([])
    first = a._tool_schemas()
    assert json.loads((a.t.root / "tools.json").read_text(encoding="utf-8")) == first
    name = next(iter(a.tools))
    a.tools[name] = dataclasses.replace(a.tools[name], description=a.tools[name].description + " (reworded in a newer version)")
    again = _redo(a)
    assert again == first                                   # 前に送った説明のまま
    assert a.counters.get("tools_frozen") == 1


def test_a_new_parameter_is_a_new_shape(env):  # noqa: F811
    a, *_ = env([])
    first = a._tool_schemas()
    name = next(iter(a.tools))
    t = a.tools[name]
    a.tools[name] = dataclasses.replace(t, params={**t.params, "extra_flag": {"type": "boolean", "description": "new"}})
    again = _redo(a)
    assert again != first and "extra_flag" in json.dumps(again)
    assert a.counters.get("tools_reshaped") == 1


def test_a_parameter_named_description_counts_in_the_shape():
    one = [{"type": "function", "function": {"name": "Bash", "description": "a", "parameters": {
        "type": "object", "properties": {"command": {"type": "string", "description": "x"}}, "required": ["command"]}}}]
    two = json.loads(json.dumps(one))
    two[0]["function"]["parameters"]["properties"]["description"] = {"type": "string"}
    three = json.loads(json.dumps(one))
    three[0]["function"]["description"] = "b"
    three[0]["function"]["parameters"]["properties"]["command"]["description"] = "y"
    assert _tools_shape(one) != _tools_shape(two)
    assert _tools_shape(one) == _tools_shape(three)


def test_restart_reads_the_stored_list(env):  # noqa: F811
    a, *_ = env([])
    first = a._tool_schemas()
    name = next(iter(a.tools))
    tools = dict(a.tools)
    tools[name] = dataclasses.replace(tools[name], description="changed")
    b = Agent(llm=a.llm, out=a.out, rid="r2", transcript=Transcript(a.t.id), ctx=a.ctx, tools=tools, system="SYSTEM")
    assert b._tool_schemas() == first


def test_switch_off(env, monkeypatch):  # noqa: F811
    a, *_ = env([])
    a._tool_schemas()
    monkeypatch.setenv("LAPS_FREEZE_TOOLS", "0")
    name = next(iter(a.tools))
    a.tools[name] = dataclasses.replace(a.tools[name], description="changed")
    assert any(s["function"]["description"] == "changed" for s in _redo(a))
