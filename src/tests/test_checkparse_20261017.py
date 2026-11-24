"""★2026-10-17 検査・試験の出力を「ファイル:行」に揃える（oc/checkparse.py・開発の強化 第 1 段）。

材料は作り物ではなく、作業用のコンテナ（Kali のコンテナ）で実際に走らせた出力（fixtures/checktool/*.txt）:
  ruff 0.14・eslint 9.39（JSON と既定の見た目）・tsc 5・cargo check / test・go build / vet / test・
  pytest（--tb=line）・jest 29・vitest 2・dotnet build（★日本語の環境の本文）。
★「落ちた試験の名前」と「場所つきの指摘」を分けて数える（直す場所と、再現する命令は別の物）。
"""
from __future__ import annotations

import os

import pytest

from laps.oc import checkparse as cp

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "checktool")


def fx(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def test_ruff_concise():
    d, failed = cp.parse("ruff", fx("ruff.txt"), "samples/py")
    assert failed == []
    assert [(x.path, x.line, x.col, x.code) for x in d] == [
        ("app.py", 1, 8, "F401"), ("app.py", 5, 20, "F821"), ("app.py", 9, 5, "F841")]
    assert d[1].msg == "Undefined name `undefined_name`"
    assert all(x.sev == "error" for x in d)


@pytest.mark.parametrize("name", ["eslint9_json.txt", "eslint9_stylish.txt"])
def test_eslint_both_formats_agree(name):
    """JSON でも既定の見た目でも、同じ場所・同じ重さ・同じ規則が出る（どちらの形でも読める）。"""
    d, failed = cp.parse("eslint", fx(name), "/work/bw-devcheck-20261017/samples/js")
    assert failed == []
    assert [(x.path, x.line, x.col, x.sev, x.code) for x in d] == [
        ("main.js", 1, 7, "warning", "no-unused-vars"), ("main.js", 2, 13, "error", "no-undef")]


def test_tsc_pretty_false():
    d, _ = cp.parse("tsc", fx("tsc.txt"), "samples/ts")
    assert [(x.path, x.line, x.col, x.code) for x in d] == [("src/a.ts", 1, 7, "TS2322"), ("src/a.ts", 2, 33, "TS2322")]


def test_dotnet_build_japanese_locale():
    """★日本語の環境の dotnet は本文が日本語で、末尾に [..csproj] が付く。場所と番号は読めて、csproj は本文に混ぜない。"""
    d, _ = cp.parse("dotnet", fx("dotnet_build.txt"), "/work/bw-devcheck-20261017/samples/cs")
    assert [(x.path, x.line, x.col, x.code) for x in d] == [
        ("Program.cs", 1, 9, "CS0029"), ("Program.cs", 2, 1, "CS0103"), ("Program.cs", 2, 19, "CS0103")]
    assert all(".csproj" not in x.msg for x in d)
    assert "'int'" in d[0].msg


def test_cargo_check_short():
    d, failed = cp.parse("cargo", fx("cargo_check.txt"), "samples/rs")
    assert failed == []
    assert [(x.path, x.line, x.col, x.code) for x in d] == [("src/main.rs", 3, 20, "E0425"), ("src/main.rs", 2, 18, "E0308")]


def test_cargo_test_failure_has_name_and_place():
    d, failed = cp.parse("cargo", fx("cargo_test.txt"), "samples/rst")
    assert failed == ["tests::it_adds"]
    assert [(x.path, x.line, x.sev) for x in d] == [("src/lib.rs", 3, "fail")]
    assert "left == right" in d[0].msg


def test_go_build_and_vet():
    d, failed = cp.parse("go", fx("go_build.txt"), "samples/go")
    assert failed == []
    assert [(x.path, x.line, x.col) for x in d] == [("main.go", 6, 6), ("main.go", 6, 14), ("main.go", 7, 14)]
    d2, _ = cp.parse("go", fx("go_vet.txt"), "samples/go")
    assert [(x.path, x.line, x.col) for x in d2] == [("main.go", 6, 14)]   # 頭の "vet: " を場所に混ぜない


def test_go_test_failure():
    d, failed = cp.parse("go", fx("go_test.txt"), "samples/got")
    assert failed == ["TestAdd"]
    assert [(x.path, x.line, x.sev, x.msg) for x in d] == [("add_test.go", 7, "fail", "add(1, 2) = 3, want 4")]


def test_pytest_tb_line():
    d, failed = cp.parse("pytest", fx("pytest_line.txt"), os.path.dirname(FIX))
    assert failed == ["tests/test_app.py::test_add"]          # そのまま打ち直せる名前
    assert d and d[0].line == 5 and d[0].path.endswith("tests/test_app.py") and d[0].sev == "fail"


def test_jest_and_vitest():
    d, failed = cp.parse("jest", fx("jest.txt"), "samples/jest")
    assert failed == ["adds"]
    assert (d[0].path, d[0].line, d[0].col, d[0].sev) == ("sum.test.js", 1, 36, "fail")
    d2, failed2 = cp.parse("vitest", fx("vitest.txt"), "samples/vitest")
    assert failed2 == ["sum.test.js > adds"]
    assert (d2[0].path, d2[0].line, d2[0].col) == ("sum.test.js", 2, 36)
    assert "expected 3 to be 4" in d2[0].msg


def test_npm_test_picks_the_right_reader():
    """package.json の `npm test` は中身が jest か vitest か分からない＝出力の形で選ぶ。"""
    assert cp.parse("npm-test", fx("vitest.txt"), "")[1] == ["sum.test.js > adds"]
    assert cp.parse("npm-test", fx("jest.txt"), "")[1] == ["adds"]


def test_windows_paths_and_relative():
    t = "C:\\work\\app\\src\\a.ts(3,5): error TS2345: Argument of type 'x'.\n"
    d, _ = cp.parse("tsc", t, "C:\\work\\app")
    assert (d[0].path, d[0].line, d[0].col) == ("src/a.ts", 3, 5)
    d2, _ = cp.parse("tsc", t, "D:\\other")              # 別のドライブ＝相対にできない（元のまま・落ちない）
    assert d2[0].path.endswith("a.ts")


def test_unknown_tool_falls_back_to_the_common_shape():
    d, _ = cp.parse("mypy", "app.py:12:3: error: Incompatible types\napp.py:14: note: see here\n", "")
    assert [(x.path, x.line, x.col, x.sev) for x in d] == [("app.py", 12, 3, "error")]   # note は指摘にしない


def test_empty_and_passing_output_is_quiet():
    for name in ("ruff", "eslint", "tsc", "cargo", "go", "pytest", "jest", "vitest", "dotnet", "php", "x"):
        assert cp.parse(name, "", "") == ([], [])
    assert cp.parse("pytest", "2 passed in 0.01s\n", "") == ([], [])
    assert cp.summary([], []) == ""


def test_summary_counts_each_kind():
    d, failed = cp.parse("eslint", fx("eslint9_json.txt"), "")
    assert cp.summary(d, failed) == "1 error・1 warning"
    d2, f2 = cp.parse("pytest", fx("pytest_line.txt"), "")
    assert cp.summary(d2, f2) == "1 failing test"
