"""スキルの門（2026-11-16 オーナー「Skills を必ず読ませるようにしたい」「インターネット上の Skills を優先させて」）。

新しい作業の最初の道具は、スキルを探して（skills.sh が先）読むか、探したうえで none と言うまで走らない。
続きの一言では張らない。2 回止めても選ばなければ通す（走行を固めない）。探す結果はインターネットの物が先に並ぶ。
"""
from __future__ import annotations

from laps.oc import skilltool
from tests.test_engine_aids_20261030 import run, sent  # noqa: F401  （偽の推論機で本物の走行を流す）

REQ = "このフォルダの python の試験を整えて、落ちている物を直してください"


def _gate_on(mp, found=True):
    mp.setenv("LAPS_SKILL_GATE", "1")
    mp.setenv("LAPS_SKILLS_SH", "0")          # 試験ではインターネットへ出ない
    if found:                                 # 探した結果が返る形（試験の環境では skills.sh も Umbra も読めない）
        from laps.tools import ToolResult
        mp.setattr(skilltool, "_search", lambda q, local: ToolResult(
            "[skills.sh — internet, preferred] q='x': 1 result(s).\n- someone/skills@python-testing", ok=True, kind="ok"))


def test_first_tool_waits_until_a_skill_is_searched_and_picked(run):
    go, ws, mp = run
    _gate_on(mp)
    script = [
        {"calls": [("Bash", {"command": "echo before"})]},                       # 門で止まる
        {"calls": [("Skill", {"action": "search", "q": "python testing"})]},
        {"calls": [("Skill", {"name": "none"})]},
        {"calls": [("Bash", {"command": "echo after"})]},                        # 今度は走る
        {"text": "直しました。"},
    ]
    text, a, fake, _ = go(script, REQ)
    assert a.counters.get("skill_gate_armed") == 1
    assert a.counters.get("skill_gate_blocked") == 1
    assert a.counters.get("skill_gate_done:none") == 1
    s = sent(fake)
    assert "Before any other tool, find the skills for this task" in s
    assert "skills.sh" in s and "Not run yet: first search for skills" in s
    assert "after" in s                                                           # 2 回目の Bash は走った
    assert "Error: Not run yet" not in s                                          # 門は失敗ではない（2026-11-16）


def test_none_before_searching_is_not_accepted(run):
    go, ws, mp = run
    _gate_on(mp)
    script = [
        {"calls": [("Skill", {"name": "none"})]},                                # 探さずに none
        {"calls": [("Bash", {"command": "echo x"})]},                            # まだ止まる
        {"text": "やめます。"},
    ]
    _, a, fake, _ = go(script, REQ)
    assert a.counters.get("skill_gate_blocked") == 1
    assert not a.counters.get("skill_gate_done:none")
    assert "Not run yet: first search for skills" in sent(fake)


def test_gives_up_after_two_blocks(run):
    go, ws, mp = run
    _gate_on(mp)
    script = [{"calls": [("Bash", {"command": f"echo {i}"})]} for i in range(3)] + [{"text": "ok"}]
    _, a, _, _ = go(script, REQ)
    assert a.counters.get("skill_gate_blocked") == 2
    assert a.counters.get("skill_gate_gave_up") == 1


def test_follow_up_words_do_not_arm_the_gate(run):
    go, ws, mp = run
    _gate_on(mp)
    _, a, _, _ = go([{"calls": [("Bash", {"command": "echo go"})]}, {"text": "ok"}], "続けて。さっきの所から、そのまま最後まで進めてください")
    assert not a.counters.get("skill_gate_armed")
    assert not a.counters.get("skill_gate_blocked")


def test_switch_off(run):
    go, ws, mp = run
    mp.setenv("LAPS_SKILL_GATE", "0")
    _, a, _, _ = go([{"calls": [("Bash", {"command": "echo go"})]}, {"text": "ok"}], REQ)
    assert not a.counters.get("skill_gate_armed")


def test_search_lists_internet_skills_first(monkeypatch):
    monkeypatch.setattr(skilltool, "_outside_block", lambda q: ["", "[skills.sh — internet, preferred] q='x': 1 result(s).",
                                                              "- someone/skills@x-skill (1,000 installs)"])
    out = str(skilltool._search("x", {}))
    assert "skills.sh" in out and "someone/skills@x-skill" in out


# ── 2026-11-16 オーナーが画面で「Error: Not run yet: load the skill …」を見た ─────────────

def test_gate_hold_is_a_held_call_not_an_error(run):
    """門で止めた呼び出しは「止めた」（denied）で返り、Error とは書かない（画面は denied を失敗に数えない）。"""
    go, ws, mp = run
    _gate_on(mp)
    script = [{"calls": [("Bash", {"command": "echo before"})]}, {"calls": [("Skill", {"action": "search", "q": "x"})]},
              {"calls": [("Skill", {"name": "none"})]}, {"text": "ok"}]
    _, a, fake, _ = go(script, REQ)
    s = sent(fake)
    assert "[Harness] Not run yet: first search for skills" in s
    assert "Error: Not run yet" not in s
    assert a.counters.get("skill_gate_blocked") == 1


def test_failed_search_opens_the_gate(run):
    """探す所が読めない（網が切れた・棚が無い）なら選ぶ物が無い＝止め続けない。"""
    go, ws, mp = run
    _gate_on(mp, found=False)
    script = [{"calls": [("Skill", {"action": "search", "q": "python testing"})]},
              {"calls": [("Bash", {"command": "echo after"})]}, {"text": "ok"}]
    _, a, fake, _ = go(script, REQ)
    assert a.counters.get("skill_gate_done:search_failed") == 1
    assert not a.counters.get("skill_gate_blocked")
    assert "after" in sent(fake)


def test_failed_read_opens_the_gate(run):
    """選んだスキルが読めなかった（置き場所が違う など）ときも、読もうとしたなら門を開ける。理由は結果でモデルに届く。"""
    go, ws, mp = run
    _gate_on(mp)
    from laps.tools import ToolResult
    mp.setattr(skilltool, "_outside_read", lambda spec, cap=20000: ToolResult(
        f"Error: no SKILL.md found for {spec}.", ok=False, kind="error"))
    mp.setenv("LAPS_SKILLS_SH", "1")
    script = [{"calls": [("Skill", {"action": "search", "q": "python testing"})]},
              {"calls": [("Skill", {"action": "read", "name": "someone/skills@python-testing"})]},
              {"calls": [("Bash", {"command": "echo after"})]}, {"text": "ok"}]
    _, a, fake, _ = go(script, REQ)
    assert a.counters.get("skill_gate_done:outside_failed") == 1
    assert not a.counters.get("skill_gate_blocked")
    assert "after" in sent(fake)
