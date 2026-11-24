"""★2026-10-17 ハーネスが自分で動く開発の守り（開発の強化 第 1 段）。推論機も本物の命令も使わない。

なぜハーネス側に置くか: 「確かめてから終えて」と促しても確かめない走行があり（37 本中 5 本）、
道具の名前を前置きに並べても既成の道具は 1 度も使われなかった（0.1.515 の 3 走行）。**促しは効かない。**

ここで固定する振る舞い:
1 編集の後の速い検査（autolint）— プロジェクトの中だけ・誤りがあれば <diagnostics> に出る・切れる
2 終わる前の自動の確かめ（autoverify）— 通れば帳簿が verified・落ちたら差し戻す・1 走行 1 回
3 足した行の見直し（diffreview）— 衝突の印と秘密は差し戻す・デバッグ出力は知らせるだけ
4 前置きの地図（repomap）— プロジェクトのときだけ・2 回組んで 1 文字も変わらない
5 Edit の複数か所（edits）— 1 つでも外れたらファイルを書き換えない
"""
from __future__ import annotations

import pytest

from laps.oc import checktool as ct
from laps.oc import diffreview as dr
from laps.oc import prompt as pr
from laps.oc import tools


# ── 1. 編集の後の速い検査 ───────────────────────────────────────────────

@pytest.fixture()
def fake_bash(monkeypatch):
    out = {"text": "", "ok": True, "kind": "ok"}
    calls: list[dict] = []

    class _R(str):
        ok = True
        kind = "ok"

    def fake(inp: dict):
        calls.append(dict(inp))
        r = _R(out["text"])
        r.ok, r.kind = out["ok"], out["kind"]
        return r
    monkeypatch.setattr("laps.oc.tools.bash_tool", fake)
    monkeypatch.setattr(tools, "_AUTOLINT_RUNS", [0])
    return calls, out


def _project(tmp_path, name="a.py", body="import os\n"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_autolint_reports_the_places(tmp_path, fake_bash, monkeypatch):
    calls, out = fake_bash
    p = _project(tmp_path)
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/ruff" if n == "ruff" else None)
    out["text"], out["ok"] = "a.py:1:8: F401 `os` imported but unused\nexit code: 1", False
    note = tools._autolint_note(p)
    assert "a.py:1:8 error F401" in note and "<diagnostics" in note
    assert calls and calls[0]["workdir"] == str(tmp_path)        # 走る場所はプロジェクトの根


def test_autolint_is_quiet_when_it_passes_and_outside_a_project(tmp_path, fake_bash, monkeypatch):
    calls, out = fake_bash
    p = _project(tmp_path)
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/ruff" if n == "ruff" else None)
    out["text"], out["ok"] = "All checks passed!\nexit code: 0", True
    assert tools._autolint_note(p) == ""
    # ★使い捨ての台本（目印なし）では走らせない
    loose = tmp_path.parent / "loose"
    loose.mkdir(exist_ok=True)
    q = loose / "one_off.py"
    q.write_text("import os\n", encoding="utf-8")
    calls.clear()
    assert tools._autolint_note(str(q)) == "" and calls == []


def test_autolint_can_be_switched_off(tmp_path, fake_bash, monkeypatch):
    calls, out = fake_bash
    p = _project(tmp_path)
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/ruff" if n == "ruff" else None)
    out["text"], out["ok"] = "a.py:1:8: F401 x\nexit code: 1", False
    monkeypatch.setenv("LAPS_AUTOLINT", "0")
    assert tools._autolint_note(p) == "" and calls == []


def test_autolint_stops_after_the_cap(tmp_path, fake_bash, monkeypatch):
    calls, out = fake_bash
    p = _project(tmp_path)
    monkeypatch.setattr(ct.shutil, "which", lambda n, path=None: "/usr/bin/ruff" if n == "ruff" else None)
    out["text"], out["ok"] = "a.py:1:8: F401 x\nexit code: 1", False
    monkeypatch.setattr(tools, "_AUTOLINT_RUNS", [tools._AUTOLINT_MAX])
    assert tools._autolint_note(p) == "" and calls == []


# ── 2. 終わる前の自動の確かめ ────────────────────────────────────────────

class _Agent:
    """loop の _autoverify・_diff_review だけを借りる最小の入れ物（推論機は呼ばない）。"""

    def __init__(self, ledger):
        import threading
        from laps.oc.loop import OcAgent
        self._ledger, self._ledger_lock = ledger, threading.Lock()
        self.counters, self._interjections = {}, []
        self._user_request = self._task_request = ""
        self.notices: list[dict] = []
        self._autoverify = OcAgent._autoverify.__get__(self)
        self._diff_review = OcAgent._diff_review.__get__(self)

    def _emit(self, kind, payload):
        self.notices.append({"kind": kind, **payload})


def _edited(path: str):
    from laps.oc import evidence
    led = evidence.Ledger()
    evidence.observe(led, "Edit", {"file_path": path}, True, "Edit applied successfully.")
    return led










# ── 3. 足した行の見直し ─────────────────────────────────────────────────

def _repo(tmp_path, body: str, name: str = "a.py"):
    import subprocess
    tmp_path.mkdir(parents=True, exist_ok=True)
    r = str(tmp_path)
    for args in (["init", "-q", "."], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=r, capture_output=True)
    (tmp_path / name).write_text("base = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=r, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=r, capture_output=True)
    (tmp_path / name).write_text(body, encoding="utf-8")
    return r, str(tmp_path / name)


@pytest.mark.parametrize("line, word, hard", [
    ("<<<<<<< HEAD", "conflict marker", True),
    ('API_KEY = "sk-ant-api03-REAL1234567890abcdefGHIJKLMNOPqrstuvwx"', "credential or key", True),
    ('console.log("x")', "debug output", False),
    ("it.only('a', () => {})", "only one test", False),
])
def test_diffreview_flags_what_should_not_be_left(tmp_path, line, word, hard):
    root, p = _repo(tmp_path, "base = 1\n" + line + "\n")
    text, is_hard = dr.review([p], root)
    assert word in text and is_hard is hard


def test_diffreview_is_quiet_on_ordinary_code(tmp_path):
    root, p = _repo(tmp_path, "base = 1\n\n\ndef add(a, b):\n    return a + b\n")
    assert dr.review([p], root) == ("", False)


def test_diffreview_outside_git_and_the_switch(tmp_path, monkeypatch):
    p = tmp_path / "a.py"
    p.write_text("<<<<<<< HEAD\n", encoding="utf-8")
    assert dr.review([str(p)], str(tmp_path)) == ("", False)      # git の外＝何も言わない
    root, q = _repo(tmp_path / "r", "base = 1\n<<<<<<< HEAD\n")
    monkeypatch.setenv("LAPS_DIFFREVIEW", "0")
    assert dr.review([q], root) == ("", False)




# ── 4. 前置きの地図 ─────────────────────────────────────────────────────

def test_repomap_block_only_for_a_project_and_is_stable(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_REPOMAP_CACHE", {})
    names = {"Read", "Edit", "Write"}
    assert pr._repomap_block(str(tmp_path), names) == ""          # 目印なし＝地図なし
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    first = pr._repomap_block(str(tmp_path), names)
    assert "# Map of this project" in first and "app.py" in first
    assert pr._repomap_block(str(tmp_path), names) == first       # 2 回目も 1 文字も変わらない
    monkeypatch.setenv("LAPS_REPOMAP", "0")
    assert pr._repomap_block(str(tmp_path), names) == ""


# ── 5. Edit の複数か所 ──────────────────────────────────────────────────

def test_edits_apply_together_or_not_at_all(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    r = tools.edit_tool({"file_path": str(p), "edits": [{"old_string": "a = 1", "new_string": "a = 10"},
                                                        {"old_string": "c = 3", "new_string": "c = 30"}]})
    assert r.ok and p.read_text(encoding="utf-8") == "a = 10\nb = 2\nc = 30\n"
    before = p.read_text(encoding="utf-8")
    r2 = tools.edit_tool({"file_path": str(p), "edits": [{"old_string": "b = 2", "new_string": "b = 20"},
                                                         {"old_string": "NOT THERE", "new_string": "x"}]})
    assert not r2.ok and "edit 2 of 2" in str(r2)
    assert p.read_text(encoding="utf-8") == before                # 1 つ外れたら何も書き換えない


def test_edits_is_offered_in_the_schema():
    """★この仕組みは前からあったが引数に出ていなかった＝モデルが知らなかった（2026-10-17）。"""
    sc = tools.specs()["Edit"].schema
    assert "edits" in sc["properties"] and sc["required"] == ["file_path"]
    assert "edits" in tools.specs()["Edit"].description
