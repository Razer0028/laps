"""2026-10-19 走行記録から: Read の find を Unity・.NET でない物に付けた形と、old と new が同じ Edit。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.oc import tools


def test_find_on_text_file_lists_matching_lines(tmp_path):
    p = tmp_path / "Assembly-CSharp.decompiled.cs"
    p.write_text("class A {\n  int hp;\n  void Heal() { hp += 1; }\n}\n", encoding="utf-8")
    r = tools.read_tool({"file_path": str(p), "find": "HP"})
    assert r.ok, str(r)
    assert "2:   int hp;" in str(r) and "3:" in str(r)
    assert "2 line(s)" in str(r)


def test_find_on_text_file_without_hit_is_empty(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("nothing here\n", encoding="utf-8")
    r = tools.read_tool({"file_path": str(p), "find": "zzz"})
    assert r.ok and r.kind == "empty"


def test_find_on_native_binary_lists_strings(tmp_path):
    p = tmp_path / "GameAssembly.dll"
    p.write_bytes(b"MZ\x90\x00" + b"\x00" * 60 + b"PlayerController_Update\x00" + b"\x01\x02" * 20
                  + "プレイヤー".encode("utf-16-le") + b"\x00\x00")
    r = tools.read_tool({"file_path": str(p), "find": "playercontroller"})
    assert r.ok, str(r)
    assert "PlayerController_Update" in str(r)
    assert "global-metadata.dat" in str(r)


def test_edit_identical_on_existing_text_is_not_an_error(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\ny = 2\n", encoding="utf-8")
    r = tools.edit_tool({"file_path": str(p), "old_string": "y = 2", "new_string": "y = 2"})
    assert r.ok and "No change was needed" in str(r)
    assert p.read_text(encoding="utf-8") == "x = 1\ny = 2\n"


def test_edit_identical_missing_text_still_fails(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n", encoding="utf-8")
    r = tools.edit_tool({"file_path": str(p), "old_string": "z = 3", "new_string": "z = 3"})
    assert not r.ok


def test_edits_mixed_identical_and_real_change(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\ny = 2\n", encoding="utf-8")
    r = tools.edit_tool({"file_path": str(p), "edits": [
        {"old_string": "x = 1", "new_string": "x = 1"},
        {"old_string": "y = 2", "new_string": "y = 3"}]})
    assert r.ok and "applied" in str(r)
    assert p.read_text(encoding="utf-8") == "x = 1\ny = 3\n"


# ── Windows の Bash（走行記録: PowerShell の引数名の誤り 6 回・複数行の py -c 2 回）──
from laps.tools import bash as _bash  # noqa: E402


def test_multiline_py_c_is_unwrapped():
    got = _bash._unwrap_multiline_python('py -X utf8 -c "\nimport sys\nprint(sys.argv)\n"')
    assert got is not None
    exe, flags, body = got
    assert exe == "py" and flags == "-X utf8"
    assert body == "import sys\nprint(sys.argv)\n"
    assert _bash._unwrap_multiline_python('py -c "print(1)"') is None
    assert _bash._unwrap_multiline_python("dir\ndir") is None


def test_multiline_py_c_runs_as_script_on_windows(monkeypatch):
    import os
    import sys
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    (line,), kw = _bash._spawn_args('python -c "\nx = 1\nprint(x)\n"')
    path = kw["_bw_script"]
    try:
        assert path.endswith(".py") and path in line and line.find("python") >= 0
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == "x = 1\nprint(x)\n"
    finally:
        os.remove(path)


def test_ps_param_hint():
    out = ("Get-FileHash : パラメーター名 'MD5' に一致するパラメーターが見つかりません。\r\n"
           "発生場所 行:1 文字:48")
    h = _bash._win_ps_param_hint(out)
    assert "-Algorithm MD5" in h
    h2 = _bash._win_ps_param_hint("Format-Hex : パラメーター名 'Offset' に一致するパラメーターが見つかりません。")
    assert "byte_offset" in h2
    h3 = _bash._win_ps_param_hint("Get-Foo : A parameter cannot be found that matches parameter name 'Bar'.")
    assert "Get-Command Get-Foo" in h3
    assert _bash._win_ps_param_hint("ok") == ""


def test_prn_hint():
    assert "py 台本.py" in _bash._win_prn_hint("py -c \"\nprint(1)\"", "デバイス PRN を初期化できません")
    assert _bash._win_prn_hint("dir", "デバイス PRN を初期化できません") == ""
