"""ssh の Permission denied に sudo を勧めない（2026-09-03 dogfood ⑩: 旧文の `sudo ssh …` をモデルが試して同じ拒否を 2 回踏んだ）。
本物の Bash 道具に、偽の ssh（拒否して 255 で終わる）を PATH で見せて確かめる。"""
from __future__ import annotations

import os, stat
from laps.tools import bash as bash_tool


def _run(monkeypatch, tmp_path, cmd):
    shim = tmp_path / "bin"; shim.mkdir()
    fake = shim / "ssh"
    fake.write_text('#!/bin/sh\necho "Permission denied, please try again." >&2\necho "root@192.0.2.160: Permission denied (publickey,password)." >&2\nexit 255\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ.get('PATH','')}")
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    return str(bash_tool._run({"command": cmd, "cwd": str(tmp_path)}))


def test_ssh_denied_gets_key_advice_not_sudo(monkeypatch, tmp_path):
    out = _run(monkeypatch, tmp_path, "ssh root@192.0.2.160 'journalctl -p 3 | wc -l'")
    assert "ssh の認証" in out and "sudo では直りません" in out
    assert "sudo ssh" not in out


def test_plain_permission_denied_still_suggests_sudo(monkeypatch, tmp_path):
    p = tmp_path / "locked.txt"; p.write_text("x"); p.chmod(0)
    out = str(bash_tool._run({"command": f"cat {p}", "cwd": str(tmp_path)})) if os.geteuid() != 0 else "root では再現不能"
    assert "sudo" in out or "root では再現不能" in out
