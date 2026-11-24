"""Strata（llama.cpp と同じ口を持つ別の推論機）向けの送り方（2026-11-02）。"""
import json
import time

import pytest
from tests.test_engine_20261030 import env  # noqa: F401（偽の推論機つきの Agent）

from laps.engine import compact
from laps.engine.llm import LLM, ContextOverflow, Info, Turn
from laps.engine.session import Transcript
from laps.oc import qwencall


def _llm(**info) -> LLM:
    llm = LLM("http://127.0.0.1:9/v1", "blackwall-coder")
    llm._info, llm._info_at = Info(llama=True, n_ctx=65536, **info), time.monotonic()
    return llm


def test_effort_is_sent_only_to_strata():
    assert _llm(strata=True).payload([], None, effort="low")["reasoning_effort"] == "low"
    assert "reasoning_effort" not in _llm().payload([], None, effort="low")
    assert "reasoning_effort" not in _llm(strata=True).payload([], None)


def test_no_effort_when_thinking_is_off():
    body = _llm(strata=True).payload([], None, effort="low", budget=0)
    assert "reasoning_effort" not in body
    assert body["chat_template_kwargs"]["enable_thinking"] is False


def test_strata_overflow_is_a_context_overflow():
    # serve/server.py の文のまま（prompt だけで窓を超えると 400）
    text = ('{"error":{"message":"request exceeds the context window: prompt (70000 tokens) leaves no room to answer '
            'in the context (65536); requests are never truncated"}}')
    with pytest.raises(ContextOverflow):
        _llm(strata=True)._raise_message(text, 400)


def test_summary_turn_saves_no_checkpoint(monkeypatch):
    for strata, want in ((True, False), (False, None)):
        llm = _llm(strata=strata)
        seen = {}

        def stream(body, **kw):
            seen.update(body)
            return Turn(text="## Objective\nok")
        monkeypatch.setattr(llm, "stream", stream)
        t = Transcript()
        for i in range(8):
            t.raw.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " + "x" * 12_000})
        compact.summarize(t, llm, "goal", 65536)
        assert seen, "要約の要求が出ていない"
        assert seen.get("strata_checkpoint") == want


# ── 崩れた道具の呼び出し（2026-11-02 Gyro-M が `<function=Bash=replace_all>` と書き、本文として答えになって走行が終わった）──
BROKEN = "確かめます。\n\n<tool_call>\n<function=Bash=replace_all>\n<parameter=command>\necho hi\n</parameter>\n</function>\n</tool_call>"


def test_broken_call_is_rewritten_not_taken_as_the_answer(env):
    a, fake, rec, ws = env([{"text": BROKEN}, {"calls": [("Write", {"file_path": "b.txt", "content": "ok"})]},
                            {"text": "b.txt を作りました。"}])
    out = a.run("b.txt を作ってください")
    assert out == "b.txt を作りました。" and (ws / "b.txt").read_text() == "ok"
    second = json.dumps(fake.bodies[1]["messages"], ensure_ascii=False)
    assert "Bash=replace_all" not in second                  # 崩れた形を履歴に残さない（次の手番が写す）
    assert "確かめます。" in second and "could not be read" in second
    assert any("could not be read" in str(f.get("message")) for t, f in rec.events if t == "notice")
    shown = "".join(f.get("text", "") for t, f in rec.events if t == "token")
    assert "確かめます。" in shown and "<tool_call>" not in shown      # 崩れた形を画面へ流さない


def test_broken_call_gives_up_after_three_tries(env):
    a, fake, rec, ws = env([{"text": BROKEN}] * 6)
    out = a.run("b.txt を作ってください")
    assert len(fake.bodies) == 4 and "書き直せません" in out and "<tool_call>" not in out


@pytest.mark.parametrize("text", [
    "書き方はこうです。\n```\n<tool_call>\n<function=Foo=bar>\n</function>\n</tool_call>\n```\n以上です。",
    "呼び出しは <tool_call> で始まります。",
    "<tool_call>\n<function=Foo>\n</function>\n</tool_call>\nと書きます。これが形です。",
    "`<tool_call><function=Foo>` の形です。",
])
def test_quoted_form_is_not_a_broken_call(text):
    assert qwencall.unread(text) is None


@pytest.mark.parametrize("text,before", [
    (BROKEN, "確かめます。"),
    ("<tool_call>\n<function=Bash=x>\n<parameter=command>\nls", ""),     # 途中で切れた
    ("読みます。\n<function=Foo=bar>\n<parameter=a>\n1\n</parameter>\n</function>", "読みます。"),   # <tool_call> なし
])
def test_unread_finds_the_broken_tail(text, before):
    assert qwencall.unread(text) == before


def test_quoted_form_still_reaches_the_screen(env):
    text = "呼び出しの形はこうです。\n<tool_call>\n<function=Foo>\n</function>\n</tool_call>\nと書きます。これが形です。"
    a, fake, rec, ws = env([{"text": text}])
    assert a.run("呼び出しの形を教えて") == text
    assert "".join(f.get("text", "") for t, f in rec.events if t == "token") == text


@pytest.mark.parametrize("pieces", [["a<tool_", "call>b"], ["a<", "function=x>"], ["ab<to", "o"]])
def test_hold_handles_a_mark_split_across_pieces(pieces):
    h = qwencall.Hold()
    shown = "".join(h.feed(p) for p in pieces)
    assert shown + h.rest() == "".join(pieces) and "<tool_call>" not in shown and "<function=" not in shown


def test_effort_is_pinned_for_the_conversation_so_the_prompt_head_does_not_change():
    """2026-11-14 実測: 見直しの手番だけ low に変えると、Strata は会話を頭から読み直した。固定した値だけを書式へ渡し、上限は変える。"""
    from laps.engine import llm as L
    llm = _llm(strata=True)
    llm.effort_pin = ""
    low = llm.payload([], None, effort="low")
    assert "reasoning_effort" not in low and low["reasoning_budget_tokens"] == L.THINK_BUDGET_LOW
    llm.effort_pin = "low"
    b = llm.payload([], None, effort="")
    assert b["reasoning_effort"] == "low" and b["reasoning_budget_tokens"] == L.THINK_BUDGET


def test_the_pin_is_the_first_request_of_the_session(tmp_path):
    from laps.engine.server import _effort_pin
    assert _effort_pin(tmp_path, "") == "" and _effort_pin(tmp_path, "low") == ""
    other = tmp_path / "b"
    other.mkdir()
    assert _effort_pin(other, "low") == "low" and _effort_pin(other, "") == "low"


def test_think_off_keeps_the_template_inside_a_conversation():
    """会話の途中で Think を Off にしても、書式に渡す値（先頭に描かれる）は変えない（2026-11-14）。"""
    llm = _llm(strata=True)
    llm.effort_pin = "low"
    on = llm.payload([], None, think=True, effort="low")
    off = llm.payload([], None, think=False, effort="")
    assert off["reasoning_effort"] == on["reasoning_effort"] == "low"
    assert "chat_template_kwargs" not in off and off["reasoning_budget_tokens"] == 16
    # 単発の呼び出し（固定しない）と、llama.cpp は今までどおり切る
    assert _llm(strata=True).payload([], None, think=False)["chat_template_kwargs"]["enable_thinking"] is False
    other = _llm()
    other.effort_pin = "low"
    assert other.payload([], None, think=False)["chat_template_kwargs"]["enable_thinking"] is False
