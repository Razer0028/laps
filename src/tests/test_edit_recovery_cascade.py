"""照合カスケード: 完全一致しない old_string を、安全な範囲で救う（2026-08-27）。

★run29 の実測: Edit 134 回中 69 回失敗（51.5%・最長 11 連続）。ほぼ全部
  `old_string not found`。救えた例は**空行の行末に空白 2 文字が足されただけ**。
  弱いモデルに一字一句の書き写しを強いる設計そのものが誤りだった。
  再生台 `bench/replay_edit_cascade.py` で 63 件中 25 件（40%）を救えると実測。

★対で書く: 「救える例」だけの試験は、危険な緩さを緑にしてしまう。
"""
from __future__ import annotations

import pytest

from laps.tools.edit import _loose_match


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


BODY = """<?php
function alpha() {
    $x = compute(1);
    return $x;
}

function beta() {
    $y = compute(2);
    return $y;
}
"""


def test_trailing_space_difference_is_saved():
    r = _loose_match(BODY, "    $x = compute(1);   ")
    assert r and "行末" in r[2]


def test_inner_space_difference_is_saved():
    r = _loose_match(BODY, "    $x  =  compute(1);")
    assert r and "行内" in r[2]


def test_anchor_span_saves_garbled_middle():
    """先頭行と末尾行は合っているが中身を写し間違えた形。"""
    old = "function alpha() {\n    $x = ???;\n}"
    r = _loose_match(BODY, old)
    assert r and "挟む" in r[2]
    start, end, _ = r
    assert BODY[start:end].startswith("function alpha() {")
    assert BODY[start:end].rstrip().endswith("}")


def test_ambiguous_match_is_refused():
    """同じ行が2箇所にある形は当てない（意図と違う箇所を直すのが最悪）。"""
    body = "def f():\n    return 1\n\ndef g():\n    return 1\n"
    assert _loose_match(body, "    return 1   ") is None


def test_runaway_span_is_refused():
    """先頭と末尾がたまたま一致する**別の場所**を掴んで、間の本物を消さない。"""
    body = ("open()\n" + "".join(f"real_line_{i}()\n" for i in range(40)) + "close()\n")
    old = "open()\nsomething()\nclose()"
    assert _loose_match(body, old) is None, "40行を巻き込む置換を許している"


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("LAPS_EDIT_FUZZY", "0")
    assert _loose_match(BODY, "    $x = compute(1);   ") is None


def test_applied_cascade_is_reported(tmp_path):
    """黙って直さない: 結果本文に「完全一致しませんでした」と段名が出る。"""
    from laps.tools import edit as E
    p = tmp_path / "a.php"
    p.write_text(BODY, encoding="utf-8")
    out = str(E.SPEC.handler({"file_path": str(p),
                              "old_string": "    $x = compute(1);   ",
                              "new_string": "    $x = compute(9);"}))
    assert "完全一致しませんでした" in out and "行末" in out
    assert "compute(9)" in p.read_text(encoding="utf-8")


def test_success_body_carries_the_diff(tmp_path):
    """★編集の結果が「OK: wrote N bytes」だけだったので、モデルは Read で確かめ直していた。"""
    from laps.tools import edit as E
    p = tmp_path / "b.php"
    p.write_text(BODY, encoding="utf-8")
    out = str(E.SPEC.handler({"file_path": str(p),
                              "old_string": "    $y = compute(2);",
                              "new_string": "    $y = compute(7);"}))
    assert "実際に入った差分" in out
    assert "-    $y = compute(2);" in out and "+    $y = compute(7);" in out
    assert "@@ 行" in out


def test_diff_is_capped(tmp_path):
    from laps.tools import edit as E
    p = tmp_path / "c.txt"
    p.write_text("\n".join(f"line{i}" for i in range(400)) + "\n", encoding="utf-8")
    out = str(E.SPEC.handler({"file_path": str(p), "old_string": "line0",
                              "new_string": "\n".join(f"new{i}" for i in range(300))}))
    assert "差分が長いので" in out and len(out) < 8000
