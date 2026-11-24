"""★2026-10-17 Check 道具（oc/checktool.py・開発の強化 第 1 段）。命令は**起こさない**（偽の Bash を差し込む）。

直す前に落ちることを確かめた物:
1 ★走らせる場所の鍵が違っていた。`bash_tool` が見るのは `workdir` で、`cwd` は黙って捨てられる。
  実際に踏んだ（2026-10-17）: 見本のプロジェクトのつもりが**そのとき居たフォルダ**で pytest が走り、
  Blackwall の全試験が始まった（止めた）。→ 渡す鍵を試験で固定する。
2 手元に無い道具を挙げない（勧めても打てない）。
3 paths を渡したら、その種類のファイルを見る道具だけがその場所に絞られる。
4 使い捨ての台本の置き場（目印の無いフォルダ）はプロジェクトと見なさない＝自動の検査を走らせない。
5 落ちたのに場所が読めないときは、黙らずに出力の末尾を見せる。
"""
from __future__ import annotations

import json
import os

import pytest

from laps.oc import checktool as ct


@pytest.fixture()
def spy(monkeypatch):
    """偽の Bash。渡された入力を覚え、決めた出力を返す（本物の命令は走らない）。"""
    calls: list[dict] = []
    out = {"text": "", "ok": True, "kind": "ok"}

    class _Res(str):
        ok = True
        kind = "ok"

    def fake(inp: dict):
        calls.append(dict(inp))
        r = _Res(out["text"])
        r.ok, r.kind = out["ok"], out["kind"]
        return r
    monkeypatch.setattr("laps.oc.tools.bash_tool", fake)
    return calls, out


def _mk(tmp_path, files: dict):
    for name, body in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_runs_in_the_project_not_the_current_folder(tmp_path, spy, monkeypatch):
    """★1: `workdir` で渡す。`cwd` だけだと黙って今いる場所で走る（実際に全試験を始めた）。"""
    calls, out = spy
    root = _mk(tmp_path, {"pyproject.toml": "[tool.pytest.ini_options]\n", "tests/test_a.py": "def test_x():\n    pass\n"})
    out["text"] = "1 passed in 0.01s\nexit code: 0"
    monkeypatch.setattr(ct, "_python", lambda: "python3")
    ct.run(root, "test")
    assert calls, "Bash を呼んでいない"
    assert calls[0].get("workdir") == root
    assert os.path.normpath(str(calls[0].get("workdir"))) != os.path.normpath(os.getcwd())
    assert isinstance(calls[0].get("timeout"), int) and calls[0]["timeout"] >= 1000   # ミリ秒


def test_only_tools_that_exist_here(tmp_path, monkeypatch):
    """★2: ruff が無ければ ruff の行は出ない。あれば出る。"""
    root = _mk(tmp_path, {"pyproject.toml": "", "a.py": "x = 1\n"})
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: None)
    assert ct.detect(root, "lint") == []
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/" + n if n == "ruff" else None)
    cmds = ct.detect(root, "lint")
    assert [c.name for c in cmds] == ["ruff"] and cmds[0].line.startswith("ruff check")


def test_paths_narrow_only_the_tools_that_read_files(tmp_path, monkeypatch):
    """★3: .py を渡したら ruff はその 1 本だけ。.ts しか渡していなければ ruff は出ない。"""
    root = _mk(tmp_path, {"pyproject.toml": "", "a.py": "", "b.ts": ""})
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/" + n if n == "ruff" else None)
    assert ct.detect(root, "lint", ["a.py"])[0].line.endswith("a.py")
    assert ct.detect(root, "lint", ["b.ts"]) == []
    assert ct.detect(root, "lint")[0].line.endswith(" .")          # paths なし＝全体


def test_scratch_folder_is_not_a_project(tmp_path):
    """★4: 目印の無い場所は None（使い捨ての台本で自動の検査を走らせない）。"""
    d = tmp_path / "scratch"
    d.mkdir()
    (d / "one_off.py").write_text("print(1)\n", encoding="utf-8")
    assert ct.project_root(str(d)) is None
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert ct.project_root(str(d / "one_off.py")) == str(tmp_path)   # 上に目印があれば、そこが根


def test_report_shows_places_and_counts(tmp_path, spy, monkeypatch):
    root = _mk(tmp_path, {"pyproject.toml": "", "a.py": ""})
    calls, out = spy
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/" + n if n == "ruff" else None)
    out["text"] = "a.py:5:20: F821 Undefined name `x`\nFound 1 error.\nexit code: 1"
    out["ok"] = False
    text, ok, res = ct.run(root, "lint")
    assert ok is False
    assert "FAILED" in text and "1 error" in text and "a.py:5:20 error F821: Undefined name `x`" in text


def test_failure_without_places_shows_the_tail(tmp_path, spy, monkeypatch):
    """★5: 形の分からない失敗でも黙らない（末尾をそのまま見せる）。"""
    root = _mk(tmp_path, {"pyproject.toml": "", "a.py": ""})
    calls, out = spy
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/" + n if n == "ruff" else None)
    out["text"] = "something went wrong\nno place here\nexit code: 2"
    out["ok"] = False
    text, ok, _ = ct.run(root, "lint")
    assert ok is False and "something went wrong" in text


def test_exit_code_decides_even_when_nothing_was_parsed(tmp_path, spy, monkeypatch):
    """指摘が 0 件でも、終了コードが 0 でなければ通っていない。"""
    root = _mk(tmp_path, {"pyproject.toml": "", "a.py": ""})
    calls, out = spy
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/" + n if n == "ruff" else None)
    out["text"] = "exit code: 1"
    out["ok"] = True                              # 道具は「走った」と言うが、中身は落ちている
    _, ok, _ = ct.run(root, "lint")
    assert ok is False
    out["text"] = "All checks passed!\nexit code: 0"
    _, ok2, _ = ct.run(root, "lint")
    assert ok2 is True


def test_denied_by_policy_is_reported_not_swallowed(tmp_path, spy, monkeypatch):
    root = _mk(tmp_path, {"pyproject.toml": "", "a.py": ""})
    calls, out = spy
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/" + n if n == "ruff" else None)
    out["text"] = "Error: this command was refused by the policy gate"
    out["ok"], out["kind"] = False, "denied"
    text, ok, res = ct.run(root, "lint")
    assert ok is False and "not run" in text and res[0].denied


def test_nothing_found_says_so(tmp_path, monkeypatch):
    root = _mk(tmp_path, {".git": ""})
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: None)
    text, ok, res = ct.run(root, "auto")
    assert res == [] and ok is False and "nothing ran" in text


def test_auto_prefers_fast_checks_then_falls_back_to_tests(tmp_path, monkeypatch):
    root = _mk(tmp_path, {"pyproject.toml": "[tool.pytest.ini_options]\n", "tests/test_a.py": "", "a.py": ""})
    monkeypatch.setattr(ct, "_python", lambda: "python3")
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: None)
    assert [c.name for c in ct.detect(root, "auto")] == ["pytest"]          # 速い検査が無ければ試験
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/" + n if n == "ruff" else None)
    assert [c.name for c in ct.detect(root, "auto")] == ["ruff"]            # 在れば速い方だけ


def test_node_tools_come_from_the_project(tmp_path, monkeypatch):
    """tsc・eslint・vitest はプロジェクトの node_modules/.bin から（PATH の別の版を使わない）。"""
    root = _mk(tmp_path, {"package.json": json.dumps({"scripts": {"test": "vitest run"}}),
                          "tsconfig.json": "{}", "eslint.config.js": "export default [];"})
    binp = os.path.join(root, "node_modules", ".bin")
    os.makedirs(binp)
    for n in ("tsc", "eslint", "vitest"):
        p = os.path.join(binp, n)
        with open(p, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(p, 0o755)
    assert [c.name for c in ct.detect(root, "types")] == ["tsc"]
    assert ct.detect(root, "types")[0].line.startswith(binp)
    assert [c.name for c in ct.detect(root, "lint", ["a.ts"])] == ["eslint"]
    assert [c.name for c in ct.detect(root, "test")] == ["vitest"]


def test_name_filter_is_passed_to_the_runner(tmp_path, monkeypatch):
    root = _mk(tmp_path, {"pyproject.toml": "[tool.pytest.ini_options]\n", "tests/test_a.py": ""})
    monkeypatch.setattr(ct, "_python", lambda: "python3")
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: None)
    assert "-k test_add" in ct.detect(root, "test", None, "test_add")[0].line


def test_paths_with_spaces_are_quoted(tmp_path, monkeypatch):
    root = _mk(tmp_path, {"pyproject.toml": ""})
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/ruff" if n == "ruff" else None)
    assert '"my file.py"' in ct.detect(root, "lint", ["my file.py"])[0].line
