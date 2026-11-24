"""2026-10-19 の点検: 本物の Bash が全角空白のパスを直す／空の失敗に行き先を言う／Edit の 2 回目の外れ／Lab の無いパス。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.oc import tools


def test_bash_tool_fixes_fullwidth_space_path(tmp_path, monkeypatch):
    real = tmp_path / "田中\u3000優輝"
    real.mkdir()
    (real / "a.txt").write_text("hi", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    typed = str(tmp_path / "田中 優輝" / "a.txt")
    r = tools.bash_tool({"command": f'cat "{typed}"', "workdir": str(tmp_path)})
    assert r.ok, str(r)
    assert "hi" in str(r)


def test_empty_fail_note_redirect_and_hidden_error():
    n = tools._empty_fail_note("py run.py > out.txt", "")
    assert "out.txt" in n and "Read" in n
    n2 = tools._empty_fail_note("dir C:\\x 2>nul", "")
    assert "2>nul" in n2
    assert tools._empty_fail_note("py run.py > out.txt", "Traceback") == ""
    assert tools._empty_fail_note("ls 2>/dev/null", "") and "/dev/null" not in tools._empty_fail_note("ls > /dev/null", "")


def test_bash_tool_empty_failure_points_to_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = tools.bash_tool({"command": "ls no_such_dir_x > out.txt 2>&1", "workdir": str(tmp_path)})
    assert not r.ok
    assert "out.txt" in str(r) and "Read that file" in str(r)


def test_edit_second_miss_changes_method(tmp_path):
    p = tmp_path / "a.php"
    p.write_text("<?php\necho 1;\n", encoding="utf-8")
    tools._EDIT_MISSES.clear()
    r1 = tools.edit_tool({"file_path": str(p), "old_string": "echo 9;", "new_string": "echo 2;"})
    assert not r1.ok and "in a row" not in str(r1)
    r2 = tools.edit_tool({"file_path": str(p), "old_string": "echo 8;", "new_string": "echo 2;"})
    assert not r2.ok and "miss 2 in a row" in str(r2) and "Write" in str(r2)
    r3 = tools.edit_tool({"file_path": str(p), "old_string": "echo 1;", "new_string": "echo 2;"})
    assert r3.ok
    assert str(p) not in tools._EDIT_MISSES and all("a.php" not in k for k in tools._EDIT_MISSES)


