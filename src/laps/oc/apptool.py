"""アプリをバックグラウンドで動かす（App 道具・2026-10-12）。

オーナー「Blackwall にバックグラウンドでアプリケーションを実行できるようにするツールを用意しておいてください」。

Bash の run_in_background は「この作業の間だけの処理」の入口（作業の終わりに止める・サーバーだけ残す）。
こちらは、利用者が使い続けるアプリ（ゲーム・Blender や Unity のようなエディタ・GUI の道具・手元のサーバー）を
起こして、作業が終わっても動かし続けるための入口。起こした物は台帳（<bg>/apps.json）に残すので、
次の作業でも list / log / stop できる。

・start: 切り離して起こす（Windows は新しいプロセスグループ・コンソールは隠す・できればジョブから抜ける）。
         出力は記録ファイルへ。数秒見て、すぐ終わった（起動に失敗した）か・窓が出たかを返す。
・list:  台帳の物が動いているか・窓の題名・動いている時間。
・log:   コンソールのプログラムが出した最後の行。
・stop:  まず窓に「閉じて」と頼む（WM_CLOSE・保存の問い合わせが出たら殺さずに知らせる）。窓が無ければ終わらせる。
         force=true ならすぐ終わらせる。★App で起こした物だけを止める（ほかのプロセスは触らない）。
・PID は使い回されるので、起動時刻も台帳に残し、違えば「もう終わった」と扱う。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid

from ..tools import ToolResult

_LOCK = threading.Lock()
MAX_WAIT = 60


# ── 台帳 ─────────────────────────────────────────────────────────────

def _book_path():
    from .. import procutil
    return procutil.bg_dir(create=True) / "apps.json"


def _load() -> list[dict]:
    try:
        with open(_book_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _save(rows: list[dict]) -> None:
    p = _book_path()
    tmp = p.with_name(p.name + f".{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows[-200:], f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


# ── プロセスを見る（Windows は ctypes・それ以外は /proc）──────────────────────

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)

    class _PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _u32.IsWindowVisible.argtypes = [wintypes.HWND]
    _u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _u32.GetWindow.restype = wintypes.HWND
    _u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    def _procs() -> list[tuple[int, int, str]]:
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return []
        out = []
        try:
            e = _PE32()
            e.dwSize = ctypes.sizeof(_PE32)
            ok = _k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), e.szExeFile))
                ok = _k32.Process32NextW(snap, ctypes.byref(e))
        finally:
            _k32.CloseHandle(snap)
        return out

    def _info(pid: int) -> tuple[bool, int | None]:
        """(動いているか, 起動時刻の値)。★os.kill(pid, 0) は Windows では相手を終わらせるので使わない。"""
        h = _k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False, None
        try:
            code = wintypes.DWORD()
            alive = bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            ct = None
            if _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                ct = (c.dwHighDateTime << 32) | c.dwLowDateTime
            return alive, ct
        finally:
            _k32.CloseHandle(h)

    def _windows(pids: set[int]) -> list[tuple[int, int, str]]:
        """pids の見えている最上位の窓（hwnd, pid, 題名）。"""
        res: list[tuple[int, int, str]] = []

        def cb(hwnd, _lp):
            try:
                if not _u32.IsWindowVisible(hwnd) or _u32.GetWindow(hwnd, 4):    # GW_OWNER
                    return True
                p = wintypes.DWORD()
                _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
                if p.value in pids:
                    n = _u32.GetWindowTextLengthW(hwnd)
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _u32.GetWindowTextW(hwnd, buf, n + 1)
                    res.append((int(hwnd or 0), int(p.value), buf.value))
            except Exception:
                pass
            return True

        _u32.EnumWindows(_WNDENUMPROC(cb), 0)
        return res

    def _ask_close(hwnds: list[int]) -> None:
        for h in hwnds:
            _u32.PostMessageW(h, 0x0010, 0, 0)               # WM_CLOSE

else:
    def _procs() -> list[tuple[int, int, str]]:
        out = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat", encoding="utf-8", errors="replace") as f:
                    s = f.read()
                name = s[s.index("(") + 1:s.rindex(")")]
                ppid = int(s[s.rindex(")") + 2:].split()[1])
                out.append((int(d), ppid, name))
            except (OSError, ValueError):
                continue
        return out

    def _info(pid: int) -> tuple[bool, int | None]:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as f:
                s = f.read()
            fields = s[s.rindex(")") + 2:].split()
            return fields[0] not in ("Z", "X"), int(fields[19])
        except (OSError, ValueError, IndexError):
            return False, None

    def _windows(pids: set[int]) -> list[tuple[int, int, str]]:
        return []

    def _ask_close(hwnds: list[int]) -> None:
        return None


def _tree(pid: int) -> set[int]:
    """pid とその子孫（ランチャーが本体を起こす形のため）。"""
    kids: dict[int, list[int]] = {}
    for p, pp, _n in _procs():
        if p != pp:
            kids.setdefault(pp, []).append(p)
    seen, todo = {pid}, [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in seen:
                seen.add(c)
                todo.append(c)
    return seen


def _row_alive(row: dict) -> bool:
    alive, ct = _info(int(row.get("pid") or 0))
    if not alive:
        return False
    return not (row.get("ct") and ct and int(row["ct"]) != int(ct))      # 同じ番号の別プロセス


def _tree_alive(row: dict) -> set[int]:
    """台帳の物（ランチャーが終わっても本体が残る形を含む）の、動いている pid。"""
    pids = set()
    if _row_alive(row):
        pids |= _tree(int(row["pid"]))
    for k in row.get("kids") or []:
        a, _ = _info(int(k))
        if a:
            pids |= _tree(int(k))
    return pids


def _titles(pids: set[int]) -> list[str]:
    try:
        return [t for _h, _p, t in _windows(pids) if t][:3]
    except Exception:
        return []


def _dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600} h {sec % 3600 // 60} min" if sec >= 3600 else f"{sec // 60} min {sec % 60} s"


def _tail(path: str, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            raw = f.read()
    except OSError:
        return ""
    try:
        from ..tools.read import decode_bytes
        text = decode_bytes(raw)
    except Exception:
        text = raw.decode("utf-8", "replace")
    return "\n".join(text.splitlines()[-n:])


# ── 起こす ────────────────────────────────────────────────────────────

def _argv_for(target: str, args: str, cwd: str):
    """(argv か 命令の文字列, 説明, ファイルを既定のアプリで開くだけか)。"""
    from . import toolenv
    path = target if os.path.isabs(target) else os.path.join(cwd, target)
    is_file = os.path.isfile(path)
    ext = os.path.splitext(path)[1].lower() if is_file else ""
    env_path = toolenv.child_path()
    if os.name == "nt":
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        if is_file and ext in (".exe", ".com"):
            return subprocess.list2cmdline([path]) + (" " + args if args else ""), path, False
        if is_file and ext in (".bat", ".cmd"):
            comspec = os.path.join(root, "System32", "cmd.exe")
            return f'"{comspec}" /d /c "{subprocess.list2cmdline([path])}{(" " + args) if args else ""}"', path, False
        if is_file and ext == ".ps1":
            from ..tools import bash as _b
            ps = _b._powershell_exe(root) or "powershell"
            return (subprocess.list2cmdline([ps, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path])
                    + (" " + args if args else "")), path, False
        if is_file and ext in (".py", ".pyw"):
            py = shutil.which("py", path=env_path) or shutil.which("python", path=env_path) or sys.executable
            return subprocess.list2cmdline([py, path]) + (" " + args if args else ""), path, False
        if is_file:
            return None, path, True                                   # 文書・ショートカット: 既定のアプリで開く
        from ..tools import bash as _b
        from . import bgwait
        comspec = os.path.join(root, "System32", "cmd.exe")
        line = target + (" " + args if args else "")
        return _b._win_cmdline(comspec, bgwait._win_script(line)), line, False
    if is_file and os.access(path, os.X_OK):
        return [path] + (shlex.split(args) if args else []), path, False
    if is_file and ext == ".py":
        return [sys.executable, path] + (shlex.split(args) if args else []), path, False
    if is_file:
        opener = shutil.which("xdg-open")
        return ([opener, path] if opener else None), path, opener is None
    sh = shutil.which("bash") or "/bin/sh"
    line = target + (" " + args if args else "")
    return [sh, "-c", line], line, False


def _start(inp: dict) -> ToolResult:
    from . import tools as _oc
    from . import toolenv
    from .. import policy, procutil
    target = str(inp.get("target") or "").strip().strip('"')
    if not target:
        return ToolResult("Error: target is required for action=start (an .exe path, a script, or a command line).",
                          ok=False, kind="error")
    args = str(inp.get("args") or "").strip()
    # ★`cwd` は道具の名前の読み替え（oc/tools._KEY_ALIASES）で `workdir` になって届くことがある（2026-10-16 の点検: 黙って作業場で起こしていた）。
    _wd = inp.get("cwd") or inp.get("workdir")
    cwd = _oc._abs(str(_wd)) if _wd else _oc._cwd()
    if not os.path.isdir(cwd):
        return ToolResult(f"Error: cwd does not exist: {cwd}", ok=False, kind="error")
    keep = inp.get("keep") not in (False, "false", "False", 0, "0")
    try:
        wait = max(0.0, min(float(inp.get("wait") if inp.get("wait") not in (None, "") else 3), float(MAX_WAIT)))
    except (TypeError, ValueError):
        wait = 3.0
    argv, shown, open_only = _argv_for(target, args, cwd)
    full = shown + (" " + args if args and os.path.isfile(shown) else "")     # 命令の文字列は args を含んでいる
    try:
        policy.authorize_command(full, cwd)
    except policy.PolicyDenied as e:
        return ToolResult(f"Error: {e}", ok=False, kind="denied")
    if open_only:
        try:
            if os.name == "nt":
                os.startfile(shown)                                 # noqa: S606 — 利用者のファイルを既定のアプリで開く
            else:
                return ToolResult(f"Error: {shown} is not a program and there is no opener here.", ok=False, kind="error")
        except OSError as e:
            return ToolResult(f"Error: could not open {shown}: {e}", ok=False, kind="error")
        return ToolResult(f"Opened {shown} with its default app (no process to track; it is not in App list).",
                          ok=True, kind="ok")
    log = procutil.bg_dir(create=True) / f"app-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.log"
    fh = open(log, "wb")
    kw: dict = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": fh, "stderr": subprocess.STDOUT,
                "env": toolenv.child_env()}
    try:
        if os.name == "nt":
            base = 0x00000200 | 0x08000000                         # 新しいプロセスグループ・コンソールは隠す
            try:
                proc = subprocess.Popen(argv, creationflags=base | 0x01000000, **kw)   # ジョブから抜ける（アプリを閉じても残す）
            except OSError:
                proc = subprocess.Popen(argv, creationflags=base, **kw)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **kw)
    except OSError as e:
        fh.close()
        return ToolResult(f"Error: could not start {shown}: {e}", ok=False, kind="error")
    finally:
        try:
            fh.close()
        except Exception:
            pass
    pid = proc.pid
    _alive, ct = _info(pid)
    if os.path.isfile(shown):
        name = os.path.basename(shown)
    else:
        m = re.match(r'\s*"([^"]+)"|\s*(\S+)', shown)
        name = os.path.basename((m.group(1) or m.group(2)) if m else shown)[:60]
    row = {"pid": pid, "ct": ct, "name": name, "cmd": full[:500],
           "cwd": cwd, "log": str(log), "started": time.time(), "keep": keep, "kids": []}
    # 少し見る: すぐ終わったら失敗を知らせる。窓が出れば題名を返す。ランチャーが本体を起こして終わる形は子を覚える。
    t0, titles, code = time.time(), [], None
    while time.time() - t0 < wait:
        code = proc.poll()
        tree = _tree(pid) if code is None else set()
        if code is None and tree:
            row["kids"] = sorted(p for p in tree if p != pid)[:20]
            titles = _titles(tree)
            if titles:
                break
        if code is not None:
            break
        time.sleep(0.5)
    if code is not None and not row["kids"]:
        tail = _tail(str(log), 20)
        return ToolResult(f"{shown} exited right away (exit code {code}) - it is not running."
                          + (f"\nLast output:\n{tail}" if tail else "\nIt printed nothing.")
                          + "\nCheck the path and arguments, or run it once in the foreground with Bash to see the error.",
                          ok=False, kind="error")
    with _LOCK:
        rows = [r for r in _load() if _tree_alive(r)] + [row]
        _save(rows)
    if not keep:
        try:
            from . import bgwait
            with bgwait._jobs_lock:
                if bgwait._job_scopes:
                    bgwait._job_scopes[-1].append({"pid": pid, "cmd": row["cmd"], "t": time.time()})
        except Exception:
            pass
    lines = [f"Started {name} in the background: pid {pid}."]
    lines.append(f"Window: {' / '.join(titles)}" if titles else
                 ("No window yet (it may still be loading, or it is a console program; App(action=log, pid="
                  f"{pid}) shows its output)."))
    lines.append(f"Output log: {log}")
    lines.append("It keeps running after this task ends; stop it with App(action=stop, pid=%d)." % pid if keep else
                 "It will be stopped when this task ends (keep=false).")
    return ToolResult("\n".join(lines), ok=True, kind="ok")


# ── 見る・止める ───────────────────────────────────────────────────────

def _find(pid_raw) -> tuple[dict | None, list[dict]]:
    rows = _load()
    try:
        pid = int(str(pid_raw).strip())
    except (TypeError, ValueError):
        return None, rows
    for r in rows:
        if int(r.get("pid") or 0) == pid or pid in (r.get("kids") or []):
            return r, rows
    return None, rows


def _list() -> ToolResult:
    with _LOCK:
        rows = _load()
    if not rows:
        return ToolResult("No apps started with App.", ok=True, kind="ok")
    out, keep_rows, now = [], [], time.time()
    for r in rows:
        pids = _tree_alive(r)
        if pids:
            t = _titles(pids)
            out.append(f"- pid {r['pid']} {r.get('name', '')}: running for {_dur(now - float(r.get('started') or now))}"
                       + (f", window: {' / '.join(t)}" if t else ", no visible window")
                       + ("" if r.get("keep", True) else ", stops when the task ends"))
            keep_rows.append(r)
        elif now - float(r.get("started") or 0) < 86400:
            out.append(f"- pid {r['pid']} {r.get('name', '')}: not running any more (log: {r.get('log', '')})")
            keep_rows.append(r)
    with _LOCK:
        _save(keep_rows)
    return ToolResult("Apps started with App:\n" + "\n".join(out), ok=True, kind="ok")


def _log(inp: dict) -> ToolResult:
    r, _rows = _find(inp.get("pid"))
    if not r:
        return ToolResult(f"Error: pid {inp.get('pid')} was not started with App (App(action=list) shows them).",
                          ok=False, kind="error")
    try:
        n = max(1, min(int(inp.get("lines") or 40), 400))
    except (TypeError, ValueError):
        n = 40
    t = _tail(r.get("log", ""), n)
    state = "running" if _tree_alive(r) else "not running"
    return ToolResult(f"{r.get('name', '')} (pid {r['pid']}, {state}) - last {n} lines of {r.get('log', '')}:\n"
                      + (t or "(it printed nothing; GUI programs usually do not)"), ok=True, kind="ok")


def _kill_tree(pids: set[int], root: int) -> None:
    if os.name == "nt":
        from .. import procrun
        for p in [root] + sorted(pids - {root}):
            try:
                procrun.run(["taskkill", "/F", "/T", "/PID", str(p)], capture_output=True, timeout=15)
            except Exception:
                pass
        return
    import signal
    for p in pids:
        try:
            os.killpg(os.getpgid(p), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(p, signal.SIGTERM)
            except OSError:
                pass
    time.sleep(2)
    for p in pids:
        if _info(p)[0]:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass


def _stop(inp: dict) -> ToolResult:
    r, rows = _find(inp.get("pid"))
    if not r:
        return ToolResult(f"Error: pid {inp.get('pid')} was not started with App, so App does not stop it "
                          "(App(action=list) shows the apps it started).", ok=False, kind="error")
    pids = _tree_alive(r)
    name = r.get("name", "")
    if not pids:
        msg = f"{name} (pid {r['pid']}) is not running any more."
    else:
        force = inp.get("force") in (True, "true", "True", 1, "1")
        wins = [] if force else _windows(pids)
        if wins:
            _ask_close([h for h, _p, _t in wins])
            t0 = time.time()
            while time.time() - t0 < 10 and _tree_alive(r):
                time.sleep(0.5)
            left = _tree_alive(r)
            if left:
                still = _titles(left)
                return ToolResult(f"Asked {name} to close, but it is still running"
                                  + (f" (window: {' / '.join(still)} - it may be asking to save)" if still else "")
                                  + ". Tell the user, or stop it with force=true (unsaved work is lost).",
                                  ok=False, kind="error")
            msg = f"Closed {name} (pid {r['pid']}) through its window."
        else:
            _kill_tree(pids, int(r["pid"]))
            time.sleep(0.5)
            msg = (f"Stopped {name} (pid {r['pid']})." if not _tree_alive(r) else
                   f"Tried to stop {name} (pid {r['pid']}) but it is still running.")
    with _LOCK:
        _save([x for x in _load() if x.get("pid") != r.get("pid")])
    return ToolResult(msg, ok="still running" not in msg, kind="ok")


def app_tool(inp: dict) -> ToolResult:
    inp = inp if isinstance(inp, dict) else {}
    a = str(inp.get("action") or ("start" if inp.get("target") else "list")).strip().lower()
    try:
        if a in ("start", "run", "launch", "open"):
            return _start(inp)
        if a in ("list", "ls", "status"):
            return _list()
        if a in ("log", "logs", "output"):
            return _log(inp)
        if a in ("stop", "kill", "close", "quit"):
            return _stop(inp)
    except Exception as e:  # noqa: BLE001 — 理由をそのまま返す
        return ToolResult(f"Error: App {a} failed: {type(e).__name__}: {e}", ok=False, kind="error")
    return ToolResult("Error: action must be start, list, log or stop.", ok=False, kind="error")


def enabled() -> bool:
    return os.environ.get("LAPS_APP", "1") != "0"


DESCRIPTION = """Run an application in the background and keep it running: a game, an editor (Blender, Unity), a GUI tool, a local server, or any program the user wants left open.
- action=start: start target (an .exe path, a script .bat/.cmd/.ps1/.py, or a command line) with args in cwd. It is detached, so it keeps running after this task ends (keep=false stops it when the task ends). Reports its pid, its window title once one appears, and its output log. If it exits at once, says so with its last output.
- action=list: the apps started with App (also from earlier tasks), whether each is still running, and their window titles.
- action=log: the last lines a console program printed (pid, lines).
- action=stop: close one (pid). It asks the app's windows to close first and reports if the app is asking something (for example to save); force=true ends it at once.
For a job that should finish inside this task (a build, a conversion, a test run), use Bash with run_in_background=true instead. App only stops apps it started."""

SCHEMA_PROPS = {
    "action": {"type": "string", "enum": ["start", "list", "log", "stop"]},
    "target": {"type": "string", "description": "start: an .exe path, a script (.bat .cmd .ps1 .py), or a command line"},
    "args": {"type": "string", "description": "start: arguments for the program"},
    "cwd": {"type": "string", "description": "start: working folder (default: the workspace)"},
    "keep": {"type": "boolean", "description": "start: keep it running after this task ends (default true)"},
    "wait": {"type": "number", "description": "start: seconds to watch it after starting (default 3, up to 60)"},
    "pid": {"type": "integer", "description": "log / stop: the pid App reported"},
    "lines": {"type": "integer", "description": "log: how many last lines (default 40)"},
    "force": {"type": "boolean", "description": "stop: end it at once without asking its windows to close"},
}
