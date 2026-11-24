"""旧い中核のコメントにあったオーナーの決まりのうち、作り直しで落としていた物（2026-10-31 に突き合わせて戻した）。"""
from __future__ import annotations

from laps.engine.session import Transcript
from tests.test_engine_20261030 import env  # noqa: F401
from tests.test_engine_aids_20261030 import notices, run, sent  # noqa: F401
from tests.test_engine_interrupt_20261031 import _HEDGES, _TODOS

_LONG = "koyomi のモデルに tanimashi のデータを追加して、ビューアで確かめてください"


# ── 2026-10-13「モデルには自由に考えさせます」 ─────
def test_cut_thoughts_do_not_force_a_tool_or_shrink_thinking(run):  # noqa: F811
    go, _ws, _mp = run
    hedged = {"think_pieces": [h + "\n" for h in _HEDGES]}
    text, a, fake, rec = go([hedged, hedged, {"text": "分かりません。ファイルが見つかりませんでした。"}], "どうなっているか教えて")
    assert a.counters.get("hedge_cut") == 2
    for body in fake.bodies[1:]:
        assert body.get("tool_choice") != "required" and body["reasoning_budget_tokens"] == 8192


def test_many_cuts_in_a_row_stop_with_a_message(run):  # noqa: F811
    go, _ws, _mp = run
    hedged = {"think_pieces": [h + "\n" for h in _HEDGES]}
    text, a, fake, rec = go([hedged] * 6, "どうなっているか教えて")
    assert a.outcome == "loop_detected" and len(fake.bodies) == 6 and "doom_loop" in rec.kinds()


# ── 2026-10-10「計画だけ」の依頼へ作業を促さない ─────
def test_plan_only_request_is_not_pushed_into_the_work(run):  # noqa: F811
    go, _ws, _mp = run
    text, a, fake, rec = go([{"calls": [("TodoWrite", _TODOS)]}, {"text": "計画は以上です。"}], "二つの機能を作る計画だけ立てて")
    assert text == "計画は以上です。" and len(fake.bodies) == 2 and not a.counters.get("end_todo")


def test_do_not_run_request_is_not_pushed(run):  # noqa: F811
    go, _ws, _mp = run
    text, a, fake, rec = go([{"text": "修正案は次のとおりです。"}], "修正案を書いて。実行しないでください")
    assert len(fake.bodies) == 1 and not a.counters.get("end_handoff")


def test_plan_nudge_is_given_up_when_no_tool_was_called_after_it(run):  # noqa: F811
    go, _ws, _mp = run
    text, a, fake, rec = go([{"calls": [("TodoWrite", _TODOS)]}, {"calls": [("Write", {"file_path": "a.txt", "content": "x"})]},
                             {"text": "一つ目を作りました。"}, {"text": "二つ目は明日にします。"}], "二つの機能を作って")
    assert a.counters.get("end_todo") == 1 and len(fake.bodies) >= 4     # 2 回目の予定表の促しはかけない


# ── 2026-10-13「続けて」でも元の依頼を見失わない ─────
def test_short_follow_up_keeps_the_original_request():
    t = Transcript()
    t.append({"role": "user", "content": _LONG})
    t.append({"role": "assistant", "content": "途中まで進めました。"})
    from laps.engine.loop import Agent
    a = Agent.__new__(Agent)
    a.t = t
    assert a._task_of("続けて") == _LONG and a._task_of("結果は") == _LONG
    assert a._task_of("新しくて十分に長い依頼の本文を、ここに書いて送りました") != _LONG


# ── 見張りを立てたまま終えない ──────────────
def test_running_monitor_is_asked_about_once(run, monkeypatch):  # noqa: F811
    go, _ws, _mp = run
    from laps.oc import monitor
    monkeypatch.setattr(monitor, "running_labels", lambda: ["app.log"])
    text, a, fake, rec = go([{"text": "起動しました。"}, {"text": "見張りは要りません。"}], "ログを見ながらこのアプリを起動してください")
    assert len(fake.bodies) == 2 and "Monitors still running: app.log" in sent(fake, 1) and a.counters.get("monitor_nudge") == 1


# ── 最初の道具の記録 ────────────────────────
def test_first_tool_calls_are_recited_on_long_runs(run, monkeypatch):  # noqa: F811
    go, _ws, _mp = run
    from laps.oc import findings
    monkeypatch.setattr(findings, "RECITE_EVERY", 2)
    calls = [{"calls": [("Bash", {"command": f"echo step{i}"})]} for i in range(4)]
    text, a, fake, rec = go(calls + [{"text": "終えました。"}], "順番に確かめて")
    assert a.counters.get("recite_early_log") and "Your first tool calls" in sent(fake) and "step0" in sent(fake)


# ── 使い捨てのスクリプト ─────────────────────
def test_throwaway_scripts_get_one_note(run):  # noqa: F811
    go, _ws, _mp = run
    w = [{"calls": [("Write", {"file_path": f"try{i}.py", "content": "print(1)\n"})]} for i in range(4)]
    text, a, fake, rec = go(w + [{"text": "作りました。"}], "データを読むスクリプトを作って")
    assert a.counters.get("script_churn_note") == 1 and "throwaway scripts" in sent(fake)


