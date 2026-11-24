"""2026-11-17 オーナー「ツールとハーネス強化」案 2〜5 の試験。
2: Desktop・Browser のまとめ打ち（action=batch）／3: Editor（アプリのエディターで開く）／4: Code（コードの地図）／
5: 同じファイルの Update File を 2 節に分けたパッチをまとめて当てる。"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from laps.engine.tools import Ctx, Result, ext, fail
from laps.engine.tools.patch import PatchError, apply_chunks, parse


def _ctx(tmp_path: Path, ws: Path | None = None) -> Ctx:
    root = tmp_path / "sessions" / "s1"
    root.mkdir(parents=True)
    return Ctx(workspace=str(ws or tmp_path), root=root, cancel=threading.Event(), preempt=threading.Event())


# ── 5 ──────────────────────────────────────────────
def test_patch_two_sections_same_file_are_merged_even_out_of_order():
    p = ("*** Begin Patch\n*** Update File: a.py\n@@\n-c = 3\n+c = 30\n"
         "*** Update File: a.py\n@@\n-a = 1\n+a = 10\n*** End Patch")
    hunks = parse(p)
    assert len(hunks) == 1 and len(hunks[0].chunks) == 2
    assert apply_chunks("a = 1\nb = 2\nc = 3\n", hunks[0].chunks, "a.py") == "a = 10\nb = 2\nc = 30\n"


def test_patch_add_and_update_of_same_file_still_refused():
    p = "*** Begin Patch\n*** Add File: a.py\n+x\n*** Update File: a.py\n@@\n-x\n+y\n*** End Patch"
    with pytest.raises(PatchError, match="Two sections"):
        parse(p)


# ── 2 ──────────────────────────────────────────────
@pytest.fixture
def fake_desktop(monkeypatch):
    calls: list[str] = []

    def fake(name, st, ctx, head="", effect=True):
        calls.append(st["action"])
        if st["action"] == "boom":
            return fail("no such window", head=name)
        return Result(f"did {st['action']}", shot=f"/x/{st['action']}.png", images=[f"/x/{st['action']}.png"])
    monkeypatch.setattr(ext, "call_legacy", fake)
    monkeypatch.setattr(ext, "_load", lambda ctx: {"Desktop": object(), "Browser": object()})
    return calls


def test_batch_runs_steps_in_order_and_returns_last_image(tmp_path, fake_desktop):
    ctx = _ctx(tmp_path)
    r = ext.tools({"name": "Desktop", "input": {"action": "batch", "steps": [
        {"action": "click", "x": 1}, {"action": "type", "text": "a"}, {"action": "look"}]}}, ctx)
    assert r.ok and fake_desktop == ["click", "type", "look"]
    assert r.images == ["/x/look.png"] and "[3] look: ok" in r.output


def test_batch_stops_at_first_failure(tmp_path, fake_desktop):
    ctx = _ctx(tmp_path)
    r = ext.tools({"name": "Browser", "input": {"action": "batch", "steps": [
        {"action": "click"}, {"action": "boom"}, {"action": "look"}]}}, ctx)
    assert not r.ok and fake_desktop == ["click", "boom"]
    assert "Stopped at step 2 of 3" in r.output


def test_batch_rejects_bad_input(tmp_path, fake_desktop):
    ctx = _ctx(tmp_path)
    assert not ext.tools({"name": "Desktop", "input": {"action": "batch"}}, ctx).ok
    many = [{"action": "click"}] * (ext.BATCH_MAX + 1)
    assert not ext.tools({"name": "Desktop", "input": {"action": "batch", "steps": many}}, ctx).ok
    r = ext.tools({"name": "Desktop", "input": {"action": "batch", "steps": [{"action": "batch", "steps": []}]}}, ctx)
    assert not r.ok and fake_desktop == []


def test_batch_how_to_mentions_batch(tmp_path, monkeypatch):
    spec = type("S", (), {"description": "Desktop.", "schema": {}})()
    monkeypatch.setattr(ext, "_load", lambda ctx: {"Desktop": spec})
    r = ext.tools({"name": "Desktop"}, _ctx(tmp_path))
    assert '"action": "batch"' in r.output


# ── 4 ──────────────────────────────────────────────
@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    (w / "src").mkdir(parents=True)
    (w / "src" / "app.py").write_text("import os\n\nLIMIT = 3\n\nclass Store:\n    def load(self):\n        return 1\n\n"
                                      "def run():\n    return Store().load()\n", encoding="utf-8")
    (w / "src" / "ui.ts").write_text("export function render(x: number) {\n  return x;\n}\nconst y = render(2);\n",
                                     encoding="utf-8")
    (w / "node_modules").mkdir()
    (w / "node_modules" / "lib.js").write_text("function render() {}\n", encoding="utf-8")
    return w


def test_code_outline_python(tmp_path, ws):
    r = ext.tools({"name": "Code", "input": {"action": "outline", "path": "src/app.py"}}, _ctx(tmp_path, ws))
    assert r.ok
    for want in ("LIMIT", "class     Store", "Store.load", "def       run"):
        assert want in r.output


def test_code_definition_and_references_skip_vendor(tmp_path, ws):
    ctx = _ctx(tmp_path, ws)
    d = ext.tools({"name": "Code", "input": {"action": "definition", "name": "render"}}, ctx)
    assert d.ok and "src/ui.ts:1" in d.output and "node_modules" not in d.output
    r = ext.tools({"name": "Code", "input": {"action": "references", "name": "render"}}, ctx)
    assert r.ok and "src/ui.ts:1 (definition)" in r.output and "src/ui.ts:4" in r.output
    m = ext.tools({"name": "Code", "input": {"action": "definition", "name": "nothing_here"}}, ctx)
    assert m.ok and m.kind == "empty"


def test_code_check_and_bad_input(tmp_path, ws):
    ctx = _ctx(tmp_path, ws)
    (ws / "bad.py").write_text("def f(:\n", encoding="utf-8")
    r = ext.tools({"name": "Code", "input": {"action": "check", "path": "bad.py"}}, ctx)
    assert r.ok and "bad.py" in r.output and "No problems" not in r.output
    assert not ext.tools({"name": "Code", "input": {"action": "fly"}}, ctx).ok
    assert not ext.tools({"name": "Code", "input": {"action": "definition", "name": "a b"}}, ctx).ok


# ── 3 ──────────────────────────────────────────────
def test_editor_outside_app_says_so(tmp_path, ws):
    from laps.tools import appcall
    appcall.set_caller(None)
    r = ext.tools({"name": "Editor", "input": {"path": "src/app.py", "line": 5}}, _ctx(tmp_path, ws))
    assert not r.ok and "only in the L.A.P.S desktop app" in r.output


def test_editor_asks_the_app(tmp_path, ws, monkeypatch):
    from laps.tools import appcall
    seen = []
    ctx = _ctx(tmp_path, ws)
    ctx.app_call = lambda op, args, timeout: (seen.append((op, args)), {"ok": True})[1]
    ctx.app_caps = ("browser", "editor")
    r = ext.tools({"name": "Editor", "input": {"path": "src/app.py", "line": 6, "diff": True}}, ctx)
    assert r.ok, r.output
    op, args = seen[0]
    assert op == "editor" and args["line"] == 6 and args["diff"] is True and args["session"] == "s1"
    assert args["path"].endswith("app.py")
    appcall.set_caller(None)
    assert not ext.tools({"name": "Editor", "input": {"path": "nope.py"}}, ctx).ok
