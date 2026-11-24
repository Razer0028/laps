"""★2026-10-19 オーナー「うまくデスクトップ操作ができずに自身のアプリを終了してしまったようです。サブモニターがある環境などにも
対応できるように改善してください」。

- oc/selfproc: エージェントの Python → アプリ本体 → アプリの画面（WebView2）を守る PID に。エクスプローラーは守らない。
- oc/shellfix: アプリ本体・WebView2 を名前で止める命令は走らせない（PID で止める形は selfproc の PID で断る）。
- oc/desktop: screen（画面 1 枚の写真）・"s2" の形・画面の写真の結果の文。補助（C#）は偽物。
- desktop_helper.cs: 自分の窓を断る守りが押す・打つ・巻く・前に出す・書き換えるのどの経路にも入っていること（文字で確かめる）。
"""
from __future__ import annotations

import base64
import io
import re

import pytest
from PIL import Image

from laps.oc import desktop, selfproc, shellfix

# pid → (親, 名前)。エクスプローラー → アプリ → エージェントの Python。アプリの下に WebView2 が 2 段・llama-server。
TABLE = {
    100: (4, "explorer.exe"),
    200: (100, "BLACKWALL Console.exe"),
    300: (200, "python.exe"),
    310: (300, "python.exe"),          # エージェントの下の子（道具が起こした python）
    400: (200, "msedgewebview2.exe"),
    401: (400, "msedgewebview2.exe"),
    402: (400, "msedgewebview2.exe"),
    500: (200, "llama-server.exe"),
    600: (100, "msedgewebview2.exe"),  # 他のアプリの WebView2
    700: (100, "notepad.exe"),
}


def test_guard_pids_cover_the_agent_the_app_and_its_web_view():
    pids = selfproc.guard_pids(TABLE, start=300)
    assert pids[:2] == [300, 200]
    assert set(pids) == {300, 200, 400, 401, 402}
    assert 100 not in pids and 600 not in pids and 500 not in pids and 310 not in pids


def test_nested_python_is_climbed_until_the_app():
    t = dict(TABLE)
    t[800] = (300, "python.exe")       # エージェントがさらに子の python の中で動く形
    assert selfproc.chain(t, 800) == [800, 300, 200]


def test_app_started_from_a_terminal_and_unknown_tables():
    t = {10: (4, "explorer.exe"), 20: (10, "WindowsTerminal.exe"), 30: (20, "cmd.exe"), 40: (30, "python.exe")}
    assert selfproc.chain(t, 40) == [40, 30]
    assert selfproc.app_names(t, start=40) == ["cmd.exe"]            # WebView2 は無いので名前に入れない
    assert selfproc.guard_pids({}, start=12345) == [12345]
    assert selfproc.app_names({}, start=1) == []


def test_app_names_for_the_windows_app():
    assert selfproc.app_names(TABLE, start=300) == ["blackwall console.exe", "msedgewebview2.exe"]


@pytest.fixture
def as_app(monkeypatch):
    monkeypatch.setattr(selfproc, "process_table", lambda: TABLE)
    monkeypatch.setattr(selfproc.os, "getpid", lambda: 300)
    return TABLE


@pytest.mark.parametrize("cmd", [
    'taskkill /F /IM "BLACKWALL Console.exe"',
    "taskkill /IM msedgewebview2.exe /F",
    "Get-Process msedgewebview2 | Stop-Process -Force",
    "Stop-Process -Name 'BLACKWALL Console'",
    "taskkill /F /PID 401",                                           # アプリの画面の WebView2 を PID で
    "taskkill /F /PID 200",                                           # アプリ本体を PID で
])
def test_shell_commands_that_stop_blackwall_are_not_run(as_app, cmd):
    new, note = shellfix.guard_self_kill(cmd, windows=True)
    assert new is None and "L.A.P.S itself" in note, (cmd, note)


@pytest.mark.parametrize("cmd", [
    "taskkill /F /PID 600",                                           # 他のアプリの WebView2
    "taskkill /F /IM notepad.exe",
    "echo msedgewebview2",
    "dir C:\\\\Users",
])
def test_other_commands_still_run(as_app, cmd):
    new, note = shellfix.guard_self_kill(cmd, windows=True)
    assert new == cmd and note == ""


def test_desktop_sends_the_guard_pids(as_app, monkeypatch, tmp_path):
    seen = []

    class H:
        def call(self, req, timeout):
            seen.append(req)
            return {"ok": True, "text": "Clicked."}
    monkeypatch.setattr(desktop, "_get_helper", lambda: H())
    desktop.desktop_tool({"action": "click", "id": 3})
    assert set(seen[0]["guard_pids"]) == {300, 200, 400, 401, 402}


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (20, 20, 20)).save(buf, "PNG")
    return buf.getvalue()


def test_look_at_a_whole_screen(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_DESKTOP_DIR", str(tmp_path))
    seen = []

    class H:
        def call(self, req, timeout):
            seen.append(dict(req))
            if req["action"] != "look":
                return {"ok": True, "text": "Clicked."}
            with open(req["out"], "wb") as f:
                f.write(_png(2560, 1440))
            return {"ok": True, "text": "picture -2560 0 2560 1440 0\nScreen s2 2560x1440 at -2560,0 (150% scale). "
                                        "Screens: s1 (main) 1920x1080 at 0,0, s2 2560x1440 at -2560,0.\n"
                                        "Windows on it, front first:\n  w3 \"Game\" - game on s2"}
    monkeypatch.setattr(desktop, "_get_helper", lambda: H())
    desktop._LAST_LOOK.clear()
    r = desktop.desktop_tool({"action": "look", "monitor": "s2"})
    assert r.ok and seen[0]["screen"] == 2 and seen[0]["action"] == "look"
    text = str(r)
    assert "whole screen" in text and "w3 \"Game\"" in text and "of 2560x1440 pixels" in text
    assert desktop._LAST_LOOK == {"left": -2560, "top": 0, "w": 2560, "h": 1440}
    desktop.desktop_tool({"action": "click", "rx": 500, "ry": 500})
    clk = [s for s in seen if s["action"] == "click"][-1]
    assert clk["x"] == -1280 and clk["y"] == 720               # 左の画面は負の位置
    desktop._LAST_LOOK.clear()


def test_description_and_schema_tell_about_screens_and_self():
    d = desktop.DESKTOP_DESCRIPTION
    assert "Several screens:" in d and "negative" in d and "Blackwall itself: read only" in d
    assert "screen" in desktop.schema()["properties"]


def _body(cs: str, name: str) -> str:
    start = cs.index(name)
    nxt = re.search(r"\n        static [^\n]*\(", cs[start + len(name):])
    return cs[start: start + len(name) + (nxt.start() if nxt else len(cs))]


def test_helper_guards_every_acting_path():
    cs = desktop.HELPER_SOURCE.read_text(encoding="utf-8")
    assert "LoadGuard(r);" in _body(cs, "static string Handle(")
    act = _body(cs, "static void ActivateFor(")
    assert act.count("RefuseSelf(") == 2
    click = _body(cs, "static string Click(")
    assert click.count("CheckPoint(") == 2 and "coveredBySelf" in click
    assert "CheckPoint(x, y, \"scroll\")" in _body(cs, "static string Scroll(")
    assert "RefuseSelf(h, \"scroll\")" in _body(cs, "static string Scroll(")
    assert "RefuseSelfForeground(\"keys\")" in _body(cs, "static string Keys(")
    assert _body(cs, "static string TypeText(").count("RefuseSelfForeground(\"text\")") == 2
    assert "RefuseSelf(TopHandle(el), \"set_value\")" in _body(cs, "static string SetValue(")
    assert "RefuseSelf(h, \"focus\")" in _body(cs, "static string Focus(")
    assert "if (Guarded(h)) { if (self == IntPtr.Zero) self = h; continue; }" in _body(cs, "static IntPtr FindWindow(")
    assert "MonitorFromPoint(p, 0)" in _body(cs, "static void CheckPoint(")
