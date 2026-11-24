"""長い処理を背景で走らせ、推論機を使わずに待つ（2026-10-08）。

オーナー「エージェントへの機能追加や最適化を検討して」→ 走行記録では、変換・学習・抽出のような
長い処理を待つのに `timeout /t 30` や `Start-Sleep 60` を何度も打っていた（しかも `timeout /t` は
道具の中ではすぐ終わり、待っていなかった）。1 回確かめるたびに 1 手番（平均 約 75 秒の推論）を使う。

ここでは:
- Bash に `run_in_background=true`（旧中核の背景起動 tools/bash.py `_spawn_background` をそのまま使う）。
- BashOutput（旧中核の tools/bash_output.py）に `wait`（秒）と `until`（正規表現）を足す。
  待っている間は道具の中で眠るだけで、推論機は呼ばない。処理が終わる・`until` が出力に現れる・
  利用者が止める、のどれかで早く戻る。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time

from ..tools import ToolResult

MAX_WAIT = 1800          # 1 回の待ちの上限（秒）。長い処理は待ちを重ねる（そのたびに中間報告の機会がある）
_POLL = 3.0


def _tail_text(path: str, n: int = 65536) -> str:
    middle = False
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(-n, os.SEEK_END)
                middle = True
            except OSError:
                fh.seek(0)
            raw = fh.read()
    except OSError:
        return ""
    # ★途中から読むと先頭が文字の途中で切れ、全体が latin-1 になって `until='完了'` が当たらなかった（2026-10-16 の点検）。
    #   BashOutput と同じ、切れ目を直してから読む関数を使う（tools/bash_output._decode_tail_window）。
    try:
        from ..tools.bash_output import _decode_tail_window
        return _decode_tail_window(raw, from_middle=middle)
    except Exception:
        pass
    from ..tools.bash import _decode_out
    try:
        return _decode_out(raw)
    except Exception:
        return raw.decode("utf-8", "replace")


def _win_script(cmd: str) -> str:
    """Windows の背景起動は cmd.exe /c の 1 行なので、複数行・PowerShell の書き方は台本に書いて渡す
    （前面の Bash と同じ扱い・tools/bash.py `_spawn_args` の注記）。1 行の cmd の命令はそのまま。"""
    from ..tools import bash as _b
    inner = _b._unwrap_multiline_powershell(cmd)
    ps_like = inner is not None or _b._looks_like_powershell(cmd)
    if not ps_like and "\n" not in cmd.strip():
        return cmd
    d = str(_b._bg_dir())
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    if ps_like:
        ps = _b._powershell_exe(root)
        if ps:
            fd, path = tempfile.mkstemp(dir=d, prefix=".bgscript-", suffix=".ps1")
            with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:
                fh.write("$ProgressPreference = 'SilentlyContinue'\n")
                fh.write((inner if inner is not None else cmd).rstrip("\n") + "\n")
                fh.write("if ($LASTEXITCODE) { exit $LASTEXITCODE }\n")
            return f'"{ps}" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{path}"'
    fd, path = tempfile.mkstemp(dir=d, prefix=".bgscript-", suffix=".cmd")
    body = "\r\n".join(cmd.strip().splitlines()) + "\r\n"
    try:
        raw = ("@echo off\r\n" + body).encode("cp932")
    except UnicodeEncodeError:
        raw = b"@echo off\r\nchcp 65001 >nul\r\n" + body.encode("utf-8")
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    return f'"{path}"'


# ── 走行ごとの背景の仕事（2026-10-10 オーナー「バックグラウンドプロセスの扱いも雑ですよね。プロセスが残ったまま
#    次の作業やったりするから消すの面倒です」）──────────────────────────────────────────────
# ★走行記録（0.1.376・19:01〜20:08）: 抽出スクリプトの v3・v5・v6 を背景で起こしたまま止めずに v7 を起こし、v7 の終わりも
#   待たずに答えて走行を閉じた。走行が終わっても背景の仕事は動き続け、利用者が手で消していた。
# ・起こした仕事を走行ごとに覚える（monitor.py の見張りと同じく、入れ子の走行＝部下の走行は自分の範囲を持つ）。
# ・同じ系統の命令（版の数字・ポートだけが違う）を起こすときは、まだ動いている古い方を止めてから起こす。
# ・走行の終わりに、この走行で起こしてまだ動いている仕事を止める。サーバー（待ち受けを続ける物）は残し、その旨を知らせる。
import threading as _threading

_jobs_lock = _threading.Lock()
_job_scopes: list[list[dict]] = []      # 走行ごとの [{pid, cmd, t}]


def begin_run() -> None:
    with _jobs_lock:
        _job_scopes.append([])


def register(pid: int, cmd: str) -> None:
    """Bash 以外の道から背景に残った仕事（前で走らせた `cmd &`）も、この走行の仕事として覚える（2026-10-16 の点検）。"""
    with _jobs_lock:
        if _job_scopes:
            _job_scopes[-1].append({"pid": int(pid), "cmd": cmd, "t": time.time()})


def _family(cmd: str) -> str:
    """版の印とポートだけを伏せた命令の形（同じ系統の見分け）。
    ★2026-10-15: 以前は数字をすべて伏せていたので、`extract.py --chunk 1` と `--chunk 2`、`part1` と `part2` のように
      並べて走らせた仕事を「同じ系統の古い物」として後の起動が止めていた。伏せるのは版の印（_v7・-v2・.v3）と、
      ポートらしい 4〜5 桁の数だけにする（作り直した台本・別のポートで起こし直したサーバは今までどおり止める）。"""
    c = " ".join((cmd or "").lower().split())
    c = re.sub(r"[_\-.]v\d+", "#v", c)
    return re.sub(r"(?<![\w.])\d{4,5}(?![\w.])", "#port", c)


def _alive(pid: int) -> bool:
    """シェルか、同じ組に残った子（`cd d && server &` の server・`nohup x &` の x）が生きているか。
    ★2026-10-15: シェルの番号だけを見ていたので、シェルがすぐ終わる形だと「終わった」と言い、走行の終わりにも止めなかった
      （BashOutput の本体は組の残りを見て alive: True と言うので、同じ結果の中で食い違っていた）。"""
    try:
        from ..tools import bash_output as _bo
        return bool(_bo._is_alive(pid)) or bool(_bo._group_members(pid))
    except Exception:
        return False


def _kill(pid: int) -> str:
    try:
        from ..tools import bash_output as _bo
        return str(_bo._run({"pid": pid, "kill": True}))
    except Exception as e:  # noqa: BLE001
        return f"Error: {e}"


def _is_server(job: dict) -> bool:
    try:
        from . import shellfix as _sf
        if _sf.looks_like_server(job.get("cmd") or ""):
            return True
        from .. import procutil
        d = procutil.bg_dir(create=False)
        tail = _tail_text(str(d / f"{job['pid']}.out"), 16384) + "\n" + _tail_text(str(d / f"{job['pid']}.err"), 16384)
        return bool(_sf._SERVER_OUT_RE.search(tail))
    except Exception:
        return False


def running_jobs() -> list[dict]:
    """この走行で起こして、まだ動いている仕事（サーバーかどうかの印つき）。"""
    with _jobs_lock:
        jobs = list(_job_scopes[-1]) if _job_scopes else []
    out = []
    for j in jobs:
        if _alive(j["pid"]):
            out.append({**j, "server": _is_server(j)})
    return out


def end_run() -> tuple[list[dict], list[dict]]:
    """この走行で起こしてまだ動いている仕事を止める（サーバーは残す）。(止めた物, 残した物)。"""
    with _jobs_lock:
        jobs = _job_scopes.pop() if _job_scopes else []
    stopped, kept = [], []
    for j in jobs:
        if not _alive(j["pid"]):
            continue
        if _is_server(j):
            kept.append(j)
            # 残したサーバーは外側の走行（あれば）へ引き継ぐ（同じ系統を起こし直すときに止められるように）
            with _jobs_lock:
                if _job_scopes:
                    _job_scopes[-1].append(j)
                else:
                    _orphans.append(j)
            continue
        _kill(j["pid"])
        stopped.append(j)
    return stopped, kept


_orphans: list[dict] = []        # 走行の外に残したサーバー（次の走行で同じ系統を起こすときに止める）


def _stop_superseded(command: str) -> list[str]:
    """同じ系統でまだ動いている古い仕事を止める。止めた物の説明の並び。"""
    fam = _family(command)
    notes = []
    with _jobs_lock:
        _orphans[:] = [j for j in _orphans if _alive(j["pid"])]
        pool = [j for sc in _job_scopes for j in sc] + list(_orphans)
    for j in pool:
        if _family(j.get("cmd") or "") != fam or not _alive(j["pid"]):
            continue
        _kill(j["pid"])
        notes.append(f"pid {j['pid']} (`{(j.get('cmd') or '')[:80]}`)")
    return notes


def start(command: str, cwd: str) -> ToolResult:
    """Bash(run_in_background=true) の本体。"""
    from ..tools import bash as _b
    try:
        superseded = _stop_superseded(command)
    except Exception:
        superseded = []
    try:
        cmd = _win_script(command) if sys.platform == "win32" else command
        text = _b._spawn_background(cmd, cwd)
    except Exception as e:  # noqa: BLE001
        return ToolResult(f"Error: could not start the command in the background: {e}", ok=False, kind="error")
    m = re.search(r"pid:\s*(\d+)", text)
    pid = m.group(1) if m else "<pid>"
    if m:
        with _jobs_lock:
            if _job_scopes:
                _job_scopes[-1].append({"pid": int(m.group(1)), "cmd": command, "t": time.time()})
        _watch(int(m.group(1)), command, cwd)
    try:
        from . import jobwatch as _jw
        text += "\n" + _jw.notify_hint()
    except Exception:
        pass
    text += (f"\nTo block on it now (no model time is used while waiting): BashOutput(pid={pid}, wait=<seconds up to "
             f"{MAX_WAIT}>, until=\"<text that marks done or failed>\").")
    if superseded:
        text += ("\nStopped the older run(s) of the same command that were still running: " + ", ".join(superseded) + ".")
    try:
        others = [j for j in running_jobs() if str(j["pid"]) != str(pid)]
    except Exception:
        others = []
    if others:
        text += ("\nAlso still running from earlier in this task: "
                 + ", ".join(f"pid {j['pid']} (`{(j.get('cmd') or '')[:60]}`)" for j in others[:5])
                 + ". Stop any you no longer need with BashOutput(pid=..., kill=true).")
    text += ("\nIf the user stops this task, jobs still running are stopped (servers are left running).")
    return ToolResult(text, ok=True, kind="ok")


def _watch(pid: int, command: str, cwd: str) -> None:
    """背景の仕事を見張り（oc/jobwatch）に載せる: 終わったら終了の番号と出力の末尾を、進まなくなったらそれを知らせる。"""
    try:
        from . import jobwatch as _jw
        from .. import procutil
        from ..tools import bash as _b
        d = procutil.bg_dir(create=False)
        if _is_server({"pid": pid, "cmd": command}):
            return                                    # 待ち受けを続ける物は「終わらない・出力しない」が普通
        _jw.track(f"pid:{pid}", label=f"pid {pid}", cmd=command, cwd=cwd, roots=lambda: [pid],
                  files=[str(d / f"{pid}.out"), str(d / f"{pid}.err")],
                  exit_code=lambda: _b.bg_exit_code(pid),          # 生死はプロセスの木で見る（Windows で tasklist を起こさない）
                  check_hint=f"BashOutput(pid={pid}) shows its output")
    except Exception:
        pass


def bash_output_tool(inp: dict) -> ToolResult:
    from ..tools import bash_output as _bo
    from .. import procutil
    from . import tools as _oc
    inp = inp if isinstance(inp, dict) else {}
    try:
        wait = max(0.0, min(float(inp.get("wait") or 0), float(MAX_WAIT)))
    except (TypeError, ValueError):
        wait = 0.0
    until = str(inp.get("until") or "")
    head = ""
    pid_raw = inp.get("pid")
    # ★kill は "false" で来ることがある（bool("false") は真・2026-10-16）。偽なら下の BashOutput へも偽で渡す。
    _kill = inp.get("kill") is True or str(inp.get("kill")).strip().lower() in ("true", "1", "yes", "on")
    if "kill" in inp:
        inp = {**inp, "kill": _kill}
    if wait > 0 and pid_raw not in (None, "") and not _kill:
        try:
            pid = int(str(pid_raw).strip())
        except ValueError:
            return ToolResult(f"Error: pid must be a number (got {pid_raw!r}).", ok=False, kind="error")
        rx = None
        if until:
            try:
                rx = re.compile(until)
            except re.error as e:
                return ToolResult(f"Error: `until` is not a valid regular expression: {e}", ok=False, kind="error")
        d = procutil.bg_dir(create=False)
        out_p, err_p = str(d / f"{pid}.out"), str(d / f"{pid}.err")
        if os.path.exists(out_p):
            from . import jobwatch as _jw
            key = f"pid:{pid}"
            before = _jw.measure(key)
            # ★出力をファイルへ回した命令（`> train.log 2>&1`）は、そのファイルにも until を当てる（BashOutput は (empty) だった）。
            logs = [out_p, err_p] + _jw.redirect_targets(_cmd_of(pid), None)
            t0 = time.time()
            reason = f"the {int(wait)} s wait ended; the process is still running"
            news: list[dict] = []
            while True:
                if rx and any(rx.search(_tail_text(p)) for p in logs):
                    reason = f"`{until}` appeared in the output"
                    break
                if not _alive(pid):
                    reason = "the process exited"
                    break
                _why = _oc.interrupted()          # ★取り消しか、新しい発言（2026-10-28 からは普通の一言でも抜ける）
                if _why:
                    reason = _why + ("; answer it first — the job keeps running" if "message" in _why else "")
                    break
                # ★この仕事が進まなくなった・入力を待っている、または他の背景の仕事が終わった（oc/jobwatch・2026-10-28）。
                ev = _jw.has_wake()
                if ev is not None:
                    news = _jw.take({ev["key"]})
                    reason = ("this job stopped making progress" if ev["key"] == key and ev["kind"] == "stall" else
                              "this job seems to wait for input" if ev["key"] == key else
                              f"another background job ({ev['label']}) needs attention")
                    break
                left = t0 + wait - time.time()
                if left <= 0:
                    break
                time.sleep(min(_POLL, left))
            prog = _jw.progress_line(key, before)
            head = (f"Waited {int(time.time() - t0)} s: {reason}.\n" + (prog + "\n" if prog else "")
                    + "".join(e["text"] + "\n\n" for e in news if e["key"] != key or e["kind"] != "exit"))
    res = _bo._run({k: v for k, v in inp.items() if k not in ("wait", "until")})
    text = str(res)
    ok = getattr(res, "ok", None)
    if ok is None:
        ok = not text.startswith("Error")
    # ★終わっていれば終了の番号を添え、見張りの「終わった」の知らせは出さない（ここで見た・oc/jobwatch）。
    try:
        pid_i = int(str(pid_raw).strip())
        if not _alive(pid_i):
            from . import jobwatch as _jw
            from ..tools import bash as _b
            code = _b.bg_exit_code(pid_i)
            if code is not None:
                text += f"\nexit code: {code}" + (" (it failed)" if code else "")
            log = _jw.redirect_targets(_cmd_of(pid_i), None)
            if log and "(empty)" in text:
                t = _tail_text(log[-1], 3000).strip()
                if t:
                    text += f"\n--- {log[-1]} (where the command wrote its output; last 3000B) ---\n{t}"
            _jw.ack_exit(f"pid:{pid_i}")
    except (TypeError, ValueError):
        pass
    except Exception:
        pass
    return ToolResult(head + text, ok=bool(ok), kind="ok" if ok else "error")


def _cmd_of(pid: int) -> str:
    """この走行で起こした仕事の命令の文（無ければ台帳の控え）。"""
    with _jobs_lock:
        for sc in _job_scopes:
            for j in sc:
                if j["pid"] == pid:
                    return j.get("cmd") or ""
    try:
        import json
        from .. import procutil
        meta = procutil.bg_dir(create=False) / f"{pid}.meta.json"
        return str(json.loads(meta.read_text(encoding="utf-8")).get("cmd") or "")
    except Exception:
        return ""


DESCRIPTION = f"""Read the output of a command started with Bash run_in_background=true, wait for it, or stop it.

- pid: the pid that Bash returned, or a job id like J1 (a command that kept running in the background). Returns the end of its output and whether it is still running (a job shows its output when it ends).
- wait: seconds to wait before reading (up to {MAX_WAIT}). You usually do not need it: you are told automatically when a background job ends or stops making progress. Waiting here does not use the model, so one long wait is much cheaper than many short checks. It returns early when the process exits, when `until` appears in its output (also in a log file the command writes to), when the job stops making progress or another background job ends, or when the user sends a message (answer it first; the job keeps running). The result says how much output and CPU the job produced during the wait. Do not poll with sleep, timeout or Start-Sleep commands.
- until: a regular expression; stop waiting as soon as it appears in the output (for example "Saved model|Traceback|Error").
- kill: true stops the process and its children.
- tail: bytes of output to return (default 4000).
- port: instead of pid, show the process listening on that TCP port (with kill=true, stop it if it belongs to this workspace)."""

SCHEMA_PROPS = {
    "pid": {"type": ["integer", "string"], "description": "The pid returned by Bash run_in_background=true, or a job id like J1"},
    "wait": {"type": "number", "description": f"Seconds to wait first (0 to {MAX_WAIT}); returns early when the process exits or `until` matches"},
    "until": {"type": "string", "description": "Regular expression that ends the wait when it appears in the output"},
    "kill": {"type": "boolean", "description": "Stop the process and its children"},
    "tail": {"type": "integer", "description": "Bytes of output to return (default 4000)"},
    "port": {"type": ["integer", "string"], "description": "TCP port, instead of pid"},
}
