"""Windows では Bash 道具の既定 timeout が短い（2026-09-07）。"""
import os, sys, re
from laps.tools import bash as B


def test_win32の既定は60秒でenvで変えられる(monkeypatch):
    src = open(B.__file__, encoding="utf-8").read()
    assert 'LAPS_BASH_TIMEOUT_WIN' in src and '"60"' in src
    assert 'int_arg(inp, "timeout", _default_to)' in src


def test_win32の打ち切り助言にWindows固有の典型が入る(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(B, "_kill_verdict", lambda cmd, pgid: ("殺しました。", True))
    h = B._timeout_hint("dir /s C:\\", "", None)
    assert "dir /s" in h and "-Depth" in h
