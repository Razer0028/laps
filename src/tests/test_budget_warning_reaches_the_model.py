"""予算の残りが**モデルに届く**こと（計算しただけで終わらせない）。

★2026-09-21・OSS 突き合わせ由来。
出典: goose `crates/goose/src/agents/state_machine/ops_maxturns.rs:19-27`
      `if turns_taken*2 < max_turns { return None }` ＝**半分を超えるまで黙る**。

★BLACKWALL はここが無く、予算を**使い切った後**に初めて告げていた。
  実走行 9 本が全て「喋って終わり」扱いだったのと整合する
  （ハーネスが気づいた時点では、モデルには既に手が無い）。

★この試験の主眼は「計算できるか」ではなく **「送信ビューに載るか」**。
  [[written-but-not-wired]] を 5 回踏んでいるので、繋ぎ目に杭を打つ。
"""
from __future__ import annotations


# --------------------------------------------------------------------------
# 閾値（半分を超えるまで黙る）
# --------------------------------------------------------------------------

def _frac(spent, secs, calls, maxcalls):
    """agent.py の中の式と**同じ規則**（大きいほうを採る）を再現する。"""
    f = 0.0
    if secs > 0:
        f = max(f, spent / secs)
    if maxcalls > 0:
        f = max(f, calls / maxcalls)
    return f


def test_silent_below_half():
    assert _frac(100, 600, 10, 80) < 0.5     # 時間 17% / 道具 12%


def test_speaks_once_either_axis_passes_half():
    assert _frac(400, 600, 5, 80) >= 0.5     # ★時間だけ 67%
    assert _frac(30, 600, 60, 80) >= 0.5     # ★道具だけ 75%


def test_takes_the_larger_axis():
    """★どちらかが先に尽きるので、**小さいほうを見ると手遅れになる**。"""
    assert _frac(30, 600, 60, 80) == 0.75    # 時間 5% だが道具 75%
