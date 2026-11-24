"""背景の仕事の見張り — 終わった・進まなくなった・入力を待っている、を知らせる（2026-10-28）。

オーナー「Blackwall を Opus とかで動かしていたんですが、スクリプトのプロセスが途中で止まっているのに気づかなかったりするので、
Claude Code のエージェントの監視の仕組みをローカル LLM 用・Blackwall 用に最適化して導入してほしい」。

走行記録（10-26・10-27 の Opus の走行）: 背景の特徴量の抽出・学習を BashOutput(wait=1800) で 30 分ずつ 2〜3 回待ち、
待ちの間は何も分からなかった（出力はログへ回してあり BashOutput は (empty)・進んでいるかはモデルが重みのファイルを数えて
確かめていた）。固まっていても 30 分は気づかない。run_in_background の仕事は終わっても、見に行くまで誰も知らせなかった。

Claude Code の仕組みに合わせたこと:
- 背景の仕事が終わったら知らせる（終了の番号・かかった時間・出力の末尾）。モデルが見に行かなくても次の区切りで届く。
- 進まなくなったら知らせる（出力も CPU も stall_sec() 秒動かない＝固まった・入力待ち・ロック・通信待ち）。
  入力を求める文（[y/N]・Password: など）で止まっていれば、早めに知らせる。
- 出力をファイルへ回している命令（`> train.log 2>&1`・`| tee`・`*>`・Out-File）は、そのファイルも出力として見る。
ローカル LLM 向けに変えたこと:
- 知らせは道具の結果の末尾に添える（新しい発言を挟まない＝前置きの使い回しを崩さない）。1 つの出来事は 1 回だけ。
- 進んでいるかは CPU と出力の両方で見る（何も書かずに計算し続ける台本を「固まった」と言わない）。
- 待ち（BashOutput・Monitor の wait）と、走行を閉じずに待つ所（loop._park_for_jobs）は、ここの知らせで起きる。

★黙らない: CPU が測れない機械では「固まった」を言わない（言えるのは出力が止まったことだけ＝計算中と区別できない）。
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

PROMPT_SEC = 20.0        # 入力を求める文で止まってから知らせるまで（秒）
CPU_EPS = 1.0            # この秒数の CPU を使っていれば「動いている」
_SAMPLE_EVERY = 2.0      # 1 つの仕事を測る間隔（秒）
TAIL_CHARS = 1200


def stall_sec() -> float:
    """出力も CPU も動かないまま、この秒数が過ぎたら「進んでいない」と知らせる。LAPS_STALL_SEC（既定 300・0 で切る）。"""
    try:
        return max(0.0, float(os.environ.get("LAPS_STALL_SEC", "300") or 0))
    except ValueError:
        return 300.0


def park_on() -> bool:
    """答えを書いても背景の仕事が残っていれば、走行を閉じずに待つ（loop._park_for_jobs）。LAPS_BG_PARK（既定 1・0 で切る）。"""
    return (os.environ.get("LAPS_BG_PARK") or "1").strip().lower() not in ("0", "off", "false", "no")


def notify_hint() -> str:
    """背景に回した仕事の結果に添える一言（モデルへ）。"""
    if park_on():
        return ("You will be told automatically when it ends, stops making progress, or waits for input, so you do not "
                "need to poll or wait for it. Keep working on something else, or simply end your turn: the job keeps "
                "running, the user can talk to you meanwhile, and you are woken with its result.")
    return "You will be told automatically when it ends or stops making progress."


# ── 出力をファイルへ回している命令 ─────────────────────────────────────────

_NULLS = {"nul", "/dev/null", "$null", "null", "nul:"}
_REDIR_RX = re.compile(
    r"(?:^|[\s;&|(])(?:[12*&]?>>?|>>?)\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s\"'&|;<>()]+))")
_TEE_RX = re.compile(
    r"\|\s*(?:tee|Tee-Object)\b(?:\s+-(?:a|-append|Append|FilePath))*\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s\"'&|;<>()]+))", re.I)
_OUTFILE_RX = re.compile(
    r"\|\s*(?:Out-File|Add-Content|Set-Content)\b(?:\s+-FilePath|\s+-Path)?\s+(?:\"([^\"]+)\"|'([^']+)'|([^\s\"'&|;<>()-][^\s\"'&|;<>()]*))",
    re.I)


def redirect_targets(cmd: str, cwd: str | None = None, limit: int = 3) -> list[str]:
    """命令が出力を書き込むファイル（`> f`・`>> f`・`2> f`・`&> f`・`*> f`・`| tee f`・`| Out-File f`）。
    `2>&1` のような向け先の付け替えと、捨てる先（nul・/dev/null・$null）は除く。相対の道は cwd から。"""
    out: list[str] = []
    for rx in (_REDIR_RX, _TEE_RX, _OUTFILE_RX):
        for m in rx.finditer(cmd or ""):
            p = next((g for g in m.groups() if g), "")
            if not p or p.startswith("&") or p.lower() in _NULLS or p.isdigit():
                continue
            if not os.path.isabs(p) and not re.match(r"^[A-Za-z]:[\\/]", p):
                p = os.path.join(cwd or os.getcwd(), p)
            p = os.path.normpath(p)
            if p not in out:
                out.append(p)
    return out[-limit:]


# ── プロセスの木の生死と CPU 時間 ─────────────────────────────────────────

def _tree_posix(roots: list[int]) -> tuple[bool, float | None, int]:
    """roots（それぞれが自分のプロセスグループの長）の組と子孫の、生死・CPU 秒・数。/proc/*/stat を 1 回なめる。"""
    try:
        tck = float(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError, AttributeError):
        tck = 100.0
    rows: dict[int, tuple[int, int, str, float]] = {}       # pid -> (ppid, pgid, state, cpu)
    try:
        names = os.listdir("/proc")
    except OSError:
        return False, None, 0
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(f"/proc/{n}/stat", "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        r = raw.rfind(")")
        f = raw[r + 2:].split()
        if len(f) < 15:
            continue
        try:
            # f[0]=state f[1]=ppid f[2]=pgrp … f[11..14]=utime stime cutime cstime
            cpu = sum(int(x) for x in f[11:15]) / tck
            rows[int(n)] = (int(f[1]), int(f[2]), f[0], cpu)
        except ValueError:
            continue
    want = set(int(p) for p in roots if p)
    members = {pid for pid, (_pp, pg, _st, _c) in rows.items() if pg in want or pid in want}
    grew = True
    while grew:                                   # setsid で組を抜けた子孫も親の鎖で拾う
        grew = False
        for pid, (pp, _pg, _st, _c) in rows.items():
            if pp in members and pid not in members:
                members.add(pid)
                grew = True
    live = [p for p in members if rows[p][2] not in ("Z", "X")]
    cpu = sum(rows[p][3] for p in members)
    return bool(live), cpu, len(live)


def _tree_windows(roots: list[int]) -> tuple[bool, float | None, int]:
    """Toolhelp の一覧から roots の子孫をたどり、生死と CPU 秒（kernel+user）を足す（oc/apptool の出荷済みの ctypes を使う）。
    ★pid は使い回されるので、子は親より後に作られた物だけを数える（作られた時刻で確かめる）。"""
    from . import apptool as _ap
    import ctypes
    from ctypes import wintypes
    k32 = _ap._k32

    def times(pid: int) -> tuple[bool, int | None, float | None]:
        h = k32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False, None, None
        try:
            code = wintypes.DWORD()
            alive = bool(k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259    # STILL_ACTIVE
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return alive, None, None
            ft = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime      # noqa: E731
            return alive, ft(c), (ft(k) + ft(u)) / 1e7
        finally:
            k32.CloseHandle(h)

    procs = _ap._procs()
    kids: dict[int, list[int]] = {}
    for pid, ppid, _exe in procs:
        kids.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    alive_n, cpu, known = 0, 0.0, False
    stack = [(int(p), None) for p in roots if p]
    while stack:
        pid, parent_ct = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        alive, ct, c = times(pid)
        if parent_ct is not None and ct is not None and ct < parent_ct:
            continue                                         # 親より前に作られた＝番号を使い回した別人
        if c is not None:
            cpu += c
            known = True
        if alive:
            alive_n += 1
        for ch in kids.get(pid, ()):
            stack.append((ch, ct if ct is not None else parent_ct))
    return alive_n > 0, (cpu if known else None), alive_n


def tree(roots: list[int]) -> tuple[bool, float | None, int]:
    """(生きている物があるか, CPU 秒の合計（測れなければ None）, 生きている数)。"""
    try:
        if sys.platform == "win32":
            return _tree_windows(roots)
        return _tree_posix(roots)
    except Exception:  # noqa: BLE001 — 見張りで走行を殺さない
        return True, None, 0


# ── 仕事の台帳 ──────────────────────────────────────────────────────────

@dataclass
class _Job:
    key: str
    label: str
    cmd: str
    scope: int
    t0: float
    roots: Callable[[], list[int]]
    files: list[str]
    extra_size: Callable[[], int] | None = None
    tail_fn: Callable[[], str] | None = None
    exit_code: Callable[[], int | None] | None = None
    ended_fn: Callable[[], bool] | None = None
    report_exit: bool = True
    check_hint: str = ""
    size: int = -1
    cpu_mark: float | None = None
    cpu_known: bool = False
    progress_t: float = 0.0
    out_t: float = 0.0
    sampled_t: float = 0.0
    stalled: bool = False
    stalled_t: float = 0.0
    prompted: bool = False
    ended: bool = False
    end_reported: bool = False
    end_t: float = 0.0


_lock = threading.RLock()
_jobs: dict[str, _Job] = {}
_scopes: list[int] = []
_pending: list[dict] = []
_seq = 0


def begin_run() -> int:
    global _seq
    with _lock:
        _seq += 1
        _scopes.append(_seq)
        return _seq


def end_run() -> None:
    """この走行の範囲を閉じる（仕事を止めるのは呼ぶ側・bgwait.end_run と loop._jobs_end）。届けていない知らせも捨てる。"""
    with _lock:
        if not _scopes:
            return
        sc = _scopes.pop()
        for k in [k for k, j in _jobs.items() if j.scope == sc]:
            del _jobs[k]
        _pending[:] = [e for e in _pending if e.get("scope") != sc]


def _scope() -> int:
    with _lock:
        return _scopes[-1] if _scopes else 0


def track(key: str, *, label: str, cmd: str, roots: Callable[[], list[int]], files: list[str] | None = None,
          cwd: str | None = None, extra_size: Callable[[], int] | None = None, tail: Callable[[], str] | None = None,
          exit_code: Callable[[], int | None] | None = None, ended: Callable[[], bool] | None = None,
          report_exit: bool = True, check_hint: str = "", t0: float | None = None) -> None:
    """仕事を見張りに載せる。files は出力のファイル（命令の書き込み先も足す）。report_exit=False は終わりを別の口が知らせる物。
    ★走行の外（範囲が無い・bwtool や MCP から起こした物）は載せない（誰も受け取らない知らせが溜まり、後の走行の待ちを切っていた）。"""
    with _lock:
        if not _scopes:
            return
    fs = list(files or [])
    for p in redirect_targets(cmd, cwd):
        if p not in fs:
            fs.append(p)
    now = time.time()
    j = _Job(key=key, label=label, cmd=cmd, scope=_scope(), t0=t0 or now, roots=roots, files=fs,
             extra_size=extra_size, tail_fn=tail, exit_code=exit_code, ended_fn=ended, report_exit=report_exit,
             check_hint=check_hint, progress_t=now, out_t=now)
    with _lock:
        _jobs[key] = j


def untrack(key: str) -> None:
    with _lock:
        _jobs.pop(key, None)
        _pending[:] = [e for e in _pending if e.get("key") != key]


def ack_exit(key: str) -> None:
    """モデルが終わりを自分で見た（BashOutput の結果に出た）＝終わりの知らせは要らない。"""
    with _lock:
        j = _jobs.get(key)
        if j is not None and j.ended:
            j.end_reported = True
        _pending[:] = [e for e in _pending if not (e.get("key") == key and e.get("kind") == "exit")]


def running_labels(scope_only: bool = True) -> list[str]:
    sc = _scope()
    with _lock:
        return [j.label for j in _jobs.values() if not j.ended and (not scope_only or j.scope == sc)]


def _size(j: _Job) -> int:
    n = 0
    for p in j.files:
        try:
            n += os.path.getsize(p)
        except OSError:
            pass
    if j.extra_size is not None:
        try:
            n += int(j.extra_size() or 0)
        except Exception:
            pass
    return n


def _read_tail(path: str, n: int = 4096) -> str:
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(-n, os.SEEK_END)
            except OSError:
                fh.seek(0)
            raw = fh.read()
    except OSError:
        return ""
    try:
        from ..tools.bash import _decode_out
        return _decode_out(raw)
    except Exception:
        return raw.decode("utf-8", "replace")


def tail_text(j: _Job, chars: int = TAIL_CHARS) -> str:
    """出力の末尾（いちばん新しく書かれたファイルから）。"""
    parts: list[tuple[float, str]] = []
    if j.tail_fn is not None:
        try:
            t = j.tail_fn() or ""
            if t.strip():
                parts.append((time.time(), t))
        except Exception:
            pass
    for p in j.files:
        t = _read_tail(p)
        if t.strip():
            try:
                parts.append((os.path.getmtime(p), t))
            except OSError:
                parts.append((0.0, t))
    if not parts:
        return ""
    parts.sort(key=lambda x: x[0])
    t = parts[-1][1].replace("\r\n", "\n")
    t = "\n".join(ln.rsplit("\r", 1)[-1] for ln in t.split("\n"))     # 進み具合の \r 上書きは最後の姿だけ
    t = t.strip("\n")
    return t[-chars:]


_PROMPT_RX = re.compile(
    r"(?i)(\[\s*[yn]\s*/\s*[yn]\s*\]|\(\s*[yn]\s*/\s*[yn]\s*\)|\byes/no\b|password|passphrase|press (?:any key|enter|return)"
    r"|続行するには|上書きしますか|よろしいですか|\?\s*$|？\s*$)")


def _waiting_prompt(j: _Job) -> str:
    """出力の最後が改行の無い問いかけで終わっていれば、その行。"""
    t = ""
    for p in j.files:
        t2 = _read_tail(p, 512)
        if t2:
            t = t2
    if not t and j.tail_fn is not None:
        try:
            t = (j.tail_fn() or "")[-512:]
        except Exception:
            t = ""
    if not t or t.endswith("\n"):
        return ""
    last = t.replace("\r\n", "\n").split("\n")[-1].rsplit("\r", 1)[-1].strip()
    return last[-160:] if last and _PROMPT_RX.search(last) else ""


def _fmt(s: float) -> str:
    s = int(max(0, s))
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _short(cmd: str, n: int = 100) -> str:
    c = " ".join((cmd or "").split())
    return c if len(c) <= n else c[:n] + "…"


def _event(j: _Job, kind: str, text: str, *, alert: bool, wake: bool, notice: str) -> dict:
    return {"key": j.key, "scope": j.scope, "kind": kind, "text": text, "alert": alert, "wake": wake,
            "notice": notice, "label": j.label}


def _check(j: _Job, now: float, force: bool = False) -> list[dict]:
    out: list[dict] = []
    if j.ended:
        return out
    if not force and now - j.sampled_t < _SAMPLE_EVERY:
        return out
    j.sampled_t = now
    ended_now = False
    if j.ended_fn is not None:
        try:
            ended_now = bool(j.ended_fn())
        except Exception:
            ended_now = False
    try:
        roots = [int(p) for p in (j.roots() or []) if p]
    except Exception:
        roots = []
    alive, cpu, _n = tree(roots) if roots else (not ended_now, None, 0)
    if ended_now or not alive:
        j.ended, j.end_t = True, now
        if j.report_exit and not j.end_reported:
            code = None
            if j.exit_code is not None:
                try:
                    code = j.exit_code()
                except Exception:
                    code = None
            tail = tail_text(j)
            head = (f"[Harness] Background job {j.label} (`{_short(j.cmd)}`) ended after {_fmt(now - j.t0)}"
                    + (f" with exit code {code}." if code is not None else "."))
            body = head + ("\nLast output:\n" + tail if tail else "\n(It wrote no output.)")
            failed = code not in (None, 0)
            if failed:
                body += ("\n→ It failed. Find the cause from the output above and fix it, or tell the user why it is "
                         "expected. Do not report the task as done while this is unresolved.")
            elif code is None:
                body += "\n→ Check from the output above that it finished as intended before you rely on it."
            out.append(_event(j, "exit", body, alert=failed, wake=True,
                              notice=f"{j.label} ended" + (f" (exit {code})" if code is not None else "")
                              + f" after {_fmt(now - j.t0)}"))
        return out
    size = _size(j)
    if j.size < 0:
        j.size = size
    elif size != j.size:
        j.size = size
        j.out_t = j.progress_t = now
    if cpu is not None:
        j.cpu_known = True
        if j.cpu_mark is None:
            j.cpu_mark = cpu
        elif cpu - j.cpu_mark >= CPU_EPS:
            j.cpu_mark = cpu
            j.progress_t = now
    if j.stalled and j.progress_t > j.stalled_t:
        j.stalled = False
        out.append(_event(j, "resumed", f"[Harness] Background job {j.label} is making progress again "
                                        f"(it was idle for {_fmt(j.progress_t - j.stalled_t + stall_sec())}).",
                          alert=False, wake=False, notice=f"{j.label} is making progress again"))
    if j.prompted and j.out_t > j.stalled_t:
        j.prompted = False
    idle = now - j.progress_t
    hint = j.check_hint or f"its output"
    if not j.prompted and not j.stalled and idle >= PROMPT_SEC and j.cpu_known:
        ask = _waiting_prompt(j)
        if ask:
            j.prompted, j.stalled_t = True, now
            out.append(_event(j, "prompt", (
                f"[Harness] ⚠ Background job {j.label} (`{_short(j.cmd)}`) seems to be waiting for input: its output "
                f"ends with \"{ask}\" and it has done nothing for {_fmt(idle)}. A background job cannot get an answer "
                f"(its input is closed), so it will not continue. Stop it and run it again non-interactively (a --yes "
                f"/ -y / --no-input option, an answer piped in, or the setting in a file)."),
                alert=True, wake=True, notice=f"{j.label} seems to wait for input: {ask[:60]}"))
            return out
    st = stall_sec()
    if st > 0 and not j.stalled and not j.prompted and idle >= st and j.cpu_known:
        j.stalled, j.stalled_t = True, now
        tail = tail_text(j, 600)
        out.append(_event(j, "stall", (
            f"[Harness] ⚠ Background job {j.label} (`{_short(j.cmd)}`) has made no progress for {_fmt(idle)}: no new "
            f"output and no CPU use (running {_fmt(now - j.t0)} in all). It may be stuck — waiting for input, a lock, "
            f"the network or another machine."
            + ("\nLast output:\n" + tail if tail else "\n(No output yet.)")
            + f"\n→ Check it now ({hint}). If it is stuck, stop it and fix the cause; if waiting is expected (a slow "
              f"download, a remote command), say so and carry on — you will be told when it moves again."),
            alert=True, wake=True, notice=f"{j.label}: no progress for {_fmt(idle)}"))
    return out


def poll(force: bool = False) -> list[dict]:
    """全部の仕事を測り、新しい出来事を台帳の待ちに積む。積んだ物を返す。force は間隔を待たずに測る（区切りで決めるとき）。"""
    now = time.time()
    with _lock:
        jobs = list(_jobs.values())
    new: list[dict] = []
    for j in jobs:
        try:
            new += _check(j, now, force)
        except Exception:  # noqa: BLE001
            continue
    if new:
        with _lock:
            for e in new:
                if e["kind"] == "exit":
                    j = _jobs.get(e["key"])
                    if j is not None:
                        j.end_reported = True
            _pending.extend(new)
    return new


def _mine(e: dict, sc: int) -> bool:
    return e.get("scope") == sc


def has_wake(exclude_key: str | None = None, force: bool = False) -> dict | None:
    """この走行の、起こすべき知らせ（終わり・停滞・入力待ち）が待っていれば 1 件（取り出さない）。"""
    poll(force)
    sc = _scope()
    with _lock:
        for e in _pending:
            if _mine(e, sc) and e.get("wake") and e.get("key") != exclude_key:
                return e
    return None


def take(keys: set[str] | None = None) -> list[dict]:
    """この走行の知らせを取り出す（keys を渡せばその仕事の分だけ）。区切りで呼ぶので間隔を待たずに測る。"""
    poll(force=True)
    sc = _scope()
    with _lock:
        got = [e for e in _pending if _mine(e, sc) and (keys is None or e.get("key") in keys)]
        _pending[:] = [e for e in _pending if e not in got]
    return got


def measure(key: str) -> dict | None:
    """いまの出力の量・CPU 秒・最後に進んだ時刻（待ちの結果に「待つ間に何が起きたか」を書くため）。"""
    with _lock:
        j = _jobs.get(key)
    if j is None:
        return None
    try:
        roots = [int(p) for p in (j.roots() or []) if p]
    except Exception:
        roots = []
    _alive, cpu, _n = tree(roots) if roots else (True, None, 0)
    return {"size": _size(j), "cpu": cpu, "t": time.time(), "out_t": j.out_t, "progress_t": j.progress_t}


def progress_line(key: str, before: dict | None) -> str:
    """待つ前と後を比べた 1 行。測れなければ ""。"""
    if not before:
        return ""
    after = measure(key)
    if not after:
        return ""
    secs = after["t"] - before["t"]
    if secs < 5:
        return ""
    d_out = after["size"] - before["size"]
    bits = [f"output +{_bytes(d_out)}" if d_out > 0 else "no new output"]
    if before.get("cpu") is not None and after.get("cpu") is not None:
        d_cpu = after["cpu"] - before["cpu"]
        bits.append(f"CPU +{d_cpu:.0f} s ({100 * d_cpu / secs:.0f}% of one core)" if d_cpu >= 0.5 else "almost no CPU")
    last = time.time() - after["out_t"]
    if d_out <= 0 and last > 60:
        bits.append(f"last output {_fmt(last)} ago")
    return f"During the wait ({_fmt(secs)}): " + ", ".join(bits) + "."


def _bytes(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.1f} KB" if n >= 1024 else f"{n} B"
