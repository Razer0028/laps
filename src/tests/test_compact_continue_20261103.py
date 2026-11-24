"""要約を会話の続きとして頼む（2026-11-03 オーナー「任せます」）。文字にまとめ直すと毎回ゼロから読む（76,126 トークン・約 6 分）。"""
from __future__ import annotations

import json
import types

from laps.engine import compact
from laps.engine.llm import BackendError, ContextOverflow, Turn
from laps.engine.loop import _NOTE_CLOSE, _NOTE_OPEN
from laps.engine.session import Transcript

GOOD = "## Objective\n- do it\n"


class Fake:
    def __init__(self, answers, strata=False, llama=True):
        self.answers, self.bodies = list(answers), []
        self._info = types.SimpleNamespace(llama=llama, strata=strata, template="")

    def info(self):
        return self._info

    def payload(self, messages, tools, **kw):
        return {"messages": messages, "tools": tools, **kw}

    def stream(self, body, **kw):
        self.bodies.append(json.loads(json.dumps(body)))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def _transcript():
    t = Transcript()
    for i in range(8):
        t.raw.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 12_000})
    return t


def test_a_continued_summary_is_used_and_nothing_is_rewritten():
    llm = Fake([Turn(text=GOOD)])
    body = {"messages": [{"role": "user", "content": "asked"}]}
    t = _transcript()
    info = compact.summarize(t, llm, "goal", 65536, continued=body)
    assert info["continued"] and not info["failed"] and len(llm.bodies) == 1
    assert llm.bodies[0]["messages"][0]["content"] == "asked"     # <conversation> へまとめ直していない
    assert "goal" in t.last_compaction()[1]["content"]


def _falls_back(answer):
    """続きの形で 2 回続けて書けなかったら、文字にまとめ直す形へ落ちる（2026-11-13 から、落とす前に続きの形で 1 回やり直す）。"""
    llm = Fake([answer, answer, Turn(text=GOOD)])
    info = compact.summarize(_transcript(), llm, "goal", 65536, continued={"messages": []})
    assert not info["continued"] and not info["failed"] and len(llm.bodies) == 3
    assert info["retried"] and info["fell"]
    assert "<conversation>" in llm.bodies[2]["messages"][0]["content"]


def _retried(answer):
    """1 回目が書けなくても、続きの形のやり直しで書ければそれを使う（会話を頭から読み直さない）。"""
    llm = Fake([answer, Turn(text=GOOD)])
    body = {"messages": [{"role": "user", "content": "asked"}], "max_tokens": 7168}
    info = compact.summarize(_transcript(), llm, "goal", 65536, continued=body)
    assert info["continued"] and info["retried"] and not info["failed"] and len(llm.bodies) == 2
    assert llm.bodies[1]["messages"][0]["content"] == "asked"     # <conversation> へまとめ直していない
    return llm


def test_a_tool_call_falls_back():
    _falls_back(Turn(text=GOOD, calls=[{"name": "Bash"}]))
    _retried(Turn(text=GOOD, calls=[{"name": "Bash"}]))


def test_a_shapeless_answer_falls_back():
    _falls_back(Turn(text="I will keep working."))
    _retried(Turn(text="I will keep working."))


def test_a_summary_cut_short_or_with_a_written_call_falls_back():
    _falls_back(Turn(text=GOOD, finish="length"))
    _falls_back(Turn(text=GOOD, cut="repeat"))
    _falls_back(Turn(text=GOOD, degenerate=True))
    _falls_back(Turn(text=GOOD + "<tool_call>\n<function=Bash>\n<parameter=command>\nls\n</parameter>\n</function>\n</tool_call>"))


def test_a_cut_summary_is_retried_with_a_higher_limit():
    llm = _retried(Turn(text=GOOD, finish="length"))
    assert llm.bodies[1]["max_tokens"] == 10752 and llm.bodies[0]["max_tokens"] == 7168


def test_a_failure_falls_back():
    """推論機に届かない・窓に収まらないときは、やり直さずに落ちる。"""
    for err in (BackendError("gone"), ContextOverflow("too long")):
        llm = Fake([err, Turn(text=GOOD)])
        info = compact.summarize(_transcript(), llm, "goal", 65536, continued={"messages": []})
        assert not info["continued"] and not info["retried"] and len(llm.bodies) == 2
        assert "<conversation>" in llm.bodies[1]["messages"][0]["content"]


def test_strata_does_not_spend_a_checkpoint():
    llm = Fake([Turn(text=GOOD)], strata=True)
    compact.summarize(_transcript(), llm, "goal", 65536, continued={"messages": []})
    assert llm.bodies[0]["strata_checkpoint"] is False


def test_only_on_local_servers_and_never_after_an_overflow(monkeypatch):
    assert compact.continued_enabled(Fake([]), False)
    assert not compact.continued_enabled(Fake([]), True)
    assert not compact.continued_enabled(Fake([], llama=False), False)
    monkeypatch.setenv("LAPS_COMPACT_CONTINUE", "0")
    assert not compact.continued_enabled(Fake([]), False)


def _body_after(last, template=""):
    from laps.engine.loop import Agent
    me = types.SimpleNamespace(
        _send_messages=lambda: [{"role": "system", "content": "S"}, dict(last)],
        llm=types.SimpleNamespace(info=lambda: types.SimpleNamespace(template=template), payload=lambda m, t, **kw: {"messages": m, "tools": t, **kw}),
        _tool_schemas=lambda: [{"name": "Bash"}], ctx=types.SimpleNamespace(mode="general"), think=True, effort="")
    return Agent._continued_summary_body(me)


def test_the_ask_is_a_new_message_after_the_last_tool_result():
    """★前の道具の結果に書き足すと、Strata の控え（次の応答の始まり）より前が変わってゼロから読む。"""
    b = _body_after({"role": "tool", "tool_call_id": "1", "content": "RESULT"}, template="last_query_index")
    assert b["messages"][-2] == {"role": "tool", "tool_call_id": "1", "content": "RESULT"}
    c = b["messages"][-1]["content"]
    assert b["messages"][-1]["role"] == "user" and c.startswith(_NOTE_OPEN + "[Harness]") and c.endswith(_NOTE_CLOSE)
    assert b["tools"] == [{"name": "Bash"}]     # 道具の定義も先頭の一部
    assert b["think"] is True and b["budget"] == compact.SUMMARY_THINK     # ★0 にすると書式の system が変わり先頭から読み直す


def test_without_the_qwen_template_the_ask_is_a_plain_message():
    b = _body_after({"role": "tool", "tool_call_id": "1", "content": "RESULT"})
    assert b["messages"][-2]["content"] == "RESULT" and b["messages"][-1]["content"].startswith("[Harness]")


def test_after_an_answer_the_ask_is_wrapped_so_the_thinking_is_not_dropped():
    b = _body_after({"role": "assistant", "content": "A"}, template="last_query_index")
    c = b["messages"][-1]["content"]
    assert b["messages"][-1]["role"] == "user" and c.startswith(_NOTE_OPEN) and c.endswith(_NOTE_CLOSE)


def test_the_real_payload_keeps_thinking_on_so_the_template_does_not_change():
    from laps.engine.llm import LLM, Info
    llm = LLM("http://127.0.0.1:9", "m")
    llm.info = lambda: Info(llama=True, strata=True, n_ctx=131072)
    b = llm.payload([], None, think=True, effort="low", budget=compact.SUMMARY_THINK, max_tokens=5120)
    assert "chat_template_kwargs" not in b and b["reasoning_effort"] == "low" and b["reasoning_budget_tokens"] == compact.SUMMARY_THINK
