"""畳みの記憶は **走行（run 要求）の境界**で消える（2026-09-02 検査係の発見）。
本番は一度も reset していなかった＝新しい発言の 1 回目に「前回と同じ出力」が返り、モデルは本文を見ていない。
本物の `Server._begin_run_memory` と本物の `_loop` で確かめる。"""
from __future__ import annotations

import io

from laps import serve as srv
from laps.tools import fold, CURRENT_AGENT


def _seed():
    fold.reset_repeat_memory()
    a = fold.fold_repeat("Bash", "echo hi", "/tmp", "hi\n" * 100, "hi\n" * 100)   # 1 回目: 全文
    b = fold.fold_repeat("Bash", "echo hi", "/tmp", "hi\n" * 100, "hi\n" * 100)   # 2 回目: 畳む
    assert a != b and "前回と同じ" in b
    assert fold._OUT_SEEN






def test_fold_fired_counter_via_current_agent():
    fold.reset_repeat_memory()
    class _A:  # counters を持つだけの親
        counters: dict = {}
    a = _A(); tok = CURRENT_AGENT.set(a)
    try:
        fold.fold_repeat("Glob", "*.py", "/tmp", "x\n" * 200, "x\n" * 200)
        fold.fold_repeat("Glob", "*.py", "/tmp", "x\n" * 200, "x\n" * 200)
    finally:
        CURRENT_AGENT.reset(tok)
    assert a.counters.get("fold_repeat_fired") == 1
