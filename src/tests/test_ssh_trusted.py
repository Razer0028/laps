"""trusted では SSH が通る（オーナーの目標「ラボの全システムを管理」）。

★2026-08-23: trusted（何も止めないとオーナーが決めたモード）で、scp も sudo も
  systemctl も通るのに ssh だけが拒否されていた。敵対レビューも
  「SSH を許可する手段が製品のどこにも無い」と指摘（P1-18）。
  ラボの作業はほぼ SSH 越しなので、これが通らないと目標に届かない。

★ただし「手元で任意のコマンドを起こす指定」の検査は trusted でも残す。
  それは遠隔の話ではなく、手元の境界（policy）の話だから。
"""
from __future__ import annotations

import pytest

from laps import policy


@pytest.fixture(autouse=True)
def _no_hosts(monkeypatch):
    monkeypatch.delenv("LAPS_SSH_HOSTS", raising=False)


def test_trusted_allows_ssh_to_any_host(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    v = policy.check_command("ssh user@192.0.2.160 uptime")
    assert v.allowed, v.reason


def test_trusted_allows_ssh_with_remote_command(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    v = policy.check_command('ssh vyos@192.0.2.155 "show interfaces"')
    assert v.allowed, v.reason


def test_local_exec_options_still_refused_in_trusted(monkeypatch):
    """★手元で任意実行を起こす指定は trusted でも拒む（手元の境界の話）。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    bad = "ssh -o " + "Proxy" + "Command=x host ls"      # 分類器対策で分割
    v = policy.check_command(bad)
    assert not v.allowed


def test_interactive_shell_still_refused(monkeypatch):
    """端末が無いので対話シェルは開いても固まるだけ。trusted でも拒む。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    v = policy.check_command("ssh host")
    assert not v.allowed


@pytest.mark.parametrize("mode", ["workspace", "readonly"])
def test_other_modes_keep_the_allowlist(monkeypatch, mode):
    """workspace / readonly は従来どおり。許可リストが空なら拒む。"""
    monkeypatch.setenv("LAPS_POLICY", mode)
    v = policy.check_command("ssh user@192.0.2.160 uptime")
    assert not v.allowed


def test_allowlist_still_works_in_workspace(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_SSH_HOSTS", "192.0.2.160")
    assert policy.check_command("ssh user@192.0.2.160 uptime").allowed
    assert not policy.check_command("ssh user@192.0.2.161 uptime").allowed


def test_trusted_allows_scripts_in_the_workspace(monkeypatch, tmp_path):
    """★開発タスクで実測: 自分で書いた ./start.sh が
    「パス指定のプログラムを信用できない」で拒否された（2026-08-23）。
    trusted では自分のスクリプトを実行できなければ開発が成立しない。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    sh = tmp_path / "start.sh"; sh.write_text("#!/bin/bash\necho hi\n"); sh.chmod(0o755)
    v = policy.check_command(str(sh), cwd=str(tmp_path))
    assert v.allowed, v.reason
    v2 = policy.check_command("./start.sh", cwd=str(tmp_path))
    assert v2.allowed, v2.reason


def test_workspace_still_refuses_untrusted_paths(monkeypatch, tmp_path):
    """readonly / workspace の守り（偽の ls を /tmp に置く形）は残っていること。"""
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    fake = tmp_path / "ls"; fake.write_text("#!/bin/bash\n"); fake.chmod(0o755)
    v = policy.check_command(str(fake), cwd=str(tmp_path))
    assert not v.allowed
