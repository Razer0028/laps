"""Windows で前面 Bash が戻らない事故の回帰（2026-09-06・owner 実機 0.1.195）。

- cmd.exe を stdin 継承・プロセスグループ無しで起動していた → 入力待ちで永久停止・taskkill /T が孫を取り逃す
- `bash`/`wsl` は WSL 起動スタブで対話プロンプトのまま戻らない → 実行前に断る
"""
import os, subprocess, sys
import pytest
from laps.tools import bash as B


def test_win32の前面spawnはstdinを継承せず新しいプロセスグループで起動する(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    args, kw = B._spawn_args("dir /b")
    # ★2026-09-21: リストではなく文字列で渡す（リストだと中の " が \\" に化ける・test_bash_win32_call_site）
    assert isinstance(args[0], str) and "cmd.exe" in args[0].lower() and args[0].endswith('/d /s /c "dir /b"')
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw.get("creationflags", 0) == getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    assert kw["shell"] is False


@pytest.mark.parametrize("cmd", ["bash -c 'ls'", "wsl ls", r"C:\Windows\System32\bash.exe -lc id", "sh -c pwd", "  BASH.EXE --version"])
def test_win32ではbashとwslを実行前に断る(monkeypatch, cmd):
    monkeypatch.setattr(sys, "platform", "win32")
    r = B._win32_refusal(cmd)
    assert r and r.startswith("Error:") and "bash" in r and "cmd.exe" in r


@pytest.mark.parametrize("cmd", ["dir /b", "python -c \"print(1)\"", "Get-ChildItem", "echo bash is a word"])
def test_win32でも通常の命令は断らない(monkeypatch, cmd):
    monkeypatch.setattr(sys, "platform", "win32")
    assert B._win32_refusal(cmd) is None


def test_linuxでは断らない(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert B._win32_refusal("bash -c ls") is None
