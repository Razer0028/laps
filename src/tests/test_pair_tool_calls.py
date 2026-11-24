"""結果の付いていない道具呼び出しを、送る直前に補う（2026-09-20・敵対レビュー F11-1）。"""
from __future__ import annotations

import copy

from laps.oc.base import _UNRUN_TOOL_RESULT, pair_tool_calls


def _asst(*ids):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": i, "type": "function", "function": {"name": "Read", "arguments": "{}"}} for i in ids]}


def _tool(i, text="ok"):
    return {"role": "tool", "tool_call_id": i, "content": text}


def test_対が揃っていれば何も足さない():
    msgs = [{"role": "user", "content": "q"}, _asst("c0_0", "c0_1"), _tool("c0_0"), _tool("c0_1"),
            {"role": "assistant", "content": "done"}]
    assert pair_tool_calls(msgs) == msgs


def test_差し戻しで結果が積まれなかった呼び出しに結果を補う():
    # 実測の形: 7 本呼んで結果 0 本のまま、ハーネスの user 発言が続く
    msgs = [{"role": "user", "content": "q"}, _asst("c3_0", "c3_1"), {"role": "user", "content": "★同じ手順を繰り返しています"}]
    out = pair_tool_calls(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool", "user"]
    assert [m.get("tool_call_id") for m in out[2:4]] == ["c3_0", "c3_1"]
    assert all(m["content"] == _UNRUN_TOOL_RESULT for m in out[2:4])


def test_一部だけ結果がある_中断の形():
    msgs = [_asst("a", "b", "c"), _tool("a"), _tool("c"), {"role": "user", "content": "続けて"}]
    out = pair_tool_calls(msgs)
    ids = [m.get("tool_call_id") for m in out if m["role"] == "tool"]
    assert sorted(ids) == ["a", "b", "c"]
    assert [m for m in out if m.get("tool_call_id") == "b"][0]["content"] == _UNRUN_TOOL_RESULT


def test_末尾で切れた記録_プロセスが死んだ形():
    out = pair_tool_calls([{"role": "user", "content": "q"}, _asst("z")])
    assert out[-1] == {"role": "tool", "tool_call_id": "z", "content": _UNRUN_TOOL_RESULT}


def test_idの無い古い記録は数で合わせる():
    a = _asst("", "")
    out = pair_tool_calls([a, {"role": "tool", "content": "1 本目だけ"}, {"role": "user", "content": "x"}])
    assert [m["role"] for m in out] == ["assistant", "tool", "tool", "user"]


def test_記録そのものは変えない():
    msgs = [_asst("c0_0"), {"role": "user", "content": "x"}]
    before = copy.deepcopy(msgs)
    pair_tool_calls(msgs)
    assert msgs == before


def test_文字列で道具を呼ぶ形には触らない():
    msgs = [{"role": "assistant", "content": "<tool_call>{}</tool_call>"}, {"role": "tool", "content": "r"}]
    assert pair_tool_calls(msgs) == msgs
