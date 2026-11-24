"""★2026-11-20 壊す形の Bash の前に状態を控える（engine/tools/bashsafe.py）。推論機は使わない。"""
from __future__ import annotations

import os
import subprocess

from laps.engine import undo
from laps.engine.tools import bashsafe


def test_targets_from_common_commands(tmp_path):
    d = tmp_path / "w"
    (d / "sub").mkdir(parents=True)
    for n in ("a.txt", "b.conf", "c.txt", "sub/x.txt"):
        (d / n).write_text("x")
    t = lambda c: sorted(os.path.relpath(p, d) for p in bashsafe.file_targets(c, str(d)))
    assert t("rm -rf sub && echo hi") == ["sub"]
    assert t("sed -i 's/a/b/' b.conf") == ["b.conf"]
    assert t("echo y > a.txt") == ["a.txt"]
    assert t("echo y >> a.txt") == []                 # 追記は控えない（壊さない）
    assert t("mv a.txt c.txt") == ["a.txt", "c.txt"]
    assert t("cp a.txt c.txt") == ["c.txt"]
    assert t("sudo rm c.txt missing.txt") == ["c.txt"]  # 無い物は数えない
    assert t("rm *.txt") == []                        # ワイルドカードは展開しない
    assert t("ls -la; cat a.txt") == []


def test_rm_r_then_undo_restores_the_folder(tmp_path):
    d = tmp_path / "w"
    (d / "sub" / "deep").mkdir(parents=True)
    (d / "sub" / "deep" / "f.txt").write_text("keep me")
    (d / "sub" / "g.txt").write_text("me too")
    rc = undo.begin(tmp_path / "conv", "r1")
    snap = bashsafe.Snapshot("rm -rf sub", str(d), "bash", rc)
    snap.before()
    subprocess.run(["rm", "-rf", str(d / "sub")], check=True)
    notes = snap.after()
    assert any("2 file(s)" in n for n in notes)
    rc.finish()
    res = rc.undo()
    assert (d / "sub" / "deep" / "f.txt").read_text() == "keep me"
    assert (d / "sub" / "g.txt").read_text() == "me too"
    assert len(res["restored"]) == 2


def test_services_parsed_and_revert_hint():
    assert bashsafe.services("sudo systemctl restart nginx && systemctl disable foo bar", "bash") == [
        ("systemd", "nginx"), ("systemd", "foo"), ("systemd", "bar")]
    assert bashsafe.services("Stop-Service -Name Spooler", "powershell") == [("windows", "Spooler")]
    assert bashsafe._revert_hint("systemd", "foo", "active/enabled", "inactive/disabled") == \
        "systemctl enable foo && systemctl start foo"
    assert bashsafe._revert_hint("windows", "Spooler", "Running/Automatic", "Stopped/Automatic") == "Start-Service Spooler"


def test_switch_off(monkeypatch):
    monkeypatch.setenv("LAPS_BASH_SNAPSHOT", "0")
    assert not bashsafe.enabled()
