"""自分（Blackwall）のプロセスの系統 — 画面操作と命令で自分を止めないための「守る PID」と「守る名前」。

★2026-10-19 オーナー「うまくデスクトップ操作ができずに自身のアプリを終了してしまったようです」。それまで守っていたのは
  エージェントの Python とその親だけで、守りが効くのも「宛先を指定しないとき」だけだった。
  ここでは Windows のプロセス表（Toolhelp32）から次をまとめる:
  ・エージェントの Python から上へ、Python が続く間たどり、最初の Python でない祖先（＝アプリ本体）まで。
  ・アプリ本体の下の WebView2（msedgewebview2.exe・アプリの画面そのもの）。
  エクスプローラーなど Windows の物は守らない（それらを操作できなくなるため）。表が取れないときは自分と親だけ。
"""
from __future__ import annotations

import os
import sys

#: 祖先に出てきても守らない（ここで止める）。アプリをエクスプローラーから起こすと、アプリの親はエクスプローラー。
NEVER_GUARD = {"explorer.exe", "svchost.exe", "services.exe", "wininit.exe", "winlogon.exe", "sihost.exe",
               "system", "smss.exe", "csrss.exe", "userinit.exe"}
WEBVIEW = "msedgewebview2.exe"


def process_table() -> dict[int, tuple[int, str]]:
    """pid → (親の pid, 実行ファイルの名前)。Windows だけ。取れなければ空。"""
    if sys.platform != "win32":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)      # TH32CS_SNAPPROCESS
        if not snap or snap == wintypes.HANDLE(-1).value:
            return {}
        table: dict[int, tuple[int, str]] = {}
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(pe))
            while ok:
                table[int(pe.th32ProcessID)] = (int(pe.th32ParentProcessID), str(pe.szExeFile))
                ok = k32.Process32NextW(snap, ctypes.byref(pe))
        finally:
            k32.CloseHandle(snap)
        return table
    except Exception:
        return {}


def chain(table: dict[int, tuple[int, str]], start: int) -> list[int]:
    """start から上へ: Python が続く間たどり、最初の Python でない祖先（アプリ）まで含める。"""
    out = [start]
    cur = start
    for _ in range(8):
        parent = table.get(cur, (0, ""))[0]
        if parent <= 4 or parent not in table or parent in out:
            break
        name = table[parent][1].lower()
        if name in NEVER_GUARD:
            break
        out.append(parent)
        if not name.startswith("python"):
            break
        cur = parent
    return out


def webviews(table: dict[int, tuple[int, str]], root: int) -> list[int]:
    """root の下の WebView2（アプリの画面）。WebView2 の下の WebView2（描画の係）もたどる。"""
    kids: dict[int, list[int]] = {}
    for pid, (ppid, _name) in table.items():
        kids.setdefault(ppid, []).append(pid)
    out: list[int] = []
    todo = [root]
    while todo and len(out) < 64:
        for k in kids.get(todo.pop(), []):
            if table[k][1].lower() == WEBVIEW and k not in out:
                out.append(k)
                todo.append(k)
    return out


def guard_pids(table: dict[int, tuple[int, str]] | None = None, start: int | None = None) -> list[int]:
    """画面操作と命令で触ってはいけない PID。"""
    start = os.getpid() if start is None else start
    table = process_table() if table is None else table
    if not table or start not in table:
        pids = [start]
        try:
            pp = os.getppid() if start == os.getpid() else 0
            if pp > 4:
                pids.append(pp)
        except Exception:
            pass
        return pids
    pids = chain(table, start)
    pids += [p for p in webviews(table, pids[-1]) if p not in pids]
    return pids


def app_names(table: dict[int, tuple[int, str]] | None = None, start: int | None = None) -> list[str]:
    """名前で止められると Blackwall が落ちる実行ファイル（アプリ本体と WebView2）。小文字・.exe つき。"""
    start = os.getpid() if start is None else start
    table = process_table() if table is None else table
    if not table or start not in table:
        return []
    up = chain(table, start)
    names: list[str] = []
    for p in up[1:]:
        n = table[p][1].lower()
        if not n.startswith("python") and n not in names:
            names.append(n)
    if webviews(table, up[-1]):          # アプリの画面が WebView2（端末から動かしているときは無い）
        names.append(WEBVIEW)
    return names
