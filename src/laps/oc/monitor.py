"""見張り（Monitor）— 命令の出力やログファイルに新しく出た行を、作業の合間にエージェントへ届ける（2026-10-08）。

オーナー「プログラムログなどの Monitor 監視機能が必要です」。Claude Code の Monitor と同じ考え方:
- 見張りを立てたら、エージェントは他の作業を続けてよい。新しく出た行（`filter` に合う行だけ）は、
  道具の結果のあと＝次の手番の前に「[Monitor m1 …] 新しい行」として届く。
- 待つことしか無いなら `Monitor(wait=秒)`。道具の中で眠るだけで、推論機は使わない
  （BashOutput(wait) と同じ。1 回確かめるたびに 1 手番＝平均 約 75 秒の推論を使うのを避ける）。
- 見張る物は 2 種類: 命令の出力（`command`・サーバ・`tail -f` など終わらない命令）と、ファイルの追記（`file`・ログ）。
  ファイルは Python で読む（Windows に `tail -f` が無い・grep の行の溜め込みで行が届かない、を避ける）。
- 走行が終わると見張りは止める（取り残しの `tail -f` を残さない）。走行をまたいで見張り続けることはしない
  （誰も読まない行を溜めない。利用者の次の依頼で立て直す）。

★黙らない: 行が多すぎて捨てたら数を言う・見張りが終わったら理由を言う・読めなかったら読めなかったと言う。

★エラーに対応させる（オーナー「監視中にエラーが出た場合などもモデルが対応できるように」）:
- エラーらしい行（Error・Exception・Traceback・FATAL・panic・failed・エラー・失敗 …）は `filter` に合わなくても届ける
  （絞り込みを成功の行だけにして、落ちたことに気づかない、を作らない。`include_errors=false` で切れる）。
- エラーらしい行が混ざった便・見張っていた命令が 0 以外で終わった便は「⚠」を付け、「いま原因を調べて直す（想定内なら
  そう言う）。片付くまで完了と言わない」と添える。画面には失敗の色（kind=monitor_error）で出す。
- 命令の終了の番号は、Linux では命令の後ろに書き出させて受け取る（背景の命令は誰も待たないので、他に知る手が無い）。
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

from ..tools import ToolResult

DEFAULT_TIMEOUT = 1800        # 秒。見張りの寿命の既定（走行が先に終われば、そこで止まる）
MAX_TIMEOUT = 4 * 3600
MAX_WAIT = 1800               # Monitor(wait=…) の 1 回の上限（BashOutput と同じ）
MAX_LINES_PER_DELIVERY = 30   # 1 回に届ける行の上限（多すぎる分は数だけ言う）
MAX_LINE_CHARS = 400
FLOOD_LINES = 400             # これだけの行が FLOOD_SECS の間に合ったら、絞り込みが緩すぎる＝止める
FLOOD_SECS = 60.0
_READ_CHUNK = 1 << 20
_POLL = 1.0
_EXIT_MARK = "__bw_monitor_exit="
# エラーらしい行。★広めに取る（取りこぼすより、モデルが「想定内」と判断するほうが安い）。
# ★2026-11-13 に狭めた（実際の行で試した）: `error_rate=0.012`・`error_analysis.json`・`timeout=30s`・`Using timeout 600` を
#   エラーと読み、「いま直せ・完了と言うな」と添えていた。エラーらしい行は絞り込みも通るので、学習のログが毎行 error_rate を
#   出すと全部届いて見張りが止まる。語の後ろが名前の続き（_・パス・拡張子）なら語ではない。timeout は設定の値と区別する。
ERROR_RX = re.compile(
    r"(?i)\b(?:errors?|exception|traceback|fatal|panic|critical|segmentation fault|"
    r"failed|failures?|unhandled|denied|refused|oom|killed|err!)(?![a-z_/\\]|\.[a-z])"
    r"|(?i:\btimed out\b|\btimeout(?![a-z_])(?!\s*[=:]?\s*\d)|out of memory|no space left|core dumped)"
    r"|エラー|失敗|例外|異常終了"
    r"|[a-z0-9_.](?:Error|Exception|Fault)\b")      # ValueError・NullPointerException（語の切れ目が無い）
# 数が 0 の集計はエラーではない（試験や構築の集計）: 「0 errors」「no errors」「Errors: 0」「failed=0」「killed=0」。
_ZERO_RX = re.compile(r"(?i)\b(?:0|no|zero) (?:errors?|failed|failures?)\b"
                      r"|\b(?:errors?|failed|failures?|killed|error count)\s*[=:]\s*0\b(?![.\d])")


def looks_like_error(line: str) -> bool:
    if not ERROR_RX.search(line):
        return False
    if _ZERO_RX.search(line) and not ERROR_RX.search(_ZERO_RX.sub("", line)):
        return False
    return True


@dataclass
class _Watch:
    id: str
    source: str                       # "command" | "file"
    target: str                       # 命令の文字列か、ファイルの道
    description: str
    rx: re.Pattern | None
    deadline: float
    include_errors: bool = True
    timeout: float = DEFAULT_TIMEOUT
    pid: int | None = None
    paths: list[str] = field(default_factory=list)
    offsets: dict[str, int] = field(default_factory=dict)
    partial: dict[str, bytes] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)
    pending_errors: int = 0
    dropped: int = 0
    exit_code: int | None = None
    total: int = 0
    recent: list[float] = field(default_factory=list)
    ended: str | None = None           # 終わった理由（届けるまで覚えておく）
    end_reported: bool = False
    read_error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.id} · {self.description}" if self.description else self.id


_lock = threading.RLock()
_scopes: list[list[_Watch]] = []      # 走行ごとの見張り（委譲した子の走行は上に積む）
_seq = 0


def _decode(raw: bytes) -> str:
    try:
        from ..tools.bash import _decode_out
        return _decode_out(raw)
    except Exception:
        return raw.decode("utf-8", "replace")


def _top() -> list[_Watch]:
    if not _scopes:
        _scopes.append([])
    return _scopes[-1]


# ── 走行の始まりと終わり（oc/loop.py が呼ぶ）──────────────────────────────

def begin_run() -> None:
    with _lock:
        _scopes.append([])


def end_run() -> int:
    """この走行で立てた見張りを全部止める。止めた（まだ動いていた）数を返す。"""
    with _lock:
        watches = _scopes.pop() if _scopes else []
    n = 0
    for w in watches:
        if w.ended is None:
            n += 1
        _stop(w, "the run ended")
    return n


def active_count() -> int:
    with _lock:
        return sum(1 for w in _top() if w.ended is None)


def running_labels() -> list[str]:
    with _lock:
        return [w.label for w in _top() if w.ended is None]


# ── 見張りの中身 ──────────────────────────────────────────────────────

def _stop(w: _Watch, reason: str) -> None:
    if w.ended is None:
        w.ended = reason
    if w.source == "command" and w.pid:
        try:
            from ..tools import bash_output as _bo
            if _bo._is_alive(w.pid):
                _bo._run({"pid": w.pid, "kill": True})
        except Exception:
            pass


def _read_new(w: _Watch, path: str, final: bool) -> None:
    """[path] の前回の続きから読み、完結した行を pending へ積む。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        if w.source == "file" and w.offsets.get(path, 0) > 0:
            # 回転（消されて作り直される途中）か削除。現れたら頭から読む。
            w.offsets[path] = 0
            w.partial[path] = b""
        return
    off = w.offsets.get(path, 0)
    if size < off:                    # 切り詰められた・回転した＝頭から読み直す
        off = 0
        w.partial[path] = b""
    if size == off and not final:
        return
    try:
        with open(path, "rb") as fh:
            fh.seek(off)
            raw = fh.read(_READ_CHUNK)
    except OSError as e:
        w.read_error = f"cannot read {path}: {e}"
        return
    w.read_error = None
    w.offsets[path] = off + len(raw)
    buf = w.partial.get(path, b"") + raw
    parts = buf.split(b"\n")
    rest = parts.pop()
    if final and rest.strip():
        parts.append(rest)
        rest = b""
    w.partial[path] = rest[-65536:]
    now = time.time()
    for p in parts:
        line = _decode(p).rstrip("\r")
        if not line.strip():
            continue
        if line.startswith(_EXIT_MARK):
            try:
                w.exit_code = int(line[len(_EXIT_MARK):].strip())
            except ValueError:
                pass
            continue
        is_err = looks_like_error(line)
        if w.rx is not None and not w.rx.search(line) and not (w.include_errors and is_err):
            continue
        if is_err:
            w.pending_errors += 1
        w.total += 1
        w.recent.append(now)
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + "…"
        if len(w.pending) < MAX_LINES_PER_DELIVERY:
            w.pending.append(line)
        else:
            w.dropped += 1


def _poll(w: _Watch) -> None:
    if w.ended is not None and w.end_reported:
        return
    alive = True
    if w.source == "command" and w.pid:
        code = None
        if sys.platform == "win32":
            # ★Windows は終了の番号を出力に書かせていない（下の _start）。起こしたプロセスから直に取る（2026-11-13: 落ちても
            #   ⚠ にならず、生死も毎回 tasklist を走らせていた。pid が使い回されると終わった後も動いて見える）。
            try:
                from ..tools import bash as _b
                code = _b.bg_exit_code(w.pid)
            except Exception:
                code = None
        if code is not None:
            alive = False
            if w.ended is None and w.exit_code is None:   # 自分で止めた後の番号（taskkill の 1）は失敗と読まない
                w.exit_code = int(code)
        else:
            try:
                from ..tools import bash_output as _bo
                alive = _bo._is_alive(w.pid)
            except Exception:
                alive = True
    for p in w.paths:
        _read_new(w, p, final=not alive)
    now = time.time()
    w.recent = [t for t in w.recent if now - t <= FLOOD_SECS]
    if w.ended is None:
        if not alive:
            w.ended = ("the command exited" if w.exit_code is None
                       else f"the command exited with exit code {w.exit_code}")
        elif len(w.recent) >= FLOOD_LINES:
            _stop(w, f"stopped: {len(w.recent)} matching lines in {int(FLOOD_SECS)} s — narrow `filter` and start it again")
        elif now >= w.deadline:
            _stop(w, f"timed out after {int(w.timeout)} s")


def _tail_lines(w: _Watch, n: int = 6) -> list[str]:
    """命令が合う行を 1 つも出さずに終わったとき、なぜ終わったかの手掛かり（出力の末尾・絞り込み前）。"""
    out: list[str] = []
    for p in w.paths:
        try:
            with open(p, "rb") as fh:
                try:
                    fh.seek(-4096, os.SEEK_END)
                except OSError:
                    fh.seek(0)
                raw = fh.read()
        except OSError:
            continue
        out += [ln.rstrip("\r")[:MAX_LINE_CHARS] for ln in _decode(raw).splitlines()
                if ln.strip() and not ln.startswith(_EXIT_MARK)]
    return out[-n:]


def _crashed(w: _Watch) -> bool:
    return w.exit_code is not None and w.exit_code != 0


def _block(w: _Watch) -> str | None:
    """届ける 1 通（新しい行と、終わったなら終わった理由）。何も無ければ None。"""
    if not w.pending and not w.dropped and (w.ended is None or w.end_reported) and not w.read_error:
        return None
    head = f"[Monitor {w.label}]"
    lines: list[str] = []
    alert = w.pending_errors > 0
    if w.pending:
        n = len(w.pending) + w.dropped
        lines.append(f"{head} ⚠ {n} new line(s), {w.pending_errors} look like errors:" if alert
                     else f"{head} {n} new line(s):")
        lines.extend(w.pending)
        if w.dropped:
            lines.append(f"(+{w.dropped} more matching lines not shown — read the source directly if you need them)")
    if w.read_error and not w.pending:
        lines.append(f"{head} {w.read_error}")
    if w.ended is not None and not w.end_reported:
        lines.append(f"{head} the monitor ended: {w.ended}." + (
            "" if w.total else " No matching line was seen."))
        if w.source == "command" and w.ended.startswith("the command exited") and (not w.total or _crashed(w)):
            tail = _tail_lines(w)
            if tail:
                lines.append("Last output (unfiltered):")
                lines.extend(tail)
        if _crashed(w):
            alert = True
            lines.append("→ The watched program stopped with an error. Find out why from the output above, fix the "
                         "cause, and start it (and a new monitor) again if it should be running. Do not report the "
                         "task as done while this is unresolved.")
        w.end_reported = True
    if alert and w.pending_errors and not _crashed(w):
        lines.append("→ Deal with this now: find the cause of the error line(s) above and fix it, or say why they "
                     "are expected. Do not report the task as done while it is unresolved.")
    if alert:
        try:
            from . import skilltool
            if skilltool.enabled():
                lines.append("  (The `debugging` skill has a step-by-step procedure; load it with the Skill tool "
                             "if you are unsure how to proceed.)")
        except Exception:
            pass
    w.pending = []
    w.pending_errors = 0
    w.dropped = 0
    return "\n".join(lines) if lines else None


def take_events() -> list[dict]:
    """全部の見張りを読み、届ける物を返す。1 件 = {label, text（推論機へ）, lines（行の数）,
    preview（画面に出す先頭の数行）, ended（今回終わったなら理由）}。"""
    out = []
    with _lock:
        watches = list(_top())
    for w in watches:
        try:
            _poll(w)
        except Exception as e:  # noqa: BLE001
            w.read_error = f"{type(e).__name__}: {e}"
        n = len(w.pending) + w.dropped
        errs = w.pending_errors
        preview = [x for x in w.pending if looks_like_error(x)][:3] or list(w.pending[:3])
        ended = w.ended if (w.ended is not None and not w.end_reported) else None
        b = _block(w)
        if b:
            out.append({"label": w.label, "text": b, "lines": n, "preview": preview, "ended": ended,
                        "errors": errs, "alert": bool(errs) or (ended is not None and _crashed(w))})
    return out


def has_ready() -> bool:
    """届ける物があるか（読むだけで、渡さない）。背景の仕事を待って眠っている走行を起こすために使う。"""
    with _lock:
        watches = list(_top())
    for w in watches:
        try:
            _poll(w)
        except Exception as e:  # noqa: BLE001
            w.read_error = f"{type(e).__name__}: {e}"
        if _block_ready(w):
            return True
    return False


# ── 道具 ────────────────────────────────────────────────────────────────

def _start(inp: dict) -> ToolResult:
    global _seq
    from . import tools as _oc
    command = str(inp.get("command") or "").strip()
    file = str(inp.get("file") or "").strip()
    if bool(command) == bool(file):
        return ToolResult("Error: give exactly one of `command` (a command whose output to watch) or `file` "
                          "(a log file to follow).", ok=False, kind="error")
    flt = str(inp.get("filter") or "")
    rx = None
    if flt:
        try:
            rx = re.compile(flt)
        except re.error as e:
            return ToolResult(f"Error: `filter` is not a valid regular expression: {e}", ok=False, kind="error")
    try:
        timeout = float(inp.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    timeout = max(10.0, min(timeout, float(MAX_TIMEOUT)))
    desc = " ".join(str(inp.get("description") or "").split())[:60]
    with _lock:
        _seq += 1
        wid = f"m{_seq}"
    inc = inp.get("include_errors")
    w = _Watch(id=wid, source="command" if command else "file", target=command or file, description=desc,
               rx=rx, deadline=time.time() + timeout, timeout=timeout,
               include_errors=not (inc in (False, "false", "False", 0, "0")))
    cwd = _oc._cwd()
    if command:
        from .. import policy
        try:
            policy.authorize_command(command, cwd)
        except policy.PolicyDenied as e:
            return ToolResult(f"Error: {e}", ok=False, kind="denied")
        except Exception:
            pass
        run_cmd = command
        if sys.platform == "win32":
            try:
                from . import shellfix as _sf
                run_cmd, _ = _sf.prepare_windows(run_cmd)
            except Exception:
                pass
        from ..tools import bash as _b
        from . import bgwait as _bgw
        from .. import procutil
        if sys.platform != "win32":
            # 終了の番号を出力の最後に書かせる（背景の命令は誰も待たないので、他に知る手が無い）。
            # ★`{ …; }` ではなく `( … )`: 命令の中の `exit 2` が包みごと終わらせ、番号を書けなかった（2026-10-08 に試して）。
            run_cmd = "( " + run_cmd + "\n); echo \"" + _EXIT_MARK + "$?\""
        try:
            text = _b._spawn_background(_bgw._win_script(run_cmd) if sys.platform == "win32" else run_cmd, cwd)
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"Error: could not start the command: {e}", ok=False, kind="error")
        m = re.search(r"pid:\s*(\d+)", text)
        if not m:
            return ToolResult(f"Error: the command started but its pid is unknown:\n{text}", ok=False, kind="error")
        w.pid = int(m.group(1))
        d = procutil.bg_dir(create=False)
        w.paths = [str(d / f"{w.pid}.out"), str(d / f"{w.pid}.err")]
        got = [m2.group(1).strip() for m2 in re.finditer(r"(?m)^\s*std(?:out|err):\s*(.+)$", text)]
        if len(got) == 2:                 # 改名できなかったときは一時の名前のまま＝決め打ちの名前では何も届かない
            w.paths = got
        what = f"the output of `{command[:200]}` (pid {w.pid})"
    else:
        path, _note = _oc._fix_path(_oc._abs(file), "existing")
        if os.path.isdir(path):
            return ToolResult(f"Error: {path} is a directory; give a file.", ok=False, kind="error")
        w.paths = [path]
        # ★見張りを立てた時点より後に足された行だけを届ける（既にある中身は Read で読める）。
        try:
            w.offsets[path] = os.path.getsize(path)
        except OSError:
            w.offsets[path] = 0       # まだ無い＝できたら頭から読む
        what = f"new lines appended to {path}" + ("" if os.path.exists(path) else " (the file does not exist yet; "
                                                     "lines are delivered once it appears)")
    with _lock:
        _top().append(w)
    return ToolResult(
        f"Started monitor {wid}: watching {what}"
        + (f", lines matching /{flt}/" if flt else ", every line")
        + (" (lines that look like errors are always delivered)" if flt and w.include_errors else "")
        + f", for up to {int(timeout)} s.\n"
        f"New lines are delivered to you between steps as \"[Monitor {wid} …]\" messages, so keep working. "
        f"To wait for them without using the model, call Monitor(wait=<seconds>). "
        f"Monitor(stop=\"{wid}\") stops it; it also stops when this run ends.",
        ok=True, kind="ok")


def _wait(inp: dict) -> ToolResult:
    from . import tools as _oc
    try:
        wait = max(1.0, min(float(inp.get("wait") or 0), float(MAX_WAIT)))
    except (TypeError, ValueError):
        wait = 60.0
    if not active_count() and not any(_block_ready(w) for w in list(_top())):
        return ToolResult("Error: no monitor is running. Start one with Monitor(command=…) or Monitor(file=…).",
                          ok=False, kind="error")
    t0 = time.time()
    reason = f"the {int(wait)} s wait ended with no new line"
    events: list[dict] = []
    while True:
        events = take_events()
        if events:
            reason = "new output arrived"
            break
        if not active_count():
            reason = "every monitor has ended"
            break
        _why = _oc.interrupted()          # ★取り消しか、新しい発言（2026-10-28 からは普通の一言でも抜ける）
        if _why:
            reason = _why
            break
        try:                              # ★背景の仕事が終わった・進まなくなった（oc/jobwatch・知らせは次の区切りで届く）
            from . import jobwatch as _jw
            _ev = _jw.has_wake()
        except Exception:
            _ev = None
        if _ev is not None:
            reason = f"background job {_ev['label']} needs attention (see the note below)"
            break
        left = t0 + wait - time.time()
        if left <= 0:
            break
        time.sleep(min(_POLL, left))
    head = f"Waited {int(time.time() - t0)} s: {reason}."
    body = "\n\n".join(e["text"] for e in events)
    still = active_count()
    tail = f"\n({still} monitor(s) still running.)" if still else "\n(No monitor is running now.)"
    return ToolResult(head + ("\n" + body if body else "") + tail, ok=True, kind="ok")


def _block_ready(w: _Watch) -> bool:
    return bool(w.pending or w.dropped or (w.ended is not None and not w.end_reported))


def _stop_tool(inp: dict) -> ToolResult:
    which = str(inp.get("stop") or "").strip()
    with _lock:
        watches = list(_top())
    hit = [w for w in watches if which in ("all", "*") or w.id == which]
    if not hit:
        names = ", ".join(w.id for w in watches) or "none"
        return ToolResult(f"Error: no monitor named {which!r} (running in this run: {names}).", ok=False, kind="error")
    out = []
    for w in hit:
        was = w.ended is None
        _stop(w, "stopped by Monitor(stop)")
        try:
            _poll(w)
        except Exception:
            pass
        w.end_reported = True        # 止めたことはこの結果で言う（後で二重に届けない）
        b = w.pending
        w.pending = []
        out.append(f"{w.label}: {'stopped' if was else 'had already ended (' + str(w.ended) + ')'}"
                   f"; {w.total} matching line(s) in total" + ("; last lines:\n" + "\n".join(b[-10:]) if b else ""))
    return ToolResult("\n".join(out), ok=True, kind="ok")


def _bg_out_file(pid) -> str:
    """背景の仕事（Bash run_in_background）の出力ファイル。無ければ ""。"""
    try:
        from .. import procutil
        p = procutil.bg_dir(create=False) / f"{int(str(pid).strip())}.out"
        return str(p) if p.exists() else ""
    except Exception:
        return ""


def monitor_tool(inp: dict) -> ToolResult:
    inp = dict(inp) if isinstance(inp, dict) else {}
    if inp.get("stop") not in (None, "", False):
        return _stop_tool(inp)
    # ★書き方の取り違えを受ける（2026-10-10 オーナーの走行で 3 回とも「見張りが無い」で終わった:
    #   Monitor(pid=…)・Monitor(file_path=…, wait=60)）。file_path/path/log は file、pid は背景の仕事の出力ファイル、
    #   見張りを始める指定と wait が一緒なら、始めてからそのまま待つ。
    if not inp.get("file"):
        for k in ("file_path", "path", "log", "logfile"):
            if inp.get(k):
                inp["file"] = inp[k]
                break
    if not inp.get("file") and not inp.get("command") and inp.get("pid") not in (None, ""):
        f = _bg_out_file(inp.get("pid"))
        if not f:
            return ToolResult(f"Error: no output file for background job pid={inp.get('pid')}. Use BashOutput(pid=…) "
                              "to read a background job, or Monitor(file=<log path>).", ok=False, kind="error")
        inp["file"] = f
    if inp.get("command") or inp.get("file"):
        res = _start(inp)
        if res.ok and inp.get("wait") not in (None, "", 0, "0"):
            w = _wait(inp)
            return ToolResult(f"{res}\n\n{w}", ok=w.ok, kind=w.kind)
        return res
    if inp.get("wait") not in (None, "", 0, "0"):
        return _wait(inp)
    # 何も指定が無い＝いまの見張りの一覧
    with _lock:
        watches = list(_top())
    if not watches:
        return ToolResult("No monitor in this run. Start one with Monitor(command=…) or Monitor(file=…).",
                          ok=True, kind="empty")
    rows = [f"{w.label}: {'running' if w.ended is None else 'ended (' + w.ended + ')'} · {w.source} · "
            f"{w.total} matching line(s)" for w in watches]
    return ToolResult("\n".join(rows), ok=True, kind="ok")


DESCRIPTION = f"""Watch a running program's output or a log file, and get new lines delivered to you while you keep working.

Start a monitor with one of:
- command: a command whose output to watch, e.g. a server, a training run, `tail -f app.log`, `journalctl -fu nginx`, `docker logs -f web`. It runs in the background.
- file: a log file to follow. Only lines appended after the monitor starts are delivered (read the existing part with Read). Works on Windows too; no `tail` or `grep` needed.
Options:
- filter: a regular expression; only matching lines are delivered (e.g. "listening on|Saved model"). Lines that look like errors (Error, Exception, Traceback, FATAL, failed, …) are delivered even when they do not match, so a crash is never silent (include_errors=false turns this off).
- description: a few words shown with each delivery.
- timeout: seconds before the monitor stops by itself (default {DEFAULT_TIMEOUT}, up to {MAX_TIMEOUT}). Every monitor also stops when this run ends.

Matching lines arrive between your steps as "[Monitor m1 · …] N new line(s): …" messages. When some look like errors, or the watched command exits with an error, the message starts with ⚠: stop what you are doing, find the cause and fix it (or explain why it is expected) before you report the task as done (at most {MAX_LINES_PER_DELIVERY} lines each; the rest are counted). A monitor that matches more than {FLOOD_LINES} lines in {int(FLOOD_SECS)} s is stopped — narrow the filter.
- wait: seconds (up to {MAX_WAIT}). If you have nothing else to do until the next line, call Monitor(wait=...). It returns as soon as a line arrives, a monitor ends, or the user interrupts, without using the model. Do not poll with sleep commands.
- stop: a monitor id (e.g. "m1") or "all".
With no arguments, lists the monitors of this run.
For a job you only need to finish, use Bash run_in_background with BashOutput(wait, until) instead."""

SCHEMA_PROPS = {
    "command": {"type": "string", "description": "Command whose output to watch (runs in the background)"},
    "file": {"type": "string", "description": "Log file to follow (new lines only)"},
    "filter": {"type": "string", "description": "Regular expression; deliver only matching lines"},
    "description": {"type": "string", "description": "A few words naming what is watched"},
    "timeout": {"type": "number", "description": f"Seconds before the monitor stops by itself (default {DEFAULT_TIMEOUT})"},
    "include_errors": {"type": "boolean", "description": "Also deliver lines that look like errors even if they do not match filter (default true)"},
    "wait": {"type": "number", "description": f"Wait up to this many seconds for the next line (up to {MAX_WAIT})"},
    "stop": {"type": "string", "description": "Monitor id to stop, or \"all\""},
}
