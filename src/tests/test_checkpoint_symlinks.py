"""木ごとの控えが**記号リンクの向き先**を控え、戻すこと（2026-09-20・敵対レビュー F7-2）。

★旧: リンクは丸ごと飛ばしていた。シェルで張り替えても普通のファイルに置き換えても、控えにも報告にも出ず、
  `restore_watermark` は「戻しました・complete=True」と言っていた。インフラの変更はリンクの張り替えが本体。
"""
from __future__ import annotations

import os

from laps.checkpoint import Checkpoint


def _mk(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("original\n")
    (ws / "conf.d").mkdir()
    os.symlink("a.txt", ws / "ln")
    os.symlink("../a.txt", ws / "conf.d" / "enabled")
    cp = Checkpoint(tmp_path / "store", task_id="t")
    cp.snapshot_watermark(ws)
    return ws, cp


def test_張り替えたリンクを戻す(tmp_path):
    ws, cp = _mk(tmp_path)
    os.remove(ws / "conf.d" / "enabled")
    os.symlink("../other.txt", ws / "conf.d" / "enabled")
    rep = cp.restore_watermark()
    assert os.readlink(ws / "conf.d" / "enabled") == "../a.txt"
    assert any("記号リンク" in r for r in rep.restored)


def test_普通のファイルに化けたリンクを戻す(tmp_path):
    ws, cp = _mk(tmp_path)
    os.remove(ws / "ln")
    (ws / "ln").write_text("now a file\n")
    cp.restore_watermark()
    assert os.path.islink(ws / "ln") and os.readlink(ws / "ln") == "a.txt"


def test_消えたリンクを戻す(tmp_path):
    ws, cp = _mk(tmp_path)
    os.remove(ws / "ln")
    cp.restore_watermark()
    assert os.path.islink(ws / "ln")


def test_変わっていないリンクは触らない(tmp_path):
    ws, cp = _mk(tmp_path)
    before = os.lstat(ws / "ln").st_ino
    rep = cp.restore_watermark()
    assert os.lstat(ws / "ln").st_ino == before, "同じ向き先なのに作り直している"
    assert not any("記号リンク" in r for r in rep.restored)


def test_ディレクトリに化けた場所は消さずに戻せなかったと言う(tmp_path):
    ws, cp = _mk(tmp_path)
    os.remove(ws / "ln")
    (ws / "ln").mkdir()
    (ws / "ln" / "keep.txt").write_text("大事な物\n")
    rep = cp.restore_watermark()
    assert (ws / "ln" / "keep.txt").exists(), "中身のあるディレクトリを消した"
    assert rep.failed and not rep.ok


def test_リンクの先は読まない_外へ出ない(tmp_path):
    ws, cp = _mk(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    os.symlink(str(outside), ws / "out")
    cp.snapshot_watermark(ws)
    assert not any("outside.txt" in str(e.path) for e in cp._watermark.values()), "リンクの先を控えに入れている"
    assert cp._wm_links[str(ws / "out")] == str(outside)
