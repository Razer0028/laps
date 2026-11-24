"""Bash と背景の仕事（手本: opencode の tool/shell.ts と core/shell.ts）。

Windows のシェルは Git Bash → pwsh → powershell → cmd の順に選ぶ。opencode は PowerShell を先に選ぶが、
小さいモデルが書く命令はほとんど POSIX の形（ls・cat・&&）なので、在るなら bash を先にする。
System32 の bash.exe は WSL の起動口なので使わない。"""
from __future__ import annotations

from laps import _envcompat
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field

from ...policy import PolicyDenied, authorize_command
from . import Ctx, Result, Tool, clip, fail, shellaid

DEFAULT_TIMEOUT = 120.0
MAX_TIMEOUT = 3600.0
#: 同じ仕事を 2 回目以降に見に行くときの最短の待ち（秒）。見に行くたびに 1 手番使うのを減らす（2026-10-21 オーナーの画面で BashOutput 22 回）。
#: ★最後の 270 は Claude の控え（5 分）が切れない長さ。300 にすると往復の分だけ必ず切れて、前の部分を全部書き直す（2026-10-31 の記録で 33/34 回）。
POLL_WAITS = (30.0, 60.0, 120.0, 270.0)
KEEP_DONE_JOBS = 32                # 終わった仕事をこの数だけ帳簿に残す（古い物は出力ごと捨てる）
SERVER_POLL_WAIT = 5.0             # サーバは終わらないので延ばさない。ただし待ちなしで繰り返させない
PROMPT_SEC = 20.0                  # 入力を求める文で止まってから知らせるまで
STDIN_FIRST_WAIT = 5.0             # 入力を開けた命令が最初の問いを出すまで待つ上限
SEND_WAIT = 2.0                    # send の後、答えを待つ既定の秒数（出力が出て静まればもっと早く戻る）
CPU_EPS = 1.0                      # この秒数の CPU を使っていれば進んでいると見る
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
WIN = os.name == "nt"
NO_WINDOW = 0x08000000


def _git_bash() -> str:
    git = shutil.which("git")
    cands = []
    if git:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
    for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LocalAppData")):
        if root:
            cands.append(os.path.join(root, "Git", "bin", "bash.exe"))
            cands.append(os.path.join(root, "Programs", "Git", "bin", "bash.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c
    return ""


_shell_cache: tuple[str, str] | None = None


def resolve_shell() -> tuple[str, str]:
    """(種類, 実体)。種類は bash / powershell / cmd。"""
    global _shell_cache
    if _shell_cache is None:
        if not WIN:
            _shell_cache = ("bash", shutil.which("bash") or "/bin/sh")
        else:
            b = _git_bash()
            if b:
                _shell_cache = ("bash", b)
            else:
                ps = shutil.which("pwsh") or shutil.which("powershell")
                _shell_cache = ("powershell", ps) if ps else ("cmd", os.environ.get("ComSpec") or "cmd.exe")
    return _shell_cache


#: 名前にこれを含む環境変数は、モデルが走らせる命令へ渡さない（手本: Codex の protocol/src/shell_environment.rs の既定の除外）。
#: `env` 1 回で鍵が会話の記録に載り、要約や記憶を通って外へ出うる。要る物は LAPS_KEEP_ENV に名前を並べて残す。
_SECRET_NAME = re.compile(r"KEY|SECRET|TOKEN", re.I)


def _scrub_secrets(env: dict) -> None:
    if os.environ.get("LAPS_ENV_SCRUB", "1") == "0":
        return
    keep = {x.strip().upper() for x in (os.environ.get("LAPS_KEEP_ENV") or "").split(",") if x.strip()}
    for name in [k for k in env if _SECRET_NAME.search(k) and k.upper() not in keep]:
        env.pop(name, None)


def child_env() -> dict:
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    user_path = _envcompat.unsetenv("LAPS_USER_PYTHONPATH", env)
    if user_path:
        env["PYTHONPATH"] = user_path
    else:
        env.pop("PYTHONPATH", None)
    env.update(GIT_PAGER="cat", PAGER="cat", GIT_TERMINAL_PROMPT="0", DEBIAN_FRONTEND="noninteractive")
    # 色と端末の飾りを止める（色の文字と進み具合の上書きは、消しても読み込みの量になる）。手本: Codex の
    # core/src/unified_exec/process_manager.rs の UNIFIED_EXEC_ENV。
    env.update(NO_COLOR="1", TERM="dumb")
    env.pop("COLORTERM", None)
    _scrub_secrets(env)
    try:                               # 道具の置き場（知識サーバー の棚から入れた物・bwtool）とレジストリの PATH を足す
        from ...oc.toolenv import child_path
        env["PATH"] = child_path(env.get("PATH", ""))
    except Exception:
        parts = env.get("LAPS_TOOLS_DIR")
        if parts and os.path.isdir(parts):
            env["PATH"] = parts + os.pathsep + env.get("PATH", "")
    return env


#: rc を読んで alias を効かせる（課題が `alias aws=awslocal` を ~/.bashrc に書くだけの環境がある・2026-09）。
#: 対話ガードのある普通の rc はすぐ return する。出力は捨て、失敗しても続ける。
_RC_PRELUDE = ('shopt -s expand_aliases 2>/dev/null || true\n'
               'if [ -n "${HOME:-}" ] && [ -f "$HOME/.bashrc" ]; then\n  . "$HOME/.bashrc" >/dev/null 2>&1 || true\nfi\n')


def _command_argv(kind: str, exe: str, command: str) -> tuple[object, str]:
    """(argv, 終わったら消すファイル)。命令は引数でなくファイルで渡す。
    引数に語が載っていると `pkill -f 語` が自分のシェルに当たって、何も出さずに死ぬ（2026-08-19 に 1 日 4 回・実験で確かめた）。
    cmd.exe は /c の文字列を最初の改行で黙って切るので、複数行もファイルで渡す。書けないときは引数で渡す。"""
    try:
        if kind == "bash":
            fd, path = tempfile.mkstemp(prefix="bwcmd-", suffix=".sh")
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(("" if os.environ.get("LAPS_BASH_RC") == "0" else _RC_PRELUDE) + command + "\n")
            return [exe, path.replace("\\", "/") if WIN else path], path
        if kind == "powershell":
            fd, path = tempfile.mkstemp(prefix="bwcmd-", suffix=".ps1")
            with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:   # BOM が無いと 5.1 が日本語を cp932 で読む
                fh.write("[Console]::OutputEncoding=[Text.Encoding]::UTF8\n$ProgressPreference='SilentlyContinue'\n"
                         + command + "\nif ($LASTEXITCODE) { exit $LASTEXITCODE }\n")
            return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path], path
        if "\n" in command.strip():
            fd, path = tempfile.mkstemp(prefix="bwcmd-", suffix=".cmd")
            body = "\r\n".join(command.strip().splitlines()) + "\r\n"
            try:
                raw = ("@echo off\r\n" + body).encode("cp932")
            except UnicodeEncodeError:
                raw = b"@echo off\r\nchcp 65001 >nul\r\n" + body.encode("utf-8")
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            return f'"{exe}" /d /s /c "{path}"', path
        try:                           # ssh の 1 本だけなら cmd.exe を通さない（cmd は一重引用符を引用符と扱わない）
            from ...oc.shellfix import ssh_direct_argv
            direct = ssh_direct_argv(command)
            if direct:
                return [shutil.which("ssh") or "ssh", *direct[1:]], ""
        except Exception:
            pass
    except OSError:
        pass
    if kind == "bash":
        return [exe, "-c", command], ""
    if kind == "powershell":
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command], ""
    return f'"{exe}" /d /s /c "{command}"', ""


def decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp932") if WIN else data.decode("utf-8", "replace")
        except UnicodeDecodeError:
            text = data.decode("utf-8", "replace")
    text = _ANSI.sub("", text).replace("\r\n", "\n")
    if "\r" in text:                    # 進み具合の上書き表示は最後の形だけ残す
        text = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))
    return text


@dataclass
class Job:
    id: str
    command: str
    proc: subprocess.Popen
    started: float
    buf: bytearray = field(default_factory=bytearray)
    read_pos: int = 0
    last_output: float = 0.0
    done: threading.Event = field(default_factory=threading.Event)
    code: int | None = None
    background: bool = False
    reported: bool = False             # 終わりをモデルへ伝えた
    stall_noted: bool = False
    input_noted: bool = False
    killed: bool = False
    script: str = ""                   # 命令を書いたファイル（終わったら消す）
    cwd: str = ""
    polls: int = 0
    progress_at: float = 0.0           # 最後に進んだ時刻（出力・書き込み先のファイル・CPU）
    sample_at: float = 0.0
    cpu: float | None = None
    file_size: int = -1
    server: bool = False
    stdin_open: bool = False           # 入力を開けたまま走らせた（BashOutput の send で書ける）
    boxed: bool = False                # 囲い（shellbox・bubblewrap）の中で走らせた
    deferred: bool = False             # BashOutput(wait) を 1 回、待たずに返して先の仕事を促した（2026-11-22）

    def text(self, start: int = 0) -> str:
        return decode(bytes(self.buf[start:]))


#: 待ち受け（別の機械や列の仕事が終わるのを待つ命令）。黙って眠るのが仕事なので「止まって見える」と言わない（2026-11-11: 自分で書いた
#: `while …; do sleep 60; done` が 5 分で止まって見えるとされ、待つ対象から外れて走行の終わりに止められていた）。
_WAITER = re.compile(r"\bbwtool(?:\.cmd|\.exe)?\s+wait\b|\b(?:while|until)\b[^#]*?(?:\bdo\b|\{)[\s\S]*?\b(?:sleep|timeout(?:\.exe)?\s+/t|Start-Sleep)\b"
                     r"|^\s*(?:sleep|Start-Sleep(?:\s+-Seconds)?|timeout(?:\.exe)?\s+/t)\s+\d+[smh]?\s*$", re.I)


def is_waiter(command: str) -> bool:
    return bool(_WAITER.search(command or ""))


#: 眠ってからログや結果を覗く命令（`sleep 25; tail -3 train.log`・`while ! grep -q DONE log; do sleep 30; done`）。
#: 2026-11-13 の記録: 新しい中核の 335 走行で Monitor は 0 回、この形が 18 走行。1 回覗くたびに 1 手番（読み込みと生成）を使う。
_SLEEP_PEEK = re.compile(r"\b(?:sleep|Start-Sleep(?:\s+-Seconds)?|timeout(?:\.exe)?\s+/t)\s+(\d+)", re.I)
POLL_HINT = ("This command sleeps and then looks. Each look costs a model turn. To wait without one: for a job you started with "
             "run_in_background use BashOutput(id=..., wait=<seconds>, until=\"<regex>\"); to follow a log file use "
             "Tools(name=\"Monitor\", input={\"file\": \"<log>\", \"filter\": \"<regex>\", \"wait\": <seconds>}), which returns "
             "when a matching line or an error line appears.")


def poll_hint(command: str, ctx: Ctx) -> str:
    """眠って覗く命令なら、待ち方の案内を走行に 1 回だけ返す。"""
    if ctx.poll_hinted or ctx.child:
        return ""
    m = _SLEEP_PEEK.search(command or "")
    if not m or int(m.group(1)) < 10 or len(command.strip().split()) <= 2:
        return ""
    ctx.poll_hinted = True
    return POLL_HINT


_ASKS_INPUT = re.compile(r"(\[y/n\]|\(y/n\)|\[yes/no\]|password:|passphrase|press any key|続行するには|enter to continue)\s*$", re.I)


class Jobs:
    """走らせた命令の帳簿。会話の間（プロセスの間）生きる。"""
    STALL_SECS = 300.0

    def __init__(self) -> None:
        self.items: dict[str, Job] = {}
        self._n = 0
        self._lock = threading.Lock()

    def start(self, command: str, cwd: str, keep_stdin: bool = False, box: str = "") -> Job:
        kind, exe = resolve_shell()
        kw: dict = {"cwd": cwd, "env": child_env(), "stdin": subprocess.PIPE if keep_stdin else subprocess.DEVNULL,
                    "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT}
        if WIN:
            kw["creationflags"] = NO_WINDOW
        else:
            kw["start_new_session"] = True
        argv, script = _command_argv(kind, exe, command)
        boxed = False
        if box and kind == "bash" and isinstance(argv, list):    # box＝作業場（囲いの中で書ける場所）
            from .shellbox import wrap
            wrapped = wrap(argv, box)
            if wrapped:
                argv, boxed = wrapped, True
        try:
            proc = subprocess.Popen(argv, **kw)
        except OSError:
            if script:
                try:
                    os.unlink(script)
                except OSError:
                    pass
            raise
        with self._lock:
            self._n += 1
            now = time.monotonic()
            job = Job(f"bg{self._n}", command, proc, now, last_output=now, script=script, cwd=cwd, progress_at=now,
                      stdin_open=keep_stdin, boxed=boxed)
            self.items[job.id] = job
            done = [j for j in self.items.values() if j.done.is_set() and (j.reported or not j.background)]
            for j in done[:-KEEP_DONE_JOBS]:
                self.items.pop(j.id, None)
        reader = threading.Thread(target=self._pump, args=(job,), daemon=True)
        reader.start()
        threading.Thread(target=self._reap, args=(job, reader), daemon=True).start()
        return job

    @staticmethod
    def _reap(job: Job, reader: threading.Thread) -> None:
        """終わりはプロセスの終了で決める。`server &` のように孫が標準出力を握ったままだと、読み終わりを待つと永久に終わらない。"""
        try:
            job.code = job.proc.wait()
        except OSError:
            job.code = -1
        reader.join(1.0)                   # 残りの出力を少し待つ。読み手は孫が閉じるまで生き続けてよい
        if job.proc.stdin is not None:     # 入力の口も閉じる（残すとファイル記述子が溜まる）
            job.stdin_open = False
            try:
                job.proc.stdin.close()
            except (OSError, ValueError):
                pass
        if job.script:
            try:
                os.unlink(job.script)
            except OSError:
                pass
        job.done.set()

    def _pump(self, job: Job) -> None:
        out = job.proc.stdout
        try:
            while True:
                chunk = out.read1(65536) if hasattr(out, "read1") else out.read(4096)
                if not chunk:
                    break
                job.buf.extend(chunk)
                job.last_output = time.monotonic()
                if len(job.buf) > 64 * 1024 * 1024:         # 際限なく持たない（頭を捨てる）
                    cut = len(job.buf) - 32 * 1024 * 1024
                    del job.buf[:cut]
                    job.read_pos = max(0, job.read_pos - cut)
        except (OSError, ValueError):
            pass
        finally:                           # ★閉じないと Bash 1 回ごとにファイル記述子が残り、上限で全部の道具が落ちる
            try:
                out.close()
            except (OSError, ValueError):
                pass

    def kill(self, job: Job) -> None:
        if job.done.is_set():
            return
        job.killed = True
        pid = job.proc.pid
        try:
            if WIN:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                               creationflags=NO_WINDOW)
            else:
                os.killpg(pid, signal.SIGTERM)
                if not job.done.wait(0.5):
                    os.killpg(pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                job.proc.kill()
            except OSError:
                pass

    def kill_all(self) -> int:
        n = 0
        for j in list(self.items.values()):
            if not j.done.is_set():
                self.kill(j)
                n += 1
        return n

    def running(self) -> list[Job]:
        return [j for j in self.items.values() if j.background and not j.done.is_set()]

    @staticmethod
    def is_server(job: Job) -> bool:
        """サーバは終わらないのが普通（走行を閉じずに待たない・止まったと言わない・走行の終わりにも止めない）。"""
        if not job.server:
            try:
                from ...oc import shellfix
                job.server = shellfix.looks_like_server(job.command) or bool(
                    shellfix._SERVER_OUT_RE.search(job.text(max(0, len(job.buf) - 16384))))
            except Exception:
                pass
        return job.server

    def waiting(self) -> list[Job]:
        """走行を閉じずに待つ仕事（サーバと、止まっていると伝えた物を除く）。"""
        return [j for j in self.running() if not j.stall_noted and not self.is_server(j)]

    def stop_work(self) -> tuple[list[Job], list[Job]]:
        """走行の終わり。残っている仕事を止める（サーバは残す）。(止めた物, 残した物)。"""
        stopped: list[Job] = []
        kept: list[Job] = []
        for j in self.running():
            if self.is_server(j):
                kept.append(j)
            else:
                self.kill(j)
                j.reported = True
                stopped.append(j)
        return stopped, kept

    @staticmethod
    def _sample(job: Job, now: float) -> None:
        """進んだかを出力・書き込み先のファイル・CPU で見る（何も書かずに計算している物を「止まった」と言わない）。"""
        if job.last_output > job.progress_at:
            job.progress_at = job.last_output
        if now - job.sample_at < 2.0:
            return
        job.sample_at = now
        try:
            from ...oc.jobwatch import redirect_targets, tree
            size = sum(os.path.getsize(p) for p in redirect_targets(job.command, job.cwd) if os.path.exists(p))
            if size != job.file_size:
                if job.file_size >= 0:
                    job.progress_at = now
                job.file_size = size
            _alive, cpu, _n = tree([job.proc.pid])
            if cpu is not None:
                if job.cpu is None:
                    job.cpu = cpu
                elif cpu - job.cpu >= CPU_EPS:
                    job.cpu = cpu
                    job.progress_at = now
        except Exception:
            pass

    def needs_look(self, j: Job) -> bool:
        """長い待ちを切り上げる理由があるか（止まっている・入力を待っている）。知らせは notes() が出す。"""
        if self.is_server(j):
            return False
        now = time.monotonic()
        self._sample(j, now)
        tail = j.text(max(0, len(j.buf) - 200)).rstrip()
        if not j.input_noted and tail and _ASKS_INPUT.search(tail) and now - j.last_output >= PROMPT_SEC:
            return True
        try:
            from ...oc.jobwatch import stall_sec
            stall = stall_sec()
        except Exception:
            stall = self.STALL_SECS
        return bool(stall and not j.stall_noted and not is_waiter(j.command) and now - j.progress_at >= stall)

    def notes(self) -> list[str]:
        """モデルへ伝える知らせ（終わった・止まっている・入力を待っている）。1 つの事は 1 回だけ。"""
        out: list[str] = []
        now = time.monotonic()
        try:
            from ...oc.jobwatch import stall_sec
            stall = stall_sec()
        except Exception:
            stall = self.STALL_SECS
        for j in list(self.items.values()):
            if not j.background:
                continue
            if j.done.is_set():
                if not j.reported:
                    j.reported = True
                    tail = j.text(max(0, len(j.buf) - 1500)).strip()
                    out.append(f"Background job {j.id} finished with exit code {j.code}: {j.command[:120]}\n"
                               f"Last output:\n{tail}\nUse BashOutput(id=\"{j.id}\") for the rest.")
                continue
            if self.is_server(j):
                continue
            self._sample(j, now)
            tail = j.text(max(0, len(j.buf) - 200)).rstrip()
            if not j.input_noted and tail and _ASKS_INPUT.search(tail) and now - j.last_output >= PROMPT_SEC:
                j.input_noted = True
                how = (f"Answer it with BashOutput(id=\"{j.id}\", send=\"...\")." if j.stdin_open else
                       "Its stdin is closed: stop it and rerun the command with stdin=true to answer, or pass the answer "
                       "on the command line (for example -y).")
                out.append(f"Background job {j.id} looks like it is waiting for input: {tail[-120:]}\n{how}")
            elif stall and not j.stall_noted and not is_waiter(j.command) and now - j.progress_at >= stall:
                j.stall_noted = True
                out.append(f"Background job {j.id} has made no progress for {int(now - j.progress_at)} seconds (no output, no CPU). "
                           "It may be stuck, or waiting on a lock or the network. Check it with BashOutput; stop it with "
                           "BashOutput(kill=true) if it is stuck.")
        return out


_READONLY = frozenset("""ls dir cat type head tail wc pwd echo printf which where whereis find findstr grep egrep fgrep rg
stat file ps tasklist whoami hostname date uname df du env printenv tree sort uniq cut tr diff cmp md5sum sha256sum
basename dirname realpath readlink id uptime free nproc lscpu lsblk ip ss netstat ping curl-head true false test""".split())
_GIT_READ = frozenset("status log diff show branch remote describe rev-parse ls-files blame".split())
_SEG = re.compile(r"\|\||&&|[|;\n&]")
_HARMLESS_REDIR = re.compile(r"\d*>&\d+|\d*&?>\s*(?:/dev/null|nul)\b", re.I)
_GIT_BRANCH_WRITES = frozenset("-d -D -m -M -c -C -f -u --delete --move --copy --force --set-upstream-to --unset-upstream --edit-description".split())
_FIND_WRITES = frozenset("-delete -exec -execdir -ok -okdir -fprint -fprint0 -fprintf -fls".split())
_IP_WRITES = frozenset("set add del delete flush change replace append".split())


def _seg_writes(w: str, rest: list[str]) -> bool:
    """読むだけの命令の、書き換える使い方。"""
    if w == "find":
        return any(x in _FIND_WRITES for x in rest)
    if w == "sed":
        return any(re.match(r"-[a-zA-Z]*i|--in-place", x) for x in rest)
    if w == "awk":
        return any("system(" in x for x in rest)
    if w == "env":
        return bool(rest)              # `env rm x` は rm を走らせる
    if w == "ip":
        return any(x in _IP_WRITES for x in rest)
    if w == "date":
        return any(x == "-s" or x.startswith("--set") for x in rest)
    if w == "sort":
        return any(x == "-o" or x.startswith("--output") for x in rest)
    if w == "hostname":
        return any(not x.startswith("-") for x in rest)
    return False


def read_only(command: str) -> bool:
    """世界を変えない命令か（おおまかな判定。終わりの確かめと、下見の子の「見るだけ」の関門が使う）。"""
    # ★2026-11-13 に穴をふさいだ（実際に流して確かめた）: `echo x 1>a`・`ls 2>err.txt`（番号つきの書き出し）・`diff <(rm a) b`・
    #   `git branch -D old`・`git remote remove origin`・`git diff --output=x` を「見るだけ」と読んでいた（下見の子の関門を抜ける）。
    #   捨てるだけの向け先（/dev/null・nul・2>&1）は先に外す（`ls 2>&1 | head` を「変える」と読んでいた）。
    command = _HARMLESS_REDIR.sub(" ", command)
    if ">" in command or "$(" in command or "`" in command or "<(" in command:
        return False
    for seg in _SEG.split(command):
        words = seg.split()
        if not words:
            continue
        w = words[0].strip("\"'").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe")
        if w == "cd":
            continue
        if w == "git":
            if len(words) < 2 or words[1] not in _GIT_READ:
                return False
            rest = words[2:]
            if any(x.startswith("--output") or x == "-o" for x in rest):
                return False
            listing = any(x in ("--list", "-l", "--contains", "--merged", "--no-merged", "--points-at") for x in rest)
            if words[1] == "branch" and any((not x.startswith("-") and not listing) or x in _GIT_BRANCH_WRITES for x in rest):
                return False               # 名前を渡す＝作る／-d -D -m -M -c -C＝消す・変える
            if words[1] == "remote" and any(not x.startswith("-") and x not in ("show", "get-url") for x in rest[:1]):
                return False               # add / remove / rename / set-url / prune / update
            continue
        if (w not in _READONLY and w != "sed" and w != "awk") or _seg_writes(w, words[1:]):
            return False
    return True


def _describe_result(job: Job, ctx: Ctx, notes: list[str], workdir_given: bool) -> Result:
    text = job.text()
    code = job.code
    head = job.command.strip().split("\n")[0][:200]
    if b"\0" in job.buf[:4096] and os.environ.get("LAPS_BASH_BINARY_GUARD", "1") != "0":
        text = (f"(binary output, {len(job.buf)} bytes, not shown. Write it to a file and use Read with hex or map, "
                "or pipe it through a filter.)")
    body, cut = clip(ctx, text)
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    meta = list(notes)
    if code in (0, None):
        note = shellaid.hollow_note(job.command, text, True)
        if note:
            meta.append(note)
        return Result(shellaid.with_metadata(body if body.strip() else "(no output)", meta), kind="ok" if body.strip() else "empty",
                      head=head, tail=f"{lines} 行", truncated=cut)
    matched_nothing = shellaid.no_match(job.command, code, text)
    if matched_nothing:
        meta.append(f"exit code 1: `{matched_nothing}` matched no lines (nothing found; not an error)")
        return Result(shellaid.with_metadata(body if body.strip() else "(no output)", meta), kind="empty", head=head, tail="0 行")
    meta += shellaid.failure_notes(job.command, code, text, job.cwd, workdir_given)
    if job.boxed:
        from .shellbox import denied_hint
        hint = denied_hint(code, text)
        if hint:
            meta.append(hint)
    note = shellaid.hollow_note(job.command, text, False)
    if note:
        meta.append(note)
    return Result(shellaid.with_metadata(body if body.strip() else "(no output)", meta), ok=False, kind="error", head=head,
                  tail=f"exit {code}", truncated=cut)


_BOX_WARNED = False


def _box_for(inp: dict, ctx: Ctx, kind: str, notes: list[str]) -> str:
    """この命令を囲う（shellbox）なら書ける作業場を返す。囲わないなら空。外を選んだ・囲えない事は notes に書く。"""
    global _BOX_WARNED
    if WIN or kind != "bash":
        return ""
    from . import shellbox
    if not shellbox.wanted(getattr(ctx, "mode", "general")):
        return ""
    if inp.get("sandbox") is False:
        why = " ".join(str(inp.get("reason") or "").split())[:200]
        notes.append("Ran outside the sandbox" + (f" ({why})" if why else " (no reason given)"))
        return ""
    if not shellbox.available():
        if not _BOX_WARNED:
            _BOX_WARNED = True
            notes.append("Development mode asks for a sandbox, but bubblewrap cannot run on this machine, so commands run "
                         "without it.")
        return ""
    return ctx.workspace


def bash(inp: dict, ctx: Ctx) -> Result:
    command = str(inp.get("command") or "").strip()
    if not command:
        return fail("command is required")
    wd = str(inp.get("workdir") or "")
    cwd = (wd if os.path.isabs(wd) else os.path.join(ctx.workspace, wd)) if wd else ctx.workspace
    notes: list[str] = []
    if not os.path.isdir(cwd):
        if not wd:
            return fail(f"workdir does not exist: {cwd}", head=command[:200])
        # `mkdir X && cd X && …` に workdir=X を付けた形（命令が走る前に失敗していた・2026-10-12）。命令自身が作るなら親で走らせる。
        if re.search(r"\b(mkdir|md|new-item|ni)\b", command, re.I) and os.path.basename(cwd.rstrip("\\/")).lower() in command.lower():
            alt = os.path.dirname(cwd.rstrip("\\/"))
            alt = alt if alt and os.path.isdir(alt) else ctx.workspace
            notes.append(f"workdir {cwd} did not exist yet and this command creates it, so the command ran in {alt}; "
                         "pass workdir only for a folder that already exists")
            cwd = alt
        else:
            return fail(f"workdir does not exist: {cwd}. Create the folder first (mkdir), or leave workdir out and "
                        "cd inside the command.", head=command[:200])
    kind = resolve_shell()[0]
    if getattr(ctx, "answer_only", False):
        # Ask モード: 質問に答えるために見るだけ。変える命令は走らせない（apply_patch の読み替えより先に見る）。
        from ...policy import check_answer_command
        v = check_answer_command(command, cwd=cwd)
        if not v.allowed:
            return fail(f"Not run in Ask mode: {v.reason}\nIn Ask mode you answer questions, so only commands that look "
                        "are run (status, logs, listings, versions, git log/diff, ssh with such a command). Answer from what "
                        "you can read, or tell the user to switch to Agent mode to have it done.", kind="denied",
                        head=command[:200])
    from .patch import SHELL_FORM, apply_patch
    m = SHELL_FORM.match(command)
    if m:                              # `apply_patch <<'EOF' … EOF`（Codex で覚えた形）は ApplyPatch と同じ道で当てる（手本: Codex の invocation.rs）
        where = m.group(1).strip("\"'") if m.group(1) else ""
        base = (where if os.path.isabs(where) else os.path.join(cwd, where)) if where else cwd
        r = apply_patch(m.group(3), ctx, base=base)
        r.output += "\n(apply_patch in Bash was applied with the ApplyPatch tool; you can call ApplyPatch directly.)"
        return r
    command, more = shellaid.fix_text(command, cwd, kind)
    notes += more
    try:
        authorize_command(command, cwd=cwd)
    except PolicyDenied as e:
        return fail(str(e), kind="denied", head=command[:200])
    try:
        from ...policy import _mode as _policy_mode
        strict = _policy_mode() != "trusted"
    except Exception:
        strict = False
    command, more, refused = shellaid.guard(command, cwd, kind, ctx.workspace, strict)
    if refused:
        return fail(refused, kind="denied", head=command[:200])
    notes += more
    if ctx.stopped():
        return fail("Not run: the user stopped the task.", kind="denied", head=command[:200])
    from . import bashsafe
    snap = None
    if bashsafe.enabled() and not getattr(ctx, "answer_only", False):
        snap = bashsafe.Snapshot(command, cwd, kind, getattr(ctx, "changes", None))
        snap.before()                  # 壊す形の命令の前に、触るファイル・サービス・網の状態を控える（2026-11-20）
    same = next((j for j in ctx.jobs.running() if j.command == command), None)
    if same is not None:
        return fail(f"This exact command is already running in the background as {same.id}. Read it with "
                    f"BashOutput(id=\"{same.id}\"), or stop it with kill=true before starting it again.", head=command[:200])
    keep_stdin = bool(inp.get("stdin")) and not ctx.child
    hint = poll_hint(command, ctx)
    if hint:
        notes.append(hint)
    box = _box_for(inp, ctx, kind, notes)
    try:
        job = ctx.jobs.start(command, cwd, keep_stdin=keep_stdin, box=box)
    except OSError as e:
        return fail(f"could not start the shell: {e}", head=command[:200])
    ctx.ran.append(command)
    if ctx.child:                      # 親の走行では使わない（持つと終わった仕事の出力が走行の間残る）
        ctx.started_jobs.append(job)
    if keep_stdin:
        # 入力を開けたまま背景で走らせ、最初の出力（問いかけ）が出るまで少しだけ待って返す（手本: Codex の exec_command と
        # write_stdin・2026-11-11 オーナー了承）。答えは BashOutput の send で書く。
        job.background = True
        t0 = time.monotonic()
        while not job.done.wait(0.1) and time.monotonic() - t0 < STDIN_FIRST_WAIT and not ctx.stopped():
            if job.buf and time.monotonic() - job.last_output >= 0.5:
                break
        first, _ = clip(ctx, job.text())
        job.read_pos = len(job.buf)
        state = f"finished with exit code {job.code}" if job.done.is_set() else "running with stdin open"
        if job.done.is_set():
            job.reported = True
        return Result(shellaid.with_metadata(
            f"[{job.id} {state}]\n{first.strip() or '(no output yet)'}\n"
            f"Type into it with BashOutput(id=\"{job.id}\", send=\"...\"); read more with BashOutput(id=\"{job.id}\", wait=<seconds>).",
            notes), head=command[:200], tail="入力を開けて開始")
    if snap is not None and snap.kept:
        notes.append(f"Before this command, {snap.kept} file(s) it changes or deletes were saved; /undo (or Rewind) can put them back.")
        snap.kept = 0
    is_server = shellaid.is_server(command)
    if inp.get("run_in_background") or (is_server and not ctx.child):
        job.background = True
        if is_server and not inp.get("run_in_background"):
            notes.append("This command starts a server that keeps running, so it was started in the background "
                         "(run_in_background=true). Check it with curl, or BashOutput for its log.")
        return Result(shellaid.with_metadata(f"Started in the background as {job.id}. You are told when it ends or stops making "
                                             f"progress; read its output with BashOutput(id=\"{job.id}\").", notes),
                      head=command[:200], tail="背景で開始")   # 行の右（2026-11-05 実機で「bg16」だけが出て意味が分からなかった・番号は中身にある）
    try:
        timeout = float(inp.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    if timeout >= 1000:                # 単位はミリ秒。1000 未満は秒のつもりで書かれた値（60 が 0.06 秒になり、子の命令がすぐ止められた）
        timeout /= 1000.0
    timeout = min(timeout, MAX_TIMEOUT) if timeout > 0 else DEFAULT_TIMEOUT
    # 長くかかる命令は止めずに背景へ回し、モデルには先に返す（その間に別の作業や利用者への返事ができる・2026-10-18 オーナー）。
    # 子の中では回さない（知らせを受ける相手がいない）。
    try:
        auto = float(os.environ.get("LAPS_AUTO_BG_SEC") or 30)
    except ValueError:
        auto = 30.0
    soft = timeout if ctx.child or auto <= 0 else min(timeout, auto)
    t0 = time.monotonic()
    while not job.done.wait(0.1):
        if ctx.stopped():
            ctx.jobs.kill(job)
            job.done.wait(3)
            partial = job.text().strip()
            msg = "Error: Stopped: the user stopped the task."
            return Result(msg + (f"\nOutput so far:\n{clip(ctx, partial)[0]}" if partial else ""), ok=False, kind="denied",
                          head=command[:200], tail="止めた")
        spoke = not ctx.child and ctx.has_message is not None and ctx.has_message()
        if spoke or time.monotonic() - t0 >= soft:
            if ctx.child:
                ctx.jobs.kill(job)
                job.done.wait(3)
                partial, _ = clip(ctx, job.text())
                return fail(f"Timed out after {int(soft)} seconds and was stopped. Output so far:\n{partial}", head=command[:200])
            # 止めずに背景へ回す（走り続けている仕事を捨てない）。
            job.background = True
            partial, _ = clip(ctx, job.text())
            job.read_pos = len(job.buf)
            return Result(shellaid.with_metadata(
                f"Still running after {int(time.monotonic() - t0)} seconds; it continues in the background as {job.id}. "
                f"You are told when it ends: do other work, or wait with BashOutput(id=\"{job.id}\", wait=<seconds>). "
                f"Output so far:\n{partial}", notes), head=command[:200], tail=f"背景 {job.id}")
    if snap is not None:
        notes += snap.after()
    return _describe_result(job, ctx, notes, bool(wd))


def _named_job(inp: dict, ctx: Ctx) -> Job | None:
    """呼び出しが指している仕事。id を省いたら、相手が 1 つに決まるときだけそれ。決まらなければ None
    （2026-10-31 North が id を書かずに until と tail を渡し、それらを黙って捨てた一覧だけが返って、組み立ての出力を 1 度も見られなかった）。"""
    jid = str(inp.get("id") or inp.get("pid") or "").strip()
    if jid:
        return ctx.jobs.items.get(jid) or next((j for j in ctx.jobs.items.values() if str(j.proc.pid) == jid), None)
    jobs = [j for j in ctx.jobs.items.values() if j.background]
    running = [j for j in jobs if not j.done.is_set()]
    if len(running) == 1:
        return running[0]
    return jobs[0] if len(jobs) == 1 else None


def running_job(inp: dict, ctx: Ctx) -> bool:
    """この BashOutput は動いている仕事を見に行く呼び出しか。結果は時間で変わるので、繰り返しの見張りに数えない
    （2026-10-31 学習の待ちに「結果は変わらない・別の手を」と返して止めた・doom_loop 1 回と repeat_anywhere 1 回）。"""
    if inp.get("kill"):
        return False
    job = _named_job(inp, ctx)
    return job is not None and not job.done.is_set()


#: 入力が命令として走る相手（シェル・ssh・su）。送る文も命令と同じ関所に通す（そうしないと関所を素通りして何でも走る）。
_INPUT_RUNS_COMMANDS = re.compile(r"(?:^|[\s;&|/])(?:bash|sh|zsh|dash|ksh|fish|ssh|su|sudo|pwsh|powershell|cmd)(?:\.exe)?(?=$|\s)", re.I)


def _send_input(job: Job, text: str, eof: bool) -> str:
    """背景の仕事の入力へ書く。だめなら理由（モデルが読む文）を返す。"""
    if job.done.is_set():
        return f"{job.id} has already finished (exit code {job.code}); there is nothing to type into."
    if not job.stdin_open or job.proc.stdin is None:
        return (f"{job.id} was started without stdin=true, so its stdin is closed. Stop it with kill=true and rerun the "
                "command with stdin=true to type into it.")
    if text in ("\u0003", "^C"):
        if WIN:
            return "Ctrl-C cannot be sent on Windows; stop the job with kill=true."
        try:
            os.killpg(job.proc.pid, signal.SIGINT)
        except OSError as e:
            return f"could not interrupt {job.id}: {e}"
        return ""
    if text.strip() and _INPUT_RUNS_COMMANDS.search(job.command):
        try:
            authorize_command(text.strip(), cwd=job.cwd)
        except PolicyDenied as e:
            return str(e)
    if text and not text.endswith(("\n", "\r")):
        text += "\n"
    try:
        if text:
            job.proc.stdin.write(text.encode("utf-8"))
            job.proc.stdin.flush()
        if eof:
            job.proc.stdin.close()
            job.stdin_open = False
    except (OSError, ValueError) as e:
        return f"could not write to {job.id}: {e}"
    job.input_noted = False                # 次の問いもまた知らせる
    return ""


#: 待たずに返す下限（秒）。短い待ちは今までどおり（数秒の待ちを返すと往復が増えるだけ）。
DEFER_MIN_WAIT = 30.0


def _defer_note(job: "Job", inp: dict, ctx: Ctx, sending: bool) -> str:
    """BashOutput(wait) を 1 回だけ待たずに返す（2026-11-22 オーナー「JOB の完了まで待機する動作をバックグラウンド待機にして
    その他の作業を先回りで進めておく動作にできる？」）。予定表に未着手が残っているときだけ。結果が要るならもう一度呼べば待つ
    （逃げ道を残す＝結果に頼る作業を先に始めさせない）。仕事が動いている間は走行を閉じない（loop._park）ので知らせは届く。"""
    from ..prompt import wait_defer_on
    from .misc import pending_todos
    if not wait_defer_on() or ctx.child or sending or inp.get("kill") or job.deferred:
        return ""
    if job.done.is_set() or ctx.jobs.is_server(job):
        return ""
    try:
        wait = float(inp.get("wait") or 0)
    except (TypeError, ValueError):
        return ""
    if wait < DEFER_MIN_WAIT:
        return ""
    left = pending_todos(ctx)
    if not left:
        return ""
    job.deferred = True
    ctx.counters["wait_deferred"] = ctx.counters.get("wait_deferred", 0) + 1
    items = "\n".join(f"- {t[:120]}" for t in left[:3])
    return ("[Harness] The job is still running and you will be told when it ends, so this call returned at once instead of "
            "waiting. Your plan still has steps not started:\n" + items + "\nDo the ones that do not need this job's result "
            "now. If your next step does need it, call BashOutput with the same wait again and it will wait. Do not start "
            "another GPU-heavy job while this one may be using the GPU.")


def bash_output(inp: dict, ctx: Ctx) -> Result:
    jid = str(inp.get("id") or inp.get("pid") or "").strip()
    job = _named_job(inp, ctx)
    if job is None:
        if jid:
            return fail(f"no such job: {jid}", head=jid)
        jobs = [j for j in ctx.jobs.items.values() if j.background]
        if not jobs:
            return Result("No background jobs.", kind="empty")
        rows = [f"{j.id}: {'running' if not j.done.is_set() else f'exit {j.code}'} — {j.command[:100]}" for j in jobs]
        return Result("\n".join(rows + ["Several jobs: pass id to read or stop one of them."]), head="一覧", tail=f"{len(jobs)} 件")
    jid = job.id
    until = None
    if inp.get("until"):
        try:
            until = re.compile(str(inp["until"]))
        except re.error as e:
            return fail(f"bad regular expression in until: {e}", head=jid)
    if inp.get("kill"):
        ctx.jobs.kill(job)
        job.done.wait(3)
        job.reported = True
    send = inp.get("send")
    sending = (send is not None and str(send) != "") or bool(inp.get("eof"))
    if sending and not inp.get("kill"):
        err = _send_input(job, "" if send is None else str(send), bool(inp.get("eof")))
        if err:
            return fail(err, head=jid)
    sent_at = len(job.buf)
    note = _defer_note(job, inp, ctx, sending)
    if note:
        new = job.text(job.read_pos)
        job.read_pos = len(job.buf)
        body, cut = clip(ctx, new)
        body = body if body.strip() else "(no new output since the last read)"
        return Result(f"[{jid} still running ({int(time.monotonic() - job.started)}s); not waited]\n{body}\n\n{note}".rstrip(),
                      head=job.command[:120], tail="先に他の作業", truncated=cut)
    try:
        # ★指定どおり待つ（旧 600 秒で切っていた）。起きるたびに 1 手番使い、Claude は控えが切れて全部書き直しになる
        #   （2026-10-31 の Sonnet: $12.75 のうち $6.18）。話しかければ・終われば・止まっていればすぐ戻るので遅れない。
        wait = min(float(inp.get("wait") or 0), MAX_TIMEOUT)
    except (TypeError, ValueError):
        wait = 0.0
    if sending and not inp.get("wait"):
        wait = SEND_WAIT
    job.polls += 0 if sending else 1        # 打ち込みは見張りの繰り返しに数えない（待ちを延ばさない）
    asked = wait
    if not sending and job.polls >= 2 and until is None and not inp.get("kill") and os.environ.get("LAPS_POLL_BACKOFF", "1") != "0":
        floor = SERVER_POLL_WAIT if ctx.jobs.is_server(job) else POLL_WAITS[min(job.polls - 2, len(POLL_WAITS) - 1)]
        wait = max(wait, floor)            # 終われば待たずに戻る
    t0 = time.monotonic()
    look_at = t0 + 2.0
    while wait > 0 and not job.done.is_set() and time.monotonic() - t0 < wait and not ctx.stopped():
        if until is not None and until.search(job.text(max(job.read_pos, len(job.buf) - 1024 * 1024))):   # 0.2 秒ごとに全部を復号しない
            break
        if ctx.has_message is not None and ctx.has_message():
            break                          # 利用者が話しかけた。待ちを切り上げて答えられるように
        if sending and len(job.buf) > sent_at and time.monotonic() - job.last_output >= 0.7:
            break                          # 打ち込んだ答えに相手が返し、静まった
        if time.monotonic() >= look_at:
            look_at = time.monotonic() + 2.0
            if ctx.jobs.needs_look(job):
                break                      # 止まっている・入力を待っている。知らせを読めるように戻る
        job.done.wait(0.2)
    new = job.text(job.read_pos)
    try:
        tail = int(inp.get("tail") or 0)
    except (TypeError, ValueError):
        tail = 0
    seen = ""
    if tail > 0:                           # 最後の N 行を覗くだけ。残りは読み進めない（2026-10-31 tail=1 が末尾の改行 1 文字だけを返し、出力も読み終わった扱いになっていた）
        lines = new.splitlines()
        seen = f"; last {min(tail, len(lines))} of {len(lines)} new lines"
        new = "\n".join(lines[-tail:])
    else:
        job.read_pos = len(job.buf)
    held = time.monotonic() - t0
    if held > asked + 1 and not job.done.is_set():       # 頼まれたより長く待った理由を黙らない
        seen += f"; held {int(held)}s: repeated checks of a running job wait at least that long"
    body, cut = clip(ctx, new)
    if job.done.is_set():
        job.reported = True
        state = f"finished with exit code {job.code}"
    else:
        state = f"still running ({int(time.monotonic() - job.started)}s)"
    body = body if body.strip() else "(no new output since the last read)"
    # 行の右は短く（2026-11-05 実機で「still running (4…」「finished with ex…」と途中で切れていた）。モデルへの本文は英語のまま。
    short = (f"終了 exit {job.code}" if job.code is not None and job.code != 0 else "終了") if "finished" in state \
        else f"実行中 {int(time.monotonic() - job.started)}s"
    return Result(f"[{jid} {state}{seen}]\n{body}".rstrip(), head=job.command[:120], tail=short, truncated=cut)


BASH = Tool(
    "Bash",
    "Run a shell command and return its output. Use it for running programs, tests, git and package managers; use Read, "
    "Edit, Write, Glob and Grep for files. Put related commands in one call with && instead of one call each. A command "
    "that runs longer than 30 seconds keeps going in the background and you are told when it ends. Servers and long "
    "jobs: run_in_background=true. stdin is closed unless stdin=true: then the command starts in the background and you "
    "answer its prompts with BashOutput(send=...).",
    {"command": {"type": "string", "description": "The command"},
     "workdir": {"type": "string", "description": "Working directory (default: the workspace). Use this instead of cd."},
     "timeout": {"type": "number", "description": "Timeout in milliseconds"},
     "run_in_background": {"type": "boolean", "description": "Return at once with a job id"},
     "stdin": {"type": "boolean", "description": "Keep stdin open for a program that asks questions or reads input (a REPL, an installer)"},
     "sandbox": {"type": "boolean", "description": "false runs outside the development-mode sandbox; give a reason"},
     "reason": {"type": "string", "description": "Why it must run outside the sandbox"}},
    bash, required=("command",), effect=True)

BASH_OUTPUT = Tool(
    "BashOutput",
    "Read new output of a background job. Leave id out when only one job could be meant; with several, that lists them. "
    "wait=<seconds> waits for it to finish, or for until=<regex> to appear. kill=true stops it.",
    {"id": {"type": "string", "description": "Job id, for example bg1"},
     "wait": {"type": "number", "description": "Seconds to wait, up to 3600. Wait as long as the job needs: you come back at once if it ends, stalls or the user speaks"},
     "until": {"type": "string", "description": "Regular expression that ends the wait when it appears in the output"},
     "tail": {"type": "integer", "description": "Show only the last N lines of the new output; the rest stays unread"},
     "send": {"type": "string", "description": "Text to type into a job started with stdin=true; Enter is added. \\u0003 sends Ctrl-C"},
     "eof": {"type": "boolean", "description": "Close the job's stdin (end of input)"},
     "kill": {"type": "boolean", "description": "Stop the job"}},
    bash_output)
