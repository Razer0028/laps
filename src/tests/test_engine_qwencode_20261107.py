"""Qwen Code から取り入れた 3 つの守り（2026-11-07 オーナー「Blackwall に Qwen Code の良い部分を導入して」）。

① 出力の上限で切れた手番の最後の呼び出しが書き換える道具なら走らせない（reject-incomplete-file-writes）
② 読んだ後にディスクで変わったファイルは Write で上書きしない（FileReadCache の mtime と大きさのずれ）
③ git の様子見（status・diff・ls-files）だけが続いたら一言（shell inspection stagnation）
"""
from __future__ import annotations

import json
import os

from laps.engine.aids import git_look_only
from tests.test_engine_aids_20261030 import run, sent  # noqa: F401（fixture）


def test_write_cut_by_the_output_limit_is_not_run(run):
    go, ws, _ = run
    cut = {"calls": [("Write", {"file_path": str(ws / "half.py"), "content": "def f():\n    return 1\n"})], "finish": "length"}
    text, a, fake, _ = go([cut, {"text": "やり直しました。"}], "half.py を作って")
    assert not (ws / "half.py").exists()
    assert "hit the token limit" in sent(fake, 1)
    assert a.counters.get("truncated_change_held") == 1


def test_read_only_call_cut_by_the_output_limit_still_runs(run):
    go, ws, _ = run
    (ws / "a.txt").write_text("hello\n")
    cut = {"calls": [("Read", {"file_path": str(ws / "a.txt")})], "finish": "length"}
    text, a, fake, _ = go([cut, {"text": "読みました。"}], "a.txt を読んで")
    assert "hello" in sent(fake, 1)
    assert not a.counters.get("truncated_change_held")


def test_write_over_a_file_changed_after_reading_is_refused(run):
    go, ws, _ = run
    p = ws / "conf.txt"
    p.write_text("one\n")

    script = [                       # Read の後、Write の前にほかの命令が書き換える
        {"calls": [("Read", {"file_path": str(p)})]},
        {"calls": [("Bash", {"command": f"printf 'two-longer\\n' > {p}"})]},
        {"calls": [("Write", {"file_path": str(p), "content": "mine\n"})]},
        {"text": "終わりました。"},
    ]
    text, a, fake, _ = go(script, "conf.txt を書き換えて")
    assert p.read_text() == "two-longer\n"
    assert "changed on disk after you last read or wrote it" in sent(fake, 3)


def test_write_after_reading_again_goes_through(run):
    go, ws, _ = run
    p = ws / "conf.txt"
    p.write_text("one\n")
    script = [
        {"calls": [("Read", {"file_path": str(p)})]},
        {"calls": [("Bash", {"command": f"printf 'two-longer\\n' > {p}"})]},
        {"calls": [("Read", {"file_path": str(p)})]},
        {"calls": [("Write", {"file_path": str(p), "content": "mine\n"})]},
        {"text": "終わりました。"},
    ]
    go(script, "conf.txt を書き換えて")
    assert p.read_text() == "mine\n"


def test_stale_guard_can_be_turned_off(run):
    go, ws, mp = run
    mp.setenv("LAPS_STALE_GUARD", "0")
    p = ws / "conf.txt"
    p.write_text("one\n")
    script = [
        {"calls": [("Read", {"file_path": str(p)})]},
        {"calls": [("Bash", {"command": f"printf 'two-longer\\n' > {p}"})]},
        {"calls": [("Write", {"file_path": str(p), "content": "mine\n"})]},
        {"text": "終わりました。"},
    ]
    go(script, "conf.txt を書き換えて")
    assert p.read_text() == "mine\n"


def test_six_git_looks_in_a_row_get_one_note(run):
    go, ws, _ = run
    cmds = ["git status", "git diff", "git status -s", "git diff --stat", "git --no-pager diff", "git status --short"]
    script = [{"calls": [("Bash", {"command": c})]} for c in cmds] + [{"text": "見ました。"}]
    text, a, fake, _ = go(script, "様子を見て")
    assert "only looked at the repository" in sent(fake, 6)
    assert "only looked at the repository" not in sent(fake, 5)
    assert a.counters.get("git_look_noted") == 1


def test_git_look_classifier():
    assert git_look_only("git status && git diff --cached")
    assert git_look_only("git -C repo ls-files")
    assert not git_look_only("git diff src/a.py")        # ファイルを指す diff は中身を見ている
    assert not git_look_only("git status; git add -A")   # 進めている
    assert not git_look_only("git log -3")
    assert not git_look_only("")
