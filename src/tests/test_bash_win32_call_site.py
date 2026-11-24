"""★2026-09-21 owner 実機（Windows 0.1.231）: **前面の Bash が 1 回も動いていなかった。**

`_spawn_args` の win32 分岐が `"stdin": DEVNULL` を返し、呼び出し側でも `stdin=` を
明示していたので、呼ぶたびに `TypeError: ... multiple values for keyword argument 'stdin'`。
0.1.196〜0.1.231 の 35 版・2 週間、Windows では Bash が全滅していた（実機の記録 5/5 失敗）。

★当時の試験（test_bash_win32_hang.py）は **`_spawn_args` の返り値だけ**を見ていた。
  部品は正しく、**繋いだ所**が壊れていた。ここは**道具の入口から Popen の呼び出しまで**を通す。
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from laps.tools import bash


class _FakeProc:
    """Popen の代役。**呼び出しの形が正しければ**ここまで来る（重複キーワードは来る前に TypeError）。"""

    pid = 4242
    returncode = 0

    def __init__(self, *a, **kw):
        _FakeProc.seen = (a, kw)
        self.stdout = open(os.devnull, "rb")
        self.stderr = open(os.devnull, "rb")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def communicate(self, timeout=None):
        return (b"", b"")

    def kill(self):
        pass

    terminate = kill


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("LAPS_POLICY", "trusted")     # 実機の既定。workspace だと門が先に断る
    monkeypatch.setattr(subprocess, "Popen", _FakeProc)
    _FakeProc.seen = None


def test_win32の前面Bashは道具の入口からPopenまで通る(as_windows):
    out = str(bash._run({"command": "dir /b", "timeout": 5}))
    assert "multiple values for keyword argument" not in out, out
    assert _FakeProc.seen is not None, f"Popen まで届いていない: {out}"
    args, kw = _FakeProc.seen
    # ★リストではなく**文字列**で渡す（リストだと中の " が \\" に化ける）
    assert isinstance(args[0], str) and args[0].endswith('/d /s /c "dir /b"'), args[0]
    assert kw["stdin"] is subprocess.DEVNULL          # 入力待ちで止まらない（0.1.196 の意図）
    assert kw["stdout"] is subprocess.PIPE and kw["stderr"] is subprocess.PIPE


def test_前面の引数は辞書で合わせるので同じ鍵が二度出ない():
    kw = bash._foreground_popen_kwargs({"stdin": subprocess.DEVNULL, "shell": False}, "/tmp")
    assert kw["stdin"] is subprocess.DEVNULL and kw["cwd"] == "/tmp" and kw["shell"] is False
    # _spawn_args の指定が勝つ
    assert bash._foreground_popen_kwargs({"cwd": "X"}, "Y")["cwd"] == "X"


def test_呼び出し側にキーワードの直書きを戻していない():
    """`Popen(*a, **kw, stdin=…)` の形に戻すと、同じ事故がまた起きる。"""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(bash))
    bad = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "Popen":
            star = [k for k in n.keywords if k.arg is None]
            named = {k.arg for k in n.keywords if k.arg}
            # `_spawn_args` 由来の辞書（_popen_kw）を広げる呼び出しに、名前つきを混ぜない
            if any(getattr(k.value, "id", "") == "_popen_kw" for k in star) and named:
                bad.append((n.lineno, sorted(named)))
    assert not bad, bad


def test_win32では二重引用符を含む命令を一字も変えずに渡す(as_windows):
    """`list2cmdline` は `"` を `\\"` に書き換える。cmd.exe はそれを戻さないので、
    `python -c "print(1)"` が**エラー無しで何も印字しない命令**になる。"""
    cmd = 'powershell -NoProfile -Command "Get-Item \'D:\\x y\\a.exe\' | Format-List *"'
    out = str(bash._run({"command": cmd, "timeout": 5}))
    assert _FakeProc.seen is not None, f"Popen まで届いていない: {out}"
    line = _FakeProc.seen[0][0]
    assert isinstance(line, str)
    assert '\\"' not in line, line                      # 引用符を書き換えていない
    assert line.endswith(f'/d /s /c "{cmd}"'), line       # 外側の 1 組だけを足している


def test_win32の背景起動も同じ形で渡す(as_windows, tmp_path):
    out = str(bash._run({"command": 'python -c "print(1)"', "run_in_background": True, "cwd": str(tmp_path)}))
    assert _FakeProc.seen is not None, f"Popen まで届いていない: {out}"
    line = _FakeProc.seen[0][0]
    assert isinstance(line, str) and line.endswith('/d /s /c "python -c "print(1)""'), line


@pytest.mark.parametrize("cmd", [
    "Get-FileHash 'D:\\x y\\a.exe' -Algorithm SHA256",
    "Format-Hex -Path 'D:\\a.dat' | Select-Object -First 16",
    "(Get-Item 'D:\\a.exe').VersionInfo | Format-List",
    "$x = Get-ChildItem -Recurse; $x.Count",
    "[System.IO.File]::ReadAllBytes('D:\\a.dat').Length",
    "get-childitem D:\\",
])
def test_PowerShellの書き方で始まる命令を見分ける(cmd):
    assert bash._looks_like_powershell(cmd)


@pytest.mark.parametrize("cmd", [
    "dir /b", "where python", "git status", "apt-get install -y jq", "ssh-keygen -l -f k",
    "docker-compose up -d", "python -c \"print('Get-Item')\"", "echo Get-Item",
    'powershell -NoProfile -Command "Get-Item x"', "pip-compile requirements.in",
])
def test_普通の命令はPowerShellに回さない(cmd):
    assert not bash._looks_like_powershell(cmd)


def test_win32でコマンドレットはps1に書いてPowerShellで走らせる(as_windows, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: None)      # pwsh 無し → 5.1 を SystemRoot から
    out = str(bash._run({"command": "Get-FileHash 'D:\\日本語\\a.exe'", "timeout": 5}))
    assert _FakeProc.seen is not None, out
    line = _FakeProc.seen[0][0]
    assert "powershell.exe" in line.lower() and "-NoProfile" in line and "-File" in line, line
    assert "Get-FileHash" not in line, "命令文を argv に置かない（.ps1 に書く）"
    # 走り終わったら一時ファイルは消えている
    m = __import__("re").search(r'-File "([^"]+)"', line)
    assert m and not os.path.exists(m.group(1))


def test_ps1はBOMつきUTF8で書く(as_windows, monkeypatch):
    """Windows PowerShell 5.1 は BOM の無い .ps1 を cp932 で読む＝日本語のパスが化ける。"""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: None)
    args, kw = bash._spawn_args("Get-Item 'D:\\日本語\\a.exe'")
    path = kw["_bw_script"]
    try:
        raw = open(path, "rb").read()
        assert raw.startswith(b"\xef\xbb\xbf")
        body = raw.decode("utf-8-sig")
        assert "Get-Item 'D:\\日本語\\a.exe'" in body and "$LASTEXITCODE" in body
    finally:
        os.unlink(path)


# ---- 2026-09-21 owner 実機（0.1.232）: 改行を含む命令が 19 回中 4 回、何も出さずに終わった ----
_REAL_MULTILINE = (
    'powershell -NoProfile -Command "\n'
    "$ext = @('.g00', '.ogg')\n"
    "foreach ($e in $ext) {\n"
    "    $files = Get-ChildItem -Path 'D:\\Blackwall\\anemoi' -Recurse -Filter \"*$e\"\n"
    "    Write-Output $files.Count\n"
    "}\n"
    '"'
)


def test_複数行のpowershellの包みは中身を取り出す():
    inner = bash._unwrap_multiline_powershell(_REAL_MULTILINE)
    assert inner is not None and inner.startswith("$ext = @(") and inner.rstrip().endswith("}")
    assert "powershell" not in inner.lower()


def test_一行のpowershellの包みには触らない():
    assert bash._unwrap_multiline_powershell('powershell -NoProfile -Command "Get-Item x"') is None
    assert bash._unwrap_multiline_powershell("dir /b") is None


def test_win32で複数行のpowershellはps1で走らせる(as_windows, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda n: None)
    args, kw = bash._spawn_args(_REAL_MULTILINE)
    path = kw["_bw_script"]
    try:
        assert path.endswith(".ps1") and "-File" in args[0]
        assert "\n" not in args[0], "cmd.exe に改行を渡すと最初の行で切れる"
        body = open(path, encoding="utf-8-sig").read()
        assert "foreach ($e in $ext) {" in body and "Write-Output $files.Count" in body
    finally:
        os.unlink(path)


def test_win32でそれ以外の複数行はcmdファイルで走らせる(as_windows):
    args, kw = bash._spawn_args("cd /d D:\\work\necho 一行目\ndir /b")
    path = kw["_bw_script"]
    try:
        assert path.endswith(".cmd") and "\n" not in args[0]
        raw = open(path, "rb").read()
        assert raw.startswith(b"@echo off\r\n") and "echo 一行目".encode("cp932") in raw and raw.count(b"\r\n") >= 4
    finally:
        os.unlink(path)
