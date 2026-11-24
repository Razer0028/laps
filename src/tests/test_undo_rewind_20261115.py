"""発言ごとの「ここまで戻す」（2026-11-15・アプリの Rewind が `/undo since=<ミリ秒>` を送る）。

続けて同じファイルを変えた 2 件の依頼を、新しい順に戻すと元の中身に揃うこと・途中の時刻なら後の 1 件だけ戻ること。
"""
from __future__ import annotations

from pathlib import Path

from laps.engine import undo


def _run(root: Path, rid: str, started: float, writes: dict[Path, str | None]) -> undo.RunChanges:
    rc = undo.RunChanges(root, rid)
    rc.started = started
    for path, text in writes.items():
        rc.record(str(path))
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8")
    rc.finish()
    return rc


def test_parse_command():
    assert undo.parse_command("/undo") == {"since": None}
    assert undo.parse_command(" /UNDO ") == {"since": None}
    assert undo.parse_command("/undo since=1763190000123") == {"since": 1763190000.123}
    assert undo.parse_command("/undo since=") is None
    assert undo.parse_command("/undo since=12") is None
    assert undo.parse_command("undo it please") is None
    assert undo.is_command("/undo since=1763190000123")


def test_rewind_two_runs_newest_first(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    conv = tmp_path / "conv"; conv.mkdir()
    a, b = ws / "a.txt", ws / "b.txt"
    a.write_text("v0", encoding="utf-8")
    _run(conv, "r1", 1000.0, {a: "v1"})
    _run(conv, "r2", 2000.0, {a: "v2", b: "new"})

    runs = undo.undoable_since(conv, 999.0)
    assert [r.rid for r in runs] == ["r2", "r1"]            # 新しい順
    res = undo.merge([r.undo() for r in runs])
    assert a.read_text(encoding="utf-8") == "v0"           # 2 件とも戻って元の中身
    assert not b.exists()                                   # r2 が作ったファイルは消える
    assert str(b) in res["deleted"] and str(a) in res["restored"]
    assert res["skipped"] == []
    assert undo.undoable_since(conv, 0.0) == []             # 戻した物は 2 回は戻さない


def test_rewind_from_the_middle_only_later_runs(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    conv = tmp_path / "conv"; conv.mkdir()
    a = ws / "a.txt"
    a.write_text("v0", encoding="utf-8")
    _run(conv, "r1", 1000.0, {a: "v1"})
    _run(conv, "r2", 2000.0, {a: "v2"})
    runs = undo.undoable_since(conv, 1500.0)
    assert [r.rid for r in runs] == ["r2"]
    undo.merge([r.undo() for r in runs])
    assert a.read_text(encoding="utf-8") == "v1"            # r1 の後の中身に戻る


def test_model_note_counts_runs():
    res = {"restored": ["/w/a.txt"], "deleted": [], "skipped": [], "bash": []}
    assert "the previous request" in undo.model_note(res)
    assert "the last 3 requests" in undo.model_note(res, 3)
