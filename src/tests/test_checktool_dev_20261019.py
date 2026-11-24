"""★2026-10-19 Check の対象を JVM・C/C++・シェル・Deno/Bun・PHPUnit まで広げた（開発の強化）。

直す前に落ちることを確かめた物:
1 Java（Gradle・Maven）・CMake・Makefile のフォルダは「プロジェクトではない」と断っていた（目印に無かった）。
2 それらで Check を呼んでも走らせる物が 0 件だった。
3 javac・maven・gradle の試験・ctest・shellcheck の出力から場所を読めなかった（汎用の形に合わない）。
材料は _scratch/devfix/gen*.sh でこの機械で実際に走らせた出力（tests/fixtures/checktool/*.txt）。
"""
from __future__ import annotations

import os
import shutil

import pytest

from laps.oc import checkparse as cp
from laps.oc import checktool as ct

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "checktool")


def _fx(name: str) -> str:
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def _mk(tmp_path, files: dict):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return str(tmp_path)


@pytest.fixture()
def have(monkeypatch):
    """手元に在る道具を決める（本物の PATH に左右されない）。"""
    tools: set[str] = set()
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: f"/usr/bin/{n}" if n in tools else None)
    return tools


# ── 1・2 見つける ─────────────────────────────────────────────

@pytest.mark.parametrize("marker", ["build.gradle", "build.gradle.kts", "pom.xml", "CMakeLists.txt", "Makefile",
                                    "deno.json"])
def test_new_markers_make_a_project(tmp_path, marker):
    root = _mk(tmp_path, {marker: "", "src/a.txt": ""})
    assert ct.project_root(os.path.join(root, "src", "a.txt")) == root


def test_gradle_prefers_the_wrapper(tmp_path, have):
    root = _mk(tmp_path, {"build.gradle": "plugins { id 'java' }\n", "gradlew": "#!/bin/sh\n"})
    os.chmod(os.path.join(root, "gradlew"), 0o755)
    have.add("gradle")
    t = [c.line for c in ct.detect(root, "test")]
    assert any(x.startswith("./gradlew") and " test" in x for x in t), t
    ty = [c for c in ct.detect(root, "types")]
    assert ty and ty[0].name == "gradle" and "classes" in ty[0].line and ty[0].parser == "jvm"


def test_gradle_android_uses_android_tasks(tmp_path, have):
    root = _mk(tmp_path, {"settings.gradle.kts": "", "app/build.gradle.kts": 'plugins { id("com.android.application") }'})
    have.add("gradle")
    assert any("testDebugUnitTest" in c.line for c in ct.detect(root, "test"))
    assert any("assembleDebug" in c.line for c in ct.detect(root, "build"))
    assert not [c for c in ct.detect(root, "types") if c.name == "gradle"]   # Android に classes は無い


def test_maven_and_filter(tmp_path, have):
    root = _mk(tmp_path, {"pom.xml": "<project/>"})
    have.add("mvn")
    t = ct.detect(root, "test", name_filter="ATest")
    assert t and t[0].line.startswith("mvn -q -B test") and "-Dtest=ATest" in t[0].line


def test_cmake_configures_when_no_build_dir(tmp_path, have):
    root = _mk(tmp_path, {"CMakeLists.txt": "project(t)\n"})
    have.update({"cmake", "ctest"})
    t = ct.detect(root, "test")
    assert t and t[0].name == "ctest" and t[0].line.startswith("cmake -S . -B build && ")
    assert not ct.detect(root, "types")            # 作り先が無ければ速い検査では組み立てない
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "CMakeCache.txt").write_text("")
    assert [c.line for c in ct.detect(root, "types")] == ["cmake --build build"]


def test_makefile_test_target_only_when_present(tmp_path, have):
    have.add("make")
    root = _mk(tmp_path, {"Makefile": "all:\n\tcc a.c\n"})
    assert not ct.detect(root, "test")
    assert [c.line for c in ct.detect(root, "build")] == ["make -k"]
    root2 = _mk(tmp_path / "b", {"Makefile": "all:\n\tcc a.c\ncheck:\n\t./t\n"})
    assert [c.line for c in ct.detect(root2, "test")] == ["make check"]


def test_shellcheck_only_for_given_shell_files(tmp_path, have):
    have.add("shellcheck")
    root = _mk(tmp_path, {".git/HEAD": "", "a.sh": "echo $1\n"})
    assert not [c for c in ct.detect(root, "lint") if c.name == "shellcheck"]
    c = [c for c in ct.detect(root, "lint", ["a.sh"]) if c.name == "shellcheck"]
    assert c and c[0].line == "shellcheck -f gcc a.sh"


def test_ruff_on_loose_python_file_in_git_repo(tmp_path, have):
    """pyproject の無いリポジトリでも、渡した .py には ruff を当てる。"""
    have.add("ruff")
    root = _mk(tmp_path, {".git/HEAD": "", "tools/x.py": "import os\n"})
    assert [c.name for c in ct.detect(root, "lint", ["tools/x.py"])] == ["ruff"]




# ── 3 読む ─────────────────────────────────────────────────────

def test_parse_javac_japanese():
    d, f = cp.parse("jvm", _fx("javac.txt"))
    assert [(x.path, x.line, x.sev) for x in d] == [("src/A.java", 3, "error"), ("src/A.java", 4, "error")]


def test_parse_gradle_compile_dedups():
    cwd = "/home/user/project/_scratch/devfix/w/gr"
    d, _ = cp.parse("jvm", _fx("gradle.txt"), cwd)
    assert len(d) == 1 and d[0].path == "src/main/java/A.java" and d[0].line == 3
    assert "incompatible types" in d[0].msg


def test_parse_gradle_test():
    d, f = cp.parse("jvm", _fx("gradle_test.txt"))
    assert f == ["ATest > fails()"]
    assert d and d[0].path == "ATest.java" and d[0].line == 7 and d[0].sev == "fail"


def test_parse_maven_compile_and_test():
    cwd = "/home/user/project/_scratch/devfix/w/mv"
    d, _ = cp.parse("jvm", _fx("maven.txt"), cwd)
    assert [(x.path, x.line, x.col) for x in d] == [("src/main/java/A.java", 3, 13)]
    d, f = cp.parse("jvm", _fx("maven_test.txt"))
    assert f == ["ATest.fails"]
    assert d and d[0].line == 7 and "expected: <2> but was: <3>" in d[0].msg


def test_parse_ctest():
    d, f = cp.parse("ctest", _fx("ctest.txt"))
    assert f == ["breaks"]


def test_parse_make_gcc_via_generic():
    d, _ = cp.parse("generic", _fx("make_gcc.txt"))
    assert [(x.path, x.line, x.sev) for x in d] == [("main.c", 2, "error"), ("main.c", 3, "error"),
                                                     ("main.c", 2, "warning")]


def test_parse_shellcheck_code():
    d, _ = cp.parse("shellcheck", _fx("shellcheck.txt"))
    assert [(x.line, x.code) for x in d] == [(2, "SC2115"), (3, "SC2154")]
    assert not d[0].msg.endswith("]")


@pytest.mark.skipif(not (shutil.which("cmake") and shutil.which("ctest") and shutil.which("cc")),
                    reason="cmake/ctest/cc が無い")
def test_real_ctest_end_to_end(tmp_path, monkeypatch):
    """本物の Bash の道具を通して CMake の試験を走らせ、落ちた試験の名前が返る。"""
    root = _mk(tmp_path, {
        "CMakeLists.txt": "cmake_minimum_required(VERSION 3.10)\nproject(t C)\nenable_testing()\n"
                          "add_executable(bad bad.c)\nadd_test(NAME breaks COMMAND bad)\n",
        "bad.c": "int main(void){return 1;}\n"})
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    text, ok, results = ct.run(root, "test")
    assert not ok, text
    assert results and results[0].failed == ["breaks"], text
