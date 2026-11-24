"""2026-10-31 新しい中核の更新（下見・Umbra の引き直し・文字コードと改行・止める）。"""
from __future__ import annotations

import io
import threading
import time

from laps.engine import loop as L
from laps.engine.server import Server, Spec
from laps.engine.tools import Ctx
from laps.engine.tools import files as F
from laps.engine.tools import misc
from laps.engine.wire import Out
from laps.oc import explore, selfreview
from tests.test_engine_aids_20261030 import run, sent  # noqa: F401


def _ctx(tmp_path, **kw):
    return Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), **kw)


# ── 下見 ───────────────────────────────────
def test_small_folders_are_not_scouted(run, monkeypatch):
    go, ws, _ = run
    monkeypatch.setenv("LAPS_EXPLORE_FIRST", "1")
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    _, a, _, _ = go([{"text": "おわり"}], "a.py を整理して報告してください。どこを直すかも書いてください")
    assert a.counters.get("explore_first:small_folder") == 1 and not a.counters.get("task_calls")


def test_large_folders_are_still_scouted(run, monkeypatch):
    go, ws, _ = run
    monkeypatch.setenv("LAPS_EXPLORE_FIRST", "1")
    for i in range(L.SCOUT_SMALL_FOLDER + 1):
        (ws / f"f{i}.txt").write_text("x\n", encoding="utf-8")
    _, a, _, _ = go([{"text": "まとめ: f0.txt があります"}, {"text": "おわり"}], "フォルダの中身を整理して報告してください。どこを直すかも書いてください")
    assert a.counters.get("explore_first:ran") == 1


def test_files_under_the_harness_folder_do_not_count_as_a_large_folder(tmp_path):
    (tmp_path / ".blackwall" / "history").mkdir(parents=True)
    for i in range(30):
        (tmp_path / ".blackwall" / "history" / f"{i}.md").write_text("x", encoding="utf-8")
    assert L._small_folder(str(tmp_path))


def test_the_scout_is_told_it_cannot_run_anything():
    assert "you cannot run anything" in explore.SCOUT_QUESTION



def test_the_request_reaches_the_tools_and_a_longer_message_replaces_it(run):
    go, ws, _ = run
    long_request = "このフォルダの設定を整理して、結果を報告してください"
    _, a, _, _ = go([{"text": "おわり"}], long_request)
    assert a.ctx.request == long_request


# ── 文字コードと改行 ───────────────────────
def test_write_keeps_the_encoding_of_an_existing_file(tmp_path):
    p = tmp_path / "a.bat"
    p.write_bytes("echo 日本語\r\n".encode("cp932"))
    ctx = _ctx(tmp_path, read_files={str(p)})
    assert F.write({"file_path": "a.bat", "content": "echo 世界\n"}, ctx).ok
    assert p.read_bytes() == "echo 世界\n".encode("cp932")


def test_write_keeps_a_bom(tmp_path):
    p = tmp_path / "a.ps1"
    p.write_bytes(b"\xef\xbb\xbfWrite-Host x\n")
    assert F.write({"file_path": "a.ps1", "content": "Write-Host y\n"}, _ctx(tmp_path, read_files={str(p)})).ok
    assert p.read_bytes() == b"\xef\xbb\xbfWrite-Host y\n"


def test_write_says_so_when_the_encoding_cannot_hold_the_text(tmp_path):
    p = tmp_path / "a.txt"
    p.write_bytes("日本語\n".encode("cp932"))
    r = F.write({"file_path": "a.txt", "content": "✓\n"}, _ctx(tmp_path, read_files={str(p)}))
    assert r.ok and "now UTF-8" in r.output and p.read_bytes() == "✓\n".encode("utf-8")


def test_edit_leaves_mixed_line_endings_alone(tmp_path):
    p = tmp_path / "m.txt"
    p.write_bytes(b"one\r\ntwo\nthree\n")
    assert F.edit({"file_path": "m.txt", "old_string": "three", "new_string": "3"}, _ctx(tmp_path)).ok
    assert p.read_bytes() == b"one\r\ntwo\n3\n"


def test_edit_still_keeps_crlf_files_crlf(tmp_path):
    p = tmp_path / "c.txt"
    p.write_bytes(b"one\r\ntwo\r\n")
    assert F.edit({"file_path": "c.txt", "old_string": "one\ntwo", "new_string": "1\n2"}, _ctx(tmp_path)).ok
    assert p.read_bytes() == b"1\r\n2\r\n"


# ── 止める・見直し ─────────────────────────
def test_stop_does_not_wait_for_the_app_to_answer(tmp_path):
    srv = Server(Out(io.StringIO()))
    ctx = _ctx(tmp_path)
    threading.Timer(0.3, ctx.cancel.set).start()
    t0 = time.monotonic()
    r = srv._app_call(Spec(id="r", prompt="x"), ctx, "browser", {}, 30.0)
    assert not r["ok"] and "stopped" in r["error"] and time.monotonic() - t0 < 3


def test_review_says_an_unjustified_result_is_a_failure():
    assert "cannot justify from the request is ✗" in selfreview.before_finish("r", [], "Japanese")
