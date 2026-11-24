"""HTTP の前に無音で固まる 3 経路の回帰（2026-09-06・owner 実機 Windows）。"""
import subprocess, sys, time, threading, socket
import pytest
from laps import envpack as E


class _Hang:
    """wait() が timeout で必ず TimeoutExpired を投げる偽プロセス（孫がパイプを握った形）。"""
    pid = 4242
    def __init__(self): self.killed = 0
    def wait(self, timeout=None):
        if self.killed: return -9
        raise subprocess.TimeoutExpired("probe", timeout)
    def kill(self): self.killed += 1


def test_探りが戻らなくても_worksはFalseで戻り木を殺す(monkeypatch):
    monkeypatch.setattr(E.shutil, "which", lambda n: "/usr/bin/" + n)
    made = []
    monkeypatch.setattr(E.subprocess, "Popen", lambda *a, **k: (made.append(k) or _Hang()))
    monkeypatch.setattr(sys, "platform", "linux")
    E._works.cache_clear() if hasattr(E._works, "cache_clear") else None
    t0 = time.time(); ok = E._works("node"); dt = time.time() - t0
    assert ok is False and dt < 5
    kw = made[0]
    assert kw["stdin"] is subprocess.DEVNULL and kw["stdout"] is subprocess.DEVNULL and kw["stderr"] is subprocess.DEVNULL


def test_win32ではbashを探らない(monkeypatch):
    monkeypatch.setattr(E.shutil, "which", lambda n: r"C:\Windows\System32\bash.exe")
    called = []
    monkeypatch.setattr(E.subprocess, "Popen", lambda *a, **k: called.append(1))
    monkeypatch.setattr(sys, "platform", "win32")
    assert E._works("bash") is False and called == []


def test_名前解決が黙っても2秒で見切る(monkeypatch):
    E._NET_CACHE.clear(); monkeypatch.delenv("LAPS_ASSUME_ONLINE", raising=False)
    def slow(*a, **k): time.sleep(6); return []
    monkeypatch.setattr(socket, "getaddrinfo", slow)
    before = socket.getdefaulttimeout()
    t0 = time.time(); ok = E.network_reachable(); dt = time.time() - t0
    assert ok is False and dt < 3.5
    assert socket.getdefaulttimeout() == before, "全体の既定 timeout を書き換えてはいけない"
    E._NET_CACHE.clear()


def test_serveのループはBaseExceptionでもdoneを出す():
    import inspect, laps.serve as S
    src = inspect.getsource(S)
    assert "except BaseException as e:        # ここで死ぬと以後 done が出ない" in src
    assert "if isinstance(e, (SystemExit, KeyboardInterrupt)):" in src
