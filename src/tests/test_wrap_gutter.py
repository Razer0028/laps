"""折り返しても段（ガター）を保つ（2026-08-28・UI 監査）。

★実測（80 桁）: 長い失敗本文を出すと、折り返された 2 行目以降が**桁 0 から**始まり
  `⎿` の段が消えていた。しかも単行の失敗では tail と本文が同じ文字列なので、
  **同一の 4 行が 2 回**出ていた（12 物理行 → 直して 6 行）。
  いちばん読みたい失敗が、いちばん読みにくい状態だった。
"""
from __future__ import annotations

import io

from rich.console import Console

from laps.term.render import Renderer, wrap_with_gutter


def test_continuation_lines_keep_the_gutter():
    out = wrap_with_gutter("x" * 200, 40, "  ⎿  ", "     ")
    lines = out.splitlines()
    assert len(lines) > 1
    assert lines[0].startswith("  ⎿  ")
    assert all(ln.startswith("     ") for ln in lines[1:]), lines


def test_wide_characters_count_as_two():
    """全角を1桁と数えると、日本語のエラー文で必ず崩れる。"""
    out = wrap_with_gutter("あ" * 60, 40, "", "")
    for ln in out.splitlines():
        assert len(ln) * 2 <= 40 + 2, ln


def test_short_text_is_unchanged():
    assert wrap_with_gutter("短い", 80, "  ⎿  ", "     ") == "  ⎿  短い"


def test_failure_body_is_not_printed_twice():
    buf = io.StringIO()
    c = Console(file=buf, width=80, force_terminal=False, no_color=True)
    r = Renderer(c)
    r.start_turn("r1")
    body = "PHP Fatal error: " + "詳細な説明が続きます。" * 12
    r.on_event({"type": "tool_call", "id": "r1", "name": "Bash", "input": {"command": "php -l x"}})
    r.on_event({"type": "tool_result", "id": "r1", "name": "Bash", "ok": False,
                "kind": "error", "result": body})
    try:
        r.end_turn("r1")
    except TypeError:
        r.end_turn()
    text = buf.getvalue()
    assert text.count("PHP Fatal error") == 1, "同じ失敗本文が2回出ている"
    # 段が保たれている（続き行がすべて 5 空白で始まる）
    body_lines = [ln for ln in text.splitlines() if "詳細な説明" in ln]
    assert body_lines
    assert all(ln.startswith("  ⎿  ") or ln.startswith("     ") for ln in body_lines), body_lines
