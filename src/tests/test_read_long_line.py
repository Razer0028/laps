"""1 行が上限より長いファイルで詰まないこと（★実バグ・2026-09-21）。

出典の型: cline `sdk/packages/core/src/extensions/tools/executors/file-read.ts:140-141`
  `if (line.length > MAX_LINE_CHARS) line = line.slice(0, MAX) + " [line truncated]"`

★BLACKWALL の旧実装は「行の境目で切る」だけだったので、**1 行が上限を超えると
  1 行も入らずに抜け**、`shown_end = offset + 0 = offset` になり、案内が
  `Read(offset=同じ値)` ＝ **同じ壁に何度でも当たる**。
  minify した JS・1 行の JSON で実際に起きる（120KB の 1 行 JSON で中身 0 行を実測）。
"""
import os

from laps.tools import read as rd


def test_a_single_huge_line_still_returns_content(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    body = "{" + ",".join(f'"k{i}":"v{i}"' for i in range(6000)) + "}"
    (tmp_path / "one_line.json").write_text(body, encoding="utf-8")
    out = str(rd.SPEC.handler({"file_path": "one_line.json"}))
    assert len(out) > 1000, f"中身が返っていない（詰む）: {out[:200]!r}"
    assert "この行はここで切りました" in out, "切ったことを言っていない"


def test_it_says_how_long_the_line_was(tmp_path, monkeypatch):
    """★黙って切らない。全体の長さを言えば、モデルは次の手を選べる。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("a" * 90000, encoding="utf-8")
    out = str(rd.SPEC.handler({"file_path": "x.txt"}))
    # ★90,000 ではなく 90,003。Read は**行番号を前に付ける**ので、数える対象は
    #   ファイルの行そのものではなく「番号を付けた後の行」になる。
    #   （最初この試験を 90,000 で書いて落とした＝★試験のほうが間違っていた）
    import re
    m = re.search(r"全 ([\d,]+) 文字", out)
    assert m, f"長さを言っていない: {out[-120:]!r}"
    n = int(m.group(1).replace(",", ""))
    assert 90000 <= n <= 90010, f"長さが妥当でない: {n}"


def test_normal_files_are_unaffected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "n.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    out = str(rd.SPEC.handler({"file_path": "n.py"}))
    assert "return 1" in out
    assert "この行はここで切りました" not in out
