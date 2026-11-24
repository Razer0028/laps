"""SystemState は Windows では PowerShell の同等命令を使う（2026-09-07・実機で df/free/uptime が WinError 2）。"""
import sys
from laps.tools import system_state as S


def test_win32では全動詞がpowershellに写る(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    t = S._verbs()
    assert set(t) == set(S._VERBS), "Linux と同じ動詞集合"
    for verb, (argv, desc) in t.items():
        assert argv[0] == "powershell" and "-NonInteractive" in argv and "-NoProfile" in argv, verb
        assert desc


def test_linuxでは従来の表(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert S._verbs() is S._VERBS


def test_win32で実行はprocrunを経由し失敗しても道具として返る(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    seen = {}
    class _R:
        returncode = 0; stdout = b"C: 100 GB"; stderr = b""
    def fake(argv, **kw): seen["argv"] = argv; return _R()
    monkeypatch.setattr(S.procrun, "run", fake)
    out = S._run({"verb": "disk"})
    assert seen["argv"][0] == "powershell" and "Get-PSDrive" in seen["argv"][-1]
    assert "100 GB" in str(out)
