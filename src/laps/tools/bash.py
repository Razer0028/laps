"""Bash tool: executes shell commands with optional timeout/cwd/background."""
from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import ToolSpec, ToolResult

from . import limits
from . import fold
from .. import procutil
from ..permission import is_destructive, confirm
from ..policy import authorize_command, PolicyDenied
from .. import sandbox as _sandbox


def _bg_dir() -> Path:
    """★実体は `procutil.bg_dir()`（2026-09-05・第4回b）。同じ 2 行を bash.py と
    bash_output.py が別々に持っていた＝この現場の常習「同じ物を 2 つ持つと片方だけ直る」。"""
    return procutil.bg_dir(create=True)


def _open_bg_tmp(d: Path, suffix: str):
    """背景の出力を受ける一時ファイル（開いた控え, パス）。

    ★Windows では**削除・改名を許す共有の指定（FILE_SHARE_DELETE）**つきで自前で開く（2026-10-08）。
      Python の普通の開き方はこの指定を付けないので、子（cmd.exe とその子）が控えを継いで書いている間は
      `{pid}.out` へ改名できず `WinError 32` になっていた（オーナーの実機の記録で 0.1.322〜333 に 4 回・
      プロセスは走っているのに「起動できなかった」と返していた）。"""
    import tempfile
    if sys.platform != "win32":
        fd, path = tempfile.mkstemp(dir=str(d), prefix=".bg-", suffix=suffix)
        return os.fdopen(fd, "wb"), path
    import ctypes
    import msvcrt
    import uuid
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    GENERIC_WRITE, SHARE_ALL, CREATE_NEW, NORMAL = 0x40000000, 0x1 | 0x2 | 0x4, 1, 0x80
    for _ in range(8):
        path = str(d / f".bg-{uuid.uuid4().hex[:12]}{suffix}")
        h = k32.CreateFileW(path, GENERIC_WRITE, SHARE_ALL, None, CREATE_NEW, NORMAL, None)
        if h not in (None, wintypes.HANDLE(-1).value):
            fd = msvcrt.open_osfhandle(h, os.O_WRONLY | os.O_BINARY)
            return os.fdopen(fd, "wb"), path
        if ctypes.get_last_error() != 80:      # ERROR_FILE_EXISTS 以外は諦めて普通に開く
            break
    fd, path = tempfile.mkstemp(dir=str(d), prefix=".bg-", suffix=suffix)
    return os.fdopen(fd, "wb"), path


def _settle_bg_file(tmp: str, final: Path) -> str:
    """一時ファイルを `{pid}.out` の名前にする。改名できなければ固いリンク、それも駄目なら一時ファイルのまま
    （★プロセスは既に走っているので、ここで失敗を返さない＝走りっぱなしの「起動できなかった」を作らない）。"""
    try:
        os.replace(tmp, final)
        return str(final)
    except OSError:
        pass
    try:
        os.link(tmp, final)
        return str(final)
    except OSError:
        return tmp


def _spawn_background(cmd: str, cwd: str | None) -> str:
    """切り離して起動する。**前面の実行と同じ約束**で走らせる（2026-09-02）。

    直した 3 つ（どれも実測の裏づけがある）:

    ① **一時ファイルの名前が固定だった**（`tmp.out` / `tmp.err`）。
       背景起動が 2 つ同時に走ると、後の起動が前の出力ファイルを**切り詰め**、
       さらに rename が `FileNotFoundError` になって「Error launching background」を
       返す——**プロセスは走っているのに失敗と報告する**形。道具は並行に呼ばれる
       （readonly でない道具でも走行によっては重なる）。名前を一意にする。

    ② **前面は bash・背景は /bin/sh（dash）だった。** `_spawn_args` は
       2026-08-29 に「Debian の `/bin/sh` は dash で、モデルが書く bash 前提の
       綴り（ブレース展開・[[ ]]・配列）が黙って壊れる」という実測から bash を
       明示するようにしたが、**この関数はその修正を写していない**
       ＝この現場が何度も踏んだ「片方で直して片方に残した」。

    ③ **Windows で必ず失敗していた**（`preexec_fn=os.setsid` は POSIX 専用）。
       `bash_output.py` の注記にも「いまは背景実行そのものが失敗するため
       ここへ到達しない」と書かれていた＝出荷している Windows 版では
       run_in_background が丸ごと使えない状態だった。

    ★止め方も書く。実測の走行では `php -S` の居残りが次の走行で
      `Address already in use` を起こしており（run44 の server.log）、
      pid は**シェルのもの**なので `kill <pid>` では子が残る。
      グループごと止める形を結果に書く（BashOutput(kill=true) と同じ意味）。
    """
    d = _bg_dir()
    # ① 一意な名前で開く（同時起動で潰し合わない）。Windows は改名を許す指定つき（_open_bg_tmp）。
    o, out_tmp = _open_bg_tmp(d, ".out")
    e, err_tmp = _open_bg_tmp(d, ".err")
    try:
        if sys.platform == "win32":
            # ③ Windows: setsid は無い。新しいプロセスグループで切り離す。
            root = os.environ.get("SystemRoot") or r"C:\Windows"
            comspec = os.path.join(root, "System32", "cmd.exe")
            if not os.path.isfile(comspec):
                comspec = os.environ.get("ComSpec") or "cmd.exe"
            argv = _win_cmdline(comspec, cmd)
            kw = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                  | getattr(subprocess, "DETACHED_PROCESS", 0)}
        else:
            # ② 前面と同じシェル（bash があれば bash）。無ければ従来どおり sh。
            sh = _posix_shell() or "/bin/sh"
            argv = [sh, "-c", cmd]
            # ★命令文を argv に置かない（前面の _spawn_args と同じ・2026-10-15）。`sh -c "<命令文>"` は文全体を自分の
            #   argv に持つので、`pkill -f 'http.server 18765'; python3 -m http.server 18765` が自分のシェルを撃ち、
            #   サーバは起きずに「launched in background」とだけ返っていた（サーバらしい命令は自動で背景へ回るので当たりやすい）。
            #   台本は走り始めに自分を消す（開いた後なら消しても読み続けられる）。書けなければ従来の形（止まるよりまし）。
            try:
                import tempfile
                fd, spath = tempfile.mkstemp(prefix="bwbg-", suffix=".sh", dir=str(d))
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write('rm -f -- "$0" 2>/dev/null\n' + cmd + "\n")
                argv = [sh, spath]
            except OSError:
                pass
            kw = {"start_new_session": True}   # = setsid（グループごと止められる）
        # ★辞書で合わせてから渡す（前面と同じ理由。同じ鍵が 2 度出る形を作らない）
        try:                                   # 前面と同じ環境（道具の PATH・起動元が書き換える前の PYTHONPATH・oc/toolenv）
            from ..oc import toolenv as _toolenv
            _cenv = _toolenv.child_env()
            if _cenv is not None and "env" not in kw:
                kw = {**kw, "env": _cenv}
        except Exception:
            pass
        p = subprocess.Popen(argv, **{"cwd": cwd, "stdout": o, "stderr": e,
                                      "stdin": subprocess.DEVNULL, **kw})
    except Exception:
        o.close(); e.close()
        for t in (out_tmp, err_tmp):
            try:
                os.unlink(t)
            except OSError:
                pass
        raise
    finally:
        # 親側の控えは閉じる（子は自分の複製を持っている）
        try:
            o.close(); e.close()
        except Exception:
            pass
    pid = p.pid
    _remember_bg(p)
    final_out = d / f"{pid}.out"
    final_err = d / f"{pid}.err"
    final_meta = d / f"{pid}.meta.json"
    got_out = _settle_bg_file(out_tmp, final_out)
    got_err = _settle_bg_file(err_tmp, final_err)
    final_meta.write_text(json.dumps({
        # ★グループ番号も控える（2026-09-05）。`start_new_session=True` なので
        #   pid = そのままグループの長。BashOutput は**長が死んだ後**でも
        #   グループで生死を見て killpg できるよう、この値を使う。
        "pid": pid, "pgid": pid, "cmd": cmd, "cwd": cwd,
        # ★どの作業場で起こしたか（2026-09-05・第4回c）。台帳は `LAPS_HOME` に
        #   あり**同居する全セッションで共有**されるので、これが無いと
        #   `ws=/srv/shared` のセッションにも他人の背景処理の通行証が出る
        #   ＝ホスト共通の kill 許可証。`procutil.bg_record_workspace_ok` が見る。
        # ★must_fix B-1（union-2・2026-09-06）: `NO_WORKSPACE` をそのまま書かない
        #   （`bg_workspace_field()` は床に届かないとき "" にする）。`NO_WORKSPACE`
        #   という**特定の文字列**を書くと、同じく作業場が無い別セッションの記録と
        #   自己一致してしまう（`bg_record_workspace_ok` 側の穴と対）。
        "workspace": procutil.bg_workspace_field(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        # ★開始時刻（tick）も控える（2026-09-05・第4回b・総括 §2-12）。
        #   PID は使い回される。台帳に番号が載っているだけで「自分の物」と
        #   見なすと、同じ番号を引き当てた**無関係のプロセスを撃つ通行証**になる。
        #   `procutil.bg_record_matches()` がこの値で同一人物かを確かめる。
        "start_ticks": procutil.proc_start_ticks(pid),
    }, ensure_ascii=False), encoding="utf-8")
    # 死んだ控えを片付ける（既定 72 時間より古い物だけ・`LAPS_BG_KEEP_HOURS=0` で停止）。
    try:
        procutil.prune_bg_records()
    except Exception:
        pass
    stop = (f"BashOutput(pid={pid}, kill=true)"
            if sys.platform != "win32" else f"taskkill /F /T /PID {pid}")
    moved = got_out == str(final_out) and got_err == str(final_err)
    return (
        f"OK: launched in background\n"
        f"  pid: {pid}\n"
        f"  stdout: {got_out}\n"
        f"  stderr: {got_err}\n"
        + (f"Use BashOutput tool with pid={pid} to poll.\n" if moved else
           "The output files could not be renamed to the pid; read them with Read (BashOutput cannot find them).\n")
        + 
        f"★止めるときは {stop}。"
        f"pid はシェルのものなので、`kill {pid}` だけでは子（サーバ本体）が残ります"
        f"（居残りは次の起動を Address already in use にします）。"
    )


# ★背景に起こした命令の Popen を控える（2026-10-28・oc/jobwatch）。終わったときの終了の番号はこれでしか分からない
#   （BashOutput は生死と出力しか見ていなかった）。古い物から捨てる（終わった物を先に）。
_BG_POPEN: dict[int, "subprocess.Popen"] = {}
_BG_POPEN_MAX = 64


def _remember_bg(p) -> None:
    try:
        if len(_BG_POPEN) >= _BG_POPEN_MAX:
            done = [k for k, v in _BG_POPEN.items() if v.poll() is not None]
            for k in (done or list(_BG_POPEN))[: max(1, len(_BG_POPEN) - _BG_POPEN_MAX + 1)]:
                _BG_POPEN.pop(k, None)
        _BG_POPEN[int(p.pid)] = p
    except Exception:
        pass


def bg_exit_code(pid) -> int | None:
    """背景に起こしたシェルの終了の番号（まだ動いている・知らない番号なら None）。"""
    try:
        p = _BG_POPEN.get(int(pid))
        return None if p is None else p.poll()
    except Exception:
        return None


def _decode_out(raw) -> str:
    """コマンドの出力を文字にする。**符号化を決め打ちしない。**

    `read.py` の判定を借りる（同じものを2つ持つと必ず片方だけ直る——実際に
    read.py だけが直り、こちらが2日そのままだった）。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):        # 呼び出し側が text=True を戻した場合の保険
        return raw
    # ★2026-09-08 監査（tools high）: Read にはバイナリ関門があるのに Bash には無く、`cat works.db` の
    #   ような出力を decode_bytes の latin-1 フォールバックが**無傷の「文字」**にしてモデルへ渡していた
    #   （128 体監査で出力の 29.6% がゴミだった型の再発口）。先頭 4KB に NUL があればバイナリと扱う。
    if os.environ.get("LAPS_BASH_BINARY_GUARD", "1") != "0" and b"\x00" in bytes(raw[:4096]):
        n = len(raw)
        # ★2026-10-11 の見回り（0.1.387）: `Get-ChildItem -Recurse | Select-String` で DLL の中を探してこの形になり、
        #   120 秒の時間切れまで走った。旧の案内は strings/xxd（Windows に無い）だった。Blackwall の道具を示す。
        return (f"Error: 出力はバイナリです（{n} バイト・先頭 4KB に NUL バイト）。文字としては読めません。\n"
                "→ バイナリのファイルの中の文字は Grep に bytes=true で探す（ASCII と UTF-16・位置つき）。"
                "Select-String・findstr・grep をバイナリのファイルに当てると、この形になります。\n"
                "→ 形式と中身は Read で見る（そのままで構造・map=true で中身の地図・hex=true で 16 進）。"
                "SQLite なら sqlite3、書庫なら unzip/tar。")
    from .read import decode_bytes
    return decode_bytes(raw)


# ★走っている子の台帳（2026-08-23・中断の穴B）。
#   「止める」は道具の境界でしか効かず、Bash の実行中（最長 720 秒）は
#   何も止められなかった。中断側から台帳の子を孫ごと殺せるようにする。
import threading as _threading
_RUNNING: set = set()
_RUNNING_LOCK = _threading.Lock()


def _register(proc) -> None:
    with _RUNNING_LOCK:
        _RUNNING.add(proc)


def _unregister(proc) -> None:
    with _RUNNING_LOCK:
        _RUNNING.discard(proc)


def kill_running() -> int:
    """いま走っている Bash を全部、孫ごと殺す。殺した数を返す。"""
    with _RUNNING_LOCK:
        procs = list(_RUNNING)
        _RUNNING.clear()
    n = 0
    for proc in procs:
        if proc.poll() is None:
            _kill_group(proc)
            n += 1
    return n


# ── 前面実行の待ち方: **パイプではなくシェルを待つ** ─────────────────────
#: シェルが終わったあと「まだ出力を掴んでいるプロセスが居る」と見なすまでの猶予（秒）。
#: goose と同じ 500ms を採る:
#:   refs/goose/crates/goose/src/agents/platform_extensions/developer/shell.rs:629
#:     `const OUTPUT_DRAIN_TIMEOUT_MILLIS: u64 = 500;`
#:   同 :599 は `timeout(..., child.wait())` ＝**子（シェル）を待つ**。パイプは
#:   :631-651 で 500ms だけ汲み、切れたら :648 `"(backgrounded process?)"` と記録して
#:   :650 `abort_handle.abort()` ＝**汲むのをやめるだけで、相手は殺さない**。
#:   goose の試験 :1334-1345 は `"echo before && sleep 300 & echo bgpid:$!"` が
#:   10 秒以内に返ることを要求している（背景に回した sleep を待たない）。
_RELEASE_GRACE_SEC = 0.5
#: 背景へ回したあとログに書き続ける上限。cline と同じ 10MB:
#:   refs/cline/apps/vscode/src/sdk/vscode-run-commands-tool.ts:58
#:     `export const PROCEED_LOG_MAX_BYTES = 10 * 1024 * 1024`（:131 で超過分を捨て、
#:   :122 の一文で「上限に達した」と本文に残す）。
#: ★ただし**読むのはやめない**。読むのをやめるとパイプが 64KB で詰まり、
#:   背景のサーバが `write()` で止まる＝「生きているのに応答しない」を作ってしまう。
_RELEASE_LOG_CAP = 10 * 1024 * 1024


def _release_grace() -> float:
    try:
        return max(0.0, float(os.environ.get("LAPS_BASH_RELEASE_GRACE", "")
                              or _RELEASE_GRACE_SEC))
    except ValueError:
        return _RELEASE_GRACE_SEC


class _Pump(_threading.Thread):
    """パイプを**読み続ける**係。読むのをやめないことが仕事。

    ★背景に回った子はシェルの stdout/stderr をそのまま持っている。誰も読まなければ
      64KB で詰まって子が止まるので、制御を返したあとも読み続けてファイルへ流す
      （cline の proceed-while-running ログと同じ考え方）。
    """

    def __init__(self, stream) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.lock = _threading.Lock()
        self.buf = bytearray()
        self.file = None
        self.capped = False
        self.n = 0                              # 読んだバイト数（oc/jobwatch が「出力が伸びているか」を見る）
        self.done = _threading.Event()

    def run(self) -> None:                      # pragma: no cover - スレッド本体
        try:
            read = getattr(self.stream, "read1", None) or self.stream.read
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                with self.lock:
                    self.n += len(chunk)
                    if self.file is None:
                        self.buf.extend(chunk)
                    elif not self.capped:
                        try:
                            if self.file.tell() + len(chunk) > _RELEASE_LOG_CAP:
                                self.capped = True
                                self.file.write(b"\n[log size cap reached; "
                                                b"further output is dropped]\n")
                            else:
                                self.file.write(chunk)
                            self.file.flush()
                        except OSError:
                            self.capped = True
        except Exception:
            pass
        finally:
            self.done.set()
            with self.lock:
                if self.file is not None:
                    try:
                        self.file.close()
                    except OSError:
                        pass
                    self.file = None
            try:
                self.stream.close()
            except Exception:
                pass

    def snapshot(self) -> bytes:
        with self.lock:
            return bytes(self.buf)

    def tail(self, n: int = 4096) -> bytes:
        """末尾の n バイト（ファイルへ流し始めた後はファイルから）。"""
        with self.lock:
            path = getattr(self, "path", None)
            if path is None:
                return bytes(self.buf[-n:])
        try:
            with open(path, "rb") as fh:
                try:
                    fh.seek(-n, os.SEEK_END)
                except OSError:
                    fh.seek(0)
                return fh.read()
        except OSError:
            return b""

    def spill_to(self, path) -> None:
        """ここまでの分をファイルへ書き出し、以後はファイルへ直接追記させる。"""
        with self.lock:
            try:
                fh = open(path, "ab")
                fh.write(bytes(self.buf))
                fh.flush()
            except OSError:
                return
            if self.done.is_set():
                # もう終わっている＝これ以上は来ない（追記係を残さない）。
                try:
                    fh.close()
                except OSError:
                    pass
                return
            self.file = fh
            self.path = path


def _wait_or_release(proc, timeout: float):
    """`communicate()` の置き換え。**待つ相手をパイプからシェルへ変える。**

    ★直した欠陥（2026-09-05・最小再現つき）: `subprocess.communicate()` が待つのは
      「パイプが EOF になること」であって「シェルが終わること」ではない。
      `cmd &` で背景に回った孫は**シェルの stdout/stderr パイプを持ったまま**走るので、
      シェルが exit 0 で終わっても EOF は来ない。結果は 2 つとも実害:
        ① 正しく起動したサーバのために、道具が既定 **120 秒**まるまる待たされる。
        ② そのあと `_kill_group()` が**プロセスグループごと SIGKILL** する
           ＝**自分が正しく起動したサーバを、道具が 120 秒後に殺していた。**
      実機の記録（run50/51・Bash 902 件）で timeout 18 件のうち **11 件**がこの形＝
      1,320 秒の待ちと 11 回の巻き添え停止。しかも返す文言は
      「打ち切られたのはサーバが終わらないからで、失敗ではありません」だった
      （`_timeout_hint` も同時に直した）。

    ★**文字列で `&` を探して直す手では届かない。** `A && B &` は**リスト全体**が
      背景に回り、その部分シェルが B の終了を待つ。だから
      `nohup php -S … > server.log 2>&1 & echo $! > server.pid`（出力をきちんと
      逃がした正しい形）でも、**部分シェル**がパイプを持って同じ 120 秒を食う——
      run51 に実在する。だから待ち方そのものを直す。

    戻り: `(stdout, stderr, status, pumps)`。status は "done" / "released" / "timeout"。
    """
    pumps = (_Pump(proc.stdout), _Pump(proc.stderr))
    try:
        proc._bw_pumps = pumps                  # 背景の仕事（loop._run_fg）の出力の量と末尾を見る口（oc/jobwatch）
    except Exception:
        pass
    for pu in pumps:
        pu.start()
    deadline = time.time() + timeout
    exited_at = None
    nap = 0.001
    while True:
        rc = proc.poll()
        drained = all(pu.done.is_set() for pu in pumps)
        # ★終わったと言うには**両方**が要る: シェルが終わっていて、かつパイプも閉じた。
        #   片方だけで済ませると、`exec 1>&- 2>&-` のように**自分で出力を閉じて走り続ける**
        #   命令を「終わった」と報告して放置してしまう（旧 `communicate` はここを
        #   timeout で捕まえていた）。その形はここに残す。
        if rc is not None and drained:
            return pumps[0].snapshot(), pumps[1].snapshot(), "done", pumps
        if rc is not None:
            if exited_at is None:
                exited_at = time.time()
            elif time.time() - exited_at >= _release_grace():
                return pumps[0].snapshot(), pumps[1].snapshot(), "released", pumps
        if time.time() >= deadline:
            return pumps[0].snapshot(), pumps[1].snapshot(), "timeout", pumps
        time.sleep(nap)
        nap = min(nap * 1.5, 0.05)


#: 命令文からポート番号を読む（`php -S 0.0.0.0:18300` / `--port 8000` / `-p 8080`）。
#: ★助言を**検証してから**書くために要る。「ポートは空いています」と言い切る前に
#:   `procutil.port_in_use()` で確かめる（言ったことが真かどうかを、言う側が見る）。
_PORT_RE = re.compile(
    r"(?:(?<=:)(?P<a>\d{2,5})\b)"
    r"|(?:--port[= ]\s*(?P<b>\d{2,5})\b)"
    r"|(?:\s-p\s+(?P<c>\d{2,5})\b)"
    # `python3 -m http.server 8123`（位置引数・記録に実在する形）
    r"|(?:http\.server\s+(?P<d>\d{2,5})\b)")


def _port_in_cmd(cmd: str) -> int | None:
    for m in _PORT_RE.finditer(cmd or ""):
        raw = m.group("a") or m.group("b") or m.group("c") or m.group("d")
        try:
            v = int(raw)
        except (TypeError, ValueError):
            continue
        if 0 < v < 65536:
            return v
    return None


def _release_record(proc, cmd: str, cwd: str | None, pumps,
                    start_ticks: int | None = None,
                    started_at: str | None = None,
                    launch_epoch: float | None = None) -> str:
    """背景に残った子を **BashOutput から追える形**にして、証拠つきで 1 段落報告する。

    ★`start_ticks` / `started_at` / `launch_epoch` は**起動直後に控えた**値
      （2026-09-05・第4回c の 4 回目）。ここへ来た時点でシェルは `poll()` で
      刈り取られており `/proc/<pgid>` はもう無いので、**この中で読むと必ず None**
      になる。すると台帳の `started_at` は「打ち切った時刻」になり、
      `procutil.bg_record_matches()` の ①（tick 一致）にも ②（起動時刻の照合）にも
      当たらず、**本物の逃げ子の記録が捨てられる**（lifecycle の時刻の関門が
      false green になる根）。渡されなければ従来どおりその場で読む（後方互換）。
    ⚠ **いちばん普通の形では、この tick はまだ一度も参照されない**（2026-09-05・
      第4回c の 5 回目・実測）。`nohup … &` ＋ `exit 0` はシェルが先に死ぬので、
      台帳に載る pid（＝シェルの pgid）は照合の時点で**もう居ない**。
      `procutil.bg_record_matches()` は先頭の `start_ticks(pid) is None` で False を
      返し、ここで控えた tick には**到達しない**（実測: 生存=False /
      bg_record_matches=False）。＝控えは正しいが**効き目が出るのは
      「シェルが生き残る形」だけ**。台帳の pid を「シェル」と「残った孫」で
      区別する話は lifecycle 係の領域なので、ここでは直さず明記する。

    ★`start_new_session=True` で起動しているので、シェルの pid = その実行専用の
      プロセスグループ ID。シェルが死んでもグループは（メンバーが居る限り）生きていて、
      `killpg` はそこへ届く。だから**グループ番号を控え**、生死も停止もそれで見る
      （`os.getpgid(死んだ pid)` は ProcessLookupError になるので使えない）。
    """
    pgid = proc.pid
    if sys.platform == "win32":
        # ★Windows では**この経路を追えない**ので、追えると言わない（2026-09-05）。
        #   `_spawn_args` の win32 分岐は新しいプロセスグループを作らず、
        #   `_kill_group` の `taskkill /T` は**親が生きている**前提で木を辿る。
        #   親（cmd.exe）が終わったあとに残った孫は、BashOutput の pid では
        #   生死も停止も扱えない——ここで BashOutput を勧めると、この現場が
        #   繰り返し踏んでいる「もっともらしいが実際には効かない助言」になる。
        return ("\n★シェルは終了しましたが、**出力を掴んだままのプロセスが残っています**"
                "（道具は待つのをやめて制御を返しました）。\n"
                "   Windows ではこの残りを道具から追えません。"
                "`tasklist` で探して `taskkill /F /PID <pid>` で止めてください。\n"
                "   追える形で起動したいなら run_in_background=true を使ってください。")
    d = _bg_dir()
    out_p, err_p = d / f"{pgid}.out", d / f"{pgid}.err"
    # ★PID は使い回される。同じ番号の古い控えが残っていると、追記した新しい出力が
    #   前の走行の出力の**続き**に見える（読む側は区別できない）。先に消す。
    for _p in (out_p, err_p):
        try:
            _p.unlink()
        except OSError:
            pass
    pumps[0].spill_to(out_p)
    pumps[1].spill_to(err_p)
    try:
        members = procutil.group_members(pgid)
    except Exception:
        members = []
    try:
        (d / f"{pgid}.meta.json").write_text(json.dumps({
            "pid": pgid, "pgid": pgid, "released": True,
            "cmd": cmd, "cwd": cwd,
            # ★どの作業場の記録か（2026-09-05・第4回c の再反証）。`_spawn_background`
            #   だけが書いていて**こちらが写し忘れていた**——`bash_output` の pid 経路が
            #   作業場を見るようになると、この経路で登録した背景処理だけが
            #   「古い台帳」扱いで止められなくなる（片方だけ直す形の再来）。
            # ★must_fix B-1（union-2・2026-09-06）: `NO_WORKSPACE` をそのまま書かない
        #   （`bg_workspace_field()` は床に届かないとき "" にする）。`NO_WORKSPACE`
        #   という**特定の文字列**を書くと、同じく作業場が無い別セッションの記録と
        #   自己一致してしまう（`bg_record_workspace_ok` 側の穴と対）。
        "workspace": procutil.bg_workspace_field(),
            # ★起動時刻（打ち切り時刻ではない）。上の docstring 参照。
            "started_at": started_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
            "launch_epoch": launch_epoch,
            "start_ticks": (start_ticks if start_ticks is not None
                            else procutil.proc_start_ticks(pgid)),
            "members": [[m["pid"], (m.get("cmdline") or "")[:200]] for m in members[:8]],
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    if members:
        who = "\n".join(f"      {procutil.describe(m)}" for m in members[:5])
        head = (f"★シェルは終了しましたが、**背景で走り続けているプロセスが "
                f"{len(members)} 本**あります（道具は待つのをやめて制御を返しました）:\n{who}")
        return (f"\n{head}\n"
                f"   以後の出力: {out_p}\n"
                f"   進み具合を見る: BashOutput(pid={pgid})／"
                f"止める: BashOutput(pid={pgid}, kill=true)"
                f"（{pgid} はこの起動のグループ番号です）")
    # ── グループに誰も居ない＝**このグループ番号では止められない** ──────────
    # ★2026-09-05（第4回b の反証・実測）: ここで `BashOutput(pid=<pgid>, kill=true)` を
    #   勧めていた。実行すると `pid … already exited`（成功の顔）が返るのに
    #   `port_in_use` は True のまま——`_FALSE_GREEN_KINDS` の "bind" ループそのもの、
    #   つまりこの作業が消そうとしている当の形を新しく作っていた。
    #   効かないと分かっている止め方を勧めない。分かっている手だけを書く。
    head = ("★シェルは終了しましたが、**出力を掴んだままのプロセス**が残っています"
            "（このプロセスグループの外へ出たようです: setsid/disown 等）。"
            "道具は待つのをやめて制御を返しました。")
    port = _port_in_cmd(cmd)
    if port is not None:
        how = (f"   ★この残りは**グループ番号 {pgid} では止められません**"
               f"（グループの外に出ています）。\n"
               f"   持ち主を見る: BashOutput(port={port})／"
               f"止める: BashOutput(port={port}, kill=true)")
    else:
        how = (f"   ★この残りは**グループ番号 {pgid} では止められません**"
               f"（グループの外に出ています）。\n"
               f"   ポート番号が分かるなら BashOutput(port=<番号>) で持ち主を出し、"
               f"分からないなら SystemState(listening) で待ち受けを見てから "
               f"PID を名指しで `kill <pid>` してください。")
    return f"\n{head}\n   以後の出力: {out_p}\n{how}"


def _kill_group(proc) -> None:
    """打ち切った命令の**孫まで**片付ける。

    ★`subprocess.run(timeout=)` が殺すのは**直接の子（シェル）だけ**。実測で
      `sleep 300 & sleep 300` を 5 秒で打ち切ったら、**孫が2つ生き残った**。
      導入を許可した（2026-08-22・オーナー決定）以上、これは
      「打ち切ったインストーラが裏で動き続ける」ことを意味する。
    """
    try:
        if sys.platform == "win32":
            # Windows は taskkill /T で木ごと。CREATE_NEW_PROCESS_GROUP に依存しない。
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


#: 走行の間だけ覚える「空の結果を返したコマンド」。回数は合図として本文に出す。
_EMPTY_SEEN: dict[str, int] = {}
_EMPTY_SEEN_LAST = [0]


def reset_repeat_memory() -> None:
    """走行の境界で空出力の記憶を消す（2026-09-08 監査: fold._OUT_SEEN と同じ欠陥の兄弟＝
    reset されず走行をまたいで「同じコマンドで空の結果は N 回目」を積み続けていた）。"""
    _EMPTY_SEEN.clear()
    _EMPTY_SEEN_LAST[0] = 0


def _seen_empty(cmd: str) -> int:
    key = " ".join(str(cmd or "").split())[:200]
    _EMPTY_SEEN[key] = _EMPTY_SEEN.get(key, 0) + 1
    _EMPTY_SEEN_LAST[0] = _EMPTY_SEEN[key]
    return _EMPTY_SEEN[key]


#: 同一結果の畳みは **fold.py の 1 本**に集めた（2026-09-02）。
#: ここに実装があった間、畳みは **Bash にしか無かった**——8 走行 10,904 件のうち
#: 反復は 2,397 件 = 22.0% で、Glob 129/369・SystemState 132/192 は畳まれずに
#: 素通りしていた。「同じ物を 2 つ持たない」ために、Bash もこの 1 本を呼ぶ。
#: 名前はこのモジュール側に残す（試験・再生スクリプトが参照している）。
_OUT_SEEN = fold._OUT_SEEN
_OUT_SEEN_N = fold._OUT_SEEN_N
_REPEAT_MIN_BYTES = fold.REPEAT_MIN_BYTES
_ERRISH = fold.ERRISH


def _repeat_key(cmd: str, cwd: str | None):
    return fold.repeat_key("Bash", cmd, cwd)


def _fold_repeat(cmd: str, cwd: str | None, raw: str, out: str) -> str:
    """同じコマンドが同じ出力を返したら畳む（実体は `fold.fold_repeat`）。"""
    return fold.fold_repeat("Bash", cmd, cwd, raw, out, what="このコマンド")


def _reset_repeat_memory() -> None:
    """試験用。走行をまたいで覚えたままにしない。"""
    fold.reset_repeat_memory()


def _terse() -> bool:
    """検収など「文脈を食わせたくない」実行か。既定は False（対話は従来どおり）。"""
    return os.environ.get("LAPS_BASH_TERSE") == "1"


def _rc_prelude() -> str:
    """命令文の前に置く、**課題（利用者）の環境を拾うための 3 行**。

    ★なぜ要るか（2026-09-13・実測で確定）
      Terminal-Bench の課題 `create-bucket` は Dockerfile の最終行
      `RUN echo 'alias aws="awslocal"' >> ~/.bashrc` **だけ**で `aws` を差し替えている。
      この道具は `bash <ファイル>` で**非対話**に走るので、
        ・`~/.bashrc` を読まない（bash は対話シェルでしか読まない）
        ・**alias を展開しない**（非対話の既定）
      の二重で前提が消え、モデルは `InvalidAccessKeyId` で諦めて
      「README とデモ用スクリプトを書いて完了」と言った（実機 pane で確認）。
      tmux 越しの手本は対話シェルなので通っていた。
      ★最小ループの器は 2026-09-13 に `bash -i` へ直して `create-bucket` が通るようになった
      （腕D 6/24 → 腕G 11/24 の 5 本のうちの 1 本）。**既定のエンジンにも同じ手当てを当てる。**

    ★安全側の作り:
      ・`. ~/.bashrc` の出力は捨てる（rc の挨拶が道具の出力に混ざらない）。
      ・失敗しても続ける（`|| true`）。
      ・**対話ガード（`case $- in *i*) … return`）がある普通の rc は早期 return するので無害**。
        ＝rc 自身の意図を尊重する形になっている。
      ・`LAPS_BASH_RC=0` で切れる（rc が壊れている現場の逃げ道）。
    ★argv には何も足さない（命令文をファイルに書く設計＝自己マッチ対策を壊さない）。
    """
    if os.environ.get("LAPS_BASH_RC") == "0":
        return ""
    return (
        "shopt -s expand_aliases 2>/dev/null || true\n"
        'if [ -n "${HOME:-}" ] && [ -f "$HOME/.bashrc" ]; then\n'
        '  . "$HOME/.bashrc" >/dev/null 2>&1 || true\n'
        "fi\n"
    )


_SCRIPT_RUN_RE = re.compile(
    r"(?:^|[\s;&|(])(?:python[0-9.]*|py|node|ruby|perl|php|bash|sh|zsh|pwsh|powershell)(?:\.exe)?\s+(?:-\S*[cerm]\b|-\s|<<|-$|\S+\.(?:py|js|mjs|rb|pl|php|sh|ps1)\b)",
    re.I)
_LOOKUP_RE = re.compile(
    r"^\s*(?:sudo\s+)?(?:ls|dir|find|fd|locate|cat|type|head|tail|less|more|grep|rg|ag|findstr|tree|stat|file|which|where|Get-ChildItem|gci|Select-String)\b",
    re.I)


def _cmd_for_note(cmd: str) -> str:
    """空出力の診断に書き戻すコマンド。長い台本は頭だけにする（2026-10-05）。

    ★全文を書き戻していた。`python3 -c "…"` の 40 行の台本だと、何も出なかった
      結果が 40 行に膨れる（モデルは自分が書いたものを既に持っている）。
    """
    one = cmd.strip()
    lines = one.splitlines()
    if len(one) <= 200 and len(lines) <= 2:
        return one
    head = lines[0][:160] if lines else one[:160]
    return f"{head} …（全 {len(lines)} 行・{len(one)} 字）"


def _empty_next_step(cmd: str) -> str:
    """空出力に添える次の一手。コマンドの形で言い分ける（2026-10-05）。"""
    if _SCRIPT_RUN_RE.search(cmd or ""):
        return ("  ★台本は何も出力していません（終了コードは上のとおり）。結果を見たいなら"
                " print / echo を足すか、台本が書いたファイルを Read で確かめる。")
    if _LOOKUP_RE.search(cmd or ""):
        return ("  ★探した物は見つかっていません。同じコマンドを繰り返さないこと。"
                "ファイルの一覧は Glob、中身は Read、文字列検索は Grep を使う。")
    return ("  ★副作用だけのコマンド（mkdir・cp 等）なら、これで成功です。"
            "情報が欲しかったなら、同じコマンドを繰り返さず、一覧は Glob・中身は Read・検索は Grep を使う。")


def _posix_shell() -> str | None:
    """実際に使うシェル。bash があれば bash（モデルの前提に合わせる）。無ければ None。"""
    import shutil
    if os.environ.get("LAPS_SHELL"):
        return os.environ["LAPS_SHELL"]
    return shutil.which("bash")



_WIN_NO_SHELL_RE = re.compile(r"^\s*(?:\S*[\\/])?(bash|wsl|sh|zsh)(?:\.exe)?(?:\s|$)", re.IGNORECASE)


def _win32_refusal(cmd: str) -> str | None:
    """Windows で `bash`/`wsl`/`sh` を先頭に置いた命令を実行前に断る。

    ★2026-09-06: Windows の `bash.exe` は WSL の起動スタブで、WSL 未導入だと
      対話プロンプトを出して**戻らない**。モデルは bash 前提の命令を出しがちなので、
      実行せずに「この機械には bash が無い。cmd/PowerShell の同等命令に」と返す。"""
    if sys.platform != "win32":
        return None
    if _WIN_NO_SHELL_RE.match(cmd or ""):
        return ("Error: この Windows 機では bash / wsl / sh は使えません（WSL の起動スタブで戻らない）。"
                "cmd.exe か PowerShell の同等命令に書き換えてください（例: `dir /b`・`Get-ChildItem`）。")
    return None

def _shell_guidance(platform_name: str | None = None) -> str:
    """Bash の説明のうち「何に使うか・この OS で何が打てるか」の段。**OS ごとに変える。**

    ★2026-09-21 オーナー指摘「モデルが使ってくれるコマンドの幅が狭い・判断ができていない」。
      実機の記録と OSS（opencode / goose / aider）を突き合わせたら、**狭めていたのは私の説明文**だった:
        旧: 「次の用途には使わないこと」「**環境ブロックに載っていないコマンドを試さないこと**」
            ＋環境ブロックに載るのは python/node/git/make など **8 個だけ**
        ＝ curl も jq も ss も journalctl も docker も「試すな」と言っていたことになる。
      2026-08-13 に 14B が Windows で `ls -la` を 8/8 打つのを止めるために書いた文で、
      その目的（一覧・表示・検索は専用の道具へ）は残すが、**禁止ではなく使い分け**として書く。
    ★OSS はどうしているか:
        opencode … 道具の説明の冒頭に「OS: …, Shell: …」。説明文そのものをシェル別
                   （bash / pwsh / powershell 5.1 / cmd.exe）に差し替える（tool/shell/prompt.ts）
        goose    … シェル優先。Windows では findstr / Select-String / Get-Content、
                   それ以外では rg / cat / sed を**名指しで勧める**（developer/mod.rs:43-）
        aider    … Platform と Shell と日付を毎回渡す（base_coder.py:1127-）
      共通点＝**いまのシェルが何かを言い、その OS で使えるコマンドを実名で挙げる**。
    """
    plat = platform_name or sys.platform
    common = (
        "★**使い分け**: ファイル名の一覧・中身の表示・文字列検索だけなら Glob / Read / Grep のほうが速く、"
        "OS も問わない（利用者が「ls で」「cat で」と言ってきても、欲しいのは結果なのでそれで答えてよい）。"
        "ディスク使用率・メモリ・failed ユニット・待ち受けポートは SystemState でも取れる。"
        "**それ以外はこの道具の仕事**——サイズ・日付・ハッシュ・バイナリの素性・プロセス・ネットワーク・"
        "サービス・パッケージ・git・ビルド・テスト・他のプログラムの実行。"
        "調査や解析では**コマンドを遠慮なく使うこと**。環境ブロックのコマンド一覧は一部にすぎず、"
        "**載っていないコマンドも試してよい**（無ければ「見つかりません」と返るだけ。"
        "1 回試して無ければ別のコマンドに替える）。"
    )
    if plat == "win32":
        return common + (
            "★**この機械のシェルは cmd.exe（Windows）。** ls / cat / grep / head / which / file / strings は無い。"
            "cmd で打てるもの: `dir /a /s` `where` `findstr` `certutil -hashfile <path> SHA256` `tasklist` "
            "`netstat -ano` `sc query` `systeminfo` `winget list` `tar -tf` `curl`。"
            "PowerShell のコマンドレット（`Get-…` `Format-…` `Select-…` など）で**始まる**命令は、"
            "そのまま書けば PowerShell で実行される。cmd の命令の途中から呼ぶときは 1 行で包む: "
            "`powershell -NoProfile -Command \"Get-Item 'C:\\path\\x.exe' | Format-List *\"`"
            "（版情報は `(Get-Item 'x.exe').VersionInfo | Format-List`、"
            "先頭のバイト列は `Format-Hex -Path 'x' | Select-Object -First 16`、"
            "ハッシュは `Get-FileHash`、フォルダごとの容量は `Get-ChildItem -Recurse | Measure-Object Length -Sum`）。"
            "複数行の命令（PowerShell のスクリプトも）はそのまま書いてよい——ファイルに落として実行される。"
            "bash / wsl / sh はこの機械では使えない。"
            # ★2026-09-21 実機: `Get-ChildItem -Name D:\Blackwall\Deep Rock Galactic` が
            #   「引数 'Galactic' を受け入れる位置指定パラメーターが見つかりません」で落ちた。
            #   絶対パスは `_win_autoquote` が実行前に直すが、**裸の相対パスは直さない**
            #   （普通の語と区別できないため）。そこはここで伝える。
            "★**空白を含むパスは必ず引用符で囲む**（`\"D:\\Deep Rock Galactic\"`）。"
            "囲まないとシェルが空白で引数を割り、フォルダ名の途中から別の引数になる。"
            "相対パスでも同じ（`\".\\Deep Rock\"`）。"
        )
    if plat == "darwin":
        return common + (
            "★**この機械のシェルは bash（macOS）。** よく効くもの: `file` `strings` `xxd | head` `stat -f` `du -sh *` "
            "`shasum -a 256` `lsof -iTCP -sTCP:LISTEN` `ps aux` `launchctl list` `log show --last 10m` "
            "`curl -sS` `jq` `tar -tf` `unzip -l` `brew list`。パイプと `&&` で繋いでよい。"
        )
    return common + (
        "★**この機械のシェルは bash（Linux）。** よく効くもの: `file` `strings` `xxd | head` `stat` `du -sh *` "
        "`sha256sum` `ss -tlnp` `ps aux` `systemctl status <unit>` `journalctl -u <unit> -n 50 --no-pager` "
        "`docker ps` `curl -sS` `jq` `tar -tf` `unzip -l` `sqlite3 <db> .schema`。"
        "パイプと `&&` で繋いでよい。"
    )


#: PowerShell の承認済み動詞のうち、実際によく書かれるもの。`apt-get` `ssh-keygen` `docker-compose`
#: のような **ハイフン入りの普通のコマンド**と区別するため、動詞を実名で持つ。
_PS_VERBS = (
    "Get|Set|New|Remove|Test|Select|Format|Measure|Where|ForEach|Sort|Group|Out|Write|Read|"
    "Start|Stop|Restart|Invoke|Import|Export|ConvertTo|ConvertFrom|Copy|Move|Rename|Add|Clear|"
    "Compare|Expand|Compress|Resolve|Join|Split|Enable|Disable|Update|Install|Uninstall|Find|"
    "Show|Wait|Tee|Push|Pop|Unblock"
)
_PS_START_RE = re.compile(
    rf"^\s*(?:\(\s*)*(?:(?:{_PS_VERBS})-[A-Za-z]+\b|\$[A-Za-z_][\w:]*\s*=|\[[A-Za-z][\w.]*\]::)",
    re.IGNORECASE)


_PS_WRAP_RE = re.compile(
    r"""^\s*(?:powershell|pwsh)(?:\.exe)?\s+(?:-(?!Command\b|c\b)[A-Za-z]+(?:\s+(?!-)[^\s"]+)?\s+)*-(?:Command|c)\s+"(.*)"\s*$""",
    re.IGNORECASE | re.DOTALL)


def _unwrap_multiline_powershell(cmd: str) -> str | None:
    """`powershell … -Command "<複数行>"` の**中身**を返す。1 行の命令や他の形は None。

    1 行ならそのまま cmd.exe 経由で動く（実機 0.1.232 で 15/15）。触るのは改行があるときだけ。
    """
    if "\n" not in (cmd or ""):
        return None
    m = _PS_WRAP_RE.match(cmd)
    if not m:
        return None
    inner = m.group(1)
    # cmd.exe 向けに書かれた引用符の逃がし（\\"）を、素の " に戻す。
    # ★`""` には触らない（PowerShell では空文字列のリテラルとして普通に書かれる）。
    inner = inner.replace('\\"', '"')
    # ★2026-09-21 owner 実機の実エラー:
    #     Write-Host ('{0:X2}' -f \$bytes[\$i])
    #     '-f' 演算子に続けて値の式を指定する必要があります。
    #   ★`\$` を戻していなかった。モデルは cmd.exe 向けの逃がしのつもりで `\$` と書くが、
    #   ★**PowerShell の逃がしはバッククォートであって円記号ではない**ので、
    #   `\$bytes` は「文字の \ ＋ 変数 $bytes」と読まれ、式として壊れる。
    #   包みを外して .ps1 にそのまま書いていたため、実機で構文エラーになっていた。
    #   ⇒ `\$` も素の `$` に戻す。
    #   ⚠ `\\$`（本当に円記号を出したい形）は壊さない——直前が \ でないときだけ戻す。
    inner = re.sub(r'(?<!\\)\\\$', '$', inner)
    return inner.strip("\r\n")


# ★2026-10-19 走行記録: `py -c "` ＋改行＋複数行の Python が 2 回、
#   「デバイス PRN を初期化できません」「'import' は…認識されていません」で失敗した。
#   cmd.exe は最初の改行で切る→ 2 行目以降が cmd の命令として走る（print → PRN 装置）。
#   ⇒ 中身を .py に書いて `py <旗> 台本.py` で走らせる。
_PY_WRAP_RE = re.compile(
    r'^\s*((?:"[^"\n]*\\)?(?:py|python3?)(?:\.exe)?"?(?:\s+-[0-9.]+)?)'
    r'((?:\s+-(?:X\s+\S+|[A-Za-bd-z]\w*))*)'
    r'\s+-c\s+"(.*)"\s*$', re.S | re.I)


def _unwrap_multiline_python(cmd: str) -> tuple[str, str, str] | None:
    """`py [-X utf8] -c "<複数行>"` を (実行ファイル, 旗, 中身) に分ける。1 行や他の形は None。"""
    if "\n" not in (cmd or ""):
        return None
    m = _PY_WRAP_RE.match(cmd)
    if not m:
        return None
    exe, flags, body = m.group(1), m.group(2).strip(), m.group(3)
    # cmd.exe 向けの逃がし（\"）は、素の " が他に無いときだけ戻す（文字列内の \" を壊さない）。
    if '"' not in body.replace('\\"', ""):
        body = body.replace('\\"', '"')
    if not body.strip():
        return None
    return exe, flags, body.strip("\r\n") + "\n"


# ★PowerShell の「パラメーター名 'X' に一致するパラメーターが見つかりません」（2026-10-19 走行記録 6 回）。
#   モデルが他の道具や新しい版の書き方を混ぜたもの。よく出る形は正しい書き方を添える。
_PS_PARAM_RE = re.compile(
    r"(?:(?P<cmd>[A-Za-z]+-[A-Za-z]+)\s*:\s*)?(?:パラメーター名 '(?P<ja>[^']+)' に一致するパラメーターが見つかりません"
    r"|A parameter cannot be found that matches parameter name '(?P<en>[^']+)')")
_PS_PARAM_FIX = {
    ("get-filehash", "md5"): "Get-FileHash -Algorithm MD5 '<パス>'（SHA1/SHA256 も -Algorithm で指定）",
    ("get-filehash", "sha1"): "Get-FileHash -Algorithm SHA1 '<パス>'",
    ("get-filehash", "sha256"): "Get-FileHash -Algorithm SHA256 '<パス>'",
    ("format-hex", "offset"): ("Windows PowerShell 5.1 の Format-Hex には -Offset/-Count が無い。"
                               "決まった位置のバイトは Read（hex=true, byte_offset, byte_length）で読む"),
    ("format-hex", "count"): "Read（hex=true, byte_offset, byte_length）で読む（5.1 の Format-Hex には -Count が無い）",
    ("format-table", "nowrap"): "Format-Table には -NoWrap が無い。-AutoSize だけにするか、Format-Table -Wrap / Out-String -Width 400",
}


def _win_ps_param_hint(out: str) -> str:
    """PowerShell の引数名の誤りに、正しい書き方を 1〜2 行で添える。当たらなければ空文字。"""
    m = _PS_PARAM_RE.search(out or "")
    if not m:
        return ""
    name = m.group("ja") or m.group("en") or ""
    cmdlet = m.group("cmd") or ""
    fix = _PS_PARAM_FIX.get((cmdlet.lower(), name.lower()))
    head = f"\n★{cmdlet or 'コマンドレット'} に -{name} という引数はありません。"
    if fix:
        return head + f"\n   正しい形: {fix}\n"
    return head + (f"\n   使える引数は `Get-Help {cmdlet} -Parameter *` か "
                   f"`(Get-Command {cmdlet}).Parameters.Keys` で確かめる。\n" if cmdlet else "\n")


def _win_prn_hint(cmd: str, out: str) -> str:
    """「デバイス PRN を初期化できません」＝複数行の台本が cmd.exe で行ごとに走った印。"""
    if "PRN" not in (out or "") or "\n" not in (cmd or ""):
        return ""
    return ("\n★複数行の台本が cmd.exe に 1 行ずつ命令として渡りました（print が PRN 装置になった）。"
            "\n   Write で .py に書いてから `py 台本.py` で走らせる。\n")


def _looks_like_powershell(cmd: str) -> bool:
    """命令が **PowerShell の書き方で始まっている**か（コマンドレット・変数代入・.NET の静的呼び出し）。

    `powershell -Command "…"` と自分で包んである命令は当たらない（cmd.exe 経由でそのまま動く）。
    """
    return bool(_PS_START_RE.match(cmd or ""))


def _powershell_exe(root: str) -> str | None:
    """PowerShell の実体。7 系（pwsh）があればそれ、無ければ Windows に必ず在る 5.1。"""
    import shutil as _sh
    p = _sh.which("pwsh")
    if p:
        return p
    cand = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if os.path.isfile(cand):
        return cand
    return _sh.which("powershell")


def _win_cmdline(comspec: str, cmd: str) -> str:
    """Windows で `cmd.exe` に渡す**コマンドライン文字列**を作る。

    ★リスト `[comspec, "/d", "/c", cmd]` で渡してはいけない（2026-09-21 に気づいた）。
      Python はリストを `list2cmdline` で 1 本の文字列にし、その際 `cmd` の中の `"` を
      **`\"` に書き換える**。ところが cmd.exe は `\"` を戻さない:

          python -c "print(1)"   →   cmd.exe /d /c "python -c \"print(1)\""
          ⇒ python には `"print(1)"`（引用符つきの文字列リテラル）が届き、**何も印字しない**
          powershell -Command "Get-Item …"  ⇒ 文字列がそのまま表示されるだけで**実行されない**

      ＝**二重引用符を含む命令が、エラーも出さずに別物になる**。0.1.196 でリスト形にしてから
      Windows では Bash が 1 回も動いていなかった（別件の TypeError）ので、誰も見ていない。
    ★文字列で渡せば Python は手を加えない。`/s` を付けると cmd.exe は**外側の 1 組の
      引用符だけ**を外し、中は一字も触らない（`shell=True` が昔からやっている形＋`/s` で決定的に）。
    """
    return f'"{comspec}" /d /s /c "{cmd}"'


def _foreground_popen_kwargs(spawn_kw: dict, cwd) -> dict:
    """前面実行の `Popen` に渡すキーワード引数を**1 つの辞書に合わせて**返す。

    ★`Popen(*a, **kw, stdin=…)` の形だと、`kw` に同じ鍵が入った瞬間に **TypeError**
      になる（2026-09-21・Windows で Bash が全滅していた原因）。辞書で合わせれば、
      同じ鍵が 2 度出る形は**書けない**。`spawn_kw`（`_spawn_args` の指定）が勝つ。
    """
    kw = {"cwd": cwd, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
          "stdin": subprocess.DEVNULL}
    kw.update(spawn_kw)
    return kw


def _spawn_args(cmd: str):
    """実行の仕方を決める。

    ★Windows で `dir` が「exit 0・出力なし」を18回返した（実機・2026-08-12）。
    `shell=True` は Windows では **COMSPEC が指すもの**を `/c` で呼ぶ。COMSPEC が
    cmd.exe でない環境（別のシェルを入れている等）では、`dir` を渡しても黙って
    終わりうる。**どのシェルで動いたかが結果から分からない**のが最悪なので、
    Windows では cmd.exe を SystemRoot から直接指す（COMSPEC に依存しない）。

    ★Linux は **bash を明示して呼ぶ**（2026-08-29・実測で判明）。`shell=True` は
      `/bin/sh` を使い、Debian ではそれが **dash**。モデルは bash 前提でコマンドを
      書く（学習の実態がそう）ので、ブレース展開・[[ ]]・source・配列・herestring が
      黙って壊れる。実測: `mkdir -p site/{video,work,api}` が**その名前のディレクトリ**を
      作り、作業場の残骸として 7 走行ぶん残っていた（頻度は 2,518 件中 1 件と低いが、
      **壊れ方が静かで、後から効く**のが問題）。
      さらに悪いのは、道具の空出力メッセージが `SHELL` を見て「shell: /bin/bash」と
      **報告していた**こと——実行は dash なのに。道具がモデルに嘘をつく状態を消す。
      bash が無い環境では従来どおり `/bin/sh`（黙って落ちない）。
    """
    if sys.platform != "win32":
        # ★**プロセスグループを分ける**（2026-08-22・実測で孫が残った）。
        #   `subprocess.run(timeout=)` が殺すのは**直接の子（シェル）だけ**。
        #   `sleep 300 & sleep 300` を 5 秒で打ち切ったら、**孫が2つ生き残った**。
        #   導入を許可した（同日）ので、これはインストーラが裏で動き続けることを
        #   意味する。start_new_session でグループを作り、打ち切り時に
        #   グループごと止める（下の except TimeoutExpired を参照）。
        _sh = _posix_shell()
        # ★命令文を **argv に置かない**（2026-09-01・実測で確定した構造的欠陥）。
        #   `bash -c "<命令文>"` はその文**全体**をシェルの argv に持つ。だから
        #   `pkill -f php` は、走らせている当のシェルに必ず一致して**自分を撃つ**。
        #   実機の記録で **9/9 = 100%** がこれ（ext -15）＝居残りサーバを止めようとした
        #   試み全部が、代わりに自分のシェルを殺していた。COMMON.md が禁じている
        #   自己マッチを、本番のエージェントが 100% の確率で踏んでいた。
        #   実験（/tmp/selfmatch）: `shell=True` は rc=-15、**ファイル渡しは rc=0 で生存**。
        #   ＝ `--prompt-file` で走行の argv から課題文を外したのと同じ手を、ここでも打つ。
        #   ⚠ 書けないときは黙って従来の形に戻す（止まるより自己マッチのほうがまし）。
        if _sh:
            try:
                import tempfile
                fd, path = tempfile.mkstemp(prefix="bwcmd-", suffix=".sh")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(_rc_prelude())
                    fh.write(cmd if cmd.endswith("\n") else cmd + "\n")
                return (([_sh, path],),
                        {"shell": False, "start_new_session": True,
                         "_bw_script": path})
            except Exception:
                pass
        return ((cmd,), {"shell": True, "start_new_session": True,
                         "executable": _sh} if _sh else
                        {"shell": True, "start_new_session": True})
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    comspec = os.path.join(root, "System32", "cmd.exe")
    if not os.path.isfile(comspec):
        comspec = os.environ.get("ComSpec") or "cmd.exe"
    # /d = AutoRun を無視（利用者の環境設定で出力が変わるのを防ぐ）
    # ★2026-09-06 owner 実機（Windows 0.1.195）: 前面の cmd.exe が **stdin を継承**し、
    #   入力待ちの命令（pause/more/choice・対話プロンプトを出す exe）で永久に止まり、
    #   さらに新しいプロセスグループが無いので `taskkill /T` が孫を取り逃していた
    #   （画面は `(running)` のまま・推論機は idle）。背景起動（_spawn_bg）と同じく
    #   stdin=DEVNULL＋CREATE_NEW_PROCESS_GROUP にする。
    kw = {"shell": False, "stdin": subprocess.DEVNULL,
          "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    # ★PowerShell の書き方で来た命令は PowerShell で走らせる（2026-09-21）。
    #   探針（実行なし・Qwen3.6・Windows の前置き）で、モデルは `Format-Hex -Path …` や
    #   `Get-FileHash …` を**包まずにそのまま**出すことがあった。cmd.exe では 100% 失敗する。
    #   opencode は Windows の既定シェルを pwsh → powershell → git bash → cmd の順で選ぶ
    #   （core/src/shell.ts:101）。ここは cmd.exe を既定に残し、**書き方で見分けて回す**
    #   ——cmd の命令（dir / where / findstr）と実行ファイルの起動は今までどおり。
    # ★2026-09-21 owner 実機（0.1.232）: **改行を含む命令が 19 回中 4 回、何も出さずに終わった。**
    #   cmd.exe は `/c` の文字列を**最初の改行で黙って切る**（goose も同じ注意を出している:
    #   developer/shell.rs:393「cmd.exe silently truncates commands at newlines」）。
    #   実例は全部 `powershell -NoProfile -Command "` ＋改行＋複数行のスクリプト＋`"`。
    #   1 行目だけが走るので PowerShell は空の命令を受け取り、exit 0・出力なし。
    #   ⇒ 包みを外して**中身を .ps1 で**走らせる。それ以外の複数行は .cmd に書いて走らせる。
    _py = _unwrap_multiline_python(cmd)
    if _py is not None:
        try:
            import tempfile
            fd, path = tempfile.mkstemp(prefix="bwcmd-", suffix=".py")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_py[2])
            exe, flags = _py[0], _py[1]
            line = f'{exe} {flags} "{path}"' if flags else f'{exe} "{path}"'
            return ((_win_cmdline(comspec, line),), {**kw, "_bw_script": path})
        except Exception:
            pass
    _inner = _unwrap_multiline_powershell(cmd)
    if _inner is not None:
        cmd = _inner
    if _inner is not None or _looks_like_powershell(cmd):
        ps = _powershell_exe(root)
        if ps:
            try:
                import tempfile
                fd, path = tempfile.mkstemp(prefix="bwcmd-", suffix=".ps1")
                # ★BOM つき UTF-8。Windows PowerShell 5.1 は BOM の無い .ps1 を ANSI（cp932）で
                #   読むので、BOM が無いと日本語のパスが化ける。
                with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:
                    fh.write("$ProgressPreference = 'SilentlyContinue'\n")
                    fh.write(cmd if cmd.endswith("\n") else cmd + "\n")
                    fh.write("if ($LASTEXITCODE) { exit $LASTEXITCODE }\n")
                line = (f'"{ps}" -NoLogo -NoProfile -NonInteractive '
                        f'-ExecutionPolicy Bypass -File "{path}"')
                return ((line,), {**kw, "_bw_script": path})
            except Exception:
                pass          # 書けないときは cmd.exe に任せる（止まるより失敗のほうが読める）
    if "\n" in cmd.strip():
        try:
            import tempfile
            fd, path = tempfile.mkstemp(prefix="bwcmd-", suffix=".cmd")
            # ★バッチは OEM コードページ（日本語 Windows なら cp932）で読まれる。表せない字は UTF-8 に
            #   切り替えてから書く（chcp 65001）。
            body = "@echo off\r\n" + "\r\n".join(cmd.strip().splitlines()) + "\r\n"
            try:
                raw = body.encode("cp932")
            except UnicodeEncodeError:
                raw = ("@echo off\r\nchcp 65001 >nul\r\n").encode("ascii") + \
                      ("\r\n".join(cmd.strip().splitlines()) + "\r\n").encode("utf-8")
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            return ((_win_cmdline(comspec, f'"{path}"'),), {**kw, "_bw_script": path})
        except Exception:
            pass
    return ((_win_cmdline(comspec, cmd),), kw)


#: 権限による失敗の形（apt/dpkg/システムパスへの書き込み等で実際に出る文言）
_PERM_DENIED_RE = re.compile(
    r"Permission denied|EACCES|Operation not permitted|are you root|"
    r"requires superuser|must be run as root|Read-only file system|"
    r"許可がありません|権限がありません", re.IGNORECASE)


#: シェルが「そのコマンド名は無い」と言うときの綴り。**名前を1つだけ**取り出す。
#:   ・bash/zsh: "foo: command not found"（"bash: line 12: foo: command not found" も）
#:   ・dash/sh : "sh: 1: foo: not found" / "sh: foo: not found"
#:   ・bash（日本語ロケール）: "bash: 行 1: foo: コマンドが見つかりません"
#:     ★実測（2026-09-03・この機械そのもの）: この道具は `bash -c` を明示的シェルとして
#:       使うが、`ja_JP.UTF-8` の環境ではその bash 自身が **"command not found" を
#:       出さない**（`コマンドが見つかりません`）。英語の綴りだけで書いた版は、
#:       この現場でいちばん普通に起きる形を**一度も検出できない**——
#:       `edit.py` の `_SYNTAX_MARKS` が同じ理由で英語と日本語の両方を持つのと同じ教訓。
#:       dash はロケールに関わらず英語のまま（実測）。
#:   ・Windows : 'foo' は、内部コマンドまたは外部コマンド…として認識されていません。
_CMD_NOT_FOUND_RES = (
    re.compile(r"(?:^|[\s:])(\S+): command not found"),
    re.compile(r"(?:^|[\s:])(\S+): not found\b"),
    re.compile(r"(?:^|[\s:])(\S+): コマンドが見つかりません"),
    re.compile(r"'([^']+)' は、内部コマンドまたは外部コマンド"),
)


def _missing_command_name(out: str) -> str | None:
    """出力から「見つからなかったコマンド名」を1つ取り出す。無ければ None。"""
    for rx in _CMD_NOT_FOUND_RES:
        m = rx.search(out)
        if m:
            name = m.group(1).strip()
            # ★パスの断片や引用符・区切り記号が混ざった取り違えを弾く（実在しない
            #   名前で PATH を探しても徒労なうえ、意味の無い助言になる）。
            if name and not any(c in name for c in "/\\'\"|&;$()<>*?"):
                return name
    return None


def _find_path_alternatives(name: str, limit: int = 6) -> list[str]:
    """PATH 上で `name` から始まる実行可能ファイルを探す。**推測せず実在するものだけ**
    （2026-09-03・dogfood ⑮: `python` が無いとき `python3` を教えられれば、モデルが
    自力で切り替えるまでの空振りを 1 手省ける。固定の別名表は書かない——
    `shutil.which` で確かめられる範囲だけを言う。書かない別名表は、その機械に
    実在しない候補を教えて「無いより悪い助言」になりうるため）。
    """
    if not name or any(c in name for c in "/\\"):
        return []
    exts = [""]
    if sys.platform == "win32":
        exts = [e for e in os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(os.pathsep) if e]
    seen: set[str] = set()
    out: list[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for fn in entries:
            stem = fn
            if sys.platform == "win32":
                for ext in exts:
                    if ext and fn.lower().endswith(ext.lower()):
                        stem = fn[: -len(ext)]
                        break
            if stem == name or stem in seen or not stem.startswith(name):
                continue
            full = os.path.join(d, fn)
            try:
                if not os.path.isfile(full) or not os.access(full, os.X_OK):
                    continue
            except OSError:
                continue
            seen.add(stem)
            out.append(stem)
            if len(out) >= limit:
                return sorted(out)
    return sorted(out)



#: 「サーバが前面で動いている」ことを示す文字列（実測: timeout 47 件のうち
#: 待受けバナー付きが 10/12。前面で待っているだけなのに、旧実装は毎回
#: 「winget なら --silent」という Windows 専用の助言を返していた）。
_LISTENING = (
    "Development Server", "started", "Listening on", "listening on",
    "Serving HTTP", "Running on", "server started", "bound to",
)

#: **無言で起動する**サーバの形（出力からは入力待ちと区別できないので、命令で見る）。
_SERVERISH = (
    "start.sh", "nginx", "php-fpm", "php -S", "http.server",
    "uvicorn", "gunicorn", "runserver", "serve", "httpd",
)


def _kill_verdict(cmd: str, pgid: int | None) -> tuple[str, str]:
    """`_kill_group()` の**後**に、本当に消えたのかを見る（2026-09-05・第4回b）。

    ★直した欠陥（実測で反証された）: 旧実装は打ち切りのたびに
      「このプロセスグループは強制終了しました（サーバは動いていません／
      ポートは空いています）。もう一度 kill する必要はありません」と**言い切って**いた。
      `setsid python3 -m http.server P` のようにグループの外へ出た起動では、
      直後の `procutil.port_in_use(port)` が **True**（生きている）。しかも
      `setsid` は run51 のトレースに **11 件**あり（`setsid nohup php -S 0.0.0.0:18300 …`）、
      モデルは実際にこの形を打っている。
      検証手段（`procutil.port_in_use` / `group_members`）は同じ変更で手元にあるのに
      使っていなかった——「もっともらしいが事実と逆」を消す作業で新しく作っていた。
    戻り: `(見出し, 残骸の描写)`。**言うのは、見てから。**
    """
    left: list[dict] = []
    if pgid and sys.platform != "win32":
        try:
            left = [m for m in procutil.group_members(pgid) if m["pid"] != os.getpid()]
        except Exception:
            left = []
    port = _port_in_cmd(cmd)
    busy = False
    if port is not None:
        try:
            busy = procutil.port_in_use(port)
        except Exception:
            busy = False
    if not left and not busy:
        tail = "（ポートは空いています）" if port is not None else ""
        return ("\n★**打ち切りに伴い、このプロセスグループは強制終了しました**"
                f"{tail}。\n   もう一度 kill する必要はありません。\n", "")
    who = ""
    if left:
        who = "\n" + "\n".join(f"      {procutil.describe(m)}" for m in left[:5])
    head = ("\n★**打ち切りに伴いプロセスグループを強制終了しましたが、"
            "生き残りが居ます**（グループの外へ出た起動＝setsid/disown/nohup 付きの"
            "サブシェル等は killpg では止まりません）。")
    if busy and port is not None:
        head += (f"\n   ポート {port} は**まだ使われています**。"
                 f"止めるなら BashOutput(port={port}, kill=true)。")
    if left:
        head += "\n   残っているプロセス:" + who
        head += "\n   グループでは止まらないので、PID を名指しで `kill <pid>` してください。"
    return head + "\n", who


#: ★空白を含む Windows のパスが**引用されていない**ときの助言（2026-09-21・実機で踏んだ）。
#:   実機の記録:
#:     Get-ChildItem -Name D:\Blackwall\Deep Rock Galactic
#:       → 引数 'Galactic' を受け入れる位置指定パラメーターが見つかりません
#:     dir /a /b D:\Blackwall\Deep Rock Galactic\*
#:       → 指定されたファイルが見つかりません
#:   ★どちらも**引用の話だと分かる文言が一つも出ない**。PowerShell は「位置指定パラメーター」、
#:   cmd は「見つかりません」としか言わないので、受け手は**パスを間違えた**と読んで別の場所を
#:   探しに行く（実機で 2 手むだにした）。
#: ★**当てずっぽうでは書かない**。候補を実際に `os.path.exists` で確かめ、
#:   在ったときだけ「引用すれば通る」と言う（在らなければ黙る＝別の原因を潰さない）。
#: ★パスの始まり方は 4 つ（2026-09-21 拡張）: `X:\`・UNC `\\server\`・`.\`・`..\`。
#:   裸の相対パス（`Deep Rock Galactic`）は**対象にしない**——普通の語と見分けがつかず、
#:   `echo Program Files` のような無関係な行まで書き換えてしまう。そちらは道具の説明で伝える。
_WIN_PATH_RUN = re.compile(
    r"""(?<!["'])((?:[A-Za-z]:\\|\\\\[^\\"'\s]+\\|\.\.?\\)[^"'\n|&<>;]*)""")

#: 引用漏れだと分かる手掛かり。日本語／英語の両方（実機は日本語ロケール）。
_WIN_QUOTE_MARKS = (
    "位置指定パラメーター", "positional parameter",
    "指定されたファイルが見つかりません", "cannot find the file specified",
    "パスが見つかりません", "cannot find the path",
    "見つかりませんでした", "does not exist",
)


#: 「これは実行ファイルではない」と OS が言っている形（日本語 Windows・英語）。
#:   ★どちらも**中身が何なのかは言わない**。言わないので、受け手は引用や PATH を疑い続ける。
_NOT_EXE_MARKS = (
    "有効な Win32 アプリケーションではありません", "有効なアプリケーションではありません",
    "有効な Win32 アプリ", "は、実行している Windows のバージョンと互換性がありません",
    "ファイル名、ディレクトリ名、またはボリューム ラベルの構文が間違っています",
    "not a valid Win32 application", "is not a valid application",
    "Exec format error",
)

#: 先頭の数バイトで分かる中身。★**実際に読んだ物だけ**を言う（推測しない）。
_MAGIC = (
    (b"MZ", "Windows の実行ファイル（PE）"),
    (b"\x7fELF", "Linux の実行ファイル（ELF）"),
    (b"PK\x03\x04", "ZIP 書庫（.zip / .jar / Office 等）"),
    (b"<!DOCTYPE html", "★HTML のページ（＝取得に失敗して web ページが保存されている）"),
    (b"<html", "★HTML のページ（＝取得に失敗して web ページが保存されている）"),
    (b"{", "JSON らしきテキスト"),
    (b"#!", "スクリプト（shebang つき）"),
    (b"\x1f\x8b", "gzip 書庫"),
    (b"version https://git-lfs", "★Git LFS のポインタ（実体を取得していない）"),
)


def _sniff(path: str) -> tuple[int, str] | None:
    """ファイルの大きさと、先頭から分かる中身を返す。読めなければ None。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return None
    lead = head.lstrip(b"\r\n\t ")
    for magic, what in _MAGIC:
        if lead.startswith(magic):
            return size, what
    return size, "先頭の数バイトからは種類が分かりません"


def _win_not_exe_hint(cmd: str, out: str, rc: int) -> str:
    r"""「実行ファイルではない」と言われたとき、**中身が何なのか**を読んで言う。

    ★2026-09-21 実測（オーナーが goose と BLACKWALL の記録を並べて提示）。
      同じ課題で**両方のエージェントが数十手ずつ溶かした**:
        goose      … 引用の付け方を 10 通り以上変えて再試行、7z・python・子エージェント 2 体
        BLACKWALL  … 思考 4,433 / 7,005 トークン、同じ場所を何度も確認
      ★どちらも「なぜ動かないか」を**一度も調べていない**。
      実体は `UnrealPak.exe`（267,883 バイト）が **GitHub の HTML ページ**だった
      ——取得に失敗して web ページが `.exe` の名前で保存されていた。
      ★`Get-Item` の 1 回、先頭 16 バイトの 1 回で分かる。

    ★OS のメッセージは「互換性がありません」としか言わないので、受け手は
      引用・PATH・アーキテクチャを疑い続ける。**中身を読めば一言で終わる。**
    """
    if rc == 0 or not any(m in out for m in _NOT_EXE_MARKS):
        return ""
    # コマンドから実行ファイルらしき道を拾う（引用の有無どちらも）
    cands: list[str] = []
    for m in re.finditer(r'"([^"\n]+?\.(?:exe|dll|bat|cmd|com))"'
                         r"|'([^'\n]+?\.(?:exe|dll|bat|cmd|com))'"
                         r"|(\S+\.(?:exe|dll|bat|cmd|com))", cmd, re.I):
        v = next((g for g in m.groups() if g), None)
        if v:
            cands.append(v)
    for c in cands:
        got = _sniff(c)
        if got is None:
            continue
        size, what = got
        return ("\n★この名前は `.exe` ですが、**中身は実行ファイルではありません**"
                f"（実際に先頭を読んで確かめました）。\n"
                f"   {c}\n"
                f"   大きさ {size:,} バイト ／ 中身: {what}\n"
                "   ⇒ 引用や PATH の問題ではありません。**そのファイルを取り直す**か、"
                "別の手段に替えてください。\n")
    return ""


def _fix_confusable_paths(cmd: str, base: str | None) -> tuple[str, str]:
    """コマンドの中の、見た目が同じで文字だけ違うパスを実在の名前に直す（2026-10-05）。

    ★実機（オーナー機・Windows）: 作業場のフォルダ名の空白が全角（U+3000）で、モデルは
      半角で書き写した。`dir /a /s "C:\\…\\Blackwall"` が 2 回「パスが見つかりません」で落ちた。
      トークナイザが全角空白を半角に寄せるので、モデルには区別できない（tools/pathfix.py の注記）。
    ★直すのは**引用符で囲まれた文字列と、区切り（\\ /）を含む語**のうち、
      実在せず、見た目が同じ実在のパスがちょうど 1 つ見つかったものだけ。
    ★`_win_autoquote` と同じく **policy より前**に直す＝審査した文字列と走る文字列を一致させる。
    """
    if not cmd:      # ★切替（LAPS_PATHFIX）は pathfix.fix_confusable が 1 か所で見る
        return cmd, ""
    from . import pathfix
    cands: list[str] = []
    for m in re.finditer(r'"([^"\n]{3,})"|\'([^\'\n]{3,})\'', cmd):
        cands.append(m.group(1) or m.group(2))
    for m in re.finditer(r'(?<![\w"\'])((?:[A-Za-z]:)?[\\/][^\s"\'|&;<>]{2,})', cmd):
        cands.append(m.group(1))
    fixes: list[tuple[str, str]] = []
    for c in dict.fromkeys(cands):
        if "\\" not in c and "/" not in c:
            continue
        try:
            if os.path.exists(c if os.path.isabs(c) else os.path.join(base or os.getcwd(), c)):
                continue
        except (OSError, ValueError):
            continue
        f = pathfix.fix_confusable(c, base=base)
        if f and f != c:
            fixes.append((c, f))
    if not fixes:
        return cmd, ""
    new = cmd
    for c, f in fixes:
        new = new.replace(c, f)
    note = "".join("\n" + pathfix.note(c, f) for c, f in fixes) + "\n"
    return new, note


def _win_autoquote(cmd: str) -> tuple[str, str]:
    r"""★**起きる前に直す**（2026-09-21・オーナー「似たような事象が出ないように」）。

    空白を含む Windows のパスが引用されていないと、シェルは空白で引数を割る。
    実機:  Get-ChildItem -Name D:\Blackwall\Deep Rock Galactic
           → 引数 'Galactic' を受け入れる位置指定パラメーターが見つかりません
    下の `_win_quote_hint` は**失敗してから**教える網で、1 往復むだになる。
    ここは実行の前に引用を足して、そもそも失敗させない。

    ★書き換えてよい条件を狭く固定する（推測で人の命令を書き換えない）:
      ① win32 のときだけ
      ② まだ引用符の中に無い `X:\…` の並びで、空白を含む
      ③ **そのパスが実在する**（`os.path.exists`）——または
         **親フォルダが実在し、かつ親の名前に空白がある**（作る前のファイルを救う）
      ④ 末尾の語が `-` `/` で始まらない（`-Recurse` を巻き込まない）
    どれか外れたら**何もしない**。直したときは必ず本文で名乗る（黙って書き換えない）。

    ★`ntpath` を明示して使う。`os.path` は走らせた OS の流儀になるので、
      Linux で読むと `dirname(r"D:\a b\c")` が空になり、**試験だけ通らない**
      （＝本番でしか出ない差を試験で踏めなくなる）。
    """
    if sys.platform != "win32" or not cmd:
        return cmd, ""
    import ntpath
    changed: list[str] = []
    pos, out = 0, []
    while True:
        m = _WIN_PATH_RUN.search(cmd, pos)
        if not m:
            break
        raw = m.group(1).rstrip()
        toks = raw.split(" ")
        cand = ""
        if len(toks) >= 2:
            # ①実在するいちばん長い並び
            for k in range(len(toks), 1, -1):
                c = " ".join(toks[:k])
                if _win_path_here(c):
                    cand = c
                    break
            # ②まだ無いファイル: 親が実在し、その親の名前に空白がある場合だけ
            if not cand:
                for k in range(len(toks), 1, -1):
                    if toks[k - 1][:1] in ("-", "/"):
                        continue
                    c = " ".join(toks[:k])
                    parent = ntpath.dirname(c.rstrip("\\"))
                    if " " in parent and os.path.isdir(parent):
                        cand = c
                        break
        if not cand:
            pos = m.end(1)
            continue
        out.append(cmd[pos:m.start(1)])
        out.append('"' + cand + '"')
        pos = m.start(1) + len(cand)
        changed.append(cand)
    if not changed:
        return cmd, ""
    out.append(cmd[pos:])
    new = "".join(out)
    names = "・".join('"' + c + '"' for c in changed)
    note = ("\n★パスに空白があったので、実行の前に引用を足しました: " + names + "\n"
            "   （引用しないとシェルが空白で引数を割り、フォルダ名の途中から別の引数に"
            "なります。実在を確かめたうえで足しています。）\n"
            "   実際に走らせた行: " + new + "\n")
    return new, note


def _win_path_here(p: str) -> bool:
    """末尾のワイルドカードと `\\` を落として実在を見る。失敗しても例外を出さない。"""
    q = p
    for suf in ("\\*", "*"):
        if q.endswith(suf):
            q = q[: -len(suf)]
    q = q.rstrip("\\")
    try:
        return bool(q) and os.path.exists(q)
    except Exception:
        return False


def _win_quote_hint(cmd: str, out: str, rc: int) -> str:
    """空白入りパスの引用漏れを**実在確認つきで**言い当てる。当たらなければ空文字。"""
    if rc == 0 or sys.platform != "win32":
        return ""
    if not any(m in out for m in _WIN_QUOTE_MARKS):
        return ""
    for m in _WIN_PATH_RUN.finditer(cmd):
        raw = m.group(1).rstrip()
        toks = raw.split(" ")
        if len(toks) < 2:
            continue  # 空白が無いなら引用の話ではない
        for k in range(len(toks), 1, -1):
            cand = " ".join(toks[:k])
            probe = cand
            for suf in ("\\*", "*"):
                if probe.endswith(suf):
                    probe = probe[: -len(suf)]
            probe = probe.rstrip("\\")
            try:
                here = bool(probe) and os.path.exists(probe)
            except Exception:
                here = False
            if not here:
                continue
            fixed = cmd[: m.start(1)] + '"' + cand + '"' + cmd[m.start(1) + len(cand):]
            return ("\n★パスに空白が入っているのに引用されていません"
                    f"（{cand!r} は**実在します**。確認済み）。\n"
                    "   シェルは空白で引数を割るので、フォルダ名の途中から別の引数として渡っています。\n"
                    f"   そのまま通る形: {fixed}\n"
                    "   PowerShell の変数展開が要るときは \" \" ではなく ' ' で囲む。\n")
    return ""


def _timeout_hint(cmd: str, partial: str, pgid: int | None = None) -> str:
    """打ち切りの理由を**部分出力から**言い当てる（2026-08-28・監査 S6）。

    ★汎用の助言は、置換しないより悪い（この現場の実測）。旧実装は場面を問わず
      winget の例を返しており、Linux でサーバを前面起動した 47 件すべてに的外れだった。
    ★`pgid` を受け取り、**殺せたかどうかを見てから**書く（`_kill_verdict`）。
    """
    p = partial or ""
    low = p.lower()
    #: ★「打ち切った」だけでなく「**殺した**」と言う（2026-09-05）。`_kill_group()` は
    #:   プロセスグループごと SIGKILL する。ここを曖昧にしていたせいで、モデルは
    #:   「サーバは無事」と読んで curl し、繋がらないと気づいてから起動し直す——
    #:   実機で 1 往復ぶん無駄にしていた（run51 の 2 例）。
    #: ★ただし**言い切らない**（2026-09-05・第4回b）。実際に消えたかを見て分岐する。
    _killed, _ = _kill_verdict(cmd, pgid)
    #: サーバ以外（インストーラ・ビルド）向け。**途中で殺された**ことのほうが重要。
    _killed_general = (_killed.rstrip("\n")
                       + "\n   中途半端な状態が残っていないか確かめてください"
                         "（apt/dpkg なら `dpkg --configure -a` が要ることがあります）。\n")
    if sys.platform == "win32":
        # ★2026-09-07: Windows で長引く典型を名指しで（汎用助言は置換しないより悪い、の例外＝OS 固有）。
        _killed_general += ("   Windows で打ち切りに当たる典型: `dir /s`・`findstr /s`・`where /r`・"
                            "`Get-ChildItem -Recurse` の**広い走査**（対象フォルダを絞る・`-Depth 2`）、"
                            "`pause`/`more`/確認プロンプト（`/y`・`-Force`・非対話フラグ）、"
                            "`winget`/`choco` の承諾待ち（`--accept-source-agreements` 等）。\n")
    if sys.platform == "win32":
        # ★Windows の `cmd.exe` で末尾の `&` は**区切り**であって背景化ではない
        #   （2026-09-05・第4回b）。`_release_record` にだけ win32 分岐を入れて
        #   助言側に入れていなかった＝片手落ち。効かない手を勧めない。
        _bg = ("run_in_background=true で呼ぶ"
               "（PID が返り、BashOutput で追える）。cmd.exe では末尾の & は"
               "背景化ではなく**区切り**なので、付けても前面のままです。")
    else:
        _bg = ("run_in_background=true で呼ぶ（pid が返り、BashOutput で追える）か、"
               "コマンドの末尾に & を付ける（シェルが終われば道具はすぐ制御を返します）。")
    if any(k.lower() in low for k in _LISTENING):
        # ★2026-09-05 訂正: 旧文は「失敗ではありません」「裸の & だけでは足りません
        #   （道具の実行が終わると一緒に片付けられます）」と書いていたが**両方とも誤り**
        #   だった。実際は ①`_kill_group()` がグループごと SIGKILL していた（＝サーバは
        #   死んでいる）②`&` で背景に回した起動はいま `_wait_or_release` が即座に
        #   制御を返す（＝片付けられない）。この現場が繰り返す
        #   「もっともらしいが事実と逆」の一例だったので、実際に捕まえた出力で
        #   裏を取って書き換えた。
        return ("\n★出力を見るに、**サーバを前面で起動して戻ってきていません**。"
                + _killed +
                "→ 背景に回すこと。" + _bg + "\n"
                "   起動を確かめるには sleep 1 のあと curl で 1 回叩く。")
    if not p:
        # ★無言で固まる原因は2つあり、助言が正反対になる（2026-09-01）。
        #   ①入力待ち  ②**無言で起動したサーバ**（nginx・php-fpm・start.sh は
        #   起動時に何も言わない）。②に「</dev/null を付けろ」と言うのは
        #   間違った助言で、この現場では**助言が無いより悪い**。
        #   出力からは区別できないので、**コマンドの形**で分ける。
        if any(k in cmd for k in _SERVERISH):
            return ("\n★何も出力しませんが、コマンドの形からすると"
                    "**サーバを前面で起動して戻ってこない**状態です"
                    "（nginx や php-fpm は起動時に何も言いません）。"
                    + _killed +
                    "→ 背景に回すこと: " + _bg + "\n"
                    "   起動を確かめるには sleep 1 のあと curl で 1 回叩く。")
        return ("\n★何も出力せずに固まりました。**入力待ち**の可能性が高いです。"
                + _killed_general +
                "→ 対話しない指定を付けるか、</dev/null を付けて標準入力を閉じてください。")
    if any(k in p for k in ("%", "ETA", "Downloading", "Receiving", "Unpacking", "Building")):
        # ★2026-09-05 訂正: 旧文は「**遅いだけ**で、止まってはいません」だった。
        #   壊れてはいなかったが、**打ち切りで殺してある**ので「止まっていない」は誤り。
        #   run50 では `apt-get install -y nginx php-fpm` がこの文言つきで
        #   120 秒で殺されている＝dpkg を途中で殺したことを誰も伝えていなかった。
        return ("\n★進行中の出力が出ています（**遅いだけ**で、壊れてはいませんでした）。"
                + _killed_general +
                "→ 時間のかかる処理は背景で走らせ（run_in_background=true）、"
                "BashOutput で進捗を見てください。")
    return ("\n★打ち切られた理由は出力から特定できません。"
            + _killed_general +
            "→ 時間のかかる処理は背景で走らせてください（" + _bg + "）。\n"
            "   進み具合は BashOutput(pid=…) で見る。")


# ── 名前で殺す命令を、作業場の中だけに絞る ──────────────────────────────
#: ★`_NAME_KILL_RE`（1 本の正規表現）は 2026-09-05・第4回c の 4 回目で**撤去した**。
#:   実物で 4 形が素通りして保護ポートのサーバが死んだ:
#:     `pkill -9 -f X &` / `nohup pkill -9 -f X &` / `pkill -9 -f X | cat` /
#:     `pkill --signal 9 -f X`
#:   末尾は `\s*$` 固定・オプションは `(?:-\S+\s+)*` で**引数を取る長い綴り**
#:   （`--signal 9` / `killall -s KILL`）を読めなかった。PID 側の `_parse_kill` は
#:   末尾の `&` を tail に剥がしていたのに、名前側だけ手当てが無かった
#:   ——この現場の常習「片方だけ直る」。→ 語に割る `_parse_name_kill()` に一本化。
#: 割り方（`_split_kill_args`）・末尾（`_split_control_tail` / `_KILL_TAIL_RE`）・
#: 前置き剥がし（`_strip_kill_prefix`）は**PID 側と同じ部品**を使う。

#: `_split_segments` が返す「区切りそのもの」の語（書き換えの対象にしない）。
#: ★単独の `&`（2026-09-05・第4回c の 5 回目・反証 B(2)）: `&` は**区切り**であって
#:   末尾ではない。`kill … & echo x` は `_split_control_tail` が末尾を剥がすので通って
#:   いたのに、**順番が逆の** `echo x & kill …` / `true & kill …` / `sleep 0 & pkill …` は
#:   どの門にも見えず保護ポートのサーバが死んだ（実物で 3 形）。
_SEG_SEPS = frozenset({";", "&&", "||", "&", "\n"})

#: ヒアドキュメントの開き（`<<WORD` / `<<-'WORD'` / `<<"WORD"`）。`<<<`（ヒアストリング）は別物。
_HEREDOC_OPEN_RE = re.compile(
    r"<<(?P<dash>-?)\s*(?:'(?P<w1>[^']*)'|\"(?P<w2>[^\"]*)\"|(?P<w3>[A-Za-z_][A-Za-z0-9_]*))")


def _heredoc_spans(cmd: str) -> list[tuple[int, int]]:
    r"""ヒアドキュメントが占める `(開始, 終了)` の範囲。**この中では区切らない**。

    ★なぜ要るか（2026-09-05・第4回c の 5 回目・反証 B(6)）: 旧実装は
      `nl_ok = "<<" not in cmd` ——`<<` が 1 つでもあれば**命令全体で改行を区切りに
      しない**という大鉈だった。だから

          cat > /dev/null <<'EOF'
          hello
          EOF
          kill -9 <保護ポートの pid>

      の 4 行目がどの門にも見えず、実物でサーバが死んだ。ヒアドキュメントは
      **終端行までしか続かない**ので、そこを見つけてその後ろは普通に割る。
    ★読めない形（終端が見つからない・`<<$VAR` のような展開）は、そこから先を
      丸ごと「範囲」にして割らない＝**取りこぼす方に倒す**（旧実装と同じ安全側）。
    """
    spans: list[tuple[int, int]] = []
    n = len(cmd)
    i = 0
    q: str | None = None
    cursor = 0                      # 次の本体が始められる最小位置（同じ行に 2 つ在る形）
    while i < n:
        ch = cmd[i]
        if q:
            if ch == q:
                q = None
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch in "\"'":
            q = ch
            i += 1
            continue
        if cmd.startswith("<<<", i):
            i += 3                  # ヒアストリング（本体を持たない）
            continue
        if cmd.startswith("<<", i):
            m = _HEREDOC_OPEN_RE.match(cmd, i)
            word = None
            if m:
                word = m.group("w1")
                if word is None:
                    word = m.group("w2")
                if word is None:
                    word = m.group("w3")
            if not word:
                spans.append((i, n))          # 読めない＝ここから先は割らない
                return spans
            eol = cmd.find("\n", m.end())
            if eol < 0:
                spans.append((i, n))          # 本体が無い＝終端も無い
                return spans
            body = max(eol + 1, cursor)
            end = _heredoc_end(cmd, body, word, bool(m.group("dash")))
            if end is None:
                spans.append((i, n))          # 終端行が見つからない
                return spans
            spans.append((i, end))
            cursor = end
            i = m.end()
            continue
        i += 1
    return spans


def _heredoc_end(cmd: str, body: int, word: str, dash: bool) -> int | None:
    """本体 `body` から終端行 `word` を探し、**その行の終わり**の位置を返す。"""
    pos = body
    n = len(cmd)
    while pos <= n:
        eol = cmd.find("\n", pos)
        line = cmd[pos:(eol if eol >= 0 else n)]
        cand = line.lstrip("\t") if dash else line
        if cand.rstrip("\r") == word:
            return eol if eol >= 0 else n
        if eol < 0:
            return None
        pos = eol + 1
    return None


def _is_bg_amp(s: str, i: int) -> bool:
    """`s[i]` の `&` が**背景化の区切り**か（`&&` / `2>&1` / `&>file` / `|&` は違う）。"""
    if s.startswith("&&", i):
        return False
    if i and s[i - 1] in "><&|":
        return False                 # `2>&1` `>&2` `<&-` `|&` `&&` の 2 文字目
    if i + 1 < len(s) and s[i + 1] in ">&":
        return False                 # `&>` `&>>`（bash の両出力リダイレクト）
    return True


def _comment_start(s: str) -> int:
    r"""引用符 / `$( … )` / `( … )` / `` ` … ` `` の**外**で、行頭か空白の直後に現れる
    `#` の位置。無ければ -1。

    ★なぜ要るか（2026-09-06・第4回c の 8 回目・反証 M-A・**3 経路が同時に黙っていた**）:
      `_split_kill_args` が `#` を語として扱っていたので、

          kill -9 <pid> # cleanup     → ['kill','-9','<pid>','#','cleanup']
          pkill -9 -f X # 1           → pats が 2 個＝「読み違えた」で素通し
          killall -9 zzz # <name>     → `#` と `<name>` が**追加の名前**になる

      の 3 つが全部おかしくなっていた。前 2 つは門が**何も見ない**まま素通し
      （実測で保護ポートのサーバが死亡・本文は `(exit 0, no output)`）。
      3 つ目は逆に**的が広がり**、元の命令が撃たない相手を門が撃った
      （5 回目の反証 A と同じ族が、別の綴りで生き残っていた）。
    ★bash の規則そのまま: `#` が**語の先頭**（行頭か空白の直後）に来たときだけ
      コメント。`curl http://a/#b` や `sed s/#/x/` の `#` は語の途中なので違う。
    """
    i, n = 0, len(s)
    q: str | None = None
    depth = 0
    while i < n:
        ch = s[i]
        if q:
            if q == '"' and ch == "\\":
                i += 2; continue
            if ch == q:
                q = None
            i += 1; continue
        if ch == "\\":
            i += 2; continue
        if ch in "\"'":
            q = ch; i += 1; continue
        if ch == "`":
            j = s.find("`", i + 1)
            i = (j + 1) if j > i else n
            continue
        if ch == "$" and i + 1 < n and s[i + 1] == "(":
            depth += 1; i += 2; continue
        if ch == "(":
            depth += 1; i += 1; continue
        if ch == ")":
            depth = max(0, depth - 1); i += 1; continue
        if ch == "#" and not depth and (i == 0 or s[i - 1].isspace()):
            return i
        i += 1
    return -1


def _strip_comment_tail(s: str) -> tuple[str, str]:
    r"""末尾のコメント（`kill -9 123 # cleanup` の `# cleanup`）を剥がす。

    戻り: (本体, 末尾)。剥がす物が無ければ (s, "")。
    ★`_strip_redirect_tail` と同じ規律で**そのまま書き戻す**（`# 以降のバイトは
      1 文字も変えない`）。コメントは行末まで＝`;` `|` `&` `>` を含んでいても
      全部コメントなので、**いちばん先に**剥がす。
    """
    i = _comment_start(s)
    if i < 0:
        return s, ""
    return s[:i].rstrip(), " " + s[i:]


def _split_segments(cmd: str) -> list[str]:
    r"""`;` / `&&` / `||` / **単独の `&`** / **改行** で割る（引用符の中は割らない）。
    名前撃ちは `pkill -f "…"; sleep 1; …` のように**連結の一部**として来ることが多い。

    ★改行（2026-09-05・第4回c の 4 回目）: `true\nkill -9 <pid>` が
      **どの門にも見えず**保護ポートのサーバを撃てた（反証 B15）。シェルでは
      改行は `;` と同じ区切り。ヒアドキュメントの本体だけは `_heredoc_spans` で
      よけて、**終端行より後ろは普通に割る**（第4回c の 5 回目・反証 B(6)）。
    ★単独の `&`（第4回c の 5 回目・反証 B(2)）: `&` も区切り。`_split_control_tail` は
      「先頭の 1 命令」しか見ないので、`&` の**後ろ**に来た kill が素通しだった。
    ★`$( … )` / `( … )` / `` ` … ` `` の中は割らない（第4回c の 5 回目）。中身は
      `_scope_nested_kill` が**別に開いて**同じ門へ通すので、ここで割ると
      `( kill -9 N & )` のような形を**両方の経路から落とす**ことになる。
    """
    prot = _heredoc_spans(cmd)
    segs: list[str] = []
    cur: list[str] = []
    q: str | None = None
    depth = 0
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            i += 1
            continue
        if ch in ("\"", "'"):
            q = ch
            cur.append(ch)
            i += 1
            continue
        if ch == "`":
            j = cmd.find("`", i + 1)
            if j < 0:
                cur.append(ch)
                i += 1
                continue
            cur.append(cmd[i:j + 1])
            i = j + 1
            continue
        if ch == "$" and i + 1 < n and cmd[i + 1] == "(":
            depth += 1
            cur.append("$(")
            i += 2
            continue
        if ch == "(":
            depth += 1
            cur.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            cur.append(ch)
            i += 1
            continue
        if (ch == "#" and not depth and (i == 0 or cmd[i - 1].isspace())
                and not any(a <= i < b for a, b in prot)):
            # ★コメントは**行末まで**（2026-09-06・第4回c の 8 回目）。ここで
            #   割らないと `kill -9 1 # ; pkill -f sshd` の後半を「別の命令」として
            #   門にかけてしまい、**実際には走らない相手**に注記を出す。
            j = cmd.find("\n", i)
            j = n if j < 0 else j
            cur.append(cmd[i:j])
            i = j
            continue
        sep = None
        if not depth and not any(a <= i < b for a, b in prot):
            if cmd.startswith("&&", i) or cmd.startswith("||", i):
                sep = cmd[i:i + 2]
            elif ch in ";\n":
                sep = ch
            elif ch == "&" and _is_bg_amp(cmd, i):
                sep = "&"
        if sep:
            segs.append("".join(cur))
            cur = []
            segs.append(sep)
            i += len(sep)
            continue
        cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return segs


def _keep_ws(s: str, fn):
    """断片の**前後の空白を保ったまま** `fn(核)` を当てる。

    ★なぜ要るか（2026-09-05・第4回c の 5 回目）: `&` を区切りに加えた途端、
      `kill -9 <pid> & echo x` の断片は `"kill -9 <pid> "`（末尾に空白つき）になり、
      書き換えが返す `"true"` を素で継ぐと `"true& echo x"` に潰れた。動きはするが、
      **モデルに返る本文が実際に走った命令と違って見える**——パイプ側で一度直した
      のと同じ形を、区切りを増やした側で作り直していた（片方だけ直る形の再来）。
    """
    core = s.strip()
    if not core:
        return None
    pre, post = s[:len(s) - len(s.lstrip())], s[len(s.rstrip()):]
    r = fn(core)
    if not r:
        return None
    return pre + r[0] + post, r[1]


def _split_control_tail(s: str) -> tuple[str, str]:
    r"""`s` を「**最初の 1 命令**」と「制御演算子から後ろ（＝末尾）」に割る。

        kill -9 123 | cat      → ("kill -9 123", " | cat")
        pkill -f x &           → ("pkill -f x", " &")
        pkill -f x & echo done → ("pkill -f x", " & echo done")

    ★なぜ要るか（2026-09-05・第4回c の 4 回目・実物で死んだ）: 門は断片の**先頭**が
      kill 系のときだけ働くので、`&`（背景化）や `| cat` が付いただけで
      `_NAME_KILL_RE` の `\s*$` にも `_KILL_TARGET_RE` の白名簿にも当たらず、
      **fail-open で素通し**になっていた。末尾は剥がして**そのまま書き戻す**。
    ★`2>&1` / `>&2` の `&` は演算子ではない（直前が `>`/`<`）。引用符・`$( … )`・
      `` ` … ` `` の中は見ない。
    """
    i, n = 0, len(s)
    q: str | None = None
    depth = 0
    while i < n:
        ch = s[i]
        if q:
            if q == '"' and ch == "\\":
                i += 2; continue
            if ch == q:
                q = None
            i += 1; continue
        if ch == "\\":
            i += 2; continue
        if ch in "\"'":
            q = ch; i += 1; continue
        if ch == "`":
            j = s.find("`", i + 1)
            i = (j + 1) if j > i else n
            continue
        if ch == "$" and i + 1 < n and s[i + 1] == "(":
            depth += 1; i += 2; continue
        if ch == "(":
            depth += 1; i += 1; continue
        if ch == ")":
            depth = max(0, depth - 1); i += 1; continue
        if depth:
            i += 1; continue
        if ch in ";|" or (ch == "&" and (not i or s[i - 1] not in "><")):
            head = s[:i].rstrip()
            return head, " " + s[i:]
        i += 1
    return s, ""


def _quoted_spans(s: str) -> list[tuple[int, int]]:
    """引用符に囲まれた範囲（`'…'` / `"…"`）。末尾剥がしがここを噛まないため。"""
    out: list[tuple[int, int]] = []
    i, n, q, start = 0, len(s), None, 0
    while i < n:
        ch = s[i]
        if q:
            if q == '"' and ch == "\\":
                i += 2; continue
            if ch == q:
                out.append((start, i + 1)); q = None
            i += 1; continue
        if ch == "\\":
            i += 2; continue
        if ch in "\"'":
            q = ch; start = i
        i += 1
    if q:
        out.append((start, n))
    return out


def _strip_redirect_tail(s: str) -> tuple[str, str]:
    r"""末尾のリダイレクト（`2>/dev/null` / `0<&-` / `>&3` / `< pids.txt`）を剥がす。

    ★引用符の中は末尾と読まない（2026-09-05・第4回c の 7 回目）: `pkill -f "a <b"` の
      ` <b"` を末尾として切ると残りが `pkill -f "a` になり、引用符が閉じないので
      `_split_kill_args` が None＝**素通し**に戻る。`>` 側は旧実装から在った穴で、
      入力側（反証 M3）を足すと踏みやすくなるので、ここで一緒に閉じる。
    戻り: (本体, 末尾)。剥がす物が無ければ (s, "")。
    """
    m = _KILL_TAIL_RE.search(s)
    if not m:
        return s, ""
    for a, b in _quoted_spans(s):
        if m.start() < b and a < m.end():        # 引用符の中に食い込む＝触らない
            return s, ""
    return s[:m.start()], " " + m.group(0).strip()


def _split_pipeline(cmd: str) -> list[str]:
    """パイプラインを要素と `|` に割る（引用符・`$( … )` の中は割らない）。

    ★`ps aux | grep x | xargs kill` の**どの位置**に kill 系が来ても門に通すため。
      `foo | pkill -f x` は末尾剥がしだけでは拾えない（先頭が `foo`）。
    ⚠ `||` は `_split_segments` の仕事なので、ここでは割らない。
    """
    out, cur, q = [], [], None
    depth = 0
    i, n = 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if q:
            cur.append(ch)
            if ch == q: q = None
            i += 1; continue
        if ch in ("\"", "'"):
            q = ch; cur.append(ch); i += 1; continue
        if ch == "`":
            # ★`` ` … ` `` の中の `|` で割らない（2026-09-06・第4回c の 8 回目）。
            #   `_split_segments` は 5 回目から逆引用符を守っていたのに、
            #   **こちらだけ守っていなかった**（この現場の常習「片方だけ直る」）。
            #   実測: ``kill -9 `ps -ef | grep X | awk '{print $2}'` `` が 3 段に
            #   割れ、`kill -9 \`ps -ef ` の対象が白名簿に当たらず `_parse_kill` が
            #   None＝**門が一度も見ない**まま素通しだった。
            j = cmd.find("`", i + 1)
            if j > i:
                cur.append(cmd[i:j + 1]); i = j + 1; continue
            cur.append(ch); i += 1; continue
        if ch == "$" and i + 1 < n and cmd[i + 1] == "(":
            depth += 1; cur.append("$("); i += 2; continue
        if ch == "(":
            depth += 1; cur.append(ch); i += 1; continue
        if ch == ")":
            depth = max(0, depth - 1); cur.append(ch); i += 1; continue
        if ch == "#" and not depth and (i == 0 or cmd[i - 1].isspace()):
            # ★コメントの中の `|` で割らない（2026-09-06・第4回c の 8 回目）。
            j = cmd.find("\n", i)
            j = n if j < 0 else j
            cur.append(cmd[i:j]); i = j; continue
        if ch == "|" and not depth and not cmd.startswith("||", i):
            sep = "|&" if cmd.startswith("|&", i) else "|"
            out.append("".join(cur)); cur = []
            out.append(sep); i += len(sep); continue
        cur.append(ch); i += 1
    out.append("".join(cur))
    return out


def _proc_table() -> list[tuple[int, str, str, str, tuple[str, ...]]]:
    """`(pid, comm, cmdline, cwd, argv)` の一覧。★/proc を直接読む＝**自分の argv に
    一致する `pgrep` を起こさない**（`pgrep -f` は自分を拾って自分を殺す・1 日 5 回）。

    ★実体は `laps/procutil.py` に 1 本化した（2026-09-05・第4回b の統合）。
      「プロセスを見る」処理を 2 つ持つと必ず片方だけ直る——この現場が何度も
      踏んだ形で、第4回では実際に 2 本のブランチがこの表を別々に持って衝突した。
      名前はここに残す（試験が `bash._proc_table` を差し替えている）。
    ★5 番目の `argv`: `cmdline` は空白で結合済みでトークン境界が失われている。
      所有権判定はトークン単位で見る必要があるので別に持つ。古い 4 要素の
      差し替えでも壊れないよう、tuple を読む側は `_row()` / `_proc_rows()` を通すこと。
    """
    return procutil.proc_table()


def _row(t) -> dict:
    """プロセス表の 1 行（tuple）を `procutil` が読む dict にする。

    ★4 要素（argv 無し）でも受ける: 試験が `_proc_table` を差し替えており、
      第4回の 12 領域で唯一の衝突がこの表の形だった。**形の違いで落ちない**ように
      する（片方の版だけが正しい、という状態を作らない）。
    """
    return {"pid": t[0], "comm": t[1], "cmdline": t[2], "cwd": t[3],
            "argv": (t[4] if len(t) > 4 else ())}



def _pid_exe_id(pid: int) -> tuple | None:
    """`/proc/<pid>/exe` が指す実行ファイルの `(st_dev, st_ino)`。読めなければ None。

    ★`killall /usr/bin/python3` は**名前ではなく実行ファイル**で選ぶ（killall(1)）。
      門が comm と文字列比較していたので一致 0＝「巻き添えなし」で素通しだった。
    """
    try:
        st = os.stat(f"/proc/{pid}/exe")
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


def _proc_rows() -> list[dict]:
    """`_proc_table()`（試験が差し替える口）を `procutil.in_workspace` が読む形に直す。

    ★`pgid` を必ず載せる（2026-09-05）: 最初これを落としていたせいで、
      プロセスグループ撃ち（`kill -- -N`）の中身検査が**本番では一度も発火できない**
      形になっていた（試験は `_proc_rows` 自体を差し替えていたので緑）。
      この現場の「書いたが繋いでいない」を自分でもう一度作りかけた。
    """
    rows = []
    for t in _proc_table():
        r = _row(t)
        r["pgid"] = procutil.pgid_of(r["pid"])
        rows.append(r)
    return rows


#: 台帳（`~/.blackwall/bg/<pid>.meta.json`）の掃除と、PID 再利用の許容差。
#: ★値の実体は `procutil` に 1 本化した（2026-09-05・第4回b の統合）。ここに残すのは
#:   名前だけ（試験がこの綴りを見ている）。同じ数字を 2 か所で持たない。
_BG_META_TTL_S = int(procutil._BG_KEEP_HOURS_DEFAULT * 3600)
_BG_START_TOLERANCE_S = int(procutil._BG_START_SLACK_SEC)


def _registered_bg_pids(workspace: str | None = None) -> set[int]:
    """自分（このエージェント）が `run_in_background` で立てた子の PID 集合。

    ★所有権判定の一次シグナル: `_spawn_background` は起動のたびに
      `<pid>.meta.json` を台帳に書く。cwd/argv によるパス判定は「相対パスで
      起動した自分のサーバ」を『中』と判定できないことがある（run50 の工程4 で
      自分のサーバを再起動できず不合格になった実例＝ここを狭めすぎると再発する）。
    ★ただし**無条件ではない**（2026-09-05）: 起動時刻（`start_ticks` か
      `started_at`）が合わない番号は PID 再利用とみなして外す
      （`procutil.bg_record_matches`）。さらに**作業場**も突き合わせる
      （`procutil.bg_record_workspace_ok`・第4回c）——台帳は `LAPS_HOME` を
      共有する同居セッション全部の物なので、見ないと「ホスト共通の kill 許可証」になる。
    ★実体は `procutil.owned_bg_pids`。同じ台帳の読み方を bash.py /
      bash_output.py / procutil.py が別々に持っていたのが第4回の衝突の半分だった
      ——「同じ物を 2 つ持つと片方だけ直る」を作らない。
    ★**ここで掃除しない**（2026-09-05・第4回c）: 以前は判定のたびに
      `procutil.prune_bg_records()` を呼んでいた。台帳は `LAPS_HOME` を
      共有する**同居セッション全部**の物なので、こちらが `kill` を 1 回判定する
      たびに**他セッションの背景処理の出力（`.out`/`.err`）を消していた**——
      「読む処理が、他人の書いた物を消す」形。掃除は起こす側
      （`_spawn_background`）と、明示の保守経路（`procutil.prune_bg_records`）だけ。
    ★`workspace` は台帳の絞り込みに使う（渡さないときは正典を使う）。
    """
    try:
        return procutil.owned_bg_pids(workspace)
    except Exception:
        return set()


def _path_within(path: str, ws: str) -> bool:
    """`procutil.path_within` の別名（この名前を使う試験・呼び出しが在る）。"""
    return procutil.path_within(path, ws)


def _proc_is_mine(pid: int, cwd: str, argv: tuple[str, ...], ws: str,
                  bg_pids: set[int], cmdline: str = "") -> bool:
    """このプロセスが「自分の物（作業場の中）」か。`_scope_name_kill` と
    `_scope_pid_kill` が共有する**唯一の**判定（実体は `procutil.in_workspace`）。"""
    if pid in bg_pids:
        return True
    return procutil.in_workspace(
        {"pid": pid, "cwd": cwd, "argv": argv, "cmdline": cmdline or " ".join(argv)}, ws)


# ── 明示 PID / プロセスグループの kill を絞る ────────────────────────────
#: `kill` として扱う綴り（`/bin/kill` は記録に実在する形）。
_KILL_PROGS = {"kill", "/bin/kill", "/usr/bin/kill"}
#: 先頭に 1 つだけ許す信号指定（`-9` / `-KILL` / `-SIGTERM`）。
_SIG_OPT_RE = re.compile(r"^-(?:[0-9]{1,2}|[A-Za-z][A-Za-z0-9]*)$")
#: 置き換えを解決できる形（記録の 44 件中 12 件がこの 2 つ）。
_SUB_CAT_RE = re.compile(r"^(?:\$\(|`)\s*cat\s+(?P<path>[^\s;|&`)]+)\s*(?:\)|`)$")
_SUB_LSOF_RE = re.compile(r"^(?:\$\(|`)\s*lsof\b(?P<args>[^)`]*)(?:\)|`)$")
#: ★`$(pgrep …)` / `` `pgrep …` ``（2026-09-06・第4回c の 8 回目・反証 M-B の F5/F6）。
#:   `kill -9 $(pgrep -f X)` は `pkill -f X` と同じ相手を撃つ。実物は起こさず、
#:   `_name_kill_matcher`（＝pkill と同じ照合器）で「当たる相手」を数える。
_SUB_PGREP_RE = re.compile(
    r"^(?:\$\(|`)\s*(?:[^\s;|&`)]*/)?pgrep\b(?P<args>[^)`]*)(?:\)|`)$")
#: ★`pgrep` で**読める**綴りだけの許可リスト（許可リストにするのは反証 A と同じ理由：
#:   「読み飛ばせた」を「正しく読めた」と扱わない）。`-c`（件数だけ出す）や
#:   `-n`/`-o`（最新/最古 1 本）や `-v`（反転）は**出る物が変わる**ので、
#:   1 つでも在れば解かない＝従来どおり素通し。
_PGREP_OK_LETTERS = set("fxi")
_PGREP_OK_LONG = frozenset({"--full", "--exact", "--ignore-case"})
#: ★`$(ps … | grep … | awk '{print $2}')`（2026-09-06・第4回c の 8 回目）。
#:   **実記録（run50〜53）に在る形**——replay の「PID ゲートが見る対象のうち素通し」
#:   5 件の 1 つがこれ（`$(ps -ef | grep "php -S 0.0.0.0:18300" | grep -v grep
#:   | awk '{print $2}')`）。`pgrep -f` と同じ「綴りから相手を選ぶ」idiom なので
#:   同じ床に載せる。⚠ **解ける形だけ解く**（下の 3 つの読み手のどれかが None を
#:   返したら丸ごと諦める＝従来どおり素通し）。
_SUB_PS_PIPE_RE = re.compile(r"^(?:\$\(|`)\s*(?P<body>ps\b[\s\S]*)(?:\)|`)$")
#: `ps` の綴りのうち**列の並びが分かる**物だけ（未知の綴りは解かない）。
#:   `-f`/`-F`/`u` が在れば PID は 2 列目、無ければ 1 列目。
_PS_OK_LETTERS = set("efFAauxw")
#: `awk '{print $N}'`（`cut` や `awk -F` は解かない）。
_AWK_PRINT_RE = re.compile(
    r"^\s*awk\s+(?P<q>['\"])\s*\{\s*print\s*\$(?P<col>\d+)\s*\}\s*(?P=q)\s*$")
#: ★`$(echo 123)`（反証 M-B の F8）。中身が十進の数字だけのときに限り解く。
_SUB_ECHO_RE = re.compile(r"^(?:\$\(|`)\s*echo\s+(?P<args>[^)`|;&]*)(?:\)|`)$")
#: `kill` の後ろに付いてくる末尾（`2>/dev/null` / `0<&-` / `< pids.txt`）。
#: ★2026-09-05（第4回c の 7 回目・反証 M3）: 旧 `(?:\s+[0-9]?>>?\s*\S+)+\s*$` は
#:   **出力**リダイレクトしか剥がさなかった。`pkill -9 -f "X" 0<&-` は
#:   `_split_kill_args` が `['"X"', '0<&-']` の 2 語に割り、
#:   「pkill のパターンは 1 つ」で `_parse_name_kill` が None＝**素通し**——
#:   実測で保護ポートのサーバが死んだ（T12）。入力側も同じ末尾として剥がす。
_KILL_REDIR = r"[0-9]?(?:>>?|<(?!<))\s*&?\s*[^\s<>|;&]+"
_KILL_TAIL_RE = re.compile(r"(?:\s+(?:" + _KILL_REDIR + r"))+\s*$")
#: 対象として認める形（数字 / ジョブ / 置き換え）。それ以外は**触らない**。
#: ★`+123`（2026-09-05・第4回c の 4 回目・実物で死んだ形 B13）: bash 組み込みの
#:   `kill` は `kill -9 +<pid>` を**受理して撃つ**。白名簿が `-?\d+` だったので
#:   `_parse_kill` が None を返し、門が一度も見ないまま素通しになっていた。
_KILL_TARGET_RE = re.compile(r"^(?:[+-]?\d+|%\S+|\$[({]?\S[\s\S]*|`[\s\S]*`)$")
#: `kill` の**前**に置かれても、そのまま kill が走る前置き（コマンド位置の語）。
#: ★2026-09-05（第4回c の再反証）: 門の入口は「断片の**先頭**が kill」だったので、
#:   `sudo kill <pid>`・`timeout 5 kill <pid>`・`nohup kill <pid> &`・
#:   `command kill <pid>`・`if …; then kill $(cat server.pid); fi` の `then` は
#:   全部**素通し**だった。最後の形は run50〜53 の実記録で `kill` 系 50 断片中 **6 件**＝
#:   「どちらも見ない残る穴」の**全部**。前置きを剥がして、剥がした分は書き戻す。
#: ★`if` / `while` / `until` / `case … in <ラベル>)`（2026-09-05・第4回c の 5 回目・
#:   反証 B(3)）: 実物で 4 形とも保護ポートのサーバが死んだ。`then` / `do` は在ったのに
#:   **条件の側**（`if pkill …; then`）が抜けていた——同じ構文の片側だけ直す形の再来。
_KILL_PREFIX_WORDS = frozenset({
    "then", "do", "else", "elif", "{", "!", "sudo", "doas", "command", "builtin",
    "exec", "env", "nohup", "setsid", "stdbuf", "nice", "ionice", "time",
    "unbuffer", "timeout", "chrt",
    "if", "while", "until", "case", "in",
    # ★`busybox pkill …`（2026-09-05・第4回c の 5 回目・反証役の probe4 が並べた形）。
    #   busybox は**次の語がアプレット名**なので、前置きとして剥がせば `pkill` が出る。
    #   ⚠ このホストに busybox は入っていないので、実物の kill では確かめていない
    #      （`_scope_name_kill` が書き換えることまでは試験で確かめた）。
    "busybox",
    # ★`function f { … }` / `coproc C { … }`（2026-09-05・第4回c の 7 回目・反証 M2）。
    "function", "coproc",
})
#: 引数を 1 つ食う前置き（`timeout 5 kill …` / `case <語> in …`）。
_KILL_PREFIX_ONE_ARG = frozenset({"timeout", "chrt", "case"})
#: 前置きの**オプションが引数を 1 つ取る**もの。★`sudo -u root pkill …`（反証 B(7)）は
#:   `-u` を旗として食い、残った `root` で前置き剥がしが止まり、門が**一度も見ない**まま
#:   素通しになっていた（実物で保護ポートのサーバが死んだ）。
_KILL_PREFIX_OPT_ARGS = {
    "sudo": frozenset({"-u", "--user", "-g", "--group", "-C", "--close-from",
                       "-p", "--prompt", "-h", "--host", "-r", "--role",
                       "-t", "--type", "-U", "--other-user", "-R", "--chroot",
                       "-D", "--chdir"}),
    "doas": frozenset({"-u", "-C"}),
    # ★`-S` / `--split-string` は**引数を食わせない**（2026-09-05・第4回c の 7 回目・
    #   自分の監査で見つけた穴）。食うと `env -S pkill -9 -f X` の `pkill` が前置きに
    #   消え、残りが読めず**黙って素通し**になる（実物の env は pkill を起こす）。
    #   引用符つきの `env -S "pkill …"` は `_NESTED_ENV_S_RE` が入れ子として開く。
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "-P", "-u", "-t"}),
    "chrt": frozenset({"-p"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
}
#: 変数代入の前置き（`FOO=1 kill …`）。
_KILL_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: `case … in` のラベル（`x)` / `*php*)`）。`$( … )` は括弧の開きを持つので当たらない。
_CASE_LABEL_RE = re.compile(r"^[^()\s]*\)$")
#: ★シェル関数の定義の頭（`f()` / `g(){`）。2026-09-05・第4回c の 7 回目・反証 M2:
#:   `_KILL_PREFIX_WORDS` に `function` と `NAME()` が無かったので、
#:   `f() { pkill -9 -f "X"; }; f` / `function f { … }` は前置き剥がしがそこで止まり、
#:   `_parse_name_kill` が `pkill` に届かないまま**素通し**だった（実測 5 形とも
#:   保護ポートのサーバが死んだ: N01 / N02 / T09 / T10 / S18）。
#:   `{` は既に前置き語なので、頭さえ剥がせば本体が門に出る。
_FUNC_DEF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\{?$")
#: `function NAME { … }` / `coproc NAME { … }` の**名前**（次の語）を食う語。
#:   ⚠ `coproc pkill -9 -f X`（名前なしの単純命令）も在るので、無条件には食わない
#:     ——食うと `pkill` が前置きに消えて、残りが読めず素通しに戻る。
#:     「名前が `()` で終わる」か「その次の語が `{`」のときだけ食う。
_KILL_PREFIX_NAMED_BLOCK = frozenset({"function", "coproc"})
_BLOCK_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


#: `$` が**展開の始まり**になる次の 1 文字（bash）。これ以外の `$` は素の文字。
#:   ★`pkill -f "myserver$"` の `$` は正規表現のアンカー＝**展開ではない**。
#:     ここを雑に「`$` が在れば読めない」にすると、正当な形まで素通しに戻る。
_EXPANSION_NEXT = set("{(*@#?$!-_0123456789") | set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _is_expansion(tok: str, i: int) -> bool:
    """`tok[i] == "$"` が展開の始まりか（末尾の `$` や `"5$"` は素の文字）。"""
    return i + 1 < len(tok) and tok[i + 1] in _EXPANSION_NEXT


#: `$'…'`（ANSI-C 引用）の 1 文字エスケープ。
_ANSI_C_ESC = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
               "n": "\n", "r": "\r", "t": "\t", "v": "\v",
               "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(body: str) -> str | None:
    r"""`$'…'` の中身を**シェルが渡す文字列**へ。読めない綴りは None。"""
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch); i += 1; continue
        if i + 1 >= n:
            return None
        e = body[i + 1]
        if e in _ANSI_C_ESC:
            out.append(_ANSI_C_ESC[e]); i += 2; continue
        if e == "x":
            m = re.match(r"[0-9A-Fa-f]{1,2}", body[i + 2:])
            if not m:
                return None
            out.append(chr(int(m.group(0), 16))); i += 2 + m.end(); continue
        if e.isdigit():
            m = re.match(r"[0-7]{1,3}", body[i + 1:])
            if not m:
                return None
            out.append(chr(int(m.group(0), 8))); i += 1 + m.end(); continue
        return None                       # 読めない＝触らない
    return "".join(out)


def _prog_literal(tok: str) -> str | None:
    r"""コマンド位置の 1 語から**引用符とバックスラッシュを外した綴り**を返す。

    ★なぜ要るか（2026-09-05・第4回c の 5 回目・反証 B(4)）: シェルは
      `\pkill` / `p"kill"` / `pk\ill` / `p'kill'` を**すべて `pkill` として実行する**のに、
      門は生の綴りを `_KILL_PROGS` や `("pkill","killall")` と文字列比較していたので
      4 形とも素通しだった（実物で保護ポートのサーバが死んだ）。
    ★展開（`$X` / `` ` ` ``）が混ざる語は**読めない**＝None（従来どおり触らない）。
      「読み飛ばせた」を「正しく読めた」と扱わない。
    ★`$'…'`（ANSI-C 引用）と `$"…"`（訳語つき二重引用）は読む（第4回c の 5 回目・
      反証 B(5)）。**パターン位置でもこの関数を通す**: 旧 `_unquote` は囲みが同じ
      引用符のときしか外さないので `pkill -f $'X'` は `$'X'` が丸ごとパターンになり、
      先頭の `$` が正規表現のアンカーとして働いて**一致 0**——門は「巻き添えなし」と
      読んで素通しにし、実物の pkill が保護ポートのサーバを撃った。
      **「読めなかった」が「安全だった」に化ける**形なので、読むか None にする。
    """
    out: list[str] = []
    i, n = 0, len(tok)
    q: str | None = None
    while i < n:
        ch = tok[i]
        if q == "'":
            if ch == "'":
                q = None
            else:
                out.append(ch)
            i += 1
            continue
        if q == '"':
            if ch == '"':
                q = None
            elif ch == "\\" and i + 1 < n and tok[i + 1] in '"\\$`\n':
                out.append(tok[i + 1]); i += 1
            elif ch == "`" or (ch == "$" and _is_expansion(tok, i)):
                # ★二重引用符の中でも `$VAR` と `` ` ` `` は展開する（第4回c の 5 回目）。
                #   ここを素通ししていたので `pkill -f "$NAME"` はパターンが `$NAME` になり、
                #   先頭の `$` がアンカーとして働いて**一致 0＝巻き添えなし**——
                #   `$'…'`（反証 B(5)）とまったく同じ「読めなかったが安全に見える」形。
                return None
            else:
                out.append(ch)
            i += 1
            continue
        if ch in "\"'":
            q = ch; i += 1; continue
        if ch == "$" and i + 1 < n and tok[i + 1] == "'":
            j = i + 2
            while j < n:                        # `\'` は終端ではない
                if tok[j] == "\\":
                    j += 2; continue
                if tok[j] == "'":
                    break
                j += 1
            if j >= n:
                return None
            body = _ansi_c(tok[i + 2:j])
            if body is None:
                return None
            out.append(body); i = j + 1; continue
        if ch == "$" and i + 1 < n and tok[i + 1] == '"':
            q = '"'; i += 2; continue           # `$"…"` は訳語つき＝中身は同じ
        if ch == "\\":
            if i + 1 >= n:
                return None
            out.append(tok[i + 1]); i += 2; continue
        if ch == "`":
            return None                    # コマンド置換＝読めない
        if ch == "$":
            if _is_expansion(tok, i):
                return None                # 展開＝読めない
            out.append(ch); i += 1; continue   # `myserver$` のアンカー等
        out.append(ch); i += 1
    if q:
        return None
    return "".join(out)


def _prefix_word(tok: str) -> str | None:
    """前置き語としての綴り（引用符・バックスラッシュ・パスを外した基底名）。"""
    lit = _prog_literal(tok)
    if lit is None:
        return None
    return os.path.basename(lit) or lit


def _split_kill_args(s: str) -> list[str] | None:
    """`kill` の行を語に割る。`$( … )` と `` ` … ` `` は**1 語として保つ**
    （`shlex` は `$(` を知らないので `['$(cat', 'x.pid)']` に割れてしまう）。
    引用符/括弧が閉じていなければ None（＝触らない）。"""
    toks: list[str] = []
    cur: list[str] = []
    q: str | None = None
    depth = 0
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if q:
            cur.append(ch)
            if ch == q:
                q = None
            i += 1
            continue
        if depth:
            cur.append(ch)
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
            continue
        if ch == "$" and i + 1 < n and s[i + 1] == "(":
            cur.append("$("); depth = 1; i += 2; continue
        if ch in "\"'`":
            q = ch; cur.append(ch); i += 1; continue
        if ch.isspace():
            if cur:
                toks.append("".join(cur)); cur = []
            i += 1; continue
        cur.append(ch); i += 1
    if q or depth:
        return None
    if cur:
        toks.append("".join(cur))
    return toks


def _parse_kill(seg: str) -> dict | None:
    """`kill` の 1 断片を（prog / 信号 / `--` / 対象 / 末尾）に分解する。

    ★旧実装は 1 本の正規表現 `kill [-SIG]... <正の十進>...` で、**負の対象**
      （`kill -9 -1` ＝ 自分が送れる全プロセス、`kill -- -1750` ＝ プロセスグループ）と
      **`0`**（自分のプロセスグループ）が丸ごと素通りしていた（第4回の反証）。
      `kill -9 -1` は root では実質「このホストを止める」に等しい。
    ★POSIX の `kill` は信号指定を**先頭 1 つだけ**取る。だから `kill -9 -1` の
      `-9` は信号、`-1` は対象。位置で決める（後続の `-N` を信号と読まない）。
    """
    # ★末尾（`| cat` / `& echo x` / `2>/dev/null`）は剥がして**そのまま書き戻す**。
    #   旧実装は「行末が `&`」だけを見ており、`kill -9 <pid> | cat` は
    #   `_KILL_TARGET_RE` の白名簿に `|` が無いので None＝**fail-open で素通し**
    #   （2026-09-05・第4回c の 4 回目・実物で保護ポートのサーバが死んだ B2）。
    # ★コメントは**いちばん先に**剥がす（2026-09-06・第4回c の 8 回目・反証 M-A）。
    #   `#` から行末までは `;` `|` `&` `>` を含んでいても全部コメントなので、
    #   末尾剥がしより先に落とさないと `kill -9 <pid> # cleanup` が
    #   `['kill','-9','<pid>','#','cleanup']` に割れ、対象の白名簿に当たらず
    #   `_parse_kill` が None＝**門が一度も見ない**まま素通しだった。
    raw, com = _strip_comment_tail(seg.strip())
    s, tail = _split_control_tail(raw)
    s, red = _strip_redirect_tail(s)
    tail = red + tail + com
    toks = _split_kill_args(s)
    if not toks:
        return None
    # ★前置きを剥がす（剥がした語はそのまま書き戻す）。`sudo`/`timeout`/`then` 等が
    #   1 語あるだけで門の外に出られていた（第4回c の再反証）。知らない語が来たら
    #   そこで諦める（＝fail-open。従来どおり触らない）。
    pre: list[str] = []
    i = 0
    while i < len(toks) and _prog_literal(toks[i]) not in _KILL_PROGS:
        w = _prefix_word(toks[i])
        if _KILL_ASSIGN_RE.match(toks[i]):
            pre.append(toks[i]); i += 1
            continue
        if w and (_CASE_LABEL_RE.match(w) or _FUNC_DEF_RE.match(w)):
            pre.append(toks[i]); i += 1          # `case x in x) kill …` / `f() {`
            continue
        if w not in _KILL_PREFIX_WORDS:
            break
        pre.append(toks[i]); i += 1
        optargs = _KILL_PREFIX_OPT_ARGS.get(w, frozenset())
        while i < len(toks) and (toks[i].startswith("-") or "=" in toks[i]):
            opt = toks[i].partition("=")[0]
            pre.append(toks[i]); i += 1
            # ★`sudo -u root pkill …`: 引数を取るオプションは**その引数も**食う。
            if opt in optargs and "=" not in toks[i - 1] and i < len(toks):
                pre.append(toks[i]); i += 1
        if w in _KILL_PREFIX_ONE_ARG and i < len(toks):
            pre.append(toks[i]); i += 1          # `timeout 5 kill …` の 5
        if w in _KILL_PREFIX_NAMED_BLOCK and i < len(toks):
            nm = _prefix_word(toks[i]) or toks[i]
            if _FUNC_DEF_RE.match(nm) or (
                    _BLOCK_NAME_RE.match(nm) and i + 1 < len(toks)
                    and (_prefix_word(toks[i + 1]) or toks[i + 1]).startswith("{")):
                pre.append(toks[i]); i += 1      # `function f {` の f（反証 M2）
    if i >= len(toks) or _prog_literal(toks[i]) not in _KILL_PROGS:
        return None
    prefix = "".join(t + " " for t in pre)
    prog = toks[i]
    i += 1
    sig: list[str] = []
    if i + 1 < len(toks) and toks[i] in ("-s", "-n"):
        sig = [toks[i], toks[i + 1]]; i += 2
    elif i + 1 < len(toks) and toks[i] != "--" and _SIG_OPT_RE.match(toks[i]):
        sig = [toks[i]]; i += 1
    sep = False
    if i < len(toks) and toks[i] == "--":
        sep = True; i += 1
    targets = toks[i:]
    if not targets:
        return None
    # ★対象の**形**を白名簿で確かめる。`kill 101 & echo x` のように 1 断片の中へ
    #   別の命令が続く形（`_split_segments` は単独の `&` や `|` で割らない）を
    #   そのまま書き換えると `kill & echo x` のような**壊れた命令**を作る。
    #   知らない形は触らない（fail-open）。
    #   ★引用符つきの数字（`kill "12345"`）も受ける（2026-09-05・第4回c の 4 回目・
    #     再生 ⑤ の盲点）。bash は素通しで撃つのに、白名簿が引用符を知らないので
    #     `_parse_kill` が None＝fail-open だった。**綴りは変えずに**中身だけ読む。
    if not all(_KILL_TARGET_RE.match(_unquote(t)) for t in targets):
        return None
    return {"prefix": prefix, "prog": prog, "sig": sig, "sep": sep,
            "targets": targets, "tail": tail}


def _resolve_kill_token(tok: str, base: str | None) -> list[int] | None:
    """置き換え（`$( … )`）の対象を PID に解決する。解決できなければ None。

    ★どこまで解くか（2026-09-05・記録で決めた）: run50〜53 の kill 系 44 件のうち
      `$(cat <file>)` が 8 件、`$(lsof -t -i :<port>)` が 4 件で**合わせて 12 件**＝
      素通りしていた形の主力。この 2 つだけ**自分で読んで**解く（外部コマンドは
      起こさない——`pgrep -f` 自己一致の再来を避ける）。
      `$!` `%1` `$(jobs -p)` `$(ps … | grep …)` は解かない（＝従来どおり素通し）。
      前者 3 つはシェル自身の子＝構造上いつも「自分の物」なので実害が無い。
    ★解けた結果で**撃つ側を広げることはしない**。使うのは「巻き添えが居るか」の
      判定だけで、居なければ命令は 1 文字も変えない（誤解決で壊さない）。
    """
    m = _SUB_CAT_RE.match(tok)
    if m:
        path = m.group("path")
        if not os.path.isabs(path):
            path = os.path.join(base or os.getcwd(), path)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw = fh.read(65536)     # ★上限つき（pid ファイルは数十バイト）
        except OSError:
            return None
        # ★中身は**外へ出さない**（判定に使うだけ）。数字も先頭 16 個で打ち切る——
        #   `$(cat /etc/shadow)` のような形で他人のファイルを読ませ、注記に
        #   数字を並べさせる経路を作らないため。
        pids = [int(x) for x in re.findall(r"\b\d{1,7}\b", raw)[:16]]
        return pids or None
    m = _SUB_LSOF_RE.match(tok)
    if m:
        pm = re.search(r":(\d+)", m.group("args"))
        if not pm:
            return None
        port = int(pm.group(1))
        owners = procutil.port_listeners(port)
        if not owners:
            return None                  # 持ち主が見えない＝分からない（素通し）
        return [r["pid"] for r in owners]
    m = _SUB_PGREP_RE.match(tok)
    if m:
        return _pgrep_match_pids(m.group("args"))
    m = _SUB_PS_PIPE_RE.match(tok)
    if m:
        return _ps_pipe_match_pids(m.group("body"))
    m = _SUB_ECHO_RE.match(tok)
    if m:
        vals = [_unquote(t) for t in (_split_kill_args(m.group("args")) or [])]
        if vals and all(re.fullmatch(r"\d{1,7}", v) for v in vals):
            return [int(v) for v in vals]
        return None
    return None


def _ps_pid_column(head: str) -> int | None:
    """`ps <綴り>` の出力で PID が何列目か（分からなければ None）。"""
    toks = _split_kill_args(head)
    if not toks:
        return None
    prog = _prog_literal(toks[0])
    if prog is None or os.path.basename(prog) != "ps":
        return None
    letters = ""
    for t in toks[1:]:
        if t.startswith("--"):
            return None                      # 長い綴りは読まない
        lit = _prog_literal(t)
        if lit is None:
            return None
        letters += lit.lstrip("-")
    if any(c not in _PS_OK_LETTERS for c in letters):
        return None                          # 未知の綴り＝列が分からない
    return 2 if set("fFu") & set(letters) else 1


def _grep_filter(stage: str):
    """`grep [-v] [-i] [-E|-F] PATTERN` を `(反転か, 照合関数)` にする。読めなければ None。

    ★BRE と ERE で意味が変わる綴りは**解かない**。`_compile_kill_re` は
      `REG_EXTENDED` なので、素の `grep`（BRE）のパターンに `+ ? { } ( ) |` や
      `\\` が在ると門の的が実物より**広くなる**＝正当な用途を潰す側へ倒れる。
    """
    toks = _split_kill_args(stage)
    if not toks:
        return None
    prog = _prog_literal(toks[0])
    if prog is None:
        return None
    base = os.path.basename(prog)
    if base not in ("grep", "egrep", "fgrep"):
        return None
    neg = icase = False
    ere = base == "egrep"
    fixed = base == "fgrep"
    pats = []
    for t in toks[1:]:
        if t.startswith("-") and len(t) > 1:
            if t.startswith("--"):
                return None
            for c in t[1:]:
                if c == "v":
                    neg = True
                elif c == "i":
                    icase = True
                elif c == "E":
                    ere = True
                elif c == "F":
                    fixed = True
                else:
                    return None              # 引数を取る綴りかもしれない＝諦める
            continue
        pats.append(_prog_literal(t))
    if len(pats) != 1 or pats[0] is None:
        return None
    pat = pats[0]
    if fixed:
        if icase:
            return neg, (lambda h, p=pat.lower(): p in h.lower())
        return neg, (lambda h, p=pat: p in h)
    if not ere and re.search(r"[+?{}()|\\]", pat):
        return None                          # BRE ↔ ERE で的が変わる綴り
    fn, why = _compile_kill_re(pat, icase=icase, exact=False)
    if why:
        return None
    return neg, fn


def _ps_pipe_match_pids(body: str) -> list[int] | None:
    """`ps … | grep … | awk '{print $N}'` が返す PID を、実物を起こさずに数える。

    解けなければ None（＝従来どおり素通し）。★照合の的は **cmdline だけ**にする。
    実物の `ps` の行には UID や時刻の列も在るが、そこまで真似ると門の的が
    実物より**広くなる**（＝正当な用途を潰す）。取りこぼす方に倒す。
    """
    stages = [s.strip() for s in _split_pipeline(body) if s not in ("|", "|&")]
    stages = [s for s in stages if s]
    if len(stages) < 3:
        return None                          # ps と awk と grep が 1 つは要る
    col = _ps_pid_column(stages[0])
    if col is None:
        return None
    m = _AWK_PRINT_RE.match(stages[-1])
    if not m or int(m.group("col")) != col:
        return None                          # 出るのが PID の列か分からない
    filters = []
    for st in stages[1:-1]:
        f = _grep_filter(st)
        if f is None:
            return None
        filters.append(f)
    if not filters:
        return None
    out: list[int] = []
    for t in _proc_table():
        row = _row(t)
        hay = row["cmdline"] or row["comm"]
        keep = True
        for neg, fn in filters:
            if bool(fn(hay)) == neg:
                keep = False
                break
        if keep:
            out.append(row["pid"])
    return out or None


def _pgrep_match_pids(args: str) -> list[int] | None:
    """`$(pgrep <args>)` が返す PID を、**実物を起こさずに**数える。

    解けなければ None（＝従来どおり素通し）。★解けた結果で**撃つ側を広げない**
    のは `_resolve_kill_token` の約束のまま——使うのは「巻き添えが居るか」だけ。

    ⚠ 限界（自己申告）: `_proc_table()` は自分と親を載せないので、
      `pgrep -f <自分に当たる綴り>` で自分が出る形はここでも見えない
      （名前撃ちの門も同じ表を見ているので、限界は両方で揃っている）。
    """
    toks = _split_kill_args(args)
    if toks is None:
        return None
    for t in toks:
        if t == "--":
            break
        if not t.startswith("-") or len(t) == 1:
            continue
        name = t.partition("=")[0]
        if name.startswith("--"):
            if name not in _PGREP_OK_LONG:
                return None                  # 読めない綴り＝解かない
        elif re.fullmatch(r"-[A-Za-z]+", name):
            if any(c not in _PGREP_OK_LETTERS for c in name[1:]):
                return None
        else:
            return None
    parsed = _parse_name_kill("pkill " + args.strip())
    if not parsed or parsed.get("unreadable") or parsed.get("unscopable"):
        return None
    hit, why = _name_kill_matcher(parsed)
    if why:
        return None
    out: list[int] = []
    for t in _proc_table():
        row = _row(t)
        r = hit(row)
        if r is None:
            return None                      # 当たるか分からない＝解かない
        if r:
            out.append(row["pid"])
    return out or None


#: `$name` / `${name}`（修飾なし）だけ。`${x:-y}` `$1` `$!` `$$` `$*` は**別物**なので
#: 含めない（読めない物を読めた振りにしない）。
_VAR_REF_RE = re.compile(
    r"\$(?:\{(?P<b>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<n>[A-Za-z_][A-Za-z0-9_]*))")
#: 展開してよい語の形（`$( … )` / `` ` … ` `` / `$!` / `$1` などが混ざれば触らない）。
_VAR_ONLY_RE = re.compile(
    r"^(?:[^$`]|\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*)*$")
#: ループの中は**同じ変数が何度も別の値になる**ので、代入の追跡をやめる語。
_LOOP_WORDS = frozenset({"for", "while", "until", "do", "done", "select"})
#: 単純な代入だけの断片（`p=$(pgrep -f X)`）。
_ASSIGN_ONE_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<val>[\s\S]*)$")


def _expand_kill_token(tok: str, kvars: dict | None) -> str | None:
    r"""kill の対象語の `$p` / `${p}` を **この命令の中の代入 → 環境 → 空** の順で展開する。

    戻り: 展開後の文字列 ／ 展開してよい形でなければ None（＝従来どおり触らない）。

    ★なぜ要るか（2026-09-06・第4回c の 8 回目・反証 M-B の F7/F10）:
      `p=$(pgrep -f X); kill -9 $p` と `kill -9 ${p}<pid>` は、どちらも
      PID 側の門が「対象が数字じゃない＝解けない」で素通しし、実測で
      保護ポートのサーバが死んだ。展開そのものは bash の規則どおりで、
      **未定義の変数は空**（`${p}4102` は `4102`）。
    ★解けた結果で**撃つ側を広げない**（`_resolve_kill_token` と同じ約束）。
      使うのは「巻き添えが居るか」の判定だけで、命令の綴りは書き換えない。
    """
    if not tok or not _VAR_ONLY_RE.match(tok) or "$" not in tok:
        return None
    cur = tok
    for _ in range(3):                       # `p=$q` のような入れ子は 3 段まで
        def _sub(m):
            name = m.group("b") or m.group("n")
            if kvars and name in kvars:
                return kvars[name]
            return os.environ.get(name, "")
        nxt = _VAR_REF_RE.sub(_sub, cur)
        if nxt == cur:
            break
        cur = nxt
        if "$(" in cur or "`" in cur:
            break                            # 置換になった＝そちらの解き手へ渡す
    return cur


def _kill_assignment(seg: str) -> tuple[str, str] | None:
    """`p=$(pgrep -f X)` のような**単純な代入だけ**の断片を読む（他は None）。"""
    toks = _split_kill_args(seg.strip())
    if not toks or len(toks) != 1:
        return None
    m = _ASSIGN_ONE_RE.match(toks[0])
    if not m:
        return None
    return m.group("name"), _unquote(m.group("val"))


def _kill_protected_pids() -> set[int]:
    """**絶対に撃たせない** PID: 1（init）／自分と自分の祖先／
    **保護ポートを LISTEN している設備**（`procutil.protected_port_pids`）。

    ★第4回の反証で実測: `_proc_table()` は `getpid()`/`getppid()` を飛ばすので、
      `kill -9 <serve の pid>` も `<term の pid>` も「表に無い＝不明＝素通し」に
      化けていた。**いちばん高くつく相手にだけ穴が開いていた。**
    ★保護ポート（2026-09-05・第4回c・実物で再現）: 22 / 18301 / 18400 の保護は
      `bash_output._stop_group`（ポート経由の停止）に**しか**入っておらず、
      `pkill -f gateway` と `kill <gateway の pid>` は同じ相手を素通しで撃てた
      （作業場が `/home/user/project` なら gateway の cwd `/home/user/project/src` は
      堂々と「中」＝ゲートは inside 判定で**何も書き換えずに通す**）。
      2026-09-03 の dogfood で実際に壊した相手なので、両方の門に同じ床を敷く。
    """
    me = os.getpid()
    out = {1, me, os.getppid()} | set(procutil.ancestors(me))
    try:
        out |= procutil.protected_port_pids()
    except Exception:
        pass
    return out


def _scope_pid_kill(cmd: str, workspace: str | None,
                    base: str | None = None,
                    kvars: dict | None = None) -> tuple[str, str] | None:
    """`kill 12345` / `kill -9 -1` / `kill 0` のような**明示対象の kill** を、
    作業場の中（＋自分が起動した背景処理）だけに絞る（2026-09-05）。

    ★なぜ要るか: `_scope_name_kill` は名前撃ちだけを見ており、明示対象の kill は
      どの層にも一切スコープされていなかった（`workspace_guard` は 0.1.191 で
      kill 系を外し、`policy._ALWAYS_DENIED` にも kill/pkill は無く、既定の
      trusted はそこも素通り）。実測: `kill -9 1`・`kill -9 -1`・`kill 0` が
      どのゲートも見ずに通っていた。
    ★fail-open が既定（分からなければ触らない）。ただし**吹き飛ぶ範囲が
      分からない形は fail-closed**（`-1` ＝送れる全プロセス、`0` ＝自分の
      プロセスグループ＝エージェント自身）。ここだけは「分からないから通す」を
      しない——通した先が全部だから。
    戻り: (書き換えた命令, 添える注記) ／ 触らないなら None。
    """
    if sys.platform == "win32" or not workspace:
        return None
    segs = _split_segments(cmd)
    if len(segs) > 1:
        changed, notes = False, []
        # ★同じ命令の中の代入を**順番に**覚える（`p=$(pgrep -f X); kill -9 $p`）。
        #   ⚠ ループの中は同じ変数が何度も別の値になるので、その命令では覚えない
        #     （覚えると「元の命令が撃たない相手を断る」＝正当用途を潰す側の誤り）。
        track = not any(w in _LOOP_WORDS
                        for w in re.findall(r"[A-Za-z_]+", cmd))
        kv = dict(kvars or {})
        for i, s in enumerate(segs):
            if s in _SEG_SEPS:
                continue
            r = _keep_ws(s, lambda c: _scope_pid_kill(c, workspace, base, kv))
            if r:
                segs[i], n = r; changed = True; notes.append(n)
            if track:
                a = _kill_assignment(s)
                if a:
                    kv[a[0]] = a[1]
        return ("".join(segs), "".join(notes)) if changed else None
    # ★パイプラインの**どの位置**の kill も拾う（`kill -9 <pid> | cat` は白名簿に
    #   `|` が無く fail-open で素通しだった・第4回c の 4 回目 B2）。
    pipes = _split_pipeline(cmd)
    if len(pipes) > 1:
        changed, notes = False, []
        for i, s in enumerate(pipes):
            if s in ("|", "|&"):
                continue
            core = s.strip()
            pre_ws, post_ws = s[:len(s) - len(s.lstrip())], s[len(s.rstrip()):]
            r = _scope_pid_kill(core, workspace, base, kvars)
            if r:
                pipes[i] = pre_ws + r[0] + post_ws
                changed = True; notes.append(r[1])
        return ("".join(pipes), "".join(notes)) if changed else None
    parsed = _parse_kill(cmd)
    if not parsed:
        return None
    ws = os.path.realpath(workspace)
    try:
        bg_pids = _registered_bg_pids(ws)
        table = {pid: (comm, cmdline, cwd, argv)
                 for pid, comm, cmdline, cwd, argv in _proc_table()}
        protected = _kill_protected_pids()
        # ★理由を分けて出す（2026-09-05・第4回c）。「撃たなかった」だけでは、
        #   モデルは同じ相手を別の綴りで撃ち直す（記録で実際に起きた形）。
        try:
            port_protected = procutil.protected_port_pids()
        except Exception:
            port_protected = set()
        own_pgid = os.getpgrp() if hasattr(os, "getpgrp") else None
    except Exception:
        return None                      # 判定材料が作れないなら触らない

    def _one_pid(pid: int) -> tuple[str, str]:
        """1 個の PID の判定 → ("inside"/"outside"/"unknown"/"forbidden", 説明)"""
        if pid in protected:
            if pid == 1:
                who = "init(pid 1)"
            elif pid in port_protected:
                who = ("保護ポート "
                       + "/".join(str(x) for x in sorted(procutil.protected_ports()))
                       + " の設備（sshd／閲覧用の中継／Android ゲートウェイ）")
            else:
                who = "エージェント自身か、その親"
            return "forbidden", f"pid {pid}  {who}"
        row = table.get(pid)
        if row is None:
            return "unknown", f"pid {pid}"
        comm, cmdline, cwd, argv = row
        if _proc_is_mine(pid, cwd, argv, ws, bg_pids, cmdline):
            return "inside", f"pid {pid}  {cmdline[:70] or comm}"
        return "outside", f"pid {pid}  {cmdline[:70] or comm}"

    keep: list[str] = []
    drop: list[str] = []
    desc: list[str] = []

    def _refuse(tok: str, why: str) -> None:
        drop.append(tok)
        desc.append(why)

    for tok in parsed["targets"]:
        _tv = _unquote(tok)              # `kill "12345"` の引用符を外して読む
        # ★`$p` / `${p}` は bash の規則どおり展開して**読むだけ**（綴りは変えない）。
        #   未定義は空＝`${p}4102` は `4102`（反証 M-B の F7/F10）。
        _ex = _expand_kill_token(_tv, kvars)
        if _ex is not None:
            _tv = _ex
        _nums = _tv.split()
        if len(_nums) > 1 and all(re.fullmatch(r"\d+", x) for x in _nums):
            # 変数に PID が複数入っていた形（`PIDS="12 34"; kill $PIDS`）。
            vs = [_one_pid(int(x)) for x in _nums]
            if any(v in ("outside", "forbidden") for v, _ in vs):
                drop.append(tok)
                for v, d in vs:
                    if v in ("outside", "forbidden"):
                        desc.append(f"{tok} → {d}")
            else:
                keep.append(tok)
            continue
        if re.fullmatch(r"[+-]?\d+", _tv):
            v = int(_tv)                 # ★`+123` も十進（bash の kill が受ける形）
            if v > 0:
                verdict, d = _one_pid(v)
                if verdict in ("inside", "unknown"):
                    keep.append(tok)
                else:
                    _refuse(tok, d)
                continue
            if v == 0:
                # 自分のプロセスグループ全部。前面の Bash は `start_new_session`
                # を付けずに起こすので**エージェントと同じグループ**＝自殺。
                _refuse(tok, f"{tok}  自分のプロセスグループ全部"
                             f"（このエージェント自身を含む・pgid {own_pgid}）")
                continue
            if v == -1:
                _refuse(tok, f"{tok}  自分が信号を送れる**全プロセス**"
                             f"（root ならこのホストのほぼ全部）")
                continue
            # -N = プロセスグループ N。中身を数えて、全部『中』なら通す。
            pgid = -v
            if own_pgid is not None and pgid == own_pgid:
                _refuse(tok, f"{tok}  自分のプロセスグループ（pgid {pgid}）")
                continue
            members = [r for r in _proc_rows() if r.get("pgid") == pgid]
            if not members:
                keep.append(tok)         # 誰も居ない＝分からない（素通し）
                continue
            bad = [r for r in members
                   if r["pid"] in protected
                   or not _proc_is_mine(r["pid"], r.get("cwd") or "",
                                        r.get("argv") or (), ws, bg_pids,
                                        r.get("cmdline") or "")]
            if bad:
                drop.append(tok)
                for r in bad[:4]:
                    desc.append(f"{tok} の中: pid {r['pid']}  "
                                f"{(r.get('cmdline') or '')[:60]}")
            else:
                keep.append(tok)
            continue
        # 数字でない対象（`$( … )`・`$!`・`%1`・変数）
        pids = _resolve_kill_token(_tv, base)
        if pids is None:
            keep.append(tok)             # 解けない＝素通し（従来どおり）
            continue
        verdicts = [_one_pid(pid) for pid in pids]
        if any(v in ("outside", "forbidden") for v, _ in verdicts):
            drop.append(tok)
            for v, d in verdicts:
                if v in ("outside", "forbidden"):
                    desc.append(f"{tok} → {d}")
        else:
            keep.append(tok)

    if not drop:
        return None                      # 巻き添えが無いなら 1 文字も変えない
    note_out = "\n".join(f"      {d}" for d in desc[:8])
    lead = cmd[:len(cmd) - len(cmd.lstrip())]   # 断片の先頭の空白は保つ
    if keep:
        head = parsed["prefix"] + parsed["prog"] \
            + "".join(" " + t for t in parsed["sig"])
        if parsed["sep"] or any(t.startswith("-") for t in keep):
            head += " --"
        new = lead + head + "".join(" " + t for t in keep) + parsed["tail"]
        note = (f"\n★`{cmd}` は作業場の外（または撃たせない相手）も狙っていました。"
                f"残りの {len(keep)} 個だけにしました（{new}）。\n"
                f"   ★撃たなかった対象:\n{note_out}\n{_KILL_ADVICE}")
        return new, note
    new = lead + parsed["prefix"] + "true" + parsed["tail"]
    note = (f"\n★`{cmd}` は**作業場の外（または撃たせない相手）だけ**を狙っていました。"
            f"撃っていません:\n{note_out}\n{_KILL_ADVICE}")
    return new, note


#: 断ったあとの案内。**「PID で撃ち直せ」とは言わない**（言うと嘘になる）。
#:   ★2026-09-05: 名前撃ちの断り文が「起動時に控えた PID を `kill <pid>` するか」と
#:     逃げ道を案内していたが、明示 PID の kill も同じ基準で断るようになったので、
#:     その案内は**両ゲートが同時に断つ経路**を作る（第4回の反証）。
#:     run53 の実記録では断りが 2 回出ており（`pkill -f php`＝docker の php、
#:     `killall php`＝別ディレクトリの検収サーバ 5 本）、どちらも**断って正しい**相手。
#:     案内を「持ち主を確かめる」「自分の物は作業場の中に居るはず」へ書き換える。
#:   ★2026-09-05（第4回c の再反証）: 「自分が run_in_background で立てた物なら
#:     BashOutput(pid=…, kill=true)」という**逃げ道の案内**が実際に効いてしまった
#:     ——兄弟の作業場 B が、A の背景処理をこの経路で止められた（台帳は
#:     `LAPS_HOME` 共有で、bash_output 側だけが作業場を見ていなかった）。
#:     bash_output の pid 経路にも作業場の一致を要求したうえで、案内文からも
#:     「立てた物なら通る」という読み方を消し、**同じ作業場・同じ走行**に限る。
_KILL_ADVICE = (
    "   ここに出た相手は**作業場の外**か、**保護された設備**です"
    "（保護ポートの持ち主は作業場の中に見えても撃ちません）。"
    "PID を名指ししても同じ基準で断ります。\n"
    "   ・BashOutput(pid=…, kill=true) も同じ床の上です"
    "（**この作業場で自分が run_in_background で起こした記録**だけが通り、"
    "保護ポートの設備は綴りに関わらず断ります）。ここに出た相手には効きません。\n"
    "   ・ポートを空けたいだけなら `ss -ltnp 'sport = :<port>'` で持ち主を確かめる"
    "（別の持ち主なら、別のポートで立てる）。\n"
    "   ・自分の物のはずなのに外に出ているなら、作業場の中で起動し直す"
    "（cwd が作業場の外だと『外』になります）。\n"
    "   ・それでも止める必要があるなら AskUserQuestion で確認してください"
    "（この道具からは撃てません）。"
)


def _strip_kill_prefix(cmd: str) -> tuple[str, str]:
    """`sudo` / `timeout 5` / `nohup` / `then` … の前置きを剥がして `(前置き, 残り)`。

    ★`_parse_kill`（PID 側）と `_parse_name_kill`（名前側）の**両方**がこれを通る。
      名前側だけ剥がしが無かったせいで `sudo pkill -f gateway` が門を素通りした
      （`kill` 側だけ直す＝この現場の常習）。
    ★知らない語で止まる（fail-open）。剥がした分はそのまま書き戻す。
    """
    pre_end = 0
    rest = cmd
    word_re = re.compile(r"(\s*)(\S+)(\s+)")
    opt_re = re.compile(r"(\s*)(-\S+|\S+=\S*)(\s+)")
    while True:
        m = word_re.match(rest)
        if not m:
            break
        word = m.group(2)
        w = _prefix_word(word)
        if _KILL_ASSIGN_RE.match(word):
            pre_end += m.end(); rest = rest[m.end():]
            continue
        if w and (_CASE_LABEL_RE.match(w) or _FUNC_DEF_RE.match(w)):
            pre_end += m.end(); rest = rest[m.end():]
            continue
        if w not in _KILL_PREFIX_WORDS:
            break
        pre_end += m.end(); rest = rest[m.end():]
        optargs = _KILL_PREFIX_OPT_ARGS.get(w, frozenset())
        while True:
            m2 = opt_re.match(rest)
            if not m2:
                break
            opt = m2.group(2).partition("=")[0]
            pre_end += m2.end(); rest = rest[m2.end():]
            if opt in optargs and "=" not in m2.group(2):
                m2b = word_re.match(rest)
                if m2b:
                    pre_end += m2b.end(); rest = rest[m2b.end():]
        if w in _KILL_PREFIX_ONE_ARG:
            m3 = word_re.match(rest)
            if m3:
                pre_end += m3.end(); rest = rest[m3.end():]
        if w in _KILL_PREFIX_NAMED_BLOCK:
            # `function f {` / `coproc C {` の**名前**だけ食う（反証 M2）。
            m4 = word_re.match(rest)
            nm = m4 and (_prefix_word(m4.group(2)) or m4.group(2))
            if nm and (_FUNC_DEF_RE.match(nm)
                       or (_BLOCK_NAME_RE.match(nm)
                           and rest[m4.end():].lstrip().startswith("{"))):
                pre_end += m4.end(); rest = rest[m4.end():]
    return cmd[:pre_end], rest


#: 信号として認める名前（`-KILL` / `-SIGKILL` / `--signal SIGHUP`）。
#: ★ここに無い `-x` は**信号ではなく旗**として読む（`-f` を `-F` 信号と読んで
#:   `kill -f <pid>` のような壊れた命令を作らないため）。1 文字の信号名は無い。
_SIGNAL_NAMES = frozenset("""
HUP INT QUIT ILL TRAP ABRT IOT BUS FPE KILL USR1 SEGV USR2 PIPE ALRM TERM
STKFLT CHLD CLD CONT STOP TSTP TTIN TTOU URG XCPU XFSZ VTALRM PROF WINCH
IO POLL PWR SYS EMT INFO LOST UNUSED
""".split())
#: **引数を 1 つ取る**オプション（道具ごとに意味が違うので分ける）。
#:   ★`pkill -s` は**セッション id**・`killall -s` は**信号**。同じ綴りで別物なので、
#:     一緒くたにすると「引数の user 名をパターンと読む」等の読み違いが起きる。
#:   ★引数を取る綴りをここに**書き落とすと素通しになる**（2026-09-05・第4回c の
#:     6 回目・`pkill --help` と突き合わせて実測）。`-O 3600` を旗と読むと `3600` が
#:     パターンに化け、pkill は「パターンは 1 つ」なので `_parse_name_kill` が
#:     None を返す＝**門が何も言わずに通す**（保護ポートの設備が丸腰）。
#:     procps-ng 4.0.4 の pkill で引数を取るのは
#:     `-g -G -q -r -s -t -u -U -F -O --cgroup --ns --nslist --signal`。
#:     `-Q` / `--newer` はこの版に**無い**（pgrep 側の綴りと取り違えていた）が、
#:     版差で在り得るので落とさずに残す（余分に食っても pats が減るだけで安全側）。
_NAME_KILL_ARG_OPTS = {
    "pkill": frozenset({"-F", "-G", "-g", "-P", "-s", "-t", "-u", "-U", "-Q",
                        "-O", "-q", "-r",
                        "--pidfile", "--group", "--pgroup", "--parent", "--session",
                        "--terminal", "--euid", "--uid", "--queue", "--signal",
                        "--ns", "--nslist", "--older", "--newer",
                        "--runstates", "--cgroup"}),
    "killall": frozenset({"-s", "-u", "-y", "-o", "-Z", "-n",
                          "--signal", "--user", "--younger-than", "--older-than",
                          "--context", "--ns"}),
}
#: そのうち**値が信号**であるもの。
_NAME_KILL_SIG_OPTS = {
    "pkill": frozenset({"--signal"}),
    "killall": frozenset({"-s", "--signal"}),
}
#: ★**判定に反映できる**旗だけの許可リスト（2026-09-05・第4回c の 5 回目・反証 A）。
#:   これに無い旗／引数つきオプションが 1 つでも在れば **`unscopable`**（＝触らない）。
#:   なぜ許可リストなのか: 旧実装は `-u/-U/-G/-g/-s/-t/-P/-F/--older/--ns/…` を
#:   **読み飛ばすだけ**で判定に反映せず、`pkill -9 -u nobody -f X` を
#:   `kill -9 <作業場の中の pid>` に書き換えていた。実物の pkill は
#:   「nobody の物だけ」＝ root のサーバに当たらないのに、書き換え後の `kill` は
#:   **当たる**——門が**元の命令が撃たない相手を撃った**（反証で 6 形中 5 形が死亡）。
#:   「読み飛ばせた」を「正しく読めた」と扱わない。
_NAME_KILL_OK_LONG = {
    "pkill": frozenset({"--full", "--exact", "--count", "--echo",
                        "--ignore-case"}),
    "killall": frozenset({"--regexp", "--exact", "--quiet", "--verbose", "--wait",
                          "--ignore-case"}),
}
#: 束ねられる短い綴り（`-fx`）の 1 文字。長い綴りと同じ意味の物だけ。
#:   ★`pkill` の `i`（大小無視）と `v`（反転）／`killall` の `I`（大小無視）を
#:     ここに入れたのは「許してよいから」ではなく**照合に反映するから**（下の
#:     `_NAME_KILL_ICASE` / `_NAME_KILL_INVERSE` で読む）。
#:   ⚠ `killall -i` は **interactive**（確認して撃つ）で大小無視ではない。同じ
#:     1 文字が道具ごとに別物なので、`killall` の `i` は入れない（＝絞り込み扱い）。
_NAME_KILL_OK_LETTERS = {"pkill": set("fxcei"), "killall": set("reqvwI")}
#: 「完全一致」を指示する綴り（`pkill -x` は名前／`-f` つきなら行全体を**全長一致**）。
_NAME_KILL_EXACT = {"pkill": ("--exact", "x"), "killall": ("--exact", "e")}
#: ★**照合を大小無視に広げる**綴り（2026-09-05・第4回c の 6 回目・反証 W15）。
#:   反映しないと、門の照合（大小区別）が実物より**狭く**なり、一致 0 →
#:   「巻き添えなし」→ **黙って素通し**になる。実測: `pkill -9 -i -f "…​.PY"` で
#:   作業場の外のサーバが注記ひとつ無しに死亡（HEAD 80da2f6・W15）。
#:   `_NAME_KILL_ARG_OPTS` 系の「絞り込み」（実物の的 ⊆ 門の的＝床は安全側）と違い、
#:   これは**門が見落とす方向**にずれるので、絞り込みと同じ扱いにしてはいけない。
_NAME_KILL_ICASE = {"pkill": ("--ignore-case", "i"), "killall": ("--ignore-case", "I")}
#: ⚠ **`pkill -v`（反転）を「読める」側に入れてはいけない**（2026-09-05・
#:   第4回c の 6 回目・**自分でいったん入れて実物で外した**）。
#:   一度は「反転も再現できる」として `hit = not hit` を実装したが、
#:   名前空間の中で実物に当てたら **procps-ng 4.0.4 の pkill は `-v` を
#:   受け付けない**（実測: `pkill: 無効なオプション -- 'v'` / exit 2 /
#:   1 本も死なない。`-v/--inverse` は **pgrep 側**の綴り）。
#:   反映してしまうと、実物が**1 本も撃たない**命令を門が
#:   `kill -9 <作業場の中の pid>` に書き換える＝5 回目に閉じた反証 A
#:   （門が、元の命令が撃たない相手を撃つ）を自分で作り直すことになる。
#:   → `-v` は今までどおり**絞り込み扱い（触らない）**。
#:   `killall -v` は verbose なので `_NAME_KILL_OK_LETTERS` 側で許す。


#: ★POSIX の**括弧式の文字クラス**（`[[:digit:]]`）を Python の綴りへ直す表
#:   （2026-09-05・第4回c の 6 回目）。実物の pkill は `regcomp(REG_EXTENDED)` で
#:   これを解するが、Python の `re` は `[[:digit:]]` を「`[`,`:`,`d`,`i`,`g`,`t` の
#:   どれか 1 文字＋`]`」と読む＝**まったく別の物に当たる**。門の照合だけが
#:   一致 0 になり「巻き添えなし」で素通しする（`-i` と同じ「見落とす方向」）。
_POSIX_CLASSES = {
    "alpha": "a-zA-Z", "digit": "0-9", "alnum": "a-zA-Z0-9",
    "upper": "A-Z", "lower": "a-z", "xdigit": "0-9A-Fa-f",
    "space": r"\s", "blank": r" \t", "word": r"\w",
    "punct": r"!-/:-@\[-`{-~", "cntrl": r"\x00-\x1f\x7f",
    "print": r"\x20-\x7e", "graph": r"\x21-\x7e",
}
_POSIX_CLASS_RE = re.compile(r"\[:([a-z]+):\]")


def _posix_to_python_re(pat: str) -> tuple[str, str | None]:
    """`[[:digit:]]` を `[0-9]` へ。戻り: (直した綴り, 直せなかった理由 or None)。"""
    unknown: list[str] = []

    def _sub(m):
        name = m.group(1)
        if name in _POSIX_CLASSES:
            return _POSIX_CLASSES[name]
        unknown.append(name)
        return m.group(0)

    out = _POSIX_CLASS_RE.sub(_sub, pat)
    if unknown:
        return pat, "知らない POSIX 文字クラス: " + ", ".join(sorted(set(unknown)))
    return out, None


# ── 実物と**同じ照合器**でパターンを読む（第4回c の 7 回目・反証 M1） ────────
#: ★何が起きていたか: `_posix_to_python_re` は `[[:digit:]]` を直すだけで、
#:   残りは **Python の `re`** に compile していた。実物は
#:   **glibc の `regcomp(REG_EXTENDED)`（GNU 拡張つき）**なので、
#:   次の綴りで的が交わらない／門の的が狭い:
#:
#:       pkill -f "X_\d+"   実物は素の d（ERE に `\d` は無い）  Python は数字
#:       pkill -f "\<X"     実物は GNU の語頭アンカー           Python は素の `<`
#:       pkill -f "a\nb"    実物は素の n                        Python は改行
#:       pkill -f "[\d]"    実物は `\` と `d` の集合            Python は数字
#:
#:   どの形も門の一致が 0 になり、`if not outside: return None` が
#:   「巻き添えなし」と読んで**黙って素通し**する——実測で保護ポートのサーバが
#:   死んだ（R01 / R03 / Q05）。**一致 0 は「巻き添えなし」ではなく「分からない」**。
#: ★直し方: 綴りを翻訳するのをやめ、**pkill / killall が呼ぶのと同じ
#:   `regcomp(REG_EXTENDED[|REG_ICASE]|REG_NOSUB)` を ctypes で直に叩く**。
#:   翻訳表は「表に無い綴りが出るたびに穴が開く」形なので、engine ごと合わせる。
#:   `-x`（完全一致）も procps-ng と同じ `^( … )$` の包み方にする
#:   （pgrep.c は `sprintf(re, "^(%s)$", pattern)` してから regcomp する）。
#: ★libc が使えない環境（非 POSIX / regcomp を持たない libc）では、
#:   従来どおり Python の `re` に落ちるが、**glibc と意味が違う綴りが在れば
#:   「読めない」として扱う**（`_ere_python_gap`）。読めなかった物を
#:   「読めた」ことにしない。
_REG_EXTENDED, _REG_ICASE, _REG_NOSUB = 1, 2, 8
#: glibc の `regex_t` は x86_64 で 64 バイト。余裕を見て確保する。
_REGEX_T_SIZE = 512
#: パターンの長さの上限（`regexec` の破滅的な後戻りに時間を吸わせない）。
_ERE_MAX = 4096
_ERE_LIB: object = False          # False=未初期化 / None=使えない


def _ere_lib():
    """`regcomp` / `regexec` を持つ libc（＝pkill が使う照合器）。"""
    global _ERE_LIB
    if _ERE_LIB is not False:
        return _ERE_LIB
    _ERE_LIB = None
    if sys.platform == "win32":
        return None
    try:
        import ctypes
        lib = ctypes.CDLL(None, use_errno=True)      # 自プロセスの libc
        if not (hasattr(lib, "regcomp") and hasattr(lib, "regexec")
                and hasattr(lib, "regfree")):
            return None
        lib.regcomp.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.regcomp.restype = ctypes.c_int
        lib.regexec.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        lib.regexec.restype = ctypes.c_int
        lib.regfree.argtypes = [ctypes.c_void_p]
        lib.regfree.restype = None
        if hasattr(lib, "regerror"):
            lib.regerror.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                     ctypes.c_char_p, ctypes.c_size_t]
            lib.regerror.restype = ctypes.c_size_t
        _ERE_LIB = lib
    except Exception:
        _ERE_LIB = None
    return _ERE_LIB


class _EreProg:
    """`regcomp` した 1 本。`search(str) -> bool`。解放は `__del__`。"""

    __slots__ = ("_lib", "_buf")

    def __init__(self, lib, buf):
        self._lib, self._buf = lib, buf

    def search(self, hay: str) -> bool:
        import ctypes
        b = hay.encode("utf-8", "surrogateescape").replace(b"\0", b" ")
        return self._lib.regexec(ctypes.byref(self._buf), b, 0, None, 0) == 0

    def __del__(self):
        try:
            import ctypes
            self._lib.regfree(ctypes.byref(self._buf))
        except Exception:
            pass


#: glibc ERE と Python `re` で**意味が違う**バックスラッシュ綴り（libc が
#: 使えないときの受け皿で使う）。GNU 拡張として glibc が解するのは
#: `\w \W \s \S \b \B \< \> \` \'` と後方参照 `\1`〜`\9`。それ以外の
#: 英数字は**素の文字**（`\d` は `d`）だが、Python は別物か error になる。
_ERE_GNU_ESC = set("wWsSbB")
_ERE_ANCHOR_ESC = {"<": r"\b", ">": r"\b", "`": r"\A", "'": r"\Z"}


def _ere_python_gap(pat: str) -> str | None:
    """glibc ERE として読んだときに Python の `re` と**意味が変わる**綴りを探す。

    戻り: 理由 ／ 差が無ければ None。★括弧式（`[…]`）の中では POSIX の規則で
    バックスラッシュが**素の文字**になるので、そこに `\\` が在れば差が出る。
    """
    i, n, inbr = 0, len(pat), False
    while i < n:
        ch = pat[i]
        if inbr:
            if pat.startswith("[.", i) or pat.startswith("[=", i):
                return "括弧式の照合要素/等価クラス（Python に無い）"
            if ch == "\\":
                return r"括弧式の中の `\`（POSIX では素の文字・Python では脱出）"
            if ch == "]" and not pat.startswith("]", i - 1 if i else 0):
                inbr = False
            elif ch == "]":
                inbr = False
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= n:
                return r"末尾の `\`"
            e = pat[i + 1]
            if e in _ERE_GNU_ESC or e.isdigit():
                i += 2
                continue
            if e in _ERE_ANCHOR_ESC:
                # `\<` `\>` `` \` `` `\'` は GNU のアンカー。Python には同じ綴りが
                # 無い（`\<` は素の `<` として通ってしまい、的が**狭くなる**）。
                return rf"`\{e}`（GNU のアンカー・Python には同じ綴りが無い）"
            if e.isalpha():
                return rf"`\{e}`（実物は素の {e}・Python は別の意味）"
            i += 2                       # 記号の脱出はどちらも素の文字
            continue
        if ch == "[":
            inbr = True
            i += 1
            if i < n and pat[i] == "^":
                i += 1
            if i < n and pat[i] == "]":  # 直後の `]` は素の文字
                i += 1
            continue
        i += 1
    return None


def _compile_kill_re(pat: str, *, icase: bool, exact: bool):
    """実物（pkill / killall）と同じ意味でパターンを compile する。

    戻り: `(照合関数, None)` ／ 読めなければ `(None, 理由)`。
    """
    if len(pat) > _ERE_MAX:
        return None, f"パターンが長すぎます（{len(pat)} 文字）"
    src = f"^({pat})$" if exact else pat        # procps-ng pgrep.c と同じ包み方
    lib = _ere_lib()
    if lib is not None:
        import ctypes
        buf = (ctypes.c_byte * _REGEX_T_SIZE)()
        flags = _REG_EXTENDED | _REG_NOSUB | (_REG_ICASE if icase else 0)
        rc = lib.regcomp(ctypes.byref(buf), src.encode("utf-8", "surrogateescape"),
                         flags)
        if rc != 0:
            why = "regcomp が受け付けません"
            if hasattr(lib, "regerror"):
                msg = ctypes.create_string_buffer(256)
                try:
                    lib.regerror(rc, ctypes.byref(buf), msg, 256)
                    why = msg.value.decode("utf-8", "replace") or why
                except Exception:
                    pass
            return None, why
        return _EreProg(lib, buf).search, None
    # ── libc が使えない: Python で読めるところまで（差が在れば「読めない」） ──
    gap = _ere_python_gap(pat)
    if gap:
        return None, gap
    fixed, why = _posix_to_python_re(src)
    if why:
        return None, why
    try:
        rx = re.compile(fixed, re.IGNORECASE if icase else 0)
    except re.error as e:
        return None, str(e)
    return rx.search, None


def _unquote(tok: str) -> str:
    """トークンを囲む引用符を 1 組だけ外す（`_split_kill_args` は引用符を残す）。"""
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def _bare_signal(tok: str) -> str | None:
    """`-9` / `-KILL` / `-SIGHUP` なら**その綴りのまま**返す（信号は逐語で保つ）。"""
    if re.fullmatch(r"-\d{1,2}", tok):
        return tok
    m = re.fullmatch(r"-(?:SIG)?([A-Za-z][A-Za-z0-9]*)", tok)
    if m and m.group(1).upper() in _SIGNAL_NAMES:
        return tok
    return None


def _sig_token(val: str) -> str | None:
    """`--signal 9` / `--signal KILL` の**値**を `kill` の綴り（`-9` / `-KILL`）へ。"""
    v = _unquote(val.strip())
    if re.fullmatch(r"\d{1,2}", v):
        return "-" + v
    if re.fullmatch(r"(?:SIG)?[A-Za-z][A-Za-z0-9]*", v) and \
            re.sub(r"^SIG", "", v.upper()) in _SIGNAL_NAMES:
        return "-" + v
    return None


def _name_kill_matcher(parsed: dict):
    r"""名前撃ち（`pkill` / `killall` / **`pgrep`**）の**照合だけ**を作って返す。

    戻り: `(hit, None)` ／ 読めなければ `(None, 理由)`。
      `hit(row) -> bool | None`（`None` ＝ `/proc/<pid>/exe` を読めず当たるか不明）。

    ★なぜ切り出したか（2026-09-06・第4回c の 8 回目・反証 M-B）:
      `kill -9 $(pgrep -f X)` は `pkill -f X` と**同じ相手**を撃つのに、
      名前撃ちの門は綴りが `kill` なので見ず、PID 側の門は対象が
      `$( … )` なので「解けない＝素通し」に落としていた（実測 F5/F6 で
      保護ポートのサーバが死亡・本文は `(exit 0, no output)`）。
      解くには**実物と同じ照合**が要るが、それを 2 本持つと
      この現場が何度も踏んだ「片方だけ直る」を作る。だから 1 本にする。
    ★外部コマンドは起こさない（`pgrep -f` の自己一致で自分を殺す形の再来を避ける）。
      見るのは `_proc_table()` だけ＝試験が差し替える口も 1 つのまま。
    """
    tool, pats = parsed["tool"], parsed["pats"]
    full, regex_mode = parsed["full"], parsed["regex"]
    icase = parsed.get("icase", False)
    exact = parsed.get("exact", False)
    # ★`killall`（`-r` 無し）は**正規表現ではない**: 名前は comm と完全一致、
    #   `/` を含む名前は**実行ファイル**で照合する（killall(1)）。
    #   旧実装は `/` つきでも comm と文字列比較していたので一致 0 →
    #   「巻き添えなし」→ 黙って素通し（実測 R06 / Q07 で保護サーバ死亡）。
    literal_names = tool == "killall" and not regex_mode
    exe_ids: dict[str, tuple] = {}
    if literal_names:
        for _p in pats:
            if "/" not in _p:
                continue
            try:
                _st = os.stat(_p)
            except OSError as e:
                return None, (f"`killall` の `/` つきの名前は**実行ファイル**で"
                              f"照合しますが、{_p!r} を読めません"
                              f"（{getattr(e, 'strerror', None) or e}）")
            exe_ids[_p] = (_st.st_dev, _st.st_ino)
    matchers = []
    if not literal_names:
        # ★照合器は**実物と同じ**（glibc の regcomp/REG_EXTENDED）。綴りの翻訳では
        #   表に無い綴りが出るたびに穴が開く（`\d` / `\<` / `[\d]` …）。
        for _p in pats:
            _fn, _why = _compile_kill_re(_p, icase=icase, exact=exact)
            if _why:
                return None, f"パターンを正規表現として読めません: {_p!r}（{_why}）"
            matchers.append(_fn)
    names = [p for p in pats if p not in exe_ids]
    exe_vals = set(exe_ids.values())

    def _hit(row: dict):
        comm, cmdline, pid = row["comm"], row["cmdline"], row["pid"]
        if literal_names:
            hit = (any(comm.lower() == p.lower() for p in names) if icase
                   else any(comm == p for p in names))
            if not hit and exe_vals:
                eid = _pid_exe_id(pid)
                if eid is None:
                    return None                 # 読めない＝当たるか分からない
                if eid in exe_vals:
                    hit = True
            return hit
        # ★`killall -r` は**名前（comm）**で見る。`pkill` は `-f` のときだけ行全体。
        #   `-x`（完全一致）は `_compile_kill_re` が `^( … )$` で包んでいる
        #   （procps-ng pgrep.c と同じ）。ここでは分岐しない。
        hay = comm if tool == "killall" else (cmdline if full else comm)
        return any(m(hay) for m in matchers)

    return _hit, None


def _parse_name_kill(seg: str) -> dict | None:
    """名前撃ち（`pkill [opts] PATTERN` / `killall [opts] NAME…`）の 1 断片を分解する。

    戻り: `{prefix, tool, flags, pats, sig, full, regex, tail}` ／ 読めなければ None。

    ★1 本の正規表現（旧 `_NAME_KILL_RE`）をやめた理由（2026-09-05・第4回c の 4 回目・
      **実物で 4 形が素通りして保護ポートのサーバが死んだ**）:
        `pkill -9 -f X &`   `nohup pkill -9 -f X &`   `pkill -9 -f X | cat`
        `pkill --signal 9 -f X`
      前 2 つは末尾の `&`、3 つ目は `|`、4 つ目は**引数を取る長い綴り**。
      PID 側の `_parse_kill` は末尾の `&` を tail に剥がしていたのに、名前側だけ
      手当てが無かった＝この現場の常習「**片方だけ直る**」。
      → 末尾（`_split_control_tail` / `_KILL_TAIL_RE`）・前置き（`_strip_kill_prefix`）・
        語割り（`_split_kill_args`）を **PID 側と同じ部品**で共有する。
    ★読み違えたら**触らない**（fail-open）。特に `pkill` はパターンを 1 つしか取らない
      ので、2 つ以上残ったら「オプションの引数をパターンと読んだ」＝諦める。
    ★`unscopable`（2026-09-05・第4回c の 5 回目・反証 A）: **読めたが判定に反映
      できない**オプションが在るときに理由の文字列が入る。`None` との違いは
      「読めなかった」と「読めたが絞りの意味を再現できない」を混ぜないため——
      呼び出し側は前者を素通し、後者を**保護ポートの床つきで断る**に振り分ける。
    """
    pre, rest = _strip_kill_prefix(seg)
    lead = rest[:len(rest) - len(rest.lstrip())]
    pre += lead
    # ★コメントを先に剥がす（2026-09-06・反証 M-A）。pkill は「パターンは 1 つ」
    #   なので `#` と後続語が増えると None＝**黙って素通し**、killall は逆に
    #   `#` と後続語が**追加の名前**になって的が広がっていた（門が、元の命令が
    #   撃たない相手を撃つ）。同じ 1 行が両方を直す。
    raw, com = _strip_comment_tail(rest.strip())
    body, tail = _split_control_tail(raw)
    body, red = _strip_redirect_tail(body)
    tail = red + tail + com
    toks = _split_kill_args(body)
    if not toks:
        return None
    # ★綴りは `_prog_literal` で読む（`\pkill` / `p"kill"` / `$'pkill'` は
    #   シェルが全部 `pkill` として起動する・第4回c の 5 回目・反証 B(4)/B(5)）。
    prog = _prog_literal(toks[0])
    if prog is None:
        return None
    tool = os.path.basename(prog)
    if tool not in ("pkill", "killall"):
        return None
    argopts = _NAME_KILL_ARG_OPTS[tool]
    sigopts = _NAME_KILL_SIG_OPTS[tool]
    oklong = _NAME_KILL_OK_LONG[tool]
    okletters = _NAME_KILL_OK_LETTERS[tool]
    exact_long, exact_letter = _NAME_KILL_EXACT[tool]
    sig: str | None = None
    flags: list[str] = []
    pats: list[str] = []
    narrow: list[str] = []          # 読めたが判定に反映できないオプション
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "--":
            pats.extend(toks[i + 1:])
            break
        if t.startswith("-") and len(t) > 1:
            name, eq, val = t.partition("=")
            if name in sigopts:
                if not eq:
                    if i + 1 >= len(toks):
                        return None
                    val = toks[i + 1]
                    i += 1
                sig = _sig_token(val) or sig
                i += 1
                continue
            if name in argopts:
                # ★引数は食う（食わないとパターンと読み違える）が、**意味は
                #   再現できない**ので「絞り込みが在る」と記録する（反証 A）。
                narrow.append(name)
                if not eq:
                    if i + 1 >= len(toks):
                        return None
                    i += 1
                i += 1
                continue
            s = _bare_signal(t)
            if s:
                sig = s
                i += 1
                continue
            # ★許可リストに無い旗は「読み飛ばせた」だけ＝判定に反映できない。
            if name.startswith("--"):
                if name not in oklong:
                    narrow.append(name)
            elif re.fullmatch(r"-[A-Za-z]+", name):
                bad = [c for c in name[1:] if c not in okletters]
                if bad:
                    narrow.extend("-" + c for c in bad)
            else:
                narrow.append(name)
            flags.append(t)
            i += 1
            continue
        pats.append(t)
        i += 1
    # ★展開（`"$l"` / `$(…)`）が混ざるパターンは**読めない**。旧実装はここで
    #   `None` を返して**黙って素通し**していた（2026-09-05・第4回c の 7 回目・
    #   反証 M5/S14: `while read -r l; do pkill -9 -f "$l"; done <<< "X"` で
    #   保護ポートのサーバが死んだのに、結果本文は `(exit 0, no output)`）。
    #   「読めなかった」を「安全だった」に化けさせない——**どのプロセスに当たるか
    #   分からない**として `_name_kill_unreliable` の受け皿へ渡す。
    lits = [_prog_literal(p) for p in pats]
    unreadable_pat: str | None = None
    if any(p is None for p in lits):
        bad = [pats[k] for k, v in enumerate(lits) if v is None]
        unreadable_pat = ("パターンに展開が混ざるので、撃つ相手を先に読めません: "
                          + ", ".join(repr(b) for b in bad[:3]))
    else:
        pats = [p for p in lits if p is not None]
    if not pats or any(not p for p in pats):
        return None
    if tool == "pkill" and len(pats) > 1:
        return None                            # 読み違えた＝触らない

    def _has(long_name: str, letter: str) -> bool:
        return any(f == long_name
                   or (re.fullmatch(r"-[A-Za-z]+", f) and letter in f[1:])
                   for f in flags)

    full = tool == "pkill" and _has("--full", "f")
    regex = tool == "killall" and _has("--regexp", "r")
    icase = _has(*_NAME_KILL_ICASE[tool])
    # ★`-x`（完全一致）は**旗のまま無視すると部分一致に化ける**（反証 A）。
    #   `pkill -x foo` は `foo` という名前**そのもの**だけ。判定に反映する。
    exact = _has(exact_long, exact_letter)
    return {"prefix": pre, "tool": tool, "flags": flags, "pats": pats,
            "sig": sig or "-TERM", "full": full, "regex": regex, "tail": tail,
            "exact": exact, "icase": icase, "unreadable": unreadable_pat,
            "unscopable": (", ".join(sorted(set(narrow))) if narrow else None)}


def _name_kill_narrowed(cmd: str, parsed: dict,
                        outside: list[tuple[int, str]],
                        guarded: list[tuple[int, str]]) -> tuple[str, str] | None:
    """**絞り込みオプションつきの名前撃ち**の扱い（2026-09-05・第4回c の 5 回目・反証 A）。

    ★何が起きていたか: `_NAME_KILL_ARG_OPTS` は `-u/-U/-G/-g/-s/-t/-P/-F/--older/--ns…`
      を**読み飛ばすだけ**で判定に一切反映せず、`_scope_name_kill` はそれを
      `kill <信号> <作業場の中の pid>` に書き換えていた。実物の
      `pkill -9 -u nobody -f X` は「nobody の物」だけ＝root で走るサーバに**当たらない**
      のに、書き換え後の `kill` は当たる。**門が、元の命令が撃たない相手を撃った**
      （反証で 6 形中 5 形＝`-u`/`-P`/`-t`/`-s`/`--older` が実際に中のサーバを殺した）。
      しかも注記は絞りの有無に関わらず同じ「作業場の中の 1 本だけに絞って撃ちました」で、
      **嘘の説明つき**だった。
    ★方針: 絞りを再現できないなら**書き換えない**（base と同じ「触らない」）。
      ただし素通しに戻すだけでは保護ポートの設備が丸腰になるので、
      **当たる形だけ断る**床を敷く（PID 側 `_kill_protected_pids` と同じ考え）。
    戻り: (命令, 注記) ／ 触る理由が無ければ None。
    """
    why = parsed["unscopable"]
    if guarded:
        who = ", ".join(f"pid {pid}" for pid, _ in guarded[:5])
        ports = "/".join(str(x) for x in sorted(procutil.protected_ports()))
        note = (f"\n★`{cmd}` は**保護ポート {ports} の設備**（{who}）に一致します"
                f"（sshd／閲覧用の中継／Android ゲートウェイ）。撃っていません。\n"
                f"   ★この命令には絞り込みオプション（{why}）が在るので、"
                f"**作業場の中だけに絞る書き換えはしていません**——"
                f"絞りの意味を再現できないまま書き換えると、"
                f"元の命令が撃たない相手を撃つことになります。\n"
                f"   ・狙いが自分のプロセスなら PID を名指ししてください"
                f"（`kill <pid>`。同じ基準で保護ポートの設備だけ断ります）。\n"
                + _KILL_ADVICE)
        return parsed["prefix"] + "true" + parsed["tail"], note
    if not outside:
        return None                       # 巻き添えの候補すら無い＝黙って通す
    note_out = "\n".join(f"      pid {pid}  {c}" for pid, c in outside[:8])
    note = (f"\n★`{cmd}` は作業場の外のプロセスにも当たり得る形ですが、"
            f"**絞り込みオプション（{why}）が在るので触っていません**"
            f"（絞りの意味を再現できないまま書き換えると、"
            f"元の命令が撃たない相手を撃つことになります）。\n"
            f"   ★一致し得る作業場の外の {len(outside)} 本"
            f"（この絞り込みで実際に当たるかはシェル側が決めます）:\n{note_out}\n"
            + _KILL_ADVICE)
    return cmd, note


def _name_kill_unreliable(cmd: str, parsed: dict, why: str) -> tuple[str, str]:
    """**照合そのものが実物と食い違う**ときの受け皿（2026-09-05・第4回c の 6 回目）。

    ★絞り込み（`-u nobody` 等）との違いが要点:
      * 絞り込み → 実物の的 ⊆ 門の的。門が見た「巻き添え候補」は**多すぎる方**に
        ずれるので、床（保護ポートの設備を断る）は安全側に効く。
      * 照合のずれ（大小無視・反転・読めないパターン）→ 実物の的 ⊄ 門の的。
        門が「一致 0」と見た相手が**実際には撃たれる**。`if not outside: return None`
        は、このとき「巻き添えなし」ではなく「**分からない**」を意味する。
        実測（HEAD 80da2f6・反証 W15）: `pkill -9 -i -f "…​.PY"` で作業場の外の
        サーバが注記ひとつ無しに死亡した。
    ★`-i` と `-v` は照合に反映できたので、ここへ来るのは
      「パターンを正規表現として読めない」形だけ。**読めないものを literal に
      落として『読めた』ことにしない**——落とすのはやめないが、
      判定は「的が分からない」として扱う。
    方針: 保護ポートの設備が 1 本でも居るなら**撃たない**（誰に当たるか分からない
      弾を、保護対象の居るホストで撃たせない）。居ないなら通すが、**黙っては通さない**。
    """
    try:
        ports = sorted(procutil.protected_ports())
        guarded_pids = sorted(procutil.protected_port_pids())
    except Exception:
        ports, guarded_pids = [], []
    if guarded_pids:
        who = ", ".join(f"pid {x}" for x in guarded_pids[:5])
        note = (f"\n★`{cmd}` は**どのプロセスに当たるかを門が再現できません**（{why}）。\n"
                f"   このホストには保護ポート "
                + "/".join(str(x) for x in ports)
                + f" の設備（{who}）が動いているので、"
                  f"当たる相手が分からない弾は撃っていません"
                  f"（sshd／閲覧用の中継／Android ゲートウェイ）。\n"
                  f"   ・狙いが自分のプロセスなら PID を名指ししてください"
                  f"（`kill <pid>`。同じ基準で保護ポートの設備だけ断ります）。\n"
                + _KILL_ADVICE)
        return parsed["prefix"] + "true" + parsed["tail"], note
    note = (f"\n★`{cmd}` は**どのプロセスに当たるかを門が再現できません**（{why}）。\n"
            f"   保護ポートの設備は動いていないのでそのまま実行しますが、"
            f"作業場の外まで当たり得ます。狙いが自分のプロセスなら"
            f" PID を名指しするほうが確実です。\n"
            + _KILL_ADVICE)
    return cmd, note


def _scope_name_kill(cmd: str, workspace: str | None) -> tuple[str, str] | None:
    """`pkill python` のような**名前撃ち**を、作業場の中のプロセスだけに絞って書き換える。

    ★なぜ要るか（2026-09-02・実害）: devbench の api 課題は「最後にサーバを止めること」を
      要求する。エージェント（trusted）が名前で撃つと、同じホストで動いている
      **他人のサービス**まで巻き添えになる。実際に corridor-bridge（python3）が
      7 分で 6 回 SIGKILL され、telemetry の受け口も 2 回死んだ。どちらも python3。
      記録にある名前撃ち 41 件は**すべて自分の php サーバが狙い**＝意図は常に「自分の物」。
    ★これは制限ではない（撃つ物は撃つ）。**狙いの外に当たる弾を落とすだけ**で、
      落とした相手は結果に列挙して、本当に狙うなら PID で撃てる道を示す。
    ★POSIX のみ。作業場が分からなければ触らない（黙って広げるより黙って通す）。
    戻り: (書き換えた命令, 添える注記) ／ 対象外なら None。
    """
    if sys.platform == "win32" or not workspace:
        return None
    # ★連結命令の中の名前撃ちも拾う: 各断片を個別に絞り、書き換えた物を継ぎ直す。
    segs = _split_segments(cmd)
    if len(segs) > 1:
        changed, notes = False, []
        for i, s in enumerate(segs):
            if s in _SEG_SEPS:
                continue
            r = _keep_ws(s, lambda c: _scope_name_kill(c, workspace))
            if r:
                segs[i], n = r; changed = True; notes.append(n)
        return ("".join(segs), "".join(notes)) if changed else None
    # ★パイプラインの**どの位置**の名前撃ちも拾う（`foo | pkill -f x`）。
    pipes = _split_pipeline(cmd)
    if len(pipes) > 1:
        changed, notes = False, []
        for i, s in enumerate(pipes):
            if s in ("|", "|&"):
                continue
            # ★要素の前後の空白は**そのまま返す**（`kill … | cat` が `true| cat` に
            #   潰れる。動くが、モデルに返る本文が実際の命令と違って見える）。
            r = _keep_ws(s, lambda c: _scope_name_kill(c, workspace))
            if r:
                pipes[i] = r[0]
                changed = True; notes.append(r[1])
        return ("".join(pipes), "".join(notes)) if changed else None
    # ★前置きの剥がし・末尾（`&` / `| cat` / `2>/dev/null`）・信号・パターンは
    #   `_parse_name_kill` に一本化（PID 側と同じ部品を使う・第4回c の 4 回目）。
    parsed = _parse_name_kill(cmd)
    if not parsed:
        return None
    if parsed.get("unreadable"):
        # ★パターンが変数/置換＝的が分からない。黙って通さない（反証 M5/S14）。
        return _name_kill_unreliable(cmd, parsed, parsed["unreadable"])
    _pre, _tail = parsed["prefix"], parsed["tail"]
    tool, pats, sigtok = parsed["tool"], parsed["pats"], parsed["sig"]
    full, regex_mode = parsed["full"], parsed["regex"]
    exact = parsed.get("exact", False)
    # ★照合を**実物に合わせて**広げる（2026-09-05・第4回c の 6 回目・反証 W15）。
    #   `-i` / `-v` を「読み飛ばした旗」として扱うと門の的が実物より狭く／ずれて、
    #   一致 0 →「巻き添えなし」→ 黙って素通しになる。ここで反映する。
    icase = parsed.get("icase", False)
    ws = os.path.realpath(workspace)
    # ★`killall`（`-r` 無し）は**正規表現ではない**: 名前は comm と完全一致、
    #   `/` を含む名前は**実行ファイル**で照合する（killall(1)）。
    #   旧実装は `/` つきでも comm と文字列比較していたので一致 0 →
    #   「巻き添えなし」→ 黙って素通し（実測 R06 / Q07 で保護サーバ死亡）。
    # ★照合は `_name_kill_matcher` **1 本**（2026-09-06・第4回c の 8 回目）。
    #   `kill -9 $(pgrep -f X)` を解く側（`_resolve_kill_token`）が同じ判断を
    #   自前で持つと、この現場が何度も踏んだ「片方だけ直る」をもう一度作る。
    _hit, _why = _name_kill_matcher(parsed)
    if _why:
        return _name_kill_unreliable(cmd, parsed, _why)
    inside, outside, refs = [], [], []
    bg_pids = _registered_bg_pids(ws)
    protected = _kill_protected_pids()
    try:
        port_protected = procutil.protected_port_pids()
    except Exception:
        port_protected = set()
    guarded: list[tuple[int, str]] = []
    unknown_exe = False
    for t in _proc_table():
        row = _row(t)
        pid, cmdline = row["pid"], row["cmdline"]
        hit = _hit(row)
        if hit is None:
            unknown_exe = True                  # 読めない＝当たるか分からない
            continue
        if not hit:
            continue
        # ★所有権の判定は `procutil.in_workspace` **1 本**（2026-09-05・第4回b の統合）。
        #   旧: `cwd.startswith(ws) or ws in cmdline`。前者は区切りが無く
        #   `/home/user/project` が `/home/user/project-restore` に当たり、後者は
        #   `/tmp` に居る `tail -f <ws>/x.log` まで「自分の物」にしていた。
        #   同じ式が `bash_output._port_report` にも写されて、そちらでは
        #   「絞る」ではなく**撃つ許可**になっていた（＝片方だけ直す形の再来）。
        # ★自分・祖先・init は「中」に見えても撃たない（自殺とホスト停止）。
        mine = pid not in protected and procutil.in_workspace(row, ws, bg_pids=bg_pids)
        if mine:
            inside.append((pid, cmdline[:70]))
        else:
            outside.append((pid, cmdline[:70]))
            if pid in port_protected:
                guarded.append((pid, cmdline[:70]))
            elif procutil.references_workspace(row, ws):
                refs.append((pid, cmdline[:70]))
    if unknown_exe:
        return _name_kill_unreliable(
            cmd, parsed,
            "`killall` の `/` つきの名前は**実行ファイル**で照合しますが、"
            "一部のプロセスの `/proc/<pid>/exe` を読めません")
    if parsed.get("unscopable"):
        # ★絞り込みオプションが在る＝**元の命令が撃つ相手を再現できない**。
        #   書き換えると「元が撃たない相手を撃つ」ので、絞らずに床だけ敷く（反証 A）。
        return _name_kill_narrowed(cmd, parsed, outside, guarded)
    if not outside:
        return None                       # 巻き添えが無いなら書き換えない
    note_out = "\n".join(f"      pid {pid}  {c}" for pid, c in outside[:8])
    if guarded:
        note_out += ("\n   ★このうち "
                     + ", ".join(f"pid {pid}" for pid, _ in guarded[:5])
                     + " は**保護ポート "
                     + "/".join(str(x) for x in sorted(procutil.protected_ports()))
                     + " の設備**です（sshd／閲覧用の中継／Android ゲートウェイ）。"
                     "作業場の中に見えても撃ちません。")
    if refs:
        # 撃つ根拠にはしないが、**黙って落とさない**（作業場のパスを触っている行）。
        note_out += ("\n   ★このうち "
                     + ", ".join(f"pid {pid}" for pid, _ in refs[:5])
                     + " は作業場のパスを参照していますが、cwd は作業場の外です"
                       "（自分の物と決めつけません）。**PID を名指ししても同じ基準で"
                       "断ります**——作業場の中で起動し直すか、持ち主を確かめてください。")
    if inside:
        # ★末尾（`&` / `| cat` / `2>/dev/null`）は**そのまま書き戻す**（意味を変えない）。
        new = (_pre + f"kill {sigtok} "
               + " ".join(str(pid) for pid, _ in inside) + _tail)
        note = (f"\n★`{cmd}` は作業場の外のプロセスにも当たる形でした。"
                f"作業場の中の {len(inside)} 本だけに絞って撃ちました（{new}）。\n"
                f"   ★撃たなかった {len(outside)} 本（作業場の外＝他のサービス）:\n{note_out}\n"
                f"{_KILL_ADVICE}")
        return new, note
    new = _pre + "true" + _tail
    note = (f"\n★`{cmd}` に一致するのは**作業場の外のプロセスだけ**でした（{len(outside)} 本）。"
            f"撃っていません:\n{note_out}\n{_KILL_ADVICE}")
    return new, note

# ── 綴りの「中」に隠れた kill を、同じ門に通す ──────────────────────────
#: 別のシェルへスクリプトを渡す形（`sh -c "…"` / `bash -lc '…'` / `env X=1 bash -c "…"`）。
_NESTED_SH_C_RE = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:[^\s;&|()'\"]*/)?"
    r"(?:sh|bash|dash|zsh|ksh|ash)\s+"
    r"(?:-{1,2}[A-Za-z][A-Za-z0-9-]*\s+)*"
    r"-[A-Za-z]*c\s+(?P<q>[\"'])(?P<body>(?:(?!(?P=q))[\s\S])*)(?P=q)")
#: `eval …`（残り全部がシェルに渡る）。
_NESTED_EVAL_RE = re.compile(r"(?:^|(?<=[\s;&|(]))eval\s+(?P<rest>\S[\s\S]*?)\s*$")
#: ★`trap '…' EXIT`（2026-09-05・第4回c の 7 回目・反証 M4）。第 1 引数は
#:   **そのままシェルに渡る**ので `eval` と同じ扱いにできる。実測で
#:   `trap 'pkill -9 -f "X"' EXIT; true` と `trap "pkill …" USR1; kill -USR1 $$` の
#:   2 形とも保護ポートのサーバが死んだ（N21 / S16）。
_NESTED_TRAP_RE = re.compile(
    r"(?:^|(?<=[\s;&|(]))trap\s+(?P<q>[\"'])(?P<body>(?:(?!(?P=q))[\s\S])*)(?P=q)")
#: ★`bash <<< '…'`（2026-09-05・第4回c の 7 回目・反証 M5/S04）。ヒアストリングで
#:   別のシェルに渡す形。`sh -c` と同じ「本体がそのまま実行される」領域。
_NESTED_SH_HERESTR_RE = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:[^\s;&|()'\"]*/)?"
    r"(?:sh|bash|dash|zsh|ksh|ash)\b(?:\s+-[A-Za-z]+)*\s*"
    r"<<<\s*(?P<q>[\"'])(?P<body>(?:(?!(?P=q))[\s\S])*)(?P=q)")
#: ★`echo '…' | bash` / `printf … | sh`（2026-09-06・第4回c の 8 回目・反証 M-C の
#:   F16/F17）。パイプの**受け側が素のシェル**なら、左の文字列はそのまま実行される
#:   ——`bash -c` と同じ「本体がそのまま走る」領域。実測で保護ポートのサーバが
#:   死んだのに、結果本文は `(exit 0, no output)` だった。
_NESTED_ECHO_PIPE_SH_RE = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:echo|printf)\s+(?:-[A-Za-z]+\s+)*"
    r"(?P<q>[\"'])(?P<body>(?:(?!(?P=q))[\s\S])*)(?P=q)\s*\|\s*"
    r"(?:[^\s;&|()'\"]*/)?(?:sh|bash|dash|zsh|ksh|ash)\b(?:\s+-[A-Za-z]+)*\s*$")
#: ★`source /dev/stdin <<< '…'` / `. /dev/stdin <<< '…'`（反証 M-C の F18/F19）。
#:   `bash <<< '…'` は 7 回目に閉じたのに、**同じ物を今のシェルで読む綴り**だけが
#:   残っていた（この現場の常習「片方だけ直る」）。
_NESTED_SOURCE_HERESTR_RE = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:source|\.)\s+/dev/stdin\s*"
    r"<<<\s*(?P<q>[\"'])(?P<body>(?:(?!(?P=q))[\s\S])*)(?P=q)")
#: ★`find … -exec pkill … \;`（反証 M5/S05）。`-exec` の後ろは**そのまま起動される
#:   1 命令**なので、`\;` / `+` までを断片として同じ門に通す。
#:   ⚠ `_split_segments` は `\;` の `;` でも割るので、断片の末尾（`\` だけ残る形）
#:     も終端として認める。
_FIND_EXEC_RE = re.compile(
    r"(?:^|(?<=[\s;&|(]))-exec(?:dir)?\s+(?P<body>[\s\S]*?)"
    r"(?:\s+(?:\\;|';'|\";\"|;|\+)|\s*\\?\s*$)")
#: ★`env -S "pkill …"`（2026-09-05・第4回c の 7 回目・自分の監査）。`env -S` は
#:   1 つの文字列を argv に割って**その先頭を起動する**。シェルは通らないが、
#:   中身は同じ門に通せる。
_NESTED_ENV_S_RE = re.compile(
    r"(?:^|(?<=[\s;&|(]))(?:[^\s;&|()'\"]*/)?env\s+(?:\S+\s+)*?"
    r"(?:-[A-Za-z]*S|--split-string)[=\s]\s*(?P<q>[\"'])(?P<body>(?:(?!(?P=q))[\s\S])*)(?P=q)")
#: ★`awk 'BEGIN{system("pkill …")}'`（反証 M5/S06）。`system()` の中身は `/bin/sh` に
#:   渡る。⚠ awk の本体は単引用符の中なので、`_nested_shell_spans` の文字走査
#:   （引用の中を見ない）では届かない＝ここは本文全体に当てる。
_AWK_SYSTEM_RE = re.compile(r"\bsystem\s*\(\s*(?P<q>[\"'])(?P<body>(?:(?!(?P=q))[^\n])*)(?P=q)\s*\)")
#: 入れ子を辿る深さの上限（`sh -c "sh -c \"…\""` のような形で回り続けない）。
_NESTED_MAX_DEPTH = 3


def _nested_shell_spans(text: str) -> list[tuple[int, int, str]]:
    r"""`text` の中で「**別のシェルがそのまま実行する**」領域 `(start, end, 囲み)`。

    拾うのは 4 つ:
      * `sh -c "…"` / `bash -lc '…'` の**スクリプト本体**
      * `echo '…' | bash` / `source /dev/stdin <<< '…'`（8 回目・反証 M-C）
      * `( … )` のサブシェル
      * `` ` … ` `` / `$( … )` のコマンド置換
      * `eval …` の残り全部

    ★なぜ要るか（2026-09-05・第4回c の再反証・**6/6 が実物で死んだ**）:
      `_kill_protected_pids()` の床は `_scope_pid_kill` / `_scope_name_kill` の
      **内側**にしか無く、その 2 つは断片の**先頭が** `kill`/`pkill`/`killall` の
      ときだけ働く。だから

          sh -c "kill -9 <pid>"        bash -c 'pkill -f …'
          ( kill -9 <pid> )            eval kill …
          xargs kill -9 <<< <pid>      python3 -c "os.kill(<pid>,9)"

      の 6 形すべてで保護ポートの設備が死んだ。前の版が「綴りに依存しない」と
      書いたのは**誤り**（撤回）。門は入口に在っても、入口が 1 つしか無ければ
      裏口はいくらでも作れる。せめて**シェルが解釈する裏口**は同じ門に通す。
    ⚠ `python3 -c "os.kill(…)"`（および perl/ruby/node の同等品）は**塞げない**。
      あれはシェル構文ではないので、この層では読み解かない
      （読み解こうとすると任意の言語の評価器を書くことになる）。実行そのものを
      止めたいなら `policy` 側で `python -c` を禁じるしかない。
    ⚠ `{ …; }` のグループ化は**ここでは**拾わない（`{` は語であって記号ではなく、
      誤って `awk '{print}'` を割る側に倒れやすい）。**取りこぼす方に倒す。**
      ただし `{ kill 123; }` のように `{` が kill の**直前の語**として来る形は
      `_parse_kill` / `_strip_kill_prefix` の前置き剥がしが拾う。
    """
    spans: list[tuple[int, int, str]] = []
    n = len(text)
    i = 0
    q: str | None = None
    stack: list[int] = []
    while i < n:
        ch = text[i]
        if q:
            if ch == "\\" and q == '"':
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in ("'", '"'):
            q = ch
            i += 1
            continue
        if ch == "`":
            j = text.find("`", i + 1)
            if j > i:
                spans.append((i + 1, j, "`"))
                i = j + 1
                continue
            i += 1
            continue
        if ch == "(":
            stack.append(i)
            i += 1
            continue
        if ch == ")":
            if stack:
                a = stack.pop()
                spans.append((a + 1, i, ""))
            i += 1
            continue
        i += 1
    for m in _NESTED_SH_C_RE.finditer(text):
        spans.append((m.start("body"), m.end("body"), m.group("q")))
    m = _NESTED_EVAL_RE.search(text)
    if m:
        a, b = m.start("rest"), m.end("rest")
        body = text[a:b]
        if len(body) >= 2 and body[0] == body[-1] and body[0] in "\"'":
            spans.append((a + 1, b - 1, body[0]))
        else:
            spans.append((a, b, ""))
    # ★`trap '…'` / `bash <<< '…'` / `awk 'BEGIN{system("…")}'`（第4回c の 7 回目）
    for rx in (_NESTED_TRAP_RE, _NESTED_SH_HERESTR_RE, _AWK_SYSTEM_RE,
               _NESTED_ENV_S_RE, _NESTED_ECHO_PIPE_SH_RE,
               _NESTED_SOURCE_HERESTR_RE):
        for m2 in rx.finditer(text):
            spans.append((m2.start("body"), m2.end("body"), m2.group("q")))
    # ★`find … -exec pkill … \;`（引用符で囲まれていないので囲みは ""）
    for m3 in _FIND_EXEC_RE.finditer(text):
        spans.append((m3.start("body"), m3.end("body"), ""))
    # **外側だけ**残す（内側は再帰が見る）。重なりも落とす。
    spans.sort(key=lambda t: (t[0], -(t[1])))
    out: list[tuple[int, int, str]] = []
    last_end = -1
    for a, b, qq in spans:
        if a >= b or a < last_end:
            continue
        out.append((a, b, qq))
        last_end = b
    return out


#: `xargs` が起こすのが kill 系か（先頭のオプションは読み飛ばす）。
_XARGS_RE = re.compile(r"(?:^|[\s;&|(])xargs\b(?P<rest>[\s\S]*)$")
#: xargs の入力として**その場で読める**形。
_HERESTRING_RE = re.compile(r"<<<\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>\S+))")
_ECHO_HEAD_RE = re.compile(r"^\s*(?:echo|printf)\s+(?P<args>[^|]*)\|")
#: `xargs kill < pids.txt`（ヒアストリング `<<<` ではない**ファイル入力**）。
#: ★`$(cat pids.txt)` は解いていたのに、同じ中身を `<` で流し込む形だけ
#:   「読めない＝断る」に落ちていた（2026-09-05・第4回c の 4 回目・反証 B8）。
#:   断るので危険ではないが、**自分の pid ファイルでも止められない**＝
#:   正当な用途を潰す側の穴。同じ解き方を通す。
_STDIN_FILE_RE = re.compile(r"<(?!<)\s*(?P<path>[^\s<>|;&]+)")
_CAT_HEAD_RE = re.compile(r"^\s*cat\s+(?P<path>[^\s|;&]+)\s*\|")
_LSOF_HEAD_RE = re.compile(r"^\s*lsof(?P<args>[^|]*)\|")


def _xargs_kill_prog(rest: str) -> str | None:
    """`xargs` の後ろで起こされるプログラム名（kill 系でなければ None）。"""
    for t in rest.replace("\n", " ").split():
        if t.startswith("-") or "=" in t:
            continue
        base = os.path.basename(t.strip("\"'"))
        return base if base in ("kill", "pkill", "killall") else None
    return None


def _scope_xargs_kill(cmd: str, workspace: str | None,
                      base: str | None = None) -> tuple[str, str] | None:
    """`… | xargs kill …` / `xargs kill -9 <<< <pid>` を同じ門に通す。

    ★塞いだ穴（2026-09-05・第4回c の再反証）: `xargs kill -9 <<< <pid>` で
      保護ポートの設備が死んだ。対象が**標準入力から来る**ので、断片の先頭を見る
      2 つの門はどちらも一致しない（＝`_parse_kill` が None）。
    ★方針: **読める形は読む・読めない形は断る。**
      * `<<< 1234` / `echo 1234 |` / `cat x.pid |` / `lsof -t -i :N |` は
        その場で PID に解ける → `_scope_pid_kill` と**同じ判定**にかける。
        1 つでも撃たせない相手が混ざっていたら断片ごと落とす
        （xargs のパイプラインは部分的な書き換えが安全にできない）。
      * `ps aux | grep … | awk … | xargs kill` のように**解けない**形は断る。
        これは実記録にある形で、狙いが「自分の php」でも当たるのは全ホストの php。
        断りの文で、解ける形（`kill $(cat …)`）と道具（BashOutput）へ誘導する。
    """
    if sys.platform == "win32" or not workspace:
        return None
    m = _XARGS_RE.search(cmd)
    if not m:
        return None
    prog = _xargs_kill_prog(m.group("rest"))
    if not prog:
        return None
    pids: list[int] | None = None
    hm = _HERESTRING_RE.search(cmd)
    if hm:
        payload = hm.group("dq") or hm.group("sq") or hm.group("bare") or ""
        pids = [int(x) for x in re.findall(r"\b\d{1,7}\b", payload)[:32]] or None
    if pids is None:
        em = _ECHO_HEAD_RE.match(cmd)
        if em:
            pids = [int(x) for x in re.findall(r"\b\d{1,7}\b", em.group("args"))[:32]] or None
    if pids is None:
        cm = _CAT_HEAD_RE.match(cmd)
        if cm:
            pids = _resolve_kill_token(f"$(cat {cm.group('path')})", base)
    if pids is None:
        lm = _LSOF_HEAD_RE.match(cmd)
        if lm:
            pids = _resolve_kill_token(f"$(lsof {lm.group('args').strip()})", base)
    if pids is None:
        fm = _STDIN_FILE_RE.search(cmd)
        if fm:
            pids = _resolve_kill_token(f"$(cat {fm.group('path')})", base)
    if pids:
        probe = "kill -9 " + " ".join(str(p) for p in pids)
        r = _scope_pid_kill(probe, workspace, base)
        if not r:
            return None                   # 全部『中』＝1 文字も変えない
        note = (f"\n★`{cmd}` は xargs 越しに撃つ形でした（対象: "
                + " ".join(str(p) for p in pids[:8]) + "）。"
                "撃たせない相手が混ざっているので、**この断片は実行しません**"
                "（パイプラインは一部だけ落とせません）。" + r[1])
        return "true", note
    note = (f"\n★`{cmd}` は **xargs 越しの {prog}** で、対象が標準入力から来ます。"
            "この道具からは撃つ相手を確かめられないので実行しません"
            "（`ps|grep|awk|xargs kill` は狙いが自分の物でも、同じ名前の他人の"
            "サービスに当たります＝2026-09-02 に corridor-bridge を 6 回撃った形）。\n"
            "   ・PID が分かっているなら `kill <pid>`（同じ門を通ります）。\n"
            "   ・pid ファイルなら `kill $(cat <file>)`（解決して判定します）。\n"
            "   ・ポートを空けたいなら BashOutput(port=<port>) で持ち主を確かめる。\n"
            + _KILL_ADVICE)
    return "true", note


def _gate_fragment(text: str, workspace: str, base: str | None,
                   depth: int) -> tuple[str, str] | None:
    """1 つの断片に**外側と同じ門**を順に当てる（入れ子 → 名前撃ち → PID 撃ち）。"""
    cur, notes = text, []
    for fn in (lambda c: _scope_nested_kill(c, workspace, base, depth),
               lambda c: _scope_name_kill(c, workspace),
               lambda c: _scope_pid_kill(c, workspace, base)):
        try:
            r = fn(cur)
        except Exception:
            r = None
        if r:
            cur, note = r
            notes.append(note)
    return (cur, "".join(notes)) if notes else None


def _scope_nested_kill(cmd: str, workspace: str | None, base: str | None = None,
                       depth: int = 0) -> tuple[str, str] | None:
    """`sh -c "…"` / `( … )` / `eval …` / `$( … )` / `xargs` の**中の kill** を
    外側と同じ門に通す。戻り: (書き換えた命令, 注記) ／ 触らないなら None。"""
    if sys.platform == "win32" or not workspace or depth >= _NESTED_MAX_DEPTH:
        return None
    segs = _split_segments(cmd)
    if len(segs) > 1:
        changed, notes = False, []
        for i, s in enumerate(segs):
            if s in _SEG_SEPS:
                continue
            r = _keep_ws(s, lambda c: _scope_nested_kill(c, workspace, base, depth))
            if r:
                segs[i], n = r
                changed = True
                notes.append(n)
        return ("".join(segs), "".join(notes)) if changed else None
    r = _scope_xargs_kill(cmd, workspace, base)
    if r:
        return r
    spans = _nested_shell_spans(cmd)
    if not spans:
        return None
    out, notes, changed = cmd, [], False
    for a, b, q in sorted(spans, key=lambda t: -t[0]):
        got = _gate_fragment(out[a:b], workspace, base, depth + 1)
        if not got:
            continue
        new_inner, note = got
        if q and q in new_inner:
            # 囲みの引用符を壊す書き換えはしない（起きない想定だが床は敷く）。
            new_inner = "true"
        out = out[:a] + new_inner + out[b:]
        notes.append(note)
        changed = True
    return (out, "".join(notes)) if changed else None


#: `LAPS_PROTECTED_PORTS` の書き損じを結果本文に出したのは 1 度きりか
#: （うるさくしない。counters は毎回上げる）。
_PORTS_NOTE_SHOWN = False


def _ports_config_note() -> str:
    """`LAPS_PROTECTED_PORTS` の書き損じを**結果本文と counters**へ出す。

    ★なぜ要るか（2026-09-05・第4回c の 5 回目・反証 C(9)）: 書き損じの通知は
      `warnings.warn` **だけ**だった。出先は stderr、既定の `once` 規則で
      プロセスに 1 回、Bash 道具の戻り値にも counters にも一切現れない——
      つまり**モデルも人も、保護が思ったとおりに効いていないことをその場で
      知る手段が無かった**。この現場の「効いているのか壊れているのか区別できない」
      形そのものなので、警告は残したまま、拾える所へ出す。
    """
    global _PORTS_NOTE_SHOWN
    try:
        procutil.protected_ports()               # ★ここで状態が更新される
        problem = procutil.ports_config_problem()
    except Exception:
        return ""
    if not problem:
        return ""
    try:
        from . import CURRENT_AGENT
        _ag = CURRENT_AGENT.get()
        if _ag is not None and hasattr(_ag, "counters"):
            _ag.counters["protected_ports_misconfig"] = \
                _ag.counters.get("protected_ports_misconfig", 0) + 1
    except Exception:
        pass
    if _PORTS_NOTE_SHOWN:
        return ""
    _PORTS_NOTE_SHOWN = True
    return f"\n★保護ポートの設定に問題があります: {problem}\n"


def _reset_ports_note() -> None:
    """試験用: 「1 度きり」の記憶を戻す。"""
    global _PORTS_NOTE_SHOWN
    _PORTS_NOTE_SHOWN = False


def _run(inp: dict) -> str:
    cmd = inp.get("command") or inp.get("cmd")
    if not cmd:
        return "Error: missing 'command'"
    # ★空白入りパスの引用漏れを**実行の前に**直す（2026-09-21）。
    #   policy より先に置く＝**審査した文字列と実際に走る文字列を一致させる**
    #   （後から書き換えると、通した物と違う物が走る）。
    cmd, _autoquote_note = _win_autoquote(cmd)
    # ★見た目が同じで文字だけ違うパス（全角空白など）も実行の前に直す（2026-10-05）。
    cmd, _pf_note = _fix_confusable_paths(cmd, inp.get("cwd") or None)
    _autoquote_note += _pf_note
    # ★数として読めない timeout で道具ごと落ちない（read.py と共有・2026-09-02）。
    from .read import BadArg, int_arg
    try:
        # ★2026-09-07: Windows の既定は短く（実機 0.1.200 の走行で 2 分の沈黙＝120 秒の打ち切りまで
        #   待った形）。`dir /s`・対話プロンプト等が長引くので 60 秒で返して助言する。
        #   LAPS_BASH_TIMEOUT_WIN で変更可（秒）。
        _default_to = 120
        if sys.platform == "win32":
            try:
                _default_to = int(os.environ.get("LAPS_BASH_TIMEOUT_WIN", "60") or 60)
            except ValueError:
                _default_to = 60
        timeout = int_arg(inp, "timeout", _default_to)
    except BadArg as e:
        return f"Error: {e}"
    if timeout == 0:
        # 0 を「上限なし」のつもりで渡してくる。`communicate(timeout=0)` は
        # **即座に打ち切って**「timeout after 0s」を返すので、意図と正反対になる。
        return ("Error: timeout=0 は「上限なし」ではありません（即座に打ち切られます）。"
                "秒数を正の整数で指定してください（長い処理は run_in_background=true で）。")
    cwd = inp.get("cwd") or None
    run_in_background = bool(inp.get("run_in_background", False))
    if cwd and not Path(cwd).exists():
        return f"Error: cwd does not exist: {cwd}"
    # Policy gate first: it is the only thing standing between a prompt-injected
    # string and this host, and it must run before *any* dispatch path.
    try:
        authorize_command(cmd, cwd=cwd)
    except PolicyDenied as e:
        return f"Error: policy denied: {e}"
    if is_destructive(cmd):
        if not confirm(cmd, kind="Bash"):
            return "Error: destructive command denied by permission gate"
    # ★sandbox: policy gate の後・スコープ調整の前に囲む（審査した文字列＝走る文字列）。
    #   LAPS_SANDBOX=auto（既定）で自動検出、=firejail/systemd-run/docker で固定、=none で無効。
    _sandbox_mode: str | None = None
    _sandbox_note = ""
    if os.environ.get("LAPS_SANDBOX", "auto") != "none":
        try:
            _sandbox_mode = _sandbox.detect_mode()
            if _sandbox_mode != "none":
                _sandbox_note = f"（sandbox: {_sandbox_mode}）"
        except Exception:
            pass  # sandbox 無効化ではなくフォールバック
    # ★名前撃ちは作業場の中だけに絞る（2026-09-02・corridor-bridge を 6 回殺した実害）。
    _scope_note = ""
    # ★所有権の基準は **`policy._workspace()`**（2026-09-05・第4回b・両反証が同じ根）。
    #   以前は `cwd or os.getcwd()`——`cwd` は Bash 道具がモデルに開いている引数
    #   なので、そこを基準にすると「何を撃ってよいか」をモデル自身が決められた:
    #     (a) `Bash(command="kill -9 1", cwd="/")` で「作業場＝/」になり看板の防御が
    #         引数ひとつで消える。
    #     (b) 逆に `cwd="<ws>/site"` を渡すと**自分のサーバが『外』に化けて撃てなくなる**
    #         （run50 工程4 の壁）。
    #   Bash の `cwd` は「ここで走らせる」であって「これが作業場だ」ではない。
    #   基準を正典へ移すと (a) と (b) が同時に消える。**cwd では広げない。**
    #   名前撃ちと明示 PID 撃ちの**両方に同じ `_ws` を渡す**（片方だけ直さない）。
    try:
        _ws = procutil.owner_workspace()
    except Exception:
        _ws = os.getcwd()
    # ★**綴りの中に隠れた kill** も同じ門へ（2026-09-05・第4回c の再反証）。
    #   `sh -c "kill …"` / `bash -c 'pkill …'` / `( kill … )` / `eval kill …` /
    #   `xargs kill` の 5 形で保護ポートの設備が実際に死んだ。名前撃ちより先に
    #   通す（入れ子を開いてから、外側の門が残りを見る）。
    try:
        _sn = _scope_nested_kill(cmd, _ws, base=cwd or os.getcwd())
    except Exception:
        _sn = None
    if _sn:
        cmd, _scope_note = _sn
    try:
        _sc = _scope_name_kill(cmd, _ws)
    except Exception:
        _sc = None
    if _sc:
        cmd, _n0 = _sc
        _scope_note += _n0
    # ★明示対象の kill も同じ形で絞る（2026-09-05・裸PID killが無ガードだった穴）。
    try:
        _sp = _scope_pid_kill(cmd, _ws, base=cwd or os.getcwd())
    except Exception:
        _sp = None
    if _sp:
        cmd, _note2 = _sp
        _scope_note += _note2
    # ★作業場が床に届いていないときは、断った理由を必ず添える（2026-09-05・第4回c）。
    #   この状態では**何ひとつ「自分の物」にならない**ので、理由が無いと
    #   モデルは同じ命令を綴りを変えて撃ち続ける（記録に何度も出る形）。
    if _scope_note:
        try:
            _scope_note += procutil.workspace_note()
        except Exception:
            pass
    # ★保護ポートの設定の書き損じは、kill が来たかどうかに関わらず知らせる
    #   （反証 C(9)）。黙って保護が変わっているのがいちばん高くつく。
    _scope_note += _ports_config_note()
    # ★sandbox 囲み: コマンドを実行形式に合わせて包む。
    _sandbox_argv: list[str] | None = None
    if _sandbox_mode and _sandbox_mode != "none":
        sandboxed_cmd = _sandbox.wrap_command(cmd, _sandbox_mode, cwd=cwd)
        if isinstance(sandboxed_cmd, str):
            cmd = sandboxed_cmd  # shell=True で走る（背景・前面共通）
        else:
            _sandbox_argv = sandboxed_cmd  # argv 形式（firejail/systemd-run/docker）
    if run_in_background:
        try:
            if _sandbox_argv:
                # argv 形式: 文字列化して背景へ（ファイルに書く形）
                return _spawn_background(" ".join(_sandbox_argv), cwd)
            return _spawn_background(cmd, cwd)
        except Exception as e:
            return f"Error launching background: {e}"
    try:
        _refuse = _win32_refusal(cmd)
        if _refuse:
            return _refuse
        # ★sandbox argv 形式の場合は _spawn_args を経由せずに直接 Popen へ。
        _script: str | None = None
        if _sandbox_argv:
            _args: tuple = (_sandbox_argv,)
            _kw = {}
        else:
            _args, _kw = _spawn_args(cmd)
            _script = _kw.pop("_bw_script", None)
        # ★`subprocess.run` から `Popen` へ（2026-08-22）。run は PID を返さないので、
        #   打ち切ったときに**孫を片付けられない**。実測: `sleep 300 & sleep 300` を
        #   5 秒で打ち切ったら、シェルは死んだが**孫が2つ生き残った**。
        #   導入を許可した以上、これは「打ち切ったインストーラが裏で動き続ける」を意味する。
        # ★2026-09-21 owner 実機（Windows 0.1.231）: **前面の Bash が 1 回も動いていなかった。**
        #   `_spawn_args` の win32 分岐が 0.1.196 で `"stdin": DEVNULL` を返すようになり、
        #   ここでも `stdin=` を明示していたので、呼び出しのたびに
        #   `TypeError: Popen() got multiple values for keyword argument 'stdin'`。
        #   ＝**2 週間・35 版のあいだ、Windows では Bash が全滅していた**（記録 5/5 失敗）。
        #   試験は `_spawn_args` の返り値だけを見ていて、**この呼び出しを通していなかった**。
        #   ⇒ 引数は**辞書で合わせてから**渡す（同じ鍵が 2 度出る形を作れなくする）。
        #      `_spawn_args` の指定が勝つ。
        _popen_kw = _foreground_popen_kwargs(_kw, cwd)
        proc = subprocess.Popen(
            *_args,
            **_popen_kw,
            # ★標準入力を切る（2026-08-22・実機がこれで10分ハングした）。
            #   指定しないと子は**サイドカーの標準入力＝アプリとの通信パイプ**を
            #   継承する。害は2つ:
            #     1. 入力待ちの命令（インストーラの同意確認など）が**永久に止まる**。
            #        実機は「600 秒のあいだ Python から1バイトも届きませんでした」で
            #        落ち、その間ずっと本体が塞がって新しい依頼が通らなかった。
            #     2. 子が**アプリからの依頼を横取り**しうる（同じパイプを読むため）。
            #   DEVNULL にすると、入力を待つ命令は EOF で**すぐ失敗する**。
            #   止まるより失敗するほうがよい——失敗は読めるが、沈黙は読めない。
            #   ★導入を許可した（同日・オーナー決定）ので、この経路は今後もっと踏まれる。
            #   （★実際の `stdin=DEVNULL` は `_foreground_popen_kwargs` が入れる）
            # ★2026-08-16: `text=True` をやめて**バイトで受ける**。
            #   `text=True` は `encoding=` を書かない限り既定の符号化を使い、
            #   サイドカーは PYTHONUTF8=1 を渡すので**常に UTF-8** になる。
            #   日本語 Windows の `cmd.exe` は OEM コードページ（cp932）で出すので、
            #   「指定されたパスが見つかりません。」が丸ごと U+FFFD に潰れていた
            #   （実機の記録と、cp932→UTF-8 で読んだ結果がバイト単位で一致）。
            #   ＝**Windows で最も多い失敗の文面をエージェントが読めていなかった。**
            #   `errors="replace"` は「落とさない」ためのもので、
            #   **読めるようにするものではない**。区別する。
        )
        _register(proc)
        # ★**起動直後に**開始 tick を控える（2026-09-05・第4回c の 4 回目・調停係の指摘）。
        #   `_release_record` はシェルを `poll()` で刈り取った**後**に
        #   `proc_start_ticks(pgid)` を呼んでいたので、`/proc/<pgid>` がもう無く
        #   **必ず None**・`started_at` も「打ち切った時刻」になっていた。
        #   その控えは `procutil.bg_record_matches()` の ①②どちらの照合にも
        #   当たらないので、**本物の逃げ子の記録が捨てられる**（lifecycle 側の
        #   時刻の関門が false green になる根）。生きているうちに読む。
        _launch_ticks = None
        _launch_epoch = time.time()
        _launch_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(_launch_epoch))
        if sys.platform != "win32":
            try:
                _launch_ticks = procutil.proc_start_ticks(proc.pid)
            except Exception:
                _launch_ticks = None

        def _drop_script() -> None:
            # 一時ファイルは**必ず**消す（残すと作業場が汚れ、次の走行の測定を壊す）。
            if _script:
                try:
                    os.unlink(_script)
                except OSError:
                    pass
        # ★**パイプではなくシェルを待つ**（2026-09-05）。旧 `communicate(timeout=)` は
        #   背景に回った孫がパイプを持っている限り EOF を待ち続け、
        #   正常に起動したサーバのために 120 秒待った末に殺していた
        #   （`_wait_or_release` の docstring に再現と実測）。
        if os.environ.get("LAPS_BASH_RELEASE", "1") == "0":
            # 退避口: 旧の待ち方（パイプの EOF を待つ）。切り分け用で、常用しない。
            try:
                _o, _e = proc.communicate(timeout=timeout)
                _status, _pumps = "done", None
            except subprocess.TimeoutExpired:
                _status, _pumps = "timeout", None
                _o = _e = b""
        else:
            _o, _e, _status, _pumps = _wait_or_release(proc, timeout)
        if _status == "timeout":
            _unregister(proc)
            # ★殺す**前に**グループ番号を控える（後からでは `os.getpgid` が落ちる）。
            _pgid = None
            if sys.platform != "win32":
                try:
                    _pgid = os.getpgid(proc.pid)
                except OSError:
                    _pgid = proc.pid
            _kill_group(proc)
            _drop_script()
            if _pumps is None:
                try:
                    _o, _e = proc.communicate(timeout=5)
                except Exception:
                    _o, _e = b"", b""
            else:
                # 殺したので、残っている分を汲み切るのを少しだけ待つ。
                # ★予算は**合計 2 秒**（2026-09-05・第4回b）。旧実装は 2 本を直列に
                #   `wait(2)` していたので、グループの外へ逃げたプロセスが両方の
                #   パイプを握っていると打ち切り予算を最大 4 秒超過した
                #   （実測: timeout=5 の呼び出しが 9.05 秒かかった）。
                _drain_deadline = time.time() + 2.0
                for _pu in _pumps:
                    _pu.done.wait(max(0.0, _drain_deadline - time.time()))
                _o, _e = _pumps[0].snapshot(), _pumps[1].snapshot()
            partial = (_decode_out(_o) + _decode_out(_e)).strip()
            return ToolResult(
                f"Error: timeout after {timeout}s（打ち切りました）"
                + (f"\n途中までの出力:\n{partial[:2000]}" if partial else "")
                + _timeout_hint(cmd, partial, _pgid),
                ok=False, kind="error")
        _release_note = ""
        if _status == "released":
            # ★殺さない。**待つのをやめるだけ**（goose shell.rs:650 と同じ判断）。
            try:
                _release_note = _release_record(
                    proc, cmd, cwd, _pumps, start_ticks=_launch_ticks,
                    started_at=_launch_at, launch_epoch=_launch_epoch)
            except Exception as _re:
                _release_note = f"\n★背景に回りましたが、控えを残せませんでした: {_re}"
        _unregister(proc)
        _drop_script()
        if _scope_note:
            _o = (_o or b"") + _scope_note.encode("utf-8")
        r = subprocess.CompletedProcess(_args, proc.returncode, _o, _e)
        out = _decode_out(r.stdout) + _decode_out(r.stderr)
        # ★空出力は**この下の `_seen_empty` が専門に扱う**（初回は全文・2 回目からは
        #   1 行＋回数）。同一結果の畳みを重ねると、二つの仕組みが同じ場を奪い合う。
        _no_output = not out.strip()
        # ★終了コードを本文に出す（2026-08-28・監査 S2-b）。出力がある失敗は
        #   本文だけ返っていたので、モデルには成功と区別がつかなかった
        #   （実測: 失敗 Bash が 3 走行で 118/24/49 件・印なし）。
        #   空出力の分岐は既に `(exit N, no output)` と言うので、そちらとは二重にしない。
        if (r.returncode != 0 and out.strip()
                and os.environ.get("LAPS_BASH_EXIT_TAG", "1") != "0"):
            out = f"[exit={r.returncode}]\n" + out
            # ★引用漏れは「パスが違う」に見える。実在を確かめてから 1 行で言う。
            out += _win_quote_hint(cmd, out, r.returncode)
            # ★「実行ファイルではない」は、中身を読めば一言で終わる（2026-09-21）。
            out += _win_not_exe_hint(cmd, out, r.returncode)
            if sys.platform == "win32":
                out += _win_ps_param_hint(out) + _win_prn_hint(cmd, out)
        if _release_note and not out.strip():
            # 背景に回った起動は「空出力」ではない（注記が本体）。
            #   ここで空出力の定型 5 行を出すと、次の一手（Glob/Read を使え）が
            #   **正反対の助言**になる。
            out = ""
        elif not out.strip() and not _terse() and _seen_empty(cmd) > 1:
            # ★同じコマンドの空出力を繰り返すとき、定型 5 行は要らない
            #   （2026-08-29・実測）: run34 の定型文つき結果 381,097 字のうち
            #   **95%（362,308 字）が2回目以降**で、1 つのコマンドは同じ空結果を
            #   **139 回**返していた。初回は full（空振り再試行を止めた実測がある）、
            #   2 回目からは 1 行＋**何回目か**を言う。回数そのものが合図になる。
            out = (f"(exit {r.returncode}, no output) "
                   f"★同じコマンドで空の結果は {_EMPTY_SEEN_LAST[0]} 回目です。"
                   f"やり方を変えてください。")
        elif not out.strip() and _terse():
            # ★検収の実行では定型 5 行を出さない（2026-08-29・監査 P2）。
            #   実測: この定型文が Bash+検収の出力の 19.7〜27.9%、検収単体では
            #   最大 52.6% を占めていた。検収ログは直上に `$ <コマンド>` を出しており、
            #   作業場も走行中ずっと同じなので、**同じことを毎回書いている**。
            #   対話中の道具呼び出しでは今までどおり出す（空振り再試行を止めた実測がある）。
            out = f"(exit {r.returncode}, no output)"
        elif not out.strip():
            # ★実機で `dir` が「(exit 0, no output)」を18回返した。原因を追うには
            #   **何をどこでどのシェルで走らせたか**が要る。空のときだけ全部書く。
            #   （成功時に足すと文脈を食うので、空のときに限る）
            # ★実際に使ったシェルを言う（2026-08-29）。SHELL 環境変数は
            #   「利用者のログインシェル」であって、ここで走らせたものではない。
            shell = (os.environ.get("COMSPEC") if sys.platform == "win32"
                     else (_posix_shell() or "/bin/sh")) or "(不明)"
            out = (
                f"(exit {r.returncode}, no output)\n"
                f"  command: {_cmd_for_note(cmd)}\n"
                f"  cwd: {cwd or os.getcwd()}\n"
                f"  shell: {shell}\n"
                f"  platform: {sys.platform}\n"
                # ★2026-08-13 の実測（30B）: 空出力に**次の一手**を1行足すと、
                #   同じコマンドの空振り再試行（実機で `dir` を6回）が止まり、
                #   道具の切替が起きる。診断文だけ（次の一手なし）は逆に
                #   「70% が黙り込む」＝net で有害だったので、必ずセットで置く。
                #   条件付きにして、副作用コマンド（mkdir 等が正当に無出力）を
                #   誤誘導しない: 「情報が欲しかったなら」に限定する。
                # ★2026-10-05: 次の一手をコマンドの形で分ける（`_empty_next_step`）。
                #   台本（python -c 等）が何も print しなかった回にも「一覧は Glob」と
                #   言っていた（test2 の走行で、デコード台本の空出力に付いた）。
                + _empty_next_step(cmd)
            )
        # ★権限で落ちた結果の**直後に** sudo を教える（2026-08-24・VDIR テスト run1〜4 で
        #   モデルは envpack の一般論「sudo を試して」を 4 走行とも読み飛ばした。
        #   帳簿の教訓と同じで、モデルは**目の前の結果**に従う——ヒントは失敗の隣に置く）。
        #   trusted のときだけ。既に sudo 付きなら言わない。
        _ssh_like = bool(re.match(r"^\s*(?:sudo\s+)?(?:ssh|scp|sftp|rsync)\b", cmd))
        if r.returncode != 0 and _PERM_DENIED_RE.search(out) and _ssh_like:
            # ★ssh の「Permission denied」は**向こうの認証**（鍵・ユーザ）で、sudo では直らない
            #   （2026-09-03 dogfood ⑩: 旧文が `sudo ssh …` を勧め、モデルはそれを試して同じ拒否を 2 回踏んだ。
            #   間違った助言は無いより悪い）。次の一手は鍵の設定かパスワード（sshpass -e）。
            out += ("\n★ssh の認証（鍵またはユーザ）で向こうに拒まれています。sudo では直りません。"
                    "`~/.ssh/config` にその Host の IdentityFile/User があるか、`-i <鍵>` / `-l <user>` を確かめてください。"
                    "パスワードを渡されているなら `SSHPASS='<値>' sshpass -e ssh …` で入れます。"
                    "どれも駄目なら、できなかったと報告してください（偽物で代用しない）。")
        elif (r.returncode != 0 and _PERM_DENIED_RE.search(out)
                and os.environ.get("LAPS_POLICY", "").strip().lower() == "trusted"
                and not cmd.lstrip().startswith("sudo")):
            out += ("\n★権限で拒否されています。trusted モードでは sudo が使えます: "
                    f"sudo {cmd.lstrip()[:120]}\n"
                    "sudo でも駄目なら、できなかったと報告してください（偽物で代用しない）。")
        # ★「そのコマンドは無い」に、**実在する代替**を添える（2026-09-03・dogfood ⑮）。
        #   実機: モデルは `python -m pytest` を先に試して `command not found`
        #   （この機械には python3 しか無い）→ 自力で python3 に切り替えた。
        #   道具は今まで「無い」とだけ言い、代替は一切示していなかった。
        #   ★固定の別名表（"python"→"python3" のような決め打ち）は書かない。
        #     その機械に実在する保証が無い助言は、助言が無いより悪い
        #     （すぐ上の ssh の教訓と同じ）。`shutil.which` 相当で PATH を実際に
        #     調べ、**見つかったものだけ**を言う。
        if r.returncode != 0:
            _missing = _missing_command_name(out)
            if _missing:
                _alts = _find_path_alternatives(_missing)
                if _alts:
                    out += (f"\n★`{_missing}` はこの機械の PATH にありません。"
                            f"代わりに実在するのは: {', '.join(_alts)}"
                            "（実際に PATH を調べた結果です。推測ではありません）。")
                else:
                    out += (f"\n★`{_missing}` はこの機械の PATH にありません。"
                            "似た名前の代替も PATH 上に見つかりませんでした。")
        # ★終了コードを**型として持たせる**。ここまで `r.returncode` は
        #   空出力のときだけ本文に書かれ、それ以外は捨てられていた。
        #   捨てた結果、下流は「認識されていません」のような**言い回しの一覧**で
        #   成否を当てるしかなく、Windows の日本語や Traceback を取り逃していた。
        #   シェルは成否を数値で言っている。推測に変換しない。
        # ★先頭 cap 文字で切るのをやめ、**中央を畳んで末尾を残す**（2026-08-29）。
        #   実測 run29/31/32/34: Bash の結果は 878,221 バイト（全体の 10.8%）で、
        #   最大 27,306 バイト。旧実装 `out[:cap]` は**末尾を捨てる**が、
        #   実行結果の結論は末尾にある（失敗の理由・要約・最後の行）。
        # ★同一判定は**畳む前**の出力で行う（畳んだ後で比べると、省いた中央だけが
        #   変わった回を「同じ」と誤判定する）。
        # ★2026-09-05 r4c 4 巡目（査読 must_fix#1・**実機 e2e で再現した回帰**）:
        #   ここは 1 値版 `fold_head_tail` を呼んでいたので、畳めなかったとき
        #   （末尾が空白/空行しか無い出力）に**何も書かずに切っていた**。
        #   実測 `LAPS_N_CTX=16384`・`python3 -c "…'DATA'*1500 + 改行*700"`:
        #       6,700B → 1,200B・本文の開示語 0（「中略」も「ここで切りました」も無し）
        #   末尾に空行が並ぶのは `_tail_room(1,200)=600B` ぶんで足りる＝ログや
        #   `print` の末尾で普通に起きる形。`fold_head_tail_told` は畳めなかった
        #   ときも印を置く。`inside_cap=False`＝印は上限の**外**に足すので、
        #   本文は 1 バイトも減らない（この道具は下で `limits.out_cap` の
        #   外枠を別に持っている）。
        _raw = out
        out = fold.fold_head_tail_told(out, limits.body_cap(30000),
                                       inside_cap=False)
        # ★畳んだ**全文をファイルに残して場所を教える**（2026-09-21・OSS 2 本から）。
        #   出典: goose `agents/large_response_handler.rs:13-45`
        #         opencode `tool/truncate.ts:14-15`
        #   ★畳み方そのものは変えない（「切った」と書く作法は既に在る）。
        #     足すのは**全文の行き先**だけ——欲しいのは全部ではなく、その中の 1 行なので、
        #     Grep / Read で取りに行けるようにする。
        #   ⚠ 残せなければ**何も言わない**（`fulltext.note` が空を返す）。
        if len(out) < len(_raw):
            from . import fulltext as _ft
            out += _ft.note(_ft.keep(_raw, "bash"), len(_raw), len(out))
        # ★黙って書き換えない。直したことは畳みの外で必ず出す。
        if _autoquote_note:
            out += _autoquote_note
        # ★同じコマンドの 2 回目以降は短くする（`_seen_empty` と同じ考え方）。
        # ★ただし**空出力には掛けない**（2026-09-02・全体試験が捕まえた）。
        #   空出力は上の `_seen_empty` が既に「初回は全文・2 回目からは 1 行＋回数」に
        #   していて、そちらのほうが情報が多い（次の一手・何回目か）。両方掛けると
        #   どちらが勝つかは**文の長さ差**で決まる——実際、畳みの文言を 1 行縮めた
        #   だけで勝者が入れ替わり、初回の定型文（cwd・shell・次の一手）が消えた。
        #   長さで決まる挙動は仕様ではない。場ごとに担当を 1 つにする。
        if not _no_output and not _release_note:
            # ★背景に回した起動は畳まない。畳むと**新しいグループ番号**が消え、
            #   モデルは前回の pid を止めに行く（同じコマンドで 2 回起動するのは普通）。
            out = _fold_repeat(cmd, cwd, _raw, out)
        _cap = limits.out_cap(30000)
        if len(out) > _cap:
            out = out[:_cap] + limits.note(
                len(out), _cap, "必要な箇所を絞って実行し直してください。")
        if _release_note:
            out = (out + _release_note) if out.strip() else _release_note.lstrip("\n")
        return ToolResult(out, ok=(r.returncode == 0),
                          kind="ok" if r.returncode == 0 else "error")
    except Exception as e:
        return f"Error: {e}"


def _server_rule(platform: str | None = None) -> str:
    """道具の説明に載せる**サーバの扱い**。★OS で助言を変える（2026-09-05・第4回b）。

    Windows の `cmd.exe` で末尾の `&` は**区切り**であって背景化ではない。
    全プラットフォーム共通で「& を付けろ」と書いていたのは、この現場が繰り返す
    「もっともらしいが、その環境では効かない助言」そのものだった。
    """
    win = (platform or sys.platform) == "win32"
    tail = ("run_in_background=true で呼ぶこと"
            "（cmd.exe では末尾の & は**区切り**で、背景化しません）。"
            if win else
            "run_in_background=true か、コマンドの末尾に & を付けること"
            "（どちらもシェルが終わった時点で制御が返る）。")
    stop = ("居残りサーバは tasklist で探し taskkill /F /PID <pid> で止める。"
            if win else
            "居残りサーバを止めるときは BashOutput(port=<番号>, kill=true) を使い、"
            "`ps aux | grep` や `pkill -f` で探さないこと（pkill -f は自分を撃つ）。")
    return ("★サーバなど終わらない処理は " + tail +
            "前面のまま起動するとタイムアウトまで止まり、"
            "そのとき**プロセスグループごと強制終了される**。" + stop)


SPEC = ToolSpec(
    name="Bash",
    # ★「任意のコマンドを実行する」しか書いていない道具は、能力の上位集合を宣言して
    #   境界を一つも書いていないのと同じで、依頼文のどの動詞にも当たってしまう。
    #   禁止は抽象語（「専用の道具を優先」）ではなく**コマンド名の実名**で書く。
    #   実測 2026-08-13: 「ls でこのディレクトリの中身を見せて」に対し 8/8 が
    #   `ls -la` を Bash に渡していた（Windows 環境ブロックを見せても 8/8）。
    #   利用者がコマンド名で言ってきても、欲しいのは結果のほうだと明示する。
    description=(
        "任意のコマンドを実行し、stdout+stderr を返す。既定のタイムアウトは 120 秒。"
        "run_in_background=true で切り離して起動し PID を返す（回収は BashOutput）。"
        # ★2026-09-05: サーバの扱いを**道具の説明に**書く。PROMPTS/ にも書いたが、
        #   この現場の実測（帳簿・envpack）が繰り返し示すとおり、モデルは
        #   **目の前の道具の説明**に従う。事実だけを短く書く（勧誘や規律は書かない）。
        + _server_rule()
        + _shell_guidance()
    ),
    schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run"},
            "description": {"type": "string", "description": "Short description of what this command does"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"},
            "cwd": {"type": "string", "description": "Working directory (optional)"},
            "run_in_background": {"type": "boolean", "description": "If true, spawn detached and return PID"},
        },
        "required": ["command"],
    },
    mutates=True, executes=True,
    handler=_run,
)
