"""Tests for Phase Ω-K-ζ: Claude Code parity additions."""
from __future__ import annotations
import json
import os
from pathlib import Path

from laps.tools import read, task_mgmt, websearch, notebook_edit, ask_user


# --- multimodal Read ---

def test_read_image_returns_data_uri(tmp_path):
    p = tmp_path / "x.png"
    # minimal 1x1 PNG
    p.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de00000010494441545881636028020000000000050001a5f645a40000000049454e44ae426082"
    ))
    out = read.SPEC.handler({"file_path": str(p)})
    assert "[image:x.png]" in out
    assert "data:image/png;base64," in out


def test_read_pdf_missing_pypdf_falls_back(tmp_path, monkeypatch):
    # Create a fake pdf file
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    out = read.SPEC.handler({"file_path": str(p)})
    # Either pypdf works (extracted text) or message about pypdf
    assert "[pdf:" in out or "pypdf" in out


# --- TaskList family ---

def _session_env(tmp: Path) -> None:
    sid = f"test_zeta_{os.getpid()}"
    os.environ["LAPS_HOME"] = str(tmp)
    os.environ["LAPS_SESSION_ID"] = sid
    (tmp / "sessions" / sid).mkdir(parents=True, exist_ok=True)


def test_taskcreate_then_list_get_update_stop(tmp_path):
    _session_env(tmp_path)
    specs = {s.name: s for s in task_mgmt.SPECS}
    r = specs["TaskCreate"].handler({"subject": "first", "description": "do the thing"})
    assert "OK" in r and "#1" in r
    r2 = specs["TaskCreate"].handler({"subject": "second"})
    assert "#2" in r2
    listed = specs["TaskList"].handler({})
    assert "first" in listed and "second" in listed
    got = specs["TaskGet"].handler({"task_id": "1"})
    assert json.loads(got)["subject"] == "first"
    upd = specs["TaskUpdate"].handler({"task_id": 1, "status": "in_progress"})
    assert "in_progress" in upd
    stop = specs["TaskStop"].handler({"task_id": 2})
    assert "OK" in stop
    listed2 = specs["TaskList"].handler({})
    assert "second" not in listed2  # deleted hides


# --- WebSearch (light smoke; we don't call live network — patch httpx if needed) ---

def test_websearch_parses_html(monkeypatch):
    fake_html = """
    <html><body>
      <div class="result">
        <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com">Example Title</a>
        <a class="result__snippet">Example snippet here</a>
      </div>
    </body></html>
    """
    class FakeResp:
        status_code = 200
        text = fake_html
    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **kw): return FakeResp()
    monkeypatch.setattr(websearch.httpx, "Client", FakeClient)
    out = websearch.SPEC.handler({"query": "example", "n": 5})
    assert "Example Title" in out
    assert "example.com" in out


# --- NotebookEdit ---

def test_notebook_insert_replace_delete(tmp_path):
    nb_path = tmp_path / "n.ipynb"
    nb = {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    nb_path.write_text(json.dumps(nb))
    spec = notebook_edit.SPEC
    r1 = spec.handler({
        "notebook_path": str(nb_path), "mode": "insert",
        "new_source": "print('hi')", "cell_type": "code",
    })
    assert "inserted" in r1
    r2 = spec.handler({
        "notebook_path": str(nb_path), "mode": "replace",
        "cell_index": 0, "new_source": "print('bye')",
    })
    assert "replaced" in r2
    body = json.loads(nb_path.read_text())
    assert "print('bye')" in "".join(body["cells"][0]["source"])
    r3 = spec.handler({"notebook_path": str(nb_path), "mode": "delete", "cell_index": 0})
    assert "deleted" in r3


# --- AskUserQuestion in headless mode ---

def test_ask_user_headless_returns_sentinel(monkeypatch):
    monkeypatch.setenv("LAPS_HEADLESS", "1")
    monkeypatch.delenv("LAPS_YES", raising=False)
    out = ask_user.SPEC.handler({"question": "ok?"})
    assert "no-tty" in out or "auto-yes" in out


def test_ask_user_auto_yes_picks_first(monkeypatch):
    monkeypatch.setenv("LAPS_HEADLESS", "1")
    monkeypatch.setenv("LAPS_YES", "1")
    out = ask_user.SPEC.handler({
        "question": "pick one",
        "options": [{"label": "Apple"}, {"label": "Banana"}],
    })
    assert "Apple" in out


# --- Worktree ---


# --- Hooks ---


# --- Session search ---


# --- Settings ---

def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    from laps.settings import Settings
    s = Settings.load()
    assert s.auto_yes is False
    s.auto_yes = True
    s.allow_patterns = ["^echo "]
    s.save()
    s2 = Settings.load()
    assert s2.auto_yes is True
    assert s2.allow_patterns == ["^echo "]
