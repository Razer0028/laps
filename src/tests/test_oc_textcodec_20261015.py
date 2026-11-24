"""★2026-10-15 oc の Edit・Write・Read の文字コード（DESIGN-improvements-20261015.md の B1）。

旧中核（tools/textcodec.py・2026-09-20）で直してあった「黙って符号化を変える・壊れたバイトを U+FFFD にする・バイナリを
書き換える」が、oc の read_text/write_text では戻っていた。決まり: Edit は厳密に読めて往復できる物だけを、読んだのと同じ
符号化で書く。できなければ触らずに断る。Write は元の符号化を保ち、表せない字があるときだけ UTF-8 にしてそう言う。
"""
from __future__ import annotations

from laps.oc import tools as oc_tools

BAT = 'cd /d "D:\\ゲーム\\データ"\r\necho 開始\r\n'.encode("cp932")


def _edit(p, old, new):
    return oc_tools.edit_tool({"file_path": str(p), "old_string": old, "new_string": new})


def test_cp932_edit_keeps_cp932(tmp_path):
    p = tmp_path / "run.bat"
    p.write_bytes(BAT)
    r = _edit(p, "echo 開始", "echo 開始します")
    assert r.ok, str(r)
    assert p.read_bytes().decode("cp932") == 'cd /d "D:\\ゲーム\\データ"\r\necho 開始します\r\n'


def test_cp932_edit_with_unrepresentable_char_is_refused_and_file_untouched(tmp_path):
    p = tmp_path / "run.bat"
    p.write_bytes(BAT)
    r = _edit(p, "echo 開始", "echo 開始 — 変換します")
    assert not r.ok and "cp932" in str(r) and "Nothing was written" in str(r)
    assert p.read_bytes() == BAT


def test_cp932_duplicate_codes_are_not_rewritten(tmp_path):
    p = tmp_path / "a.txt"
    raw = "設定\r\n".encode("cp932") + b"\xfa\x5c\r\n" + "値=1\r\n".encode("cp932")   # IBM 拡張の字（NEC と重複）
    p.write_bytes(raw)
    r = _edit(p, "値=1", "値=2")
    assert not r.ok and "Nothing was changed" in str(r)
    assert p.read_bytes() == raw


def test_bom_utf8_with_a_broken_byte_is_refused(tmp_path):
    p = tmp_path / "b.txt"
    raw = b"\xef\xbb\xbfhello \xff world\n"
    p.write_bytes(raw)
    assert not _edit(p, "hello", "HELLO").ok
    assert p.read_bytes() == raw


def test_binary_is_not_edited(tmp_path):
    p = tmp_path / "x.cfg"
    raw = b"HDR\x00\x01\x02\x80\x90 end"
    p.write_bytes(raw)
    assert not _edit(p, "HDR", "HDR2").ok
    assert p.read_bytes() == raw


def test_utf16_is_read_as_text_and_edited_in_place(tmp_path):
    p = tmp_path / "out.txt"
    p.write_bytes("\ufeff結果: 成功\r\n件数 3\r\n".encode("utf-16-le"))
    r = oc_tools.read_tool({"file_path": str(p)})
    assert r.ok and "結果: 成功" in str(r) and "binary" not in str(r).lower()
    e = _edit(p, "件数 3", "件数 4")
    assert e.ok, str(e)
    assert p.read_bytes()[:2] == b"\xff\xfe"
    assert p.read_bytes()[2:].decode("utf-16-le") == "結果: 成功\r\n件数 4\r\n"


def test_write_keeps_cp932_or_says_it_changed_to_utf8(tmp_path):
    p = tmp_path / "run.bat"
    p.write_bytes(BAT)
    r = oc_tools.write_tool({"file_path": str(p), "content": "echo 終了\r\n"})
    assert r.ok and "Kept the file's encoding (cp932)" in str(r)
    assert p.read_bytes() == "echo 終了\r\n".encode("cp932")
    r = oc_tools.write_tool({"file_path": str(p), "content": "echo 終了 — 完了\r\n"})
    assert r.ok and "written as UTF-8" in str(r)
    assert p.read_bytes() == "echo 終了 — 完了\r\n".encode("utf-8")


def test_utf8_files_are_unchanged_behaviour(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("x = 1  # 値\n", encoding="utf-8")
    assert _edit(p, "x = 1", "x = 2 — ok").ok
    assert p.read_text(encoding="utf-8") == "x = 2 — ok  # 値\n"
    r = oc_tools.write_tool({"file_path": str(p), "content": "y = 3\n"})
    assert r.ok and "Kept the file's encoding" not in str(r) and "UTF-8" not in str(r)
