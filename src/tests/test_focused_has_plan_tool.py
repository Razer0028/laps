"""ハーネスが依存する道具は、どの toolset にも入っていること。

★2026-08-19 に見つけた穴: 完了判定 `_open_todos()` は **TodoWrite が書いた予定**を
  読む。ところが `FOCUSED_TOOLS` に TodoWrite が無く、`serve.py` は `tools` 未指定の
  とき focused を既定にする。つまりアプリ以外の呼び出しでは、
  「まだ終わっていない」と差し戻す仕組みが**黙って死ぬ**。

★鳴らない仕組みは、壊れていても気づけない。だから試験で縛る。
"""
from __future__ import annotations


import laps.tools as tools_mod


def test_focused_に予定の道具が入っている() -> None:
    assert "TodoWrite" in tools_mod.FOCUSED_TOOLS


def test_ハーネスが読む道具が実在する() -> None:
    """★`_open_todos` が読む先を書ける道具が登録されていること。"""
    assert "TodoWrite" in tools_mod.by_name()


def test_focused_は小さいまま() -> None:
    """★道具数は選択精度に効く（50個で84-95% → 200個で41%）。
    足すのは**ハーネスが依存するものだけ**にする。"""
    assert len(tools_mod.FOCUSED_TOOLS) <= 8, tools_mod.FOCUSED_TOOLS
