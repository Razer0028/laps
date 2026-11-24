"""SystemState の全動詞を最低1回は実行する（2026-09-03・W3 道具担当の点検）。

既存の試験は disk/memory/failed_units の3つと壊れた引数だけを確かめており、
`load` / `uptime` / `top_memory` / `listening` / `os` は**一度も呼ばれていなかった**
（`_VERBS` の構造だけを見る試験はあったが、`_run` を実際に通す試験が無かった）。
verb ごとに argv が違う以上、「実際に走らせて exit=0 で答えが返る」ことは
verb の数だけ確かめておく価値がある（例えば `ss` が無い環境で `listening` が
どう失敗するかは、実際に呼ばないと分からない）。
"""
from __future__ import annotations

import pytest

from laps.tools import system_state as S


@pytest.mark.parametrize("verb", sorted(S._VERBS))
def test_全動詞が見出しつきで答えを返す(verb):
    out = str(S._run({"verb": verb}))
    assert out.startswith(f"[SystemState {verb}] exit="), out[:200]
    # rc は数値として読める（型が壊れていない）
    rc_part = out.split("exit=", 1)[1].split("\n", 1)[0]
    int(rc_part)
