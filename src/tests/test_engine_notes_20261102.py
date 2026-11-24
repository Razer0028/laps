"""[Harness] の一言の送り方と、持ち時間の知らせ（2026-11-02 Terminal-Bench の難しい課題の見直しで見つけた直し）。"""
from __future__ import annotations

import time

import pytest

from laps.engine.aids import Aids, _time_limit
from laps.engine.llm import Info
from laps.oc import selfreview
from tests.test_engine_20261030 import env  # noqa: F401
from tests.test_engine_aids_20261030 import run  # noqa: F401

QWEN = "{%- set ns = namespace(multi_step_tool=true, last_query_index=messages|length - 1) %}"


def _call(i: int) -> list[dict]:
    return [{"id": f"c{i}", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]


def _with_template(a, monkeypatch, template: str) -> None:
    monkeypatch.setattr(a.llm, "info", lambda **k: Info(template=template, llama=True))


def _worked(a) -> None:
    a.t.append({"role": "user", "content": "do it"})
    a.t.append({"role": "assistant", "content": "", "reasoning_content": "T1", "tool_calls": _call(1)})
    a.t.append({"role": "tool", "tool_call_id": "c1", "content": "out1"})


def test_note_after_a_tool_result_goes_into_the_result(env, monkeypatch):  # noqa: F811
    """user の形で送ると Qwen 系の書式が前の思考を全部落とし、推論機が窓を読み直す（9.5 万トークンで 167 秒）。"""
    a, *_ = env([])
    _with_template(a, monkeypatch, QWEN)
    _worked(a)
    a._note("[Harness] one")
    a._note("[Harness] two")
    sent = a._send_messages()
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool"]
    assert sent[-1]["content"] == "out1\n\n[Harness] one\n\n[Harness] two"
    assert sent[2]["reasoning_content"] == "T1"


def test_the_transcript_keeps_the_note_as_it_was(env, monkeypatch):  # noqa: F811
    """送る形だけを変える（記録の note は要約や数えが読む）。"""
    a, *_ = env([])
    _with_template(a, monkeypatch, QWEN)
    _worked(a)
    a._note("[Harness] one")
    a._send_messages()
    assert a.t.raw[-1]["role"] == "user" and a.t.raw[-1]["bw_note"] and a.t.raw[-2]["content"] == "out1"


def test_sent_prefix_does_not_change_when_a_note_is_added(env, monkeypatch):  # noqa: F811
    a, *_ = env([])
    _with_template(a, monkeypatch, QWEN)
    _worked(a)
    before = a._send_messages()
    a._note("[Harness] one")
    after = a._send_messages()
    assert after[:-1] == before[:-1] and after[-1]["content"].startswith(before[-1]["content"])


def test_note_after_an_answer_is_wrapped_for_a_template_that_drops_thoughts(env, monkeypatch):  # noqa: F811
    a, *_ = env([])
    _with_template(a, monkeypatch, QWEN)
    _worked(a)
    a.t.append({"role": "assistant", "content": "done", "reasoning_content": "T2"})
    a._note("[Harness] review")
    a._note("[Harness] and more")
    sent = a._send_messages()
    assert sent[-1] == {"role": "user", "content": "<tool_response>\n[Harness] review\n\n[Harness] and more\n</tool_response>"}


def test_note_after_an_answer_stays_plain_for_other_templates(env, monkeypatch):  # noqa: F811
    a, *_ = env([])
    _with_template(a, monkeypatch, "<|START_TEXT|>")
    _worked(a)
    a.t.append({"role": "assistant", "content": "done"})
    a._note("[Harness] review")
    assert a._send_messages()[-1] == {"role": "user", "content": "[Harness] review"}


def test_a_real_message_is_never_folded_into_a_tool_result_or_the_wrapper(env, monkeypatch):  # noqa: F811
    a, *_ = env([])
    _with_template(a, monkeypatch, QWEN)
    _worked(a)
    a.t.append({"role": "user", "content": "stop that"})
    sent = a._send_messages()
    assert sent[-1] == {"role": "user", "content": "stop that"} and sent[-2]["content"] == "out1"

    a.t.append({"role": "assistant", "content": "ok"})
    a._note("[Harness] review")
    a.t.append({"role": "user", "content": "one more"})
    last = a._send_messages()[-1]
    assert last["role"] == "user" and last["content"].endswith("one more") and not last["content"].endswith("</tool_response>")


def test_a_full_run_sends_the_nudge_inside_the_tool_result(env):  # noqa: F811
    a, fake, rec, ws = env([{"calls": [("Write", {"file_path": "a.txt", "content": "hi"})]},
                            {"text": "", "finish": "stop"},
                            {"text": "a.txt を作りました。"}])
    a.run("a.txt を作ってください")
    third = fake.bodies[2]["messages"]
    assert not any(m["role"] == "user" and str(m["content"]).startswith("[Harness]") for m in third)
    assert "[Harness] Your last turn had no answer" in [m for m in third if m["role"] == "tool"][-1]["content"]


def test_time_limit_ignores_inf_nan_and_junk(monkeypatch):
    for v, want in (("inf", 0), ("nan", 0), ("1e400", 0), ("abc", 0), ("600", 600), ("-5", 0)):
        monkeypatch.setenv("LAPS_TIME_LIMIT_SECONDS", v)
        assert _time_limit() == want, v


def test_time_limit_is_stated_with_the_request(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAPS_TIME_LIMIT_SECONDS", "360")
    a, fake, *_ = env([{"text": "はい。"}])
    a.aids = Aids(a)
    a.run("こんにちは")
    assert "This task has a time limit of 360 s (about 6 min)" in str(fake.bodies[0]["messages"])


def test_no_time_note_without_a_limit(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("LAPS_TIME_LIMIT_SECONDS", raising=False)
    a, fake, *_ = env([{"text": "はい。"}])
    a.aids = Aids(a)
    a.run("こんにちは")
    assert "time limit" not in str(fake.bodies[0]["messages"]) and a.aids.time_note() == ""


def test_time_note_at_half_and_eighty_percent_once_each(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAPS_TIME_LIMIT_SECONDS", "100")
    a, *_ = env([])
    a.aids = Aids(a)
    a.started = time.monotonic() - 10
    assert a.aids.time_note() == ""
    a.started = time.monotonic() - 55
    assert "55 s of this task's 100 s time limit have passed (45 s left)" in a.aids.time_note()
    assert a.aids.time_note() == ""
    a.started = time.monotonic() - 85
    assert "(15 s left)" in a.aids.time_note()
    assert a.aids.time_note() == ""


@pytest.mark.parametrize("fix", [True, False])
def test_review_asks_to_take_out_test_leftovers_only_when_it_may_change_things(fix):
    assert ("take out what your own checks left" in selfreview._steps("English", fix=fix)) is fix


def test_work_only_outside_the_folder_is_still_reviewed(run, monkeypatch):  # noqa: F811
    """git-multibranch: 作業がすべて /git と /etc だと、変更なしと見て見直しが走らなかった。"""
    go, *_ = run
    _, a, *_ = go([{"text": "はい。"}], "こんにちは")
    f = a.finish
    monkeypatch.setattr(f, "work_calls", lambda: 8)
    assert not f._review_due()
    a.aids.sys_changes += [("useradd git", True), ("useradd git", True), ("rm x", False)]
    assert f._review_targets() == ["system: useradd git"] and f._review_due()
