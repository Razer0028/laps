"""An edit that breaks syntax should be refused, not written.

SWE-agent found a prominent failure mode was models repeatedly editing the same
snippet after introducing a syntax error. The gate refuses an edit only when it
turns a VALID file INVALID — so incrementally fixing an already-broken file is
never blocked.
"""
from __future__ import annotations
import os
from pathlib import Path

import pytest

from laps.tools import edit as E


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("LAPS_EDIT_SYNTAX_GATE", raising=False)
    return tmp_path


def _write(tmp, name, body):
    p = tmp / name
    p.write_text(body)
    return p


def test_edit_that_breaks_python_syntax_is_refused_and_file_untouched(workspace):
    f = _write(workspace, "m.py", "def f(a, b):\n    return a + b\n")
    before = f.read_text()
    out = E._run({"file_path": str(f), "old_string": "return a + b",
                  "new_string": "return a +"})   # trailing operator → SyntaxError
    assert out.startswith("Error") and "構文" in out
    assert f.read_text() == before, "拒否したのにファイルが変わっている"


def test_a_valid_to_valid_python_edit_still_applies(workspace):
    f = _write(workspace, "m.py", "def f(a, b):\n    return a + b\n")
    out = E._run({"file_path": str(f), "old_string": "a + b", "new_string": "a * b"})
    assert out.startswith("OK")
    assert "a * b" in f.read_text()


def test_fixing_an_already_broken_file_is_not_blocked(workspace):
    # old is already invalid → the gate must not fire, even if new is still invalid.
    f = _write(workspace, "m.py", "def f(a, b):\n    return a +\n")
    out = E._run({"file_path": str(f), "old_string": "return a +",
                  "new_string": "return a + b"})   # this one fixes it
    assert out.startswith("OK") and "a + b" in f.read_text()
    # and even old-invalid → still-invalid is allowed (not the gate's job)
    g = _write(workspace, "n.py", "def g(:\n    pass\n")
    out2 = E._run({"file_path": str(g), "old_string": "pass", "new_string": "return"})
    assert out2.startswith("OK")


def test_broken_json_edit_is_refused(workspace):
    f = _write(workspace, "c.json", '{"a": 1, "b": 2}\n')
    out = E._run({"file_path": str(f), "old_string": '"b": 2', "new_string": '"b": 2,'})
    assert out.startswith("Error") and "構文" in out


def test_non_parseable_files_are_never_gated(workspace):
    f = _write(workspace, "notes.txt", "hello\n")
    out = E._run({"file_path": str(f), "old_string": "hello", "new_string": "def f(:"})
    assert out.startswith("OK")  # .txt is not syntax-checked


def test_the_gate_can_be_disabled(workspace, monkeypatch):
    monkeypatch.setenv("LAPS_EDIT_SYNTAX_GATE", "0")
    f = _write(workspace, "m.py", "x = 1\n")
    out = E._run({"file_path": str(f), "old_string": "x = 1", "new_string": "x = ("})
    assert out.startswith("OK"), "ゲート無効化時は書けるべき"


def test_appending_a_second_yaml_document_is_not_rejected(workspace):
    # Multi-doc YAML (k8s/Ansible) is valid; safe_load would wrongly reject it.
    f = _write(workspace, "k.yaml", "kind: Deployment\n")
    out = E._run({"file_path": str(f), "old_string": "kind: Deployment\n",
                  "new_string": "kind: Deployment\n---\nkind: Service\n"})
    assert out.startswith("OK"), f"正しい multi-doc YAML が拒否された: {out}"
    assert "---" in f.read_text()


def test_truly_broken_yaml_is_still_refused(workspace):
    f = _write(workspace, "k.yaml", "a: 1\n")
    out = E._run({"file_path": str(f), "old_string": "a: 1", "new_string": "a: 1\n  b: ]["})
    assert out.startswith("Error") and "構文" in out


def test_crlf_line_endings_are_preserved(workspace):
    # A one-line edit must not rewrite every line ending (whole-file-rewrite bug).
    p = workspace / "w.txt"
    p.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    out = E._run({"file_path": str(p), "old_string": "beta", "new_string": "BETA"})
    assert out.startswith("OK")
    data = p.read_bytes()
    assert data == b"alpha\r\nBETA\r\ngamma\r\n", f"CRLF が壊れた: {data!r}"


def test_oversize_files_skip_the_gate_fast(workspace):
    big = "x = 1\n" + ("# pad\n" * 400000)   # >2MB
    f = _write(workspace, "big.py", big)
    # introduce a syntax error; oversize → gate skips → edit applies
    out = E._run({"file_path": str(f), "old_string": "x = 1", "new_string": "x = ("})
    assert out.startswith("OK")
