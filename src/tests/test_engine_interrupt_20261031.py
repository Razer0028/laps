"""走行の途中の一言（割り込み）と、思考の堂々巡りと、問いの選択肢（2026-10-31 オーナーの指摘）。

割り込みの枠と「一言の後は続けさせる促しをかけない」決まりは旧い中核にあり、作り直しで落としていた。
子が止める合図を自分で消して最後まで走る不具合もここで見る。"""
from __future__ import annotations

import dataclasses
import json
import threading
import time

from laps.engine.llm import LLM
from laps.engine.loop import INTERJECTION_HEAD
from laps.engine.repeat import RepeatWatch
from laps.engine.tools import Ctx, base_tools
from laps.engine.tools.misc import ask, lift_choices
from tests.test_engine_20261030 import Fake, env  # noqa: F401
from tests.test_engine_aids_20261030 import notices, run, sent  # noqa: F401

_TODOS = {"todos": [{"content": "一つ目", "status": "in_progress"}, {"content": "二つ目", "status": "pending"}]}


def _say_during(a, text, force=False):
    """最初の道具を走らせるときに一言を送る。"""
    orig = a._execute
    told = []

    def later(tcs):
        if not told:
            told.append(1)
            a.inject(text, force=force)
        orig(tcs)
    a._execute = later


def _go(go, script, request, text):
    """go は走行まで行うので、Agent.run の前に一言を仕込めない。_execute を差し替えて渡す。"""
    from laps.engine import loop
    orig = loop.Agent._execute
    told = []

    def later(self, tcs):
        if not told:
            told.append(1)
            self.inject(text)
        orig(self, tcs)
    loop.Agent._execute = later
    try:
        return go(script, request)
    finally:
        loop.Agent._execute = orig


# ── 枠 ──────────────────────────────────
def test_message_reaches_the_model_in_the_frame_and_the_raw_text_is_kept(env):  # noqa: F811
    a, fake, rec, _ = env([{"calls": [("Bash", {"command": "echo hi"})]}, {"text": "了解しました。"}])
    _say_during(a, "やっぱり止めて")
    a.run("長い依頼の本文です")
    body = json.dumps(fake.bodies[1]["messages"], ensure_ascii=False)
    assert INTERJECTION_HEAD in body and "やっぱり止めて" in body and "comes first" in body
    assert "長い依頼の本文です" in body.split(INTERJECTION_HEAD)[1]   # 何をしていたかも添える
    assert a.interjections == ["やっぱり止めて"]                       # 覚える側は原文のまま


# ── 促しをかけない ───────────────────────
def test_plan_nudge_still_works_without_a_message(run):  # noqa: F811
    go, _ws, _mp = run
    text, a, fake, rec = go([{"calls": [("TodoWrite", _TODOS)]}, {"text": "途中ですが報告します。"}, {"text": "全部終えました。"}],
                            "二つの機能を作って")
    assert a.counters.get("end_todo") and len(fake.bodies) > 2         # 下の「止めて」の試験の対照


def test_no_nudge_after_the_user_says_stop(run):  # noqa: F811
    go, _ws, _mp = run
    text, a, fake, rec = _go(go, [{"calls": [("TodoWrite", _TODOS)]}, {"text": "止めました。"}], "二つの機能を作って", "いったん止めて")
    assert text == "止めました。" and len(fake.bodies) == 2
    assert not a.counters.get("end_todo") and not a.counters.get("end_handoff")


def test_a_question_is_answered_and_the_work_goes_on(run):  # noqa: F811
    go, _ws, _mp = run
    script = [{"calls": [("Bash", {"command": "echo one"})]}, {"text": "人物は 3 人分です。"},
              {"calls": [("Bash", {"command": "echo two"})]}, {"text": "残りも終えました。"}]
    text, a, fake, rec = _go(go, script, "長い作業を進めて", "参照している人物は何人分でしたっけ？")
    assert len(fake.bodies) == 4 and a.counters.get("question_answered_go_on") == 1
    assert "You answered the user's question" in sent(fake, 2)
    assert "人物は 3 人分です。" in text and "残りも終えました。" in text   # 答えは最後の答えからも消えない


def test_going_on_after_a_question_is_asked_only_once(run):  # noqa: F811
    go, _ws, _mp = run
    script = [{"calls": [("Bash", {"command": "echo one"})]}, {"text": "3 人分です。"}, {"text": "作業は終わっています。"}]
    text, a, fake, rec = _go(go, script, "長い作業を進めて", "何人分でしたっけ？")
    assert len(fake.bodies) == 3 and a.counters.get("question_answered_go_on") == 1


def test_a_stop_worded_question_ends_the_run(run):  # noqa: F811
    go, _ws, _mp = run
    text, a, fake, rec = _go(go, [{"calls": [("Bash", {"command": "echo one"})]}, {"text": "止めました。"}],
                             "長い作業を進めて", "止めてくれない？")
    assert text == "止めました。" and len(fake.bodies) == 2 and not a.counters.get("question_answered_go_on")


def test_a_progress_question_with_a_make_word_still_goes_on(run):  # noqa: F811
    go, _ws, _mp = run
    script = [{"calls": [("Bash", {"command": "echo one"})]}, {"text": "まだです。"}, {"text": "いま終えました。"}]
    text, a, fake, rec = _go(go, script, "長い作業を進めて", "もう追加しましたか？")
    assert a.counters.get("question_answered_go_on") == 1 and "まだです。" in text and "いま終えました。" in text


def test_review_request_during_the_run_reaches_the_model(run):  # noqa: F811
    go, _ws, _mp = run
    text, a, fake, rec = _go(go, [{"calls": [("Bash", {"command": "echo one"})]}, {"text": "見直しました。"}], "長い作業を進めて", "レビューして")
    assert a.counters.get("review_mode") == 1 and "review" in rec.kinds()


# ── 子 ──────────────────────────────────
def test_forced_message_stops_a_running_subagent(env):  # noqa: F811
    a, fake, rec, _ = env([{"calls": [("Task", {"description": "調べる", "prompt": "ずっと調べて", "subagent_type": "explore"})]},
                           {"calls": [("Bash", {"command": "tail -f /dev/null"})]},
                           {"text": "別のことをします。"}], tools=base_tools("full"))
    a.ctx.spawn = a.spawn
    threading.Timer(1.0, lambda: a.inject("別のことをして", force=True)).start()
    t0 = time.monotonic()
    assert a.run("調べて") == "別のことをします。"
    assert time.monotonic() - t0 < 15
    last = json.dumps(fake.bodies[-1]["messages"], ensure_ascii=False)
    assert len(fake.bodies) == 3 and "Stopped before finishing" in last and "別のことをして" in last
    assert not a.ctx.preempt.is_set()                                  # 親の次の道具を撃たない
    assert a.counters.get("preempted_tool") == 1


def test_subagent_follows_the_think_level(env):  # noqa: F811
    a, fake, rec, _ = env([{"calls": [("Task", {"description": "調べる", "prompt": "見て", "subagent_type": "explore"})]},
                           {"text": "子の答え"}, {"text": "親の答え"}], tools=base_tools("full"))
    a.ctx.spawn = a.spawn
    a.effort = "low"
    a.run("調べて")
    assert [b.get("reasoning_budget_tokens") for b in fake.bodies] == [2048, 2048, 2048]


# ── 間に合わなかった一言 ──────────────────
def test_late_message_is_reported_not_lost(env):  # noqa: F811
    a, fake, rec, _ = env([{"finish": "content_filter"}])
    orig = a._generate

    def later(**k):
        t = orig(**k)
        assert a.inject("遅れた一言")
        return t
    a._generate = later
    a.run("やって")
    late = [f for t, f in rec.events if t == "notice" and f.get("kind") == "inject_ignored"]
    assert [f["text"] for f in late] == ["遅れた一言"]
    assert a.inject("もっと遅い一言") is False


# ── 迷いの書き出し ────────────────────────
_HEDGES = ["しかし、おそらく設定ファイルは別の場所にあるのかもしれません。", "または、おそらく起動の順番が違うのでしょう。",
           "おそらく利用者はカタログを新しく作りたいのだと思います。", "しかし、依頼の文からはどれとも言えません。",
           "または、ビューアの側を書き換える方法もあります。", "もしかすると最初の読み方が間違っていたのかもしれません。",
           "However, the log may have been rotated before the failure happened.", "Maybe the service reads another path at startup.",
           "Alternatively we could regenerate the whole index from scratch.", "But that would lose the entries somebody added by hand.",
           "Wait, the timestamps on those two files do not line up at all.", "Actually the second host never received that package.",
           "Perhaps a stale lock file is what keeps the daemon from starting.", "Or the permissions changed during the last upgrade run.",
           "ただし、その場合は別の警告が記録に残っているはずです。", "いや、警告が出ないように設定されていた可能性も残ります。",
           "たぶん前回の作業者が手で直したのだと考えられます。", "でも、それなら履歴に何か跡があってもよさそうです。"]
_PLAIN = ["まず設定ファイルの場所を確かめて、中身を読みます。", "次に起動の順番を一覧にして、依存の向きを調べます。",
          "カタログの形式は既存の物に合わせて書き換えます。", "ビューアの側は変更しないで済むことを確かめました。",
          "The log shows the failure started right after the upgrade.", "The service reads its config from the path in the unit file.",
          "So the index only needs the three new entries added at the end.", "The timestamps on those two files line up with the deploy."] * 2


def _feed(watch, sentences):
    for s in sentences:
        s += "\n"
        for i in range(0, len(s), 7):
            hit = watch.feed(s[i:i + 7])
            if hit:
                return hit
    return None


def test_going_back_and_forth_is_cut_as_hedging():
    w = RepeatWatch(hedge=15)
    assert _feed(w, _HEDGES) and w.kind == "hedge"


def test_a_plain_thought_with_a_few_hedges_is_not_cut():
    w = RepeatWatch(hedge=15)
    mixed = [x for pair in zip(_PLAIN, _HEDGES[:5] + [""] * 11) for x in pair if x]
    assert _feed(w, mixed) is None


def test_hedging_rule_can_be_off():
    assert _feed(RepeatWatch(), _HEDGES) is None


def test_hedging_rule_leaves_other_inference_servers_alone(env, monkeypatch):  # noqa: F811
    a, fake, rec, _ = env([{"think_pieces": [h + "\n" for h in _HEDGES]}, {"text": "答えです。"}])
    orig = a.llm.info()
    monkeypatch.setattr(a.llm, "info", lambda: dataclasses.replace(orig, llama=False))
    assert a.run("教えて") == "答えです。" and not a.counters.get("hedge_cut")


def test_hedging_cut_asks_to_settle_it_with_a_tool(run):  # noqa: F811
    go, _ws, _mp = run
    text, a, fake, rec = go([{"think_pieces": [h + "\n" for h in _HEDGES]}, {"text": "確かめました。"}], "どうなっているか教えて")
    assert a.counters.get("hedge_cut") == 1 and not a.counters.get("repeat_cut")
    assert "going back and forth" in sent(fake, 1) and fake.bodies[1]["reasoning_budget_tokens"] == 8192


# ── 思考の深さ ────────────────────────────
def test_think_level_sets_the_budget(monkeypatch):
    monkeypatch.delenv("LAPS_REASONING_BUDGET", raising=False)
    fake = Fake([])
    try:
        llm = LLM(fake.url, "north-test")
        msgs = [{"role": "user", "content": "x"}]
        assert llm.payload(msgs, None, effort="low")["reasoning_budget_tokens"] == 2048
        assert llm.payload(msgs, None)["reasoning_budget_tokens"] == 8192
        assert llm.payload(msgs, None, effort="high")["reasoning_budget_tokens"] == 16384
        assert llm.payload(msgs, None, effort="low", budget=1536)["reasoning_budget_tokens"] == 1536
        monkeypatch.setenv("LAPS_REASONING_BUDGET", "500")
        assert llm.payload(msgs, None, effort="low")["reasoning_budget_tokens"] == 500
    finally:
        fake.close()


# ── 問いの選択肢 ──────────────────────────
def test_choices_written_in_the_question_become_buttons():
    # 実際の走行記録の形（数字）
    q, labels = lift_choices("追加する必要があります。確認したい内容を教えてください：\n\n1) 新しい結合モデルを作成します。\n"
                             "2) カテゴリも存在します。\n3) または、直接追加します（拡張）。\n4) ビューアを確認します。\n\n"
                             "どの方法を希望されますか？")
    assert labels == ["新しい結合モデルを作成します。", "カテゴリも存在します。", "または、直接追加します（拡張）。", "ビューアを確認します。"]
    assert q == "追加する必要があります。確認したい内容を教えてください：\n\nどの方法を希望されますか？"
    # 実際の走行記録の形（文字）
    q, labels = lift_choices("どの方法で実行できますか？\n\nA) 生成します。\nB) カタログを生成します。\nC) カテゴリを追加します。\n\n選択してください。")
    assert labels == ["生成します。", "カタログを生成します。", "カテゴリを追加します。"] and "A)" not in q
    for text in ("選んでください\n（1）一つ目\n（2）二つ目", "選んでください\n①一つ目\n②二つ目", "選んでください\n1. 一つ目\n\n2. 二つ目", "選んでください\n１）一つ目\n２）二つ目"):
        assert lift_choices(text) == ("選んでください", ["一つ目", "二つ目"]), text


def test_things_that_are_not_choices_stay_in_the_question():
    two_questions = ("データを追加したいと考えています。\n\n1) どのファイルがベースですか？ (おそらく a.obj)\n"
                     "2) どのような形式を望んでいますか？ (b.obj, c.json)\n\n最も便利な方法で実行できます。")
    for text in (two_questions, "1) 一つ目\n2) 二つ目",                    # 問いの文が残らない
                 "版は\n1.2.3 と\n1.2.4 です", "順番が\n2) 二つ目\n1) 一つ目", "Q: 何ですか\nA: 答えです",
                 "途中に\n1) 一つ目\nここに説明の文\n2) 二つ目", "一つだけ\n1) 一つ目"):
        assert lift_choices(text) == (text, []), text


def test_ask_uses_the_lifted_choices_only_when_options_are_empty(tmp_path):
    seen = []

    def on_ask(q, labels, descs):
        seen.append((q, labels, descs))
        return labels[0] if labels else "自由に書いた答え"
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event())
    ctx.ask = on_ask
    assert "一つ目" in ask({"question": "選んで\n1) 一つ目\n2) 二つ目"}, ctx).output
    assert seen[-1] == ("選んで", ["一つ目", "二つ目"], ["", ""])
    ask({"question": "選んで\n1) 一つ目\n2) 二つ目", "options": ["X", "Y"]}, ctx)
    assert seen[-1] == ("選んで\n1) 一つ目\n2) 二つ目", ["X", "Y"], ["", ""])
    ask({"question": "何と呼びますか"}, ctx)
    assert seen[-1] == ("何と呼びますか", [], [])
