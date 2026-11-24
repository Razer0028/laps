"""★2026-10-18 Check が pytest の無い Python と目印の無い試験のフォルダで動く（oc/checktool・oc/checkparse）。

直す前に落ちることを確かめた物:
1 pytest の入っていない Python（作業用のコンテナ の /opt/py・Windows の素の Python）では、Check(kind="test") が
  「No module named pytest」を落ちた試験として返していた。同じ試験は unittest で走る（標準ライブラリ）。
2 目印（pyproject.toml・.git など）の無いフォルダは、試験のファイルがあっても「プロジェクトの中ではない」と断っていた
  （オーナー機の Haiku の走行: `_scratch\\evalbench`）。自動の検査（autolint・autoverify）は今までどおり走らせない。
3 unittest の出力の読み方。材料は 作業用のコンテナ で本物を走らせた出力（tests/fixtures/checktool/unittest_*.txt）。
"""
from __future__ import annotations

import os

from laps.oc import checkparse as cp
from laps.oc import checktool as ct

FX = os.path.join(os.path.dirname(__file__), "fixtures", "checktool")


def _fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


def test_unittest_failures_point_at_the_project_lines():
    diags, failed = cp.parse("unittest", _fx("unittest_fail.txt"), "/tmp/ut-fix")
    assert failed == ["tests.test_calc.CalcTest.test_average", "tests.test_calc.CalcTest.test_median_even"]
    got = {(d.path, d.line): d.msg for d in diags}
    assert "NameError: name 'total' is not defined" in got[("calc.py", 5)]          # いちばん深い自分の行
    assert "AssertionError: 3 != 2.5" in got[("tests/test_calc.py", 17)]
    assert all(d.sev == "fail" for d in diags)


def test_unittest_windows_paths():
    text = _fx("unittest_fail.txt").replace("/tmp/ut-fix/tests/test_calc.py", r"D:\TEST\_scratch\evalbench\tests\test_calc.py") \
        .replace("/tmp/ut-fix/calc.py", r"D:\TEST\_scratch\evalbench\calc.py")
    diags, _ = cp.parse("unittest", text, r"D:\TEST\_scratch\evalbench")
    assert {(d.path.replace("\\", "/"), d.line) for d in diags} == {("calc.py", 5), ("tests/test_calc.py", 17)}


def test_unittest_ok_has_nothing():
    assert cp.parse("unittest", _fx("unittest_ok.txt"), "/tmp/ut-fix") == ([], [])


def test_missing_pytest_falls_back_to_unittest(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("import unittest\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    seen = []

    def fake_run_one(cmd, timeout=None):
        seen.append(cmd)
        if cmd.name == "pytest":
            return ct.Result(cmd, False, _fx("nopytest.txt") + "\nexit code: 1", [], [])
        d, f = cp.parse(cmd.parser, _fx("unittest_fail.txt"), str(tmp_path))
        return ct.Result(cmd, False, _fx("unittest_fail.txt"), d, f)
    monkeypatch.setattr(ct, "run_one", fake_run_one)
    text, ok, results = ct.run(str(tmp_path), "test", name_filter="median")
    assert [c.name for c in seen] == ["pytest", "unittest"]
    assert "-m unittest discover -v -s tests" in seen[1].line and "-k median" in seen[1].line   # tests/ は package でない
    assert not ok and "No module named pytest" not in text and "test_median_even" in text


def test_folder_with_tests_but_no_marker_is_checked(tmp_path, monkeypatch):
    """★2: 目印が無くても、試験のファイルがあれば Check はそこで走る。自動の検査の根（project_root）は None のまま。"""
    d = tmp_path / "evalbench"
    d.mkdir()
    (d / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (d / "test_calc.py").write_text("import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
                                    "    def test_add(self):\n        self.assertEqual(add(1, 2), 3)\n", encoding="utf-8")
    assert ct.project_root(str(d)) is None
    ran = []
    monkeypatch.setattr(ct, "run", lambda root, kind, paths, flt, timeout: (ran.append((root, kind)), ("x", True, []))[1])
    r = ct.check_tool({"kind": "test", "path": str(d)})
    assert ran == [(str(d), "test")] and "not inside a project" not in str(r)
    (d / "test_calc.py").unlink()
    r2 = ct.check_tool({"kind": "test", "path": str(d)})
    assert "not inside a project" in str(r2)                                      # 試験が無ければ今までどおり断る
