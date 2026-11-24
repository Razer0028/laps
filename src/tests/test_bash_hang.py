"""入力待ちで止まらない／打ち切りで孫を残さない。

★実機で起きた形（2026-08-22・v0.1.83）:
  「600 秒のあいだ Python から1バイトも届きませんでした」で走行が落ち、
  **その10分間ずっと本体が塞がって新しい依頼が通らなかった**。
  作業の上限を撤去した直後なので、詰まった走行を止めるものが無かった。

  原因: Bash が `stdin` を指定しておらず、子は**サイドカーの標準入力＝
  アプリとの通信パイプ**を継承していた。入力待ちの命令（インストーラの
  同意確認など）は永久に止まる。しかも子がアプリの依頼を横取りしうる。

★導入を許可した（同日・オーナー決定）ので、この経路は今後もっと踏まれる。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from laps.tools import bash


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のシェル前提")
def test_input_waiting_command_fails_fast():
    """★止まるより失敗するほうがよい。失敗は読めるが、沈黙は読めない。"""
    t = time.time()
    out = str(bash._run({"command": 'read -p "同意? " x', "timeout": 20}))
    assert time.time() - t < 5, "入力待ちで止まっている"
    assert "exit 1" in out or "Error" in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のプロセスグループ前提")
def test_timeout_kills_grandchildren():
    """★実測で孫が2つ生き残った。打ち切ったインストーラが裏で動き続ける形。"""
    marker = f"blackwall_test_{os.getpid()}"
    out = str(bash._run({
        "command": f"sleep 47 & sleep 47 # {marker}", "timeout": 3}))
    assert "timeout" in out.lower()
    time.sleep(1)
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True)
    left = [l for l in ps.stdout.splitlines() if l.strip() == "sleep 47"]
    assert not left, f"孫が生き残っている: {len(left)} 個"


def test_normal_command_still_works():
    """★守りを足して普通の命令が動かなくなっては本末転倒。"""
    out = str(bash._run({"command": "echo hello"}))
    assert "hello" in out


def test_timeout_message_points_at_the_likely_cause():
    """打ち切りの文面が「次に何をすればよいか」を言うこと。"""
    out = str(bash._run({"command": "sleep 30", "timeout": 2}))
    assert "対話しない指定" in out or "--silent" in out
