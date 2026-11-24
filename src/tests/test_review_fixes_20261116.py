"""2026-11-16 システム全体のレビューで見つけた物の直し（エンジン側）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from laps.engine import skillguard, undo
from laps.engine.loop import _max_call_id
from laps.engine.server import parse_run


@pytest.mark.parametrize("bad", ["..", "../..", "/home/x", "a/b", "..\\x", "", "x" * 200])
def test_run_id_that_is_not_a_plain_name_is_replaced(bad):
    spec = parse_run({"id": bad, "prompt": "hi"}, 7)
    assert spec.id == "r7"


def test_plain_run_id_is_kept():
    assert parse_run({"id": "r-1731690000.5:ab", "prompt": "hi"}, 7).id == "r-1731690000.5:ab"


def test_undo_store_refuses_a_path_like_id(tmp_path):
    with pytest.raises(ValueError):
        undo.RunChanges(tmp_path, "..")
    assert undo.begin(tmp_path, "..") is None              # 控えを取らない＝戻せないだけ（会話のフォルダは消さない）
    assert tmp_path.exists()


def test_undo_that_restores_nothing_stays_undoable(tmp_path):
    ws, conv = tmp_path / "ws", tmp_path / "conv"
    ws.mkdir(); conv.mkdir()
    f = ws / "a.txt"
    f.write_text("v0", encoding="utf-8")
    rc = undo.RunChanges(conv, "r1")
    rc.record(str(f)); f.write_text("v1", encoding="utf-8"); rc.finish()
    f.write_text("hand edit", encoding="utf-8")            # 依頼の後に手で変えた
    res = undo.last_undoable(conv).undo()
    assert res["skipped"] and not res["restored"]
    assert undo.last_undoable(conv) is not None             # まだ戻せる（手の変更を戻せば、次の /undo で同じ依頼を戻す）
    f.write_text("v1", encoding="utf-8")
    res2 = undo.last_undoable(conv).undo()
    assert res2["restored"] and f.read_text(encoding="utf-8") == "v0"


def test_rewind_keeps_skips_that_a_later_request_restored(tmp_path):
    ws, conv = tmp_path / "ws", tmp_path / "conv"
    ws.mkdir(); conv.mkdir()
    f = ws / "a.txt"
    f.write_text("v0", encoding="utf-8")
    a = undo.RunChanges(conv, "rA"); a.started = 1000.0
    a.record(str(f)); f.write_text("vA", encoding="utf-8"); a.finish()
    f.write_text("hand", encoding="utf-8")                  # A と B の間に手で変えた
    b = undo.RunChanges(conv, "rB"); b.started = 2000.0
    b.record(str(f)); f.write_text("vB", encoding="utf-8"); b.finish()
    res = undo.merge([r.undo() for r in undo.undoable_since(conv, 999.0)])
    assert str(f) in res["restored"]
    assert any(p == str(f) for p, _ in res["skipped"])      # A の分は戻らなかったと言う（前は隠していた）


def test_call_id_continues_from_the_highest_used_number():
    raw = [{"role": "assistant", "tool_calls": [{"id": "c7"}, {"id": "c8"}]},
           {"role": "tool", "tool_call_id": "c7"}]           # c8 の結果が欠けている（途中で落ちた）
    assert _max_call_id(raw) == 8


def test_skill_scan_does_not_say_ok_for_files_it_did_not_read(tmp_path):
    (tmp_path / "SKILL.md").write_text("# s\nok\n", encoding="utf-8")
    for i in range(3):
        (tmp_path / f"f{i}.txt").write_text("fine\n", encoding="utf-8")
    v = skillguard.scan_skill(tmp_path, max_files=2)
    assert v.level == "caution" and any(f.kind == "unchecked" for f in v.findings)


def test_none_before_searching_repeats_the_gate():
    from laps.engine.tools import misc

    class C:
        skill_gate = "search"
        workspace = "."
    r = misc.skill({"name": "none"}, C())
    assert not r.ok and "search first" in str(r.output)
    C.skill_gate = "pick"
    assert misc.skill({"name": "none"}, C()).ok
