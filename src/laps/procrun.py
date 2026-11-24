"""subprocess.run(capture_output=True) の**固まらない**代替（2026-09-06・会議 [space:推論機不達]）。

★実機（Windows 利用者の PC）で確定した機序: `subprocess.run(capture_output=True, timeout=N)` は
  stdout/stderr を**読取スレッド**で吸い出し `communicate()` がそれを join する。子孫がパイプの
  ハンドルを継いだまま残ると EOF が来ず、join は返らない。しかも TimeoutExpired の後始末で
  もう一度 `communicate()` を呼び、そちらには timeout が無い＝**timeout を付けても止まらない**。
  実例: `git check-ignore -q`（tools/grep.py）で ThreadPoolExecutor のスレッドが永久停止し、
  道具の束の直後に CPU 0・通信 0 で走行が固まった（stall ダンプ・ClaudeCode 実測）。

ここでは**パイプと読取スレッドを使わない**:
  ・stdin は DEVNULL（`input` があれば一時ファイル）
  ・stdout/stderr は**一時ファイル**（`capture=False` なら DEVNULL）
  ・`Popen` → `wait(timeout)` → 超えたら**木ごと kill**（win32: taskkill /F /T・posix: killpg）
  ・戻りは `subprocess.CompletedProcess`（returncode/stdout/stderr）。timeout は
    `subprocess.TimeoutExpired` を**殺した後に**投げる（既存の except を変えずに済む）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from typing import Any

_MAX_READ = int(os.environ.get("LAPS_PROCRUN_MAX_BYTES", str(8 * 1024 * 1024)) or 8 * 1024 * 1024)
_REAL_RUN = subprocess.run   # 試験が subprocess.run を差し替えたときだけ、それを尊重する（下の run() 冒頭）


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def _read(path: str, text: bool, errors: str):
    try:
        with open(path, "rb") as f:
            data = f.read(_MAX_READ)
    except Exception:
        data = b""
    if text:
        return data.decode("utf-8", errors=errors)
    return data


def run(argv, *, timeout: float | None = None, text: bool = False, cwd=None, env=None,
        input=None, errors: str = "replace", capture: bool = True, check: bool = False,
        shell: bool = False, **_ignored: Any) -> subprocess.CompletedProcess:
    """`subprocess.run(argv, capture_output=True, timeout=…, text=…, cwd=…, env=…, input=…)` の置き換え。

    `_ignored` は `capture_output=True` など旧呼び出しの残りを黙って受ける（移行のため）。

    ★試験の二重（monkeypatch で `subprocess.run` を差し替えた fake）を壊さないため、差し替えられて
      いるときだけ**その fake に委ねる**（本番＝差し替え無し＝一時ファイル経路）。本番の振る舞いは変えない。"""
    if subprocess.run is not _REAL_RUN:
        kw2: dict[str, Any] = {"capture_output": capture, "timeout": timeout, "cwd": cwd, "env": env}
        if shell:
            kw2["shell"] = True
        if text:
            kw2["text"] = True
            kw2["errors"] = errors
        if input is not None:
            kw2["input"] = input
        if check:
            kw2["check"] = True
        return subprocess.run(argv, **kw2)
    tmpdir = tempfile.mkdtemp(prefix="bw-run-")
    out_path = os.path.join(tmpdir, "out")
    err_path = os.path.join(tmpdir, "err")
    in_path = os.path.join(tmpdir, "in")
    files = []
    try:
        if capture:
            fo = open(out_path, "wb"); fe = open(err_path, "wb"); files += [fo, fe]
            stdout, stderr = fo, fe
        else:
            stdout = stderr = subprocess.DEVNULL
        if input is not None:
            with open(in_path, "wb") as f:
                f.write(input.encode("utf-8") if isinstance(input, str) else bytes(input))
            fi = open(in_path, "rb"); files.append(fi); stdin = fi
        else:
            stdin = subprocess.DEVNULL
        kw: dict[str, Any] = {"stdin": stdin, "stdout": stdout, "stderr": stderr, "cwd": cwd, "env": env}
        if shell:
            kw["shell"] = True
        if sys.platform == "win32":
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["start_new_session"] = True
        proc = subprocess.Popen(argv, **kw)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            for f in files:
                try: f.close()
                except Exception: pass
            out = _read(out_path, text, errors) if capture else (b"" if not text else "")
            err = _read(err_path, text, errors) if capture else (b"" if not text else "")
            raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
        for f in files:
            try: f.close()
            except Exception: pass
        out = _read(out_path, text, errors) if capture else ("" if text else b"")
        err = _read(err_path, text, errors) if capture else ("" if text else b"")
        cp = subprocess.CompletedProcess(argv, rc, out, err)
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, argv, output=out, stderr=err)
        return cp
    finally:
        for f in files:
            try: f.close()
            except Exception: pass
        for p in (out_path, err_path, in_path):
            try: os.unlink(p)
            except Exception: pass
        try: os.rmdir(tmpdir)
        except Exception: pass
