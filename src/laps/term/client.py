"""`python -m laps.serve` を子プロセスとして起動し、JSON 行で話す薄い殻。

ここはエンジンではない（設計 §0）。本体・道具・policy・ethics はすべて子プロセスの
中にあり、この殻がするのは 4 つだけ:

* 起動時の環境変数で実行設定を渡す（`LAPS_POLICY` 等は線の向こうから送れない）。
* stdin に 1 行 1 JSON を書く（send / cancel / ping / answer / quit）。
* stdout を行ごとに読み、JSON にして Queue に積む（読むスレッドは 1 本）。
* stderr は別スレッドで吸う（吸わないとパイプが詰まって子が止まる）。

★ 符号化は自分で固定する（Windows の cp932 で 4 回壊れた教訓）。text=False で bytes を
  読み、utf-8 で decode する。子にも PYTHONUTF8=1 を渡す。
★ 壊れた行は捨てない（`_garbage` として積む）。黙って消えると「何も起きない」に見える。
★ 子が死んだら `_exit` を積む。読む側はこれで「待っても来ない」と分かる。
★ 読むスレッドは**自分の子の Queue** に積む（self._q を後から見に行かない）。restart で
  子が入れ替わった後に、旧い子の `_exit` が新しい子の列へ混ざる競合が実測で出た
  （孫プロセスがパイプを握っていると旧ポンプは quit の後も生きている）。
★ 読みはバッファ付き（bufsize=-1）。bufsize=0 の FileIO.readline は 1 バイトずつ
  read(2) を呼び、200KB の行 20 本で 0.94 秒かかった（バッファ付きは 0.017 秒）。
  行は揃った時点で返るので token の遅れは出ない（実測 0.02 秒）。
"""
from __future__ import annotations

from .. import paths as _lp
from laps import _envcompat
import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

__all__ = ["ServeClient", "decode_line", "SERVE_MODULE"]

SERVE_MODULE = "laps.serve"

# start() のやり直しで引き継ぐ設定の鍵（restart(**overrides) が受け付けるのもこれだけ）。
_SETTING_KEYS = ("workspace", "backend", "model", "policy", "ethics", "home",
                 "tools", "max_iters", "python", "debug_log", "extra_env", "module")

# stderr の末尾を何行覚えておくか（起動失敗の説明に使う。debug_log とは別）。
_STDERR_TAIL = 40


def _close_quietly(s: Any) -> None:
    try:
        if s is not None:
            s.close()
    except (OSError, ValueError):
        pass


def decode_line(raw: bytes) -> dict | None:
    """stdout の 1 行をイベントにする。空行は None。壊れた行は `_garbage` で返す。"""
    line = raw.decode("utf-8", errors="replace").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return {"type": "_garbage", "line": line}
    if not isinstance(obj, dict):
        return {"type": "_garbage", "line": line}
    t = obj.get("type")
    if isinstance(t, str) and t.startswith("_"):
        # `_exit` `_garbage` はこの殻の内部名。線の向こうから来たら事象ではない
        # （子が `_exit` を書けば events() が「死んだ」と誤って終わる）。
        return {"type": "_garbage", "line": line}
    return obj


class ServeClient:
    """serve の客。1 インスタンス = 子プロセス 1 本（restart で入れ替わる）。"""

    def __init__(self, *, workspace: str, backend: str, model: str,
                 policy: str = "workspace", ethics: str = "standard",
                 home: str | None = None, tools: str = "full", max_iters: int = 0,
                 python: str | None = None, debug_log: str | None = None,
                 extra_env: dict | None = None, module: str = SERVE_MODULE) -> None:
        self.workspace = str(workspace)
        self.backend = backend
        self.model = model
        self.policy = policy
        self.ethics = ethics
        self.home = home
        self.tools = tools
        self.max_iters = int(max_iters)
        self.python = python or sys.executable
        self.debug_log = debug_log
        self.extra_env = dict(extra_env or {})
        # 試験が「起動に失敗する子」を作るための口。通常は触らない。
        self.module = module

        self.status: dict = {}          # 最後の ready / pong
        self._proc: subprocess.Popen | None = None
        self._q: "queue.Queue[dict]" = queue.Queue()
        self._held: collections.deque[dict] = collections.deque()  # 待ち中に脇へ寄せた事象
        self._threads: list[threading.Thread] = []
        self._wlock = threading.Lock()
        self._seq = 0
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL)
        self._exited = False

    # -- 状態 ----------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    @property
    def settings(self) -> dict:
        """いまの起動設定（/status の表示と restart の土台）。"""
        return {k: getattr(self, k) for k in _SETTING_KEYS}

    def stderr_tail(self) -> str:
        return "".join(self._stderr_tail)

    # -- 起動 ----------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        for k, v in self.extra_env.items():
            # ★旧い名前（BLACKWALL_*）で渡されても新しい名前に効かせる（子の中では LAPS が勝つので、片方だけ入れると負ける）。
            if k.startswith((_envcompat.NEW, _envcompat.OLD)):
                _envcompat.setenv(k, str(v), env)
            else:
                env[k] = str(v)
        # 呼び出し側の sys.path を継ぐ（開発ツリーから起動しても子が本体を見つける）。
        paths = [p for p in sys.path if isinstance(p, str) and p]
        prev = env.get("PYTHONPATH", "")
        if prev:
            paths.append(prev)
        env.setdefault("LAPS_USER_PYTHONPATH", prev)     # 道具に渡す環境では元へ戻す（oc/toolenv.child_env）
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(paths))
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["LAPS_BACKEND"] = self.backend
        env["LAPS_MODEL"] = self.model
        env["LAPS_WORKSPACE"] = self.workspace
        env["LAPS_POLICY"] = self.policy
        env["LAPS_ETHICS"] = self.ethics
        env["LAPS_NATIVE_TOOLS"] = "1"
        env["LAPS_TOOLSET"] = self.tools
        # 反復の上限なし（オーナー指示）。時間・道具数の予算も外す。
        env["LAPS_MAX_SECONDS"] = "0"
        env["LAPS_MAX_TOOL_CALLS"] = "0"
        if self.home:
            env["LAPS_HOME"] = str(self.home)
        _envcompat.apply(env)                # 旧い名前（BLACKWALL_*）にも同じ値を入れる（LAPS が勝つ）
        return env

    def prepare_parts(self) -> None:
        """同梱の道具の部品（Listen の whisper.cpp とモデル・ffmpeg）を裏でそろえる（2026-10-07 オーナー
        「Linux 版はサーバーの推論機使うのでそれ以外そろえてもらえればいい」）。Windows のアプリ（sidecar.rs）と
        同じ入口 `python -m laps.oc.parts`。そろっていれば数秒で終わる。待たない（端末版を閉じても続く）。
        LAPS_PREPARE_PARTS=0 で止める。試験の中（pytest）では走らせない。"""
        if os.environ.get("LAPS_PREPARE_PARTS", "").strip() == "0" or "PYTEST_CURRENT_TEST" in os.environ:
            return
        home = Path(self.home) if self.home else _lp.dot_dir()
        try:
            home.mkdir(parents=True, exist_ok=True)
            log = open(home / "parts.log", "ab")
        except OSError:
            return

        def _lower() -> None:                         # 利用者の作業を優先する
            try:
                os.nice(10)
            except OSError:
                pass
        try:
            subprocess.Popen([sys.executable, "-m", "laps.oc.parts"], env=self._env(), cwd=str(home),
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                             preexec_fn=_lower if os.name == "posix" else None)
        except OSError:
            pass
        finally:
            log.close()

    def start(self, timeout: float = 20.0) -> dict:
        """子を起動し、`ready` の notice を返す。来なければ RuntimeError（stderr の末尾つき）。"""
        if self.alive:
            raise RuntimeError("もう起動している（restart を使う）")
        ws = Path(self.workspace)
        try:
            ws.mkdir(parents=True, exist_ok=True)   # serve 自身も起動時に作る。cwd に使うので先に
        except OSError as e:
            raise RuntimeError(f"作業場を用意できない: {ws}: {e}") from e
        # 子ごとに新しい列と stderr の控えを持つ（旧い子のポンプが生き残っていても混ざらない）
        q: "queue.Queue[dict]" = queue.Queue()
        tail: collections.deque[str] = collections.deque(maxlen=_STDERR_TAIL)
        self._q = q
        self._stderr_tail = tail
        self._held.clear()
        self._exited = False
        try:
            self._proc = subprocess.Popen(
                [self.python, "-m", self.module],
                cwd=str(ws), env=self._env(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, bufsize=-1,
            )
        except OSError as e:
            raise RuntimeError(f"子プロセスを起動できない: {self.python} -m {self.module}: {e}") from e
        proc = self._proc
        t_out = threading.Thread(target=self._pump_stdout, args=(proc, q),
                                 name="bw-term-stdout", daemon=True)
        t_err = threading.Thread(target=self._pump_stderr, args=(proc, tail),
                                 name="bw-term-stderr", daemon=True)
        self._threads = [t_out, t_err]
        t_out.start()
        t_err.start()

        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self._kill_child()
                raise RuntimeError(
                    f"{timeout:.0f} 秒待っても ready が来ない。stderr:\n{self.stderr_tail()}")
            try:
                ev = self._q.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if ev.get("type") == "_exit":
                # stderr スレッドが吐き切るのを少し待つ（説明が空だと直せない）
                t_err.join(timeout=1.0)
                raise RuntimeError(
                    f"子プロセスが ready の前に終了した（code={ev.get('code')}）。stderr:\n"
                    f"{self.stderr_tail()}")
            if ev.get("type") == "notice" and ev.get("kind") == "ready":
                self.status = ev
                return ev
            self._held.append(ev)   # ready の前に来た事象も捨てない

    def _pump_stdout(self, proc: subprocess.Popen, q: "queue.Queue[dict]") -> None:
        out = proc.stdout
        assert out is not None
        try:
            for raw in iter(out.readline, b""):
                ev = decode_line(raw)
                if ev is not None:
                    q.put(ev)
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(out)   # 読み手が自分で閉じる（他のスレッドから閉じると lock で固まる）
            code = proc.wait()
            # 先に積んでから旗を立てる（逆だと events(None) が「死んで空」と見て
            # `_exit` を出さずに戻る瞬間ができる）。旗はこの子がまだ現役のときだけ。
            q.put({"type": "_exit", "code": code, "pid": proc.pid})
            if self._proc is proc:
                self._exited = True

    def _pump_stderr(self, proc: subprocess.Popen, tail: collections.deque[str]) -> None:
        err = proc.stderr
        assert err is not None
        f = None
        if self.debug_log:
            try:
                Path(self.debug_log).parent.mkdir(parents=True, exist_ok=True)
                f = open(self.debug_log, "ab")
            except OSError:
                f = None   # 書けないなら捨てる（診断のために本体を止めない）
        try:
            for raw in iter(err.readline, b""):
                tail.append(raw.decode("utf-8", errors="replace"))
                if f is not None:
                    try:
                        f.write(raw)
                        f.flush()
                    except OSError:
                        pass
        except (OSError, ValueError):
            pass
        finally:
            _close_quietly(err)
            if f is not None:
                f.close()

    def debug_note(self, text: str) -> None:
        """クライアント側の出来事を `--debug FILE` に 1 行足す（無ければ何もしない）。

        ★なぜ要るか（実機・2026-09-03）: `--debug FILE` を付けても 0 バイトだった日が
          あった。理由は単純——`_pump_stderr` は**子の stderr だけ**をこのファイルへ
          落としており、serve は正常系では stderr に 1 バイトも書かない（`_log` は
          例外パスでしか呼ばれない）。中断が 73 秒止まらなかった走行のような
          「何も壊れていないのに長い」ケースは、クライアントが見ている**往復**
          （cancel を送った・受理の notice が来た・done が来た、を app.run_turn が渡す）が
          いちばんの手がかりだが、それがどこにも残っていなかった。
        ★子の stderr と**同じファイル**に追記する（別ファイルにすると、後から2つを
          時刻で突き合わせる手間が要る）。`_pump_stderr` は 'ab' で開いたまま子の寿命の
          間ずっと保持するが、こちらは呼ばれるたびに 'a' で開いて閉じる——別の
          ファイルディスクリプタでも O_APPEND どうしなので、短い行なら競合しない。
        """
        if not self.debug_log:
            return
        try:
            Path(self.debug_log).parent.mkdir(parents=True, exist_ok=True)
            with open(self.debug_log, "a", encoding="utf-8") as f:
                f.write(f"[term {time.strftime('%H:%M:%S')}] {text}\n")
        except OSError:
            pass

    # -- 書く ----------------------------------------------------------------

    def _write(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise RuntimeError("子プロセスが動いていない")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._wlock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError) as e:   # BrokenPipe / closed
                raise RuntimeError(f"子プロセスに書けない: {e}") from e

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def send(self, messages: list[dict], *, rid: str | None = None,
             session: str | None = None, source: str = "term") -> str:
        """send を書き、その id を返す。policy/ethics/home/native_tools は**入れない**
        （serve が _FORBIDDEN で Rejected にする。それらは起動時の環境変数で渡してある）。"""
        rid = rid or self._next_id("r")
        body: dict[str, Any] = {
            "type": "send", "id": rid, "messages": list(messages),
            "model": self.model, "workspace": self.workspace, "tools": self.tools,
            "backend": self.backend, "max_iters": self.max_iters, "source": source,
        }
        if session:
            body["session"] = session
        self._write(body)
        return rid

    def cancel(self, rid: str) -> None:
        self._write({"type": "cancel", "id": rid})

    def answer(self, ask_id: str, text: str) -> None:
        self._write({"type": "answer", "ask": ask_id, "text": text})

    def inject(self, rid: str, text: str, *, force: bool = False) -> None:
        """走行中の割り込み（serve の inject・作業は捨てない）。force は区切りを待たず、いまの生成と Bash を止めて渡す。
        ★2026-10-12 全画面表示（tui.py）から使う。アプリの強制割り込み（0.1.342）と同じ線。"""
        self._write({"type": "inject", "id": rid, "text": text, "force": bool(force), "source": "term"})

    def ping(self, timeout: float = 5.0) -> dict:
        """ping を送り pong（status と同じ鍵）を返す。途中の事象は脇へ寄せ、events() で出す。"""
        pid = self._next_id("p")
        self._write({"type": "ping", "id": pid})
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{timeout:.0f} 秒待っても pong が来ない")
            try:
                ev = self._q.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if ev.get("type") == "notice" and ev.get("kind") == "pong" and ev.get("id") == pid:
                self.status = ev
                return ev
            self._held.append(ev)
            if ev.get("type") == "_exit":
                raise RuntimeError(f"pong を待つ間に子プロセスが終了した（code={ev.get('code')}）")

    # -- 受ける --------------------------------------------------------------

    def events(self, timeout: float | None = None) -> Iterator[dict]:
        """受信した事象を順に yield する。

        timeout を与えると、その秒数だけ待って何も来なければ None を 1 つ yield して戻る
        （呼び手は波を描き直してまた呼ぶ）。None なら事象が来るまで待つが、子が居なくなって
        積み残しも無ければ戻る（永久に待たない）。`_exit` を出したらそこで終わる。
        """
        while True:
            if self._held:
                ev = self._held.popleft()
            else:
                if timeout is None:
                    if self._exited and self._q.empty():
                        return
                    try:
                        ev = self._q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                else:
                    try:
                        ev = self._q.get(timeout=timeout)
                    except queue.Empty:
                        yield None
                        return
            yield ev
            if ev.get("type") == "_exit":
                return

    # -- 止める --------------------------------------------------------------

    def _kill_child(self) -> None:
        """自分が起動した Popen だけを相手にする（pid の再利用で他人を撃たない）。

        閉じるのは stdin だけ。stdout/stderr は読んでいるスレッドが EOF で自分で閉じる
        ——他のスレッドから BufferedReader を close すると readline の lock 待ちで
        固まり、孫プロセスが stderr を握っている間（実測 6 秒）restart が止まった。
        serve のイベント出口は os.dup(1)（継承不可）なので stdout の EOF は子の死と
        同時に来る。stderr は孫に継がれうるが、控えは子ごとに分けてあるので残っても害はない。
        """
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
            except OSError:
                pass
        _close_quietly(proc.stdin)

    def quit(self, timeout: float = 5.0) -> None:
        """quit を送って待つ。残れば SIGTERM → SIGKILL（自分が起動した子だけ）。"""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                self._write({"type": "quit"})
            except RuntimeError:
                pass
            try:
                if proc.stdin is not None:
                    proc.stdin.close()   # stdin の EOF も「終わり」の合図（serve の読み口）
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        self._kill_child()
        # stdout のポンプは子の死と同時に終わる（`_exit` を積み終えるのを待つ）。
        # stderr は孫プロセスが握っていると EOF が来ないので短く見切る
        # （daemon スレッドなので残っても害はない。控えは子ごとに分けてある）。
        for t, limit in zip(self._threads, (2.0, 0.5)):
            t.join(timeout=limit)
        self._threads = []

    def close(self) -> None:
        self.quit()

    def __enter__(self) -> "ServeClient":
        if not self.alive:
            self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.quit()

    # -- 切替 ----------------------------------------------------------------

    def restart(self, timeout: float = 20.0, **overrides: Any) -> dict:
        """quit → 同じ設定＋上書きで start。policy/ethics の切替はこれ（環境変数でしか渡せない）。"""
        bad = sorted(k for k in overrides if k not in _SETTING_KEYS)
        if bad:
            raise TypeError(f"restart が知らない設定: {', '.join(bad)}")
        self.quit()
        # ★失敗したら設定を**戻す**（2026-08-23 レビュー high）。戻さないと、失敗した
        #   `/policy trusted` の値が残り、次の send の自動再起動で**無確認のまま** trusted の子が
        #   立つ（実測で再現）。切替が成立したときだけ新しい値を持つ。
        before = {k: getattr(self, k) for k in overrides}
        for k, v in overrides.items():
            if k == "max_iters":
                v = int(v)
            elif k == "extra_env":
                v = dict(v or {})
            elif k == "workspace":
                v = str(v)
            setattr(self, k, v)
        try:
            return self.start(timeout=timeout)
        except Exception:
            for k, v in before.items():
                setattr(self, k, v)
            raise
