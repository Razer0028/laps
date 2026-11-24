"""Tests for Ω-K-ε tool additions: multi-Edit, TodoWrite, Bash background."""
from __future__ import annotations
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from laps.tools import edit, todowrite, bash, bash_output


# --- multi-Edit ---

def test_edit_single_classic():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("alpha\nbravo\ncharlie\n")
        p = f.name
    try:
        r = edit.SPEC.handler({"file_path": p, "old_string": "bravo", "new_string": "BRAVO"})
        assert r.startswith("OK")
        assert Path(p).read_text() == "alpha\nBRAVO\ncharlie\n"
    finally:
        os.unlink(p)


def test_edit_multi_sequential():
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("one\ntwo\nthree\n")
        p = f.name
    try:
        r = edit.SPEC.handler({
            "file_path": p,
            "edits": [
                {"old_string": "one", "new_string": "ONE"},
                {"old_string": "three", "new_string": "THREE"},
            ],
        })
        assert r.startswith("OK")
        assert Path(p).read_text() == "ONE\ntwo\nTHREE\n"
    finally:
        os.unlink(p)


def test_edit_multi_atomic_on_failure():
    """If edit[1] fails, the file must be unchanged (edit[0] not flushed)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("foo\nbar\n")
        p = f.name
    orig = Path(p).read_text()
    try:
        r = edit.SPEC.handler({
            "file_path": p,
            "edits": [
                {"old_string": "foo", "new_string": "FOO"},
                {"old_string": "DOES_NOT_EXIST", "new_string": "x"},
            ],
        })
        assert r.startswith("Error")
        assert "edit[1]" in r
        assert Path(p).read_text() == orig
    finally:
        os.unlink(p)


# --- TodoWrite ---

def _todo_env(tmp: Path) -> str:
    sid = "test_session_" + str(int(time.time() * 1000))
    os.environ["LAPS_HOME"] = str(tmp)
    os.environ["LAPS_SESSION_ID"] = sid
    (tmp / "sessions" / sid).mkdir(parents=True, exist_ok=True)
    return sid


def test_todowrite_replace_then_complete(tmp_path):
    _todo_env(tmp_path)
    r = todowrite.SPEC.handler({
        "todos": [
            {"content": "task A", "status": "pending"},
            {"content": "task B", "status": "pending"},
        ]
    })
    assert "0/2 done" in r
    r2 = todowrite.SPEC.handler({"complete": "task A"})
    assert "1/2 done" in r2


def test_todowrite_add_string(tmp_path):
    _todo_env(tmp_path)
    todowrite.SPEC.handler({"add": "first task"})
    r = todowrite.SPEC.handler({"add": ["second", "third"]})
    assert "0/3 done" in r


def test_todowrite_read_only_returns_snapshot(tmp_path):
    _todo_env(tmp_path)
    todowrite.SPEC.handler({"add": "alpha"})
    r = todowrite.SPEC.handler({})
    assert "alpha" in r
    assert "TODO" in r


# --- Bash background ---

def test_bash_background_spawns_and_polls(tmp_path):
    os.environ["LAPS_HOME"] = str(tmp_path)
    r = bash.SPEC.handler({
        "command": "echo hello-bg && sleep 0.2 && echo bye",
        "run_in_background": True,
    })
    assert r.startswith("OK: launched")
    # Parse pid from response
    for line in r.splitlines():
        if "pid:" in line:
            pid = int(line.split(":")[-1].strip())
            break
    # Wait for completion
    time.sleep(0.6)
    out = bash_output.SPEC.handler({"pid": pid})
    assert "hello-bg" in out
    assert "bye" in out
    assert "alive: False" in out


def test_bash_output_missing_pid_errors(tmp_path):
    os.environ["LAPS_HOME"] = str(tmp_path)
    r = bash_output.SPEC.handler({"pid": 99999999})
    assert r.startswith("Error: no background record")
