"""走行を止める上限を既定で外した（2026-11-16 オーナー「同じ手の繰り返し…これなくして。Desktop ツールとか何回も使うと
止まっちゃうみたい。完全に制限なくしていい。その他の制限も全部取り払って」）。

試験の既定（conftest）は今までの上限で回すので、ここでは環境変数を消して本番の既定を確かめる。
"""
from __future__ import annotations

from tests.test_engine_aids_20261030 import run, sent  # noqa: F401

_SWITCHES = ("LAPS_DOOM_LOOP", "LAPS_LOOP_GUARD", "LAPS_STUCK_GATE", "LAPS_REPEAT_STOP", "LAPS_LOOK_STREAK",
             "LAPS_MAX_EMPTY", "LAPS_MAX_UNREAD", "LAPS_CHILD_MAX_ITERS")


def _production_defaults(mp):
    for k in _SWITCHES:
        mp.delenv(k, raising=False)


def test_same_call_many_times_is_not_stopped(run):
    go, ws, mp = run
    _production_defaults(mp)
    script = [{"calls": [("Bash", {"command": "echo same"})]} for _ in range(8)] + [{"text": "終わりました。"}] * 3   # 終える前の確かめの分も
    text, a, fake, _ = go(script, "同じ確かめを何度もして")
    assert a.counters.get("tool_calls:Bash") == 8
    assert not a.counters.get("doom_loop") and not a.counters.get("repeat_anywhere_blocked")
    assert a.outcome != "loop_detected" and "終わりました。" in text
    assert "so it was not run" not in sent(fake)


def test_switch_brings_the_doom_loop_back(run):
    go, ws, mp = run
    _production_defaults(mp)
    mp.setenv("LAPS_DOOM_LOOP", "1")
    script = [{"calls": [("Bash", {"command": "echo same"})]} for _ in range(4)] + [{"text": "ok"}]
    _, a, fake, _ = go(script, "同じ確かめを何度もして")
    assert a.counters.get("doom_loop")
    assert "so it was not run" in sent(fake)
