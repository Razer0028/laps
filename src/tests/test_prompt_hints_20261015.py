"""★2026-10-15 前置きのぶつかる指図・思い出させる一言の進み・形式を見分ける瞬間の棚の案内。

・merged の前置きと道具の説明が逆のことを言わない（多数のファイルの置換・帳面の .md・終わりの報告）。opencode の形は元のまま。
・思い出させる一言は、前の一言からの進み（済んだ ToDo・帳面の件数）を書く＝毎回同じ定型文にしない。
・棚の案内は、台本の後だけでなく Read が二進のファイルを開いた・xxd などで覗いた所でも手掛かりを拾う。
"""
from __future__ import annotations

from laps.oc import findings, promptbase


# ── 前置き（merged）のぶつかる指図 ─────────────────────────────────────

def _bash(monkeypatch, style: str) -> str:
    monkeypatch.setenv("LAPS_PROMPT_BASE", style)
    monkeypatch.setattr(promptbase.sys, "platform", "linux")
    return promptbase.bash_text("", default_timeout_ms=120000, max_lines=2000, max_bytes=50000)


def test_merged_bash_allows_bulk_sed_that_the_base_recommends(monkeypatch):
    base = promptbase.merged_base("linux")
    assert "sed, sd or perl -pi" in base                       # 本文: 多数のファイルの置換は sed・sd・perl -pi
    t = _bash(monkeypatch, "merged")
    assert "Edit files: Use Edit for one file (NOT sed/awk); sed, sd or perl -pi for the same change across many files" in t
    assert "always prefer using the dedicated tools for these commands" not in t
    assert "For a single file or a single search, use the dedicated tools" in t


def test_opencode_bash_is_unchanged(monkeypatch):
    t = _bash(monkeypatch, "opencode")
    assert "    - Edit files: Use Edit (NOT sed/awk)\n" in t
    assert "Avoid using Bash with the `find`, `grep`, `cat`, `head`, `tail`, `sed`, `awk`, or `echo` commands" in t


def test_merged_write_allows_the_findings_ledger(monkeypatch):
    from laps.oc import tools as oc_tools
    monkeypatch.setenv("LAPS_PROMPT_BASE", "merged")
    w = promptbase.tool_text("Write", oc_tools.WRITE_DESCRIPTION)
    assert "NEVER proactively create documentation files" in w
    assert ".laps/ (FINDINGS.md) are not documentation" in w
    assert ".laps/FINDINGS.md" in promptbase.merged_base("linux")
    monkeypatch.setenv("LAPS_PROMPT_BASE", "opencode")
    assert "FINDINGS" not in promptbase.tool_text("Write", oc_tools.WRITE_DESCRIPTION)


def test_merged_end_of_task_report_is_one_rule():
    base = promptbase.merged_base("linux")
    assert "report briefly what you did" in base
    assert "just stop, rather than providing an explanation" not in base
    assert "give the short report described above" in base
    assert "just stop, rather than providing an explanation" in promptbase.OPENCODE_BASE


# ── 思い出させる一言の進み ────────────────────────────────────────────

def _ledger(tmp_path, facts: int, dead: int = 0) -> None:
    p = findings.ledger_path(str(tmp_path))
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(f"- 2026-10-15 事実 {i} offset 0x{i:04x}" for i in range(facts))
    dr = "\n".join(f"- 2026-10-15 行き止まり {i}" for i in range(dead))
    p.write_text(findings.HEAD + f"\n{findings.H_FACTS}\n{rows}\n\n{findings.H_DEAD}\n{dr}\n", encoding="utf-8")


def test_recite_reports_progress_since_the_last_reminder(tmp_path):
    _ledger(tmp_path, 1)
    todos = [{"content": "a", "status": "completed"}, {"content": "b", "status": "pending"}]
    prev = findings.recite_snapshot(todos, str(tmp_path))
    assert prev == {"done": 1, "facts": 1, "dead": 0}
    _ledger(tmp_path, 3)
    todos[1]["status"] = "completed"
    note = findings.recite_note("セーブデータの形式を調べて", todos, str(tmp_path), prev=prev)
    assert "1 todo items completed, 2 facts added" in note
    assert "Check that what you are doing now" not in note      # 進んでいる走行に同じ促しを繰り返さない


def test_recite_says_when_nothing_moved(tmp_path):
    _ledger(tmp_path, 2)
    todos = [{"content": "b", "status": "in_progress"}]
    prev = findings.recite_snapshot(todos, str(tmp_path))
    note = findings.recite_note("セーブデータの形式を調べて", todos, str(tmp_path), prev=prev)
    assert "no todo item was completed and nothing was added" in note
    assert "Check that what you are doing now still serves that request." in note


def test_recite_without_prev_keeps_the_old_shape(tmp_path):
    note = findings.recite_note("依頼", [], str(tmp_path))
    assert note.startswith("[Harness] Reminder of the request you are working on: 「依頼」")
    assert "Since the last reminder" not in note and "no todo item" not in note
    assert note.endswith("Check that what you are doing now still serves that request.")


# ── 形式を見分ける瞬間の棚の案内 ──────────────────────────────────────

