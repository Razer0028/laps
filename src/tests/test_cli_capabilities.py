"""能力を「道具」ではなく CLI として置く型（2026-09-12）。

★設計: 薄い中核が勝った理由は「毎手番どの道具かを選ばせない」こと。だが
  リモート作業・知識参照・巻き戻しは目標に必要で捨てられない。
  → 道具宣言をやめ、端末から叩ける CLI にする。前置きは増えない。
"""
from __future__ import annotations

import os
import subprocess
import sys

import laps.cli_checkpoint as UN


# ── bw-undo ─────────────────────────────────────────────────
def _sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True)


def test_git_の作業場で戻せる(tmp_path, monkeypatch):
    ws = tmp_path / "g"; ws.mkdir()
    _sh("git init -q && git config user.email a@b && git config user.name a", str(ws))
    (ws / "f.txt").write_text("original")
    _sh("git add -A && git commit -qm init", str(ws))
    monkeypatch.chdir(ws)
    assert UN.main(["mark"]) == 0
    (ws / "f.txt").write_text("DESTROYED")
    assert UN.main(["revert"]) == 0
    assert (ws / "f.txt").read_text() == "original", "戻っていない"


def test_git_の無い作業場でも戻せる(tmp_path, monkeypatch):
    """★端末に直接打つ中核では、git の無い場所でも作業する。"""
    ws = tmp_path / "n"; ws.mkdir()
    (ws / "f.txt").write_text("original")
    monkeypatch.chdir(ws)
    assert UN.main(["mark"]) == 0
    (ws / "f.txt").write_text("DESTROYED")
    assert UN.main(["revert"]) == 0
    assert (ws / "f.txt").read_text() == "original"


def test_印が無ければ戻さない(tmp_path, monkeypatch, capsys):
    """★「戻した」と言って何もしないのが最悪。印が無いなら失敗と言う。"""
    ws = tmp_path / "x"; ws.mkdir()
    monkeypatch.chdir(ws)
    assert UN.main(["revert"]) == 1
    assert "印がありません" in capsys.readouterr().err


def test_mark_は手元の変更を消さない(tmp_path, monkeypatch):
    """★`git stash push` だと手元が消えて驚く。控えるだけで作業木は触らない。"""
    ws = tmp_path / "m"; ws.mkdir()
    _sh("git init -q && git config user.email a@b && git config user.name a", str(ws))
    (ws / "f.txt").write_text("original")
    _sh("git add -A && git commit -qm init", str(ws))
    (ws / "f.txt").write_text("work in progress")
    monkeypatch.chdir(ws)
    UN.main(["mark"])
    assert (ws / "f.txt").read_text() == "work in progress", "mark が手元を消した"


def test_エントリポイントが登録されている():
    import tomllib
    from pathlib import Path
    s = tomllib.loads(Path("pyproject.toml").read_text())["project"]["scripts"]
    assert s["bw-undo"] == "laps.cli_checkpoint:main"
