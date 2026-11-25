"""入口・REPL（設計 §4・§5・§11）。`blackwall` コマンド＝ここの main()。

ここはエンジンではない（§0）。することは 4 つ:
  引数と設定を決める → ServeClient（子の serve）を起動 → 入力を send か `/` に振り分ける →
  事象を Renderer に流す（ask は利用者に聞いて answer を返す）。

★ 会話の履歴は本体が持つ（§11）。最初の send は session=None で [user] 1 件、done の session を
  覚え、以後は session=<id> で**新しい利用者発言 1 件だけ**を送る。
★ Live（Renderer）の間に print()/prompt_toolkit を直接使うと画面が崩れる。ask の前に Live を
  一時停止（Renderer.pause・ターンは閉じない）、答えを返してから Renderer.resume で再開する。
  cancel／子の死亡で done が来ないときは end_turn。
★ Ctrl-C: 1 回目は cancel（done は来るので待つ）、2 回目で終了。
"""
from __future__ import annotations

from .. import paths as _lp
from laps import _envcompat
import argparse
import collections
import json
import os
import select
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

from rich.console import Console
from rich.text import Text

from . import __version__, config, slash
from . import theme as T
from .client import ServeClient
from .render import Renderer, card_on, mask_private, privacy_on, record_on
from .wave import calm_from_env, make_factory

__all__ = [
    "main", "build_parser", "AppContext", "run_turn", "exit_code", "wave_allowed",
    "make_wave_factory", "record_event", "EVENTS_KEEP", "ACCEPTED",
]

ACCEPTED = frozenset({"accepted", "accepted_after_pushback"})
EVENTS_KEEP = 200           # /sendlog に載せる直近の生イベント数（token は結合）
PROMPT = "› "
#: 入力欄が空のときだけ出る案内。★幅 60 の端末でも 1 行に収まる長さにする（`› ` の 2 桁を引いて 58 桁）。
PLACEHOLDER = "やりたいことを書く  /help 一覧  Alt+Enter 改行"


# --------------------------------------------------------------------------
# 小さな判定
# --------------------------------------------------------------------------

def exit_code(outcome: Any) -> int:
    """-p の終了コード: accepted 系 0／cancelled 130／それ以外 1。"""
    o = str(outcome or "")
    if o in ACCEPTED:
        return 0
    if o == "cancelled":
        return 130
    return 1


def wave_allowed(console: Console) -> bool:
    """波を作るか。非 TTY（パイプ）と dumb 端末では作らない（§3）。"""
    try:
        return bool(console.is_terminal) and not bool(console.is_dumb_terminal)
    except Exception:
        return False


def make_wave_factory(console: Console, calm: bool) -> Optional[Callable[[], Any]]:
    if not wave_allowed(console):
        return None
    # ★`wave=off` のときは**係そのものを作らない**（2026-09-01・SPEC 項目 21）。
    #   旧: off でも Wave を作っていた。絵は 1 行も出ないが、`render._refresh_rate()` が
    #   波の period で Live を回し続ける＝**消したはずの物が刷新回数だけ生きている**。
    return make_factory(lambda: console.width, calm=calm)


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def _short_path(p: str) -> str:
    try:
        home = str(Path.home())
        if p == home or p.startswith(home + os.sep):
            return "~" + p[len(home):]
    except Exception:
        pass
    return p


# --------------------------------------------------------------------------
# 状態
# --------------------------------------------------------------------------

@dataclass
class AppContext:
    console: Console
    client: Any
    renderer: Renderer
    cfg: dict
    session: Optional[str] = None
    stats: dict = field(default_factory=lambda: {"turns": 0, "tools": 0, "secs": 0.0, "tokens": 0})
    running_rid: Optional[str] = None
    wave_factory: Optional[Callable[[], Any]] = None
    install_id_path: Path = field(default_factory=lambda: config.config_path().parent / "install_id")
    exit_requested: bool = False
    # ★設定ファイルの場所（2026-10-19）。/model・/backend claude|server で選んだ送り先をここに残す。None なら残さない（試験）。
    config_path: Optional[Path] = None
    # 以下は /sendlog と ask のための追加（設計 §11）
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=EVENTS_KEEP))
    last_input: str = ""
    last_outcome: str = ""
    ask_fn: Optional[Callable[[str], str]] = None     # 1 行読む（prompt 文字列 → 入力）。None なら input()
    # ★y/N の確かめ（2026-11-03・/model stop）。全画面は小窓で聞く。None なら対話のときだけ ask_fn で 1 行読む。
    confirm_fn: Optional[Callable[[str], bool]] = None
    restarts: int = 0
    json_out: bool = False
    # ★対話（REPL）で走っているか。走行中の告知（#14）は対話のときだけ出す:
    #   -p をパイプに流している人にとって、余分な 1 行は解析の邪魔にしかならない。
    interactive: bool = False
    # ★走行中のキーの見張りを差し替える（2026-10-12 全画面表示 tui.py）。全画面表示は端末を自分で読むので、
    #   _KeyWatch が同じ fd を raw にして取り合わないよう、画面の Ctrl-C を数える物を渡す。None なら従来どおり。
    keys_factory: Optional[Callable[[Callable[[], None]], Any]] = None


def record_event(ctx: AppContext, ev: dict) -> None:
    """直近 EVENTS_KEEP 件を持つ。連続する token は 1 件に結合（200 件が全部 token にならないように）。"""
    if ev.get("type") == "token" and ctx.events:
        last = ctx.events[-1]
        if last.get("type") == "token" and last.get("id") == ev.get("id"):
            last["text"] = str(last.get("text") or "") + str(ev.get("text") or "")
            return
    ctx.events.append(dict(ev))


def _send(ctx: AppContext, messages: list[dict]) -> str:
    """send を書く口を 1 つにする。"""
    # ★--json は人が見ない自動の走行（課題集など）。会話の一覧に載せず、知らせも鳴らさない（convindex.BATCH_SOURCE）。
    return ctx.client.send(messages, session=ctx.session, source="batch" if ctx.json_out else "term")


def _read_line(ctx: AppContext, prompt: str) -> str:
    if ctx.ask_fn is not None:
        return ctx.ask_fn(prompt)
    return input(prompt)


# --------------------------------------------------------------------------
# 走行中のキー（Ctrl-C / Esc）
# --------------------------------------------------------------------------


def _apply_interrupts(keys, interrupted) -> bool:
    """溜まった Ctrl-C を**押された数だけ**処理する（2026-08-28・UI 監査）。

    ★旧実装は `if keys.take() and interrupted():` だった。take() は溜まった押下の
      **数**を返すのに、interrupted() は 1 回しか呼ばれない——つまり 0.25 秒の窓の中で
      2 連打しても 1 回に潰れ、「1 回目は cancel して待つ・2 回目で打ち切る」という
      契約が**実装で守られていなかった**。止めるが届かないのは、この画面の仕事
      （見張って介入する）の根幹に当たる。
    """
    for _ in range(keys.take()):
        if interrupted():
            return True
    return False

def _tty_fd(ctx: "AppContext") -> Optional[int]:
    """走行中にキーを見張る端末の fd。stdin が端末でなければ None（パイプ・試験）。"""
    try:
        return sys.stdin.fileno() if sys.stdin.isatty() else None
    except (AttributeError, ValueError, OSError):
        return None


class _KeyWatch:
    """走行中だけ端末を raw にして Ctrl-C / Esc を**キー**として読む。

    ★なぜ: 端末の Ctrl-C は SIGINT をフォアグラウンドのプロセス群**全体**に送る。子の serve は
      SIGINT で止まる（serve.main の _bye）ので、cooked のままだと「cancel を送って done を待つ」
      前に子が死ぬ（pty で実測: code=-2 → 再起動）。ISIG を切れば端末は何も送らず、
      このスレッドが 0x03 を読んで cancel の合図にする（Esc 単独も同じ・設計 §4）。
    ★走行中に打たれた他の文字は捨てる（走行中は入力を受けない・§11）。
    ★端末の状態は stop() で必ず戻す。戻し損ねると以後のシェルが壊れる。

    ★`on_first`（2026-09-04・W2）: **1 回目の Ctrl-C を読んだその場**で呼ぶ。
      旧実装は「数えるだけ」で、cancel を線に書くのは主ループ（`run_turn` の
      `events()` ループ）の仕事だった＝**中断の速さが主ループの都合に縛られていた**。
      主ループが 1 つの事象の処理で詰まっている間（描画・ask・再起動）、押した
      Ctrl-C はどこにも出ていかない。「止めたのに止まらない」の原因を後から
      切り分けるとき、この経路が主ループを経由していること自体が二択を作る。
      止めるのは見張りの仕事にする。**描画はしない**（Rich の Live は
      スレッド安全ではない。画面はこれまでどおり主ループが出す）。
    ★`reader_error`（同）: 読み手が死んだら黙って消えない。旧実装は
      `os.read` が OSError を投げると **return して終わり**——ISIG は切ったままなので
      端末も SIGINT を作らず、**その走行の Ctrl-C が完全に無効**になる（画面にも
      記録にも何も出ない）。いまは理由を残し、端末を戻して SIGINT に道を譲る。
    """

    def __init__(self, fd: Optional[int], on_first: Optional[Callable[[], None]] = None) -> None:
        self.fd = fd
        self.on_first = on_first
        self._hits = 0
        self._fired = False
        self.hit_at: Optional[float] = None    # 1 回目の Ctrl-C を**読んだ**時刻
        self.reader_error = ""                 # 見張りが死んだ理由（空なら生きている）
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._saved: Any = None

    def start(self) -> None:
        if self.fd is None or self._thread is not None:
            return
        try:
            import termios
            # ★前景でないとき（`blackwall -p … &` の背景ジョブ）は端末に触らない（2026-08-23 レビュー）:
            #   tcsetattr が SIGTTOU を起こして止まり、kill -CONT しても再び止まる。
            try:
                if os.tcgetpgrp(self.fd) != os.getpgrp():
                    self._saved = None
                    return
            except OSError:
                pass
            self._saved = termios.tcgetattr(self.fd)
            attrs = list(self._saved)
            attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)   # lflag
            attrs[6] = list(attrs[6])
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        except Exception:
            self._saved = None
            return          # raw にできない端末では従来どおり（SIGINT に任せる）
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bw-term-keys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        t = self._thread
        if t is None:
            self._restore(flush=True)   # 読み手が先に死んでいても端末は必ず戻す
            return
        self._stop.set()
        t.join(timeout=1.0)
        self._thread = None
        self._restore(flush=True)

    def _restore(self, *, flush: bool) -> None:
        """端末の状態を戻す。**何度呼んでも安全**（読み手の死と stop() の両方から呼ぶ）。"""
        with self._lock:
            saved, self._saved = self._saved, None
        if saved is None:
            return
        try:
            import termios
            if flush:
                termios.tcflush(self.fd, termios.TCIFLUSH)   # 走行中の打鍵を次の行に残さない
            termios.tcsetattr(self.fd, termios.TCSANOW, saved)
        except Exception:
            pass

    @property
    def alive(self) -> bool:
        """見張りが生きているか（False なら Ctrl-C は SIGINT 任せに戻っている）。"""
        return not self.reader_error and self._thread is not None

    def _died(self, why: str) -> None:
        """読み手が死んだ。理由を残し、**端末を戻して SIGINT に道を譲る**。

        ★戻さないと ISIG が切れたままで、端末も SIGINT を作らず、この走行の
          Ctrl-C は**どこにも届かない**（黙って効かない中断は、効かないことすら
          分からない）。戻せば Ctrl-C は SIGINT になり、`run_turn` の
          `except KeyboardInterrupt` が受ける——子の serve も一緒に SIGINT を
          受けるので子は死ぬが、`_exit` から 1 回だけ立て直す配線がある。
          **止まらないより、止まって立て直すほうがよい。**
        """
        self.reader_error = why
        self._stop.set()
        self._restore(flush=False)   # 打鍵は捨てない（押した Ctrl-C を SIGINT として活かす）

    def take(self) -> int:
        """溜まった中断の合図の数を返して 0 に戻す。"""
        with self._lock:
            n, self._hits = self._hits, 0
        return n

    def _loop(self) -> None:
        fd = self.fd
        eofs = 0
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.1)
            except (OSError, ValueError) as e:
                self._died(f"select: {type(e).__name__}: {e}")
                return
            if not r:
                continue
            try:
                data = os.read(fd, 256)
            except OSError as e:
                self._died(f"read: {type(e).__name__}: {e}")
                return
            if not data:
                # ★端末の相手（pty の master・ssh・tmux）が消えると、Linux では
                #   **select は「読める」と言い続け os.read は b"" を返し続ける**
                #   （EIO は来ない。2026-09-04 に実測）。旧実装はこれを `continue` で
                #   受けていたので、①1 コアを 100% 回しながら ②ISIG は切ったまま
                #   ③Ctrl-C は永久に届かない、という三重の黙った故障になっていた。
                #   続けて空なら相手は居ない。端末を戻して降りる。
                eofs += 1
                if eofs >= 3:
                    self._died("read: 端末の相手が消えた（EOF が続く）")
                    return
                continue
            eofs = 0
            n = data.count(b"\x03") + (1 if data == b"\x1b" else 0)
            if not n:
                continue
            with self._lock:
                self._hits += n
                first = not self._fired
                if first:
                    self._fired = True
                    self.hit_at = time.time()
            if first and self.on_first is not None:
                # ★lock の外で呼ぶ（cancel の書き込みは線への I/O で、
                #   take() を待たせる理由が無い）。ここで投げても見張りは止めない。
                try:
                    self.on_first()
                except Exception:
                    pass


# --------------------------------------------------------------------------
# 1 ターン
# --------------------------------------------------------------------------

def _ask_user(ctx: AppContext, ev: dict) -> str:
    """ask を利用者に見せて答えを返す。選択肢があれば番号で、Enter だけなら空。"""
    c = ctx.console
    q = str(ev.get("question") or "")
    opts = [str(o) for o in (ev.get("options") or [])]
    t = Text()
    t.append("? ", style=T.S_RED)
    t.append(q, style=f"bold {T.TEXT}")
    c.print(t)
    for i, o in enumerate(opts, 1):
        c.print(Text(f"  {i}. {o}", style=T.S_META))
    if opts:
        c.print(Text("  番号か自由な文（Enter だけで空）", style=T.S_DIM))
    if ctx.json_out or (ctx.ask_fn is None and not sys.stdin.isatty()):
        return ""   # 聞く相手がいない。空で返して本体に判断させる（待たせない）
    try:
        ans = _read_line(ctx, "? ")
    except (EOFError, KeyboardInterrupt):
        return ""
    ans = str(ans).strip()
    if ans.isdigit() and opts and 1 <= int(ans) <= len(opts):
        ans = opts[int(ans) - 1]
    c.print(Text(f"? {ans or '(空)'}", style=T.S_DIM))   # 入力行は消えるので、答えを 1 行残す
    return ans


CANCEL_NOTICE = "中断を要求しました（エンジンの応答を待っています。もう一度 Ctrl-C で終了）"
RUNNING_NOTE = "実行中… Ctrl-C で中断（実行中に打った文字は受け付けません）"
# ★cancel を送ってから done も来ない（心拍も鳴らない）まま何秒で「まだ止まっていない」
#   を言うか（2026-09-03）。実機で 73 秒 止まらなかった走行があり、その間クライアントは
#   何も出さず固まって見えた。心拍の既定間隔（LAPS_HEARTBEAT_SECS=15）より短く、
#   かつ cancel の通常の往復（数百ms〜数秒）よりは十分長い値。
CANCEL_STALL_SECS = 10.0


def _debug_note(ctx: "AppContext", text: str) -> None:
    """`--debug FILE` に 1 行足す（無ければ静かに何もしない）。

    ★なぜ要るか（実機・2026-09-03）: `--debug FILE` を付けても 0 バイトだった日があった。
      serve は正常系では stderr に 1 バイトも書かない（`_log` は例外パスでしか呼ばれない）ので、
      `--debug` は「子が死んだとき」以外は空になる。中断が長引いたような
      「何も壊れていないのに長い」ケースを追うには、クライアントが見ている**往復**
      （cancel を送った・受理された・done が来た）がいちばんの手がかりだが、
      それがどこにも残っていなかった。
    """
    fn = getattr(ctx.client, "debug_note", None)
    if callable(fn):
        try:
            fn(text)
        except Exception:
            pass


def _announce_running(ctx: AppContext) -> None:
    """Live（波・実行中の行）が出せない端末で、待っていることを 1 行だけ言う。

    ★`TERM=dumb` では 3.4 秒の間に 349 バイトしか出ず、画面が完全に静止していた（実測）。
      止まったのか考えているのかが区別できない。中断の方法もここでしか見えない。
    """
    if not ctx.interactive or wave_allowed(ctx.console):
        return          # 波（Live）が出るなら、そこに実行中の行が出る
    ctx.console.print(Text(f"{T.MARK_RUN} {RUNNING_NOTE}", style=T.S_META))


def _pause_live(r: Renderer) -> bool:
    """ask の間だけ Live を止める。**ターンは閉じない**（Renderer.pause の薄い殻）。"""
    try:
        return bool(r.pause())
    except Exception:
        r.end_turn()
        return False


def _resume_live(ctx: AppContext, rid: str, paused: bool) -> None:
    """ask の後に描画へ戻る。一時停止できていたら Live だけ戻し、閉じていたら新しいターンを開く。"""
    if paused:
        ctx.renderer.resume()
    else:
        ctx.renderer.start_turn(rid)


# シグナルの意味と、それに対する次の一手。★`code=-9` は「-9 という終了コード」ではない
#   （子が SIGKILL で殺された）。番号のまま出すと、読んだ人に打てる手が無い。
SIGNAL_HINTS = {
    9: ("SIGKILL・メモリ不足の可能性",
        "→ 推論サーバーのコンテキスト長（-c）か並列数を下げてください。全文は --debug FILE で取れます"),
    11: ("SIGSEGV・エンジンが異常終了", "→ --debug FILE を付けて再現させ、その中身を見てください"),
    6: ("SIGABRT・エンジンが自分で止まった", "→ 直前のツールの呼び出しと --debug FILE を見てください"),
    15: ("SIGTERM・外から止められた", "→ 誰かが止めたか、systemd の停止です。/status で疎通を見てください"),
}
EXIT_TAIL_LINES = 5


def _exit_reason(code: Any) -> tuple[str, str]:
    """(人が読める理由, 次の一手)。"""
    try:
        n = int(code)
    except (TypeError, ValueError):
        return (f"code={code}", "→ /status で疎通を確認してください")
    if n < 0:
        why, nxt = SIGNAL_HINTS.get(-n, (f"シグナル {-n}", "→ --debug FILE で全文を取ってください"))
        return (f"シグナル {-n} = {why}", nxt)
    return (f"終了コード {n}", "→ /status で疎通を確かめ、--debug FILE で全文を取ってください")


def _report_exit(ctx: AppContext, ev: dict) -> None:
    """子が死んだことを、理由・材料（stderr の末尾）・次の一手の 3 つで言う。

    ★材料は ServeClient がすでに持っている（stderr_tail）のに、走行中の死では一度も
      使っていなかった。原因の書いてある行は、たいてい死ぬ直前の stderr にある。
    """
    from .render import INDENT, TAIL
    c = ctx.console
    why, nxt = _exit_reason(ev.get("code"))
    c.print(Text(f"{T.MARK_BAD} エンジンが終了しました（{why}）", style=T.S_BAD))
    tail = ""
    try:
        fn = getattr(ctx.client, "stderr_tail", None)
        tail = str(fn() or "") if callable(fn) else ""
    except Exception:
        tail = ""
    lines = [x for x in tail.splitlines() if x.strip()][-EXIT_TAIL_LINES:]
    for i, ln in enumerate(lines):
        c.print(Text((TAIL if i == 0 else INDENT) + ln, style=T.S_DIM))
    c.print(Text("  " + nxt, style=T.S_META))


def _respawn(ctx: AppContext) -> bool:
    """子が死んだので作り直す（1 回だけ）。成功なら True。"""
    c = ctx.console
    try:
        ctx.client.restart()
    except Exception as e:
        c.print(Text(f"エンジンを再起動できません: {e}", style=T.S_RED))
        return False
    ctx.restarts += 1
    # ★「やり直してください」だけだと、同じ原因（例: メモリ不足）ならもう一度死ぬ。
    #   何が届いていないかを言い、直す手は _report_exit の「次の一手」に任せる。
    c.print(Text("エンジンを立て直しました（さきほど送った内容は届いていません）", style=T.S_META))
    return True


def _update_stats(ctx: AppContext, done: dict, summary: dict) -> None:
    st = ctx.stats
    st["turns"] = int(st.get("turns", 0)) + 1
    tc = done.get("tool_calls")
    n_tools = 0
    if isinstance(tc, dict):
        for v in tc.values():
            try:
                n_tools += int(v)
            except (TypeError, ValueError):
                pass
    else:
        n_tools = int(summary.get("tools") or 0)
    st["tools"] = int(st.get("tools", 0)) + n_tools
    try:
        secs = float(done.get("secs") or summary.get("secs") or 0.0)
    except (TypeError, ValueError):
        secs = 0.0
    st["secs"] = float(st.get("secs", 0.0)) + secs
    try:
        st["tokens"] = int(st.get("tokens", 0)) + int(done.get("total_tokens") or 0)
    except (TypeError, ValueError):
        pass


def run_turn(ctx: AppContext, text: str) -> dict:
    """利用者の 1 発言を送り、done（または子の死亡・2 回目の Ctrl-C）まで事象を描く。
    戻り値は {"outcome": str, "rid": str, "done": dict|None}。"""
    r = ctx.renderer
    c = ctx.console
    ctx.last_input = text
    r.user(text)
    messages = [{"role": "user", "content": text}]
    # ★子が居ないなら先に立て直す。/status は「停止（次の send で再起動）」と言っているのに、
    #   実際は「送れません: 子プロセスが動いていない」で終わっていた（kill -9 で実測）。
    #   _exit の「再起動は 1 回だけ」は走行中の連鎖死を止めるための上限で、利用者が打った
    #   send ごとに 1 回立て直すのは別（利用者の操作で区切られる）。
    if getattr(ctx.client, "alive", True) is False:
        c.print(Text("エンジンが止まっているので再起動します", style=T.S_META))
        try:
            ctx.client.restart()
        except Exception as e:
            c.print(Text(f"エンジンを再起動できません: {e}", style=T.S_RED))
            ctx.last_outcome = "send_failed"
            return {"outcome": "send_failed", "rid": "", "done": None}
    try:
        rid = _send(ctx, messages)
    except Exception as e:
        c.print(Text(f"送れません: {e}", style=T.S_RED))
        ctx.last_outcome = "send_failed"
        return {"outcome": "send_failed", "rid": "", "done": None}
    ctx.running_rid = rid
    # ★波は start_turn より**先に**渡す。start_turn が Live を作るとき、その Live の
    #   刷新回数は波の period から決まる（render._refresh_rate）。後から渡すと、
    #   すでに走っている Live は前のターンの値のままになる（＝`--calm` が効かない）。
    if ctx.wave_factory is not None:
        r.set_wave(ctx.wave_factory())
    r.start_turn(rid)
    _announce_running(ctx)

    interrupts = 0
    done: dict | None = None
    outcome = "unknown"
    # ★中断の壁時計（2026-09-03）。cancel を送った時刻を覚え、done も来ないまま
    #   CANCEL_STALL_SECS 秒経ったら「まだ止まっていない」を 1 回だけ言う（下のループ）。
    cancel_t0: float | None = None
    stall_warned = False
    # ★cancel の書き手は 2 つ（見張りスレッドと主ループ）。どちらから来ても
    #   **線に書くのは 1 回だけ**にする（2026-09-04・W2）。
    cancel_lock = threading.Lock()
    cancel_sent = False
    cancel_error = ""

    def _write_cancel() -> bool:
        """cancel を線に一度だけ書く。既に書いてあれば True のまま何もしない。"""
        nonlocal cancel_sent, cancel_error, cancel_t0
        with cancel_lock:
            if cancel_sent:
                return True
            try:
                ctx.client.cancel(rid)
            except Exception as e:
                cancel_error = f"{type(e).__name__}: {e}"
                return False
            cancel_sent = True
            cancel_t0 = time.time()
            return True

    def _cancel_from_key() -> None:
        """1 回目の Ctrl-C を**読んだその場**（見張りスレッド）で cancel を書く。

        ★これが W2 の要点: 中断の速さを主ループから切り離す。画面には触らない
          （Rich の Live はスレッド安全ではない）——notice は主ループが出す。
        ★記録に「読んだ時刻 → 線に書き終えた時刻」を残す。この 2 つが無かったので、
          73 秒止まらなかった走行が「端末が遅れた」のか「本体が遅れた」のか
          決められなかった（W1 も二択で止まった）。次からは 1 行で決まる。
        """
        t0 = time.time()
        ok = _write_cancel()
        _debug_note(ctx, f"Ctrl-C を読んだ→cancel {'送信' if ok else '送信できず'} "
                         f"rid={rid} {(time.time() - t0) * 1000:.1f} ms"
                         + ("" if ok else f": {cancel_error}"))

    keys = (ctx.keys_factory(_cancel_from_key) if ctx.keys_factory is not None
            else _KeyWatch(_tty_fd(ctx), on_first=_cancel_from_key))
    key_death_noted = False

    def interrupted() -> bool:
        """Ctrl-C（キーでも SIGINT でも）。1 回目は cancel して待つ。True なら打ち切る。"""
        nonlocal interrupts, outcome
        interrupts += 1
        if interrupts == 1:
            already = cancel_sent      # 見張りが先に書いていたか（普通はこちら）
            if not _write_cancel():
                r.end_turn()
                c.print(Text(f"cancel を送れません: {cancel_error}", style=T.S_RED))
                outcome = "cancelled"
                _debug_note(ctx, f"cancel 送信できず rid={rid}: {cancel_error}")
                return True
            if already:
                lag = (time.time() - cancel_t0) if cancel_t0 else 0.0
                _debug_note(ctx, f"cancel は見張りが送信済み rid={rid}"
                                 f"（主ループが気づくまで {lag * 1000:.0f} ms）")
            else:
                _debug_note(ctx, f"cancel 送信 rid={rid}")
            # Live の上に流すので renderer 経由（直接 print すると波の残骸が残る）
            r.on_event({"type": "notice", "kind": "cancel", "message": CANCEL_NOTICE})
            return False
        r.end_turn()
        ctx.exit_requested = True
        outcome = "cancelled"
        _debug_note(ctx, f"2 回目の Ctrl-C rid={rid}（強制終了へ）")
        # quit は走行中の子を最大 5 秒待ってから SIGTERM する（実測 ~10 秒）。黙っていると固まって見える。
        c.print(Text("終了します（エンジンを止めています…）", style=T.S_META))
        return True

    keys.start()
    try:
        while True:
            stop = False
            try:
                for ev in ctx.client.events(timeout=0.25):
                    if _apply_interrupts(keys, interrupted):
                        stop = True
                        break
                    if keys.reader_error and not key_death_noted:
                        # ★黙って効かない中断を作らない（2026-09-04・W2）。見張りが死ぬと
                        #   Ctrl-C は SIGINT に戻る（_KeyWatch._died が端末を戻す）が、
                        #   それが起きたこと自体はどこにも出ていなかった。
                        key_death_noted = True
                        _debug_note(ctx, f"キーの見張りが止まった: {keys.reader_error}"
                                         "（以後 Ctrl-C は SIGINT として届く）")
                    if (cancel_t0 is not None and not stall_warned
                            and time.time() - cancel_t0 >= CANCEL_STALL_SECS):
                        # ★心拍（serve notice kind=heartbeat）に頼らない: request_cancel が
                        #   飛んでいる HTTP を閉じた後、次の反復境界に届くまでの区間は
                        #   心拍の対象（走行中）の外側で起き得る。壁時計だけで判定する。
                        stall_warned = True
                        elapsed = time.time() - cancel_t0
                        r.cancel_stall(elapsed)
                        _debug_note(ctx, f"中断が {elapsed:.0f} 秒止まっていない rid={rid}")
                    if ev is None:
                        continue            # 波は Live が勝手に回す
                    et = ev.get("type")
                    if (et == "notice" and str(ev.get("kind") or "") in ("cancel", "cancelled")
                            and cancel_t0 is not None):
                        _debug_note(ctx, f"cancel の notice を受信 kind={ev.get('kind')} "
                                        f"applied={ev.get('applied')} rid={rid}")
                    if et == "_exit":
                        record_event(ctx, ev)
                        r.end_turn()        # Live を止めてから言う（Live 中の print は画面を崩す）
                        _report_exit(ctx, ev)
                        outcome = "crashed"
                        if ctx.restarts == 0:
                            _respawn(ctx)
                        else:
                            c.print(Text("  → 再起動は 1 回だけです。/exit して立ち上げ直してください",
                                         style=T.S_META))
                        stop = True
                        break
                    record_event(ctx, ev)
                    if et == "ask":
                        keys.stop()         # 端末を戻してから聞く（prompt_toolkit が自分で raw にする）
                        paused = _pause_live(r)   # Live を止めてから聞く（ターンは閉じない）
                        ans = _ask_user(ctx, ev)
                        try:
                            ctx.client.answer(str(ev.get("ask") or ""), ans)
                        except Exception as e:
                            c.print(Text(f"answer を送れません: {e}", style=T.S_RED))
                        _resume_live(ctx, rid, paused)
                        keys.start()
                        continue
                    r.on_event(ev)
                    if et == "done":
                        done = dict(ev)
                        sid = done.get("session")
                        if sid:
                            ctx.session = str(sid)
                        summary = r.end_turn()
                        _update_stats(ctx, done, summary)
                        outcome = str(done.get("outcome") or "unknown")
                        if cancel_t0 is not None:
                            _debug_note(ctx, f"done 受信 rid={rid} outcome={outcome} "
                                            f"cancel から {time.time() - cancel_t0:.1f} 秒")
                        stop = True
                        break
                else:
                    # events() が None を出して戻った（timeout）。描き直しは Live がするので、また待つ。
                    continue
            except KeyboardInterrupt:
                stop = interrupted()
            if stop:
                break
    finally:
        keys.stop()
        ctx.running_rid = None
        # ★どの抜け方でも Live を畳む（2026-09-02）。旧: 描画の途中で例外が出ると
        #   `run_turn` はそのまま抜け、Live が走ったまま＝**カーソルを隠したまま**
        #   プロセスが終わっていた（Rich の Live は stop() でしか戻さず atexit も無い）。
        #   正常終了時は end_turn 済みなので空振りする。
        r.close()
    ctx.last_outcome = outcome
    return {"outcome": outcome, "rid": rid, "done": done}


# --------------------------------------------------------------------------
# 非対話（-p）
# --------------------------------------------------------------------------

def _run_once_json(ctx: AppContext, text: str) -> int:
    """--json: 事象をそのまま 1 行ずつ stdout。描画はしない。"""
    rid = _send(ctx, [{"role": "user", "content": text}])
    ctx.running_rid = rid
    # 端末から起動したときの Ctrl-C を子に届けない（上の _KeyWatch）。
    # ★`on_first` で線に直接書く（run_turn と同じ理由）。ここの主ループは
    #   `out.write` + flush を持っており、読み手が遅いパイプでは詰まりうる。
    def _cancel_now() -> None:
        try:
            ctx.client.cancel(rid)
        except Exception:
            pass
    keys = _KeyWatch(_tty_fd(ctx), on_first=_cancel_now)
    keys.start()
    try:
        return _json_loop(ctx, rid, keys)
    finally:
        keys.stop()
        ctx.running_rid = None


def _json_loop(ctx: AppContext, rid: str, keys: "_KeyWatch") -> int:
    out = sys.stdout
    interrupts = 0
    outcome = "unknown"

    def interrupted() -> bool:
        nonlocal interrupts
        interrupts += 1
        if interrupts >= 2:
            return True
        try:
            ctx.client.cancel(rid)
        except Exception:
            return True
        return False

    while True:
        stop = False
        try:
            for ev in ctx.client.events(timeout=0.25):
                if _apply_interrupts(keys, interrupted):
                    return 130
                if ev is None:
                    continue
                out.write(json.dumps(ev, ensure_ascii=False) + "\n")
                out.flush()
                et = ev.get("type")
                if et == "ask":
                    ctx.client.answer(str(ev.get("ask") or ""), "")
                elif et == "done":
                    outcome = str(ev.get("outcome") or "unknown")
                    stop = True
                    break
                elif et == "_exit":
                    outcome = "crashed"
                    stop = True
                    break
            else:
                continue        # timeout（None）で戻った。また待つ
        except KeyboardInterrupt:
            if interrupted():
                return 130
        if stop:
            break
    return exit_code(outcome)


# --------------------------------------------------------------------------
# 対話
# --------------------------------------------------------------------------

GAP = "  "          # 欄の区切りは空白 2 桁。★`·`(U+00B7) は East Asian Ambiguous ＝
                    #   端末によって 1 桁にも 2 桁にもなる。銘は 86 桁で 2 行に割れていた。


def _tail_path(p: str, keep: int = 2) -> str:
    """`~/…/末尾 keep 階層`。keep<0 なら最後の 1 つだけ（前置きも落とす）。
    銘が幅に収まらないときの最後の削り方（全体は /status で見られる）。"""
    short = _short_path(p)
    parts = [x for x in short.split(os.sep) if x and x != "~"]
    if keep < 0:
        return parts[-1] if parts else short
    if len(parts) <= keep:
        return short
    head = "~" if short.startswith("~") else ""
    return head + "/…/" + "/".join(parts[-keep:])


def _short_host(h: str) -> str:
    """`192.0.2.160:8741` → 推論機:8741`（IPv4 のときだけ）。届く先が同じ網なら末尾で足りる。"""
    host, _, port = str(h).partition(":")
    q = host.split(".")
    if len(q) == 4 and all(x.isdigit() for x in q):
        return "." + q[3] + (":" + port if port else "")
    return h


def _ellipsis(s: str, n: int) -> str:
    """表示幅 n 桁に収める（切ったことが分かるように `…` を残す）。"""
    if n <= 0:
        return ""
    if slash.disp_width(s) <= n:
        return s
    out = ""
    for ch in s:
        if slash.disp_width(out + ch) > n - 1:
            break
        out += ch
    return out + "…"


def _header_text(ctx: AppContext, *, ws_keep: int = 0, short_host: bool = False,
                 version: bool = True, show_help: bool = True, show_engine: bool = True,
                 model_max: int = 0, ws_max: int = 0) -> Text:
    """銘の 1 行を作る。ws_keep=0 は作業場をそのまま、1/2 は末尾だけ。
    model_max / ws_max は 0 で無制限（最後の手段として字数で切る）。"""
    s = ctx.client.settings
    pol, eth = str(s.get("policy") or ""), str(s.get("ethics") or "")
    danger = pol == "trusted" or eth == "raw"
    ws = str(s.get("workspace") or "")
    host = _host_of(str(s.get("backend") or ""))
    if privacy_on():
        ws, host = mask_private(ws), "<host>"
    t = Text()
    if card_on():
        # カード表示では上に大きな銘がある＝名前を重ねず、印だけ置く。
        t.append("▐", style=T.S_RED)
    else:
        t.append("L.A.P.S", style=T.S_RED)
    if version:
        t.append(f" v{__version__}", style=T.S_META)
    if show_engine:
        model = str(s.get("model") or "")
        if model_max:
            model = _ellipsis(model, model_max)
        t.append(GAP + f"{model}@{_short_host(host) if short_host else host}", style=T.S_META)
    ws_text = _short_path(ws) if ws_keep == 0 else _tail_path(ws, ws_keep)
    if ws_max:
        ws_text = _ellipsis(ws_text, ws_max)
    t.append(GAP + ws_text, style=T.S_META)
    # ★実行ゲートと倫理の刻みは「いま何が起きうるか」。危険な組み合わせだけ赤にする。
    if pol or eth:
        t.append(GAP + f"{pol}/{eth}", style=T.S_RED if danger else T.S_META)
    if show_help:
        t.append(GAP + "/help", style=T.S_DIM)
    return t


# 幅に収まらないときに削る順（左ほど軽い犠牲）。★policy/ethics は表に無い
#   ＝どれだけ狭くても落とさない（安全に関わる 2 つ）。
HEADER_TRIES = (
    {},
    {"ws_keep": 2},
    {"ws_keep": 2, "short_host": True},
    {"ws_keep": 2, "short_host": True, "version": False},
    {"ws_keep": 1, "short_host": True, "version": False},
    {"ws_keep": -1, "short_host": True, "version": False},
    {"ws_keep": -1, "short_host": True, "version": False, "show_help": False},
    # 最後の砦: 送り先も落とす。残すのは 銘・作業場・policy/ethics だけ。
    {"ws_keep": -1, "short_host": True, "version": False, "show_help": False,
     "show_engine": False},
)


def header_line(ctx: AppContext) -> Text:
    """銘の 1 行。幅に収まる形を**後ろから削って**選ぶ（折り返させない）。

    ★2 行に割れると、どこまでが銘でどこからが会話か分からなくなる（実測: 幅 80 で
      86 桁＝常に 2 行だった）。幅は `slash.disp_width`（曖昧幅を 2 と数える安全側）。
    """
    width = max(20, int(getattr(ctx.console, "width", 80) or 80))
    last = dict(HEADER_TRIES[-1])
    for kw in HEADER_TRIES:
        t = _header_text(ctx, **kw)
        if slash.disp_width(t.plain) <= width:
            return t
    # ★どれも収まらない（モデル名や作業場の名前がとても長い）。**削る表に無い 2 つ**
    #   （policy/ethics）を守るため、自由文の 2 つを 1 桁ずつ削る。
    s = ctx.client.settings
    m = 1 if not last.get("show_engine", True) else (slash.disp_width(str(s.get("model") or "")) or 1)
    w = slash.disp_width(_tail_path(str(s.get("workspace") or ""), -1)) or 1
    while slash.disp_width(t.plain) > width and (m > 1 or w > 1):
        if m >= w:
            m -= 1
        else:
            w -= 1
        t = _header_text(ctx, model_max=m, ws_max=w, **last)
    return t


def _history_path(ctx: AppContext) -> Path:
    return Path(ctx.install_id_path).parent / "history"


def is_first_run(ctx: AppContext) -> bool:
    """初めての起動か。★履歴ファイル（prompt_toolkit が 1 行目を打った時に作る）で見る。
    install_id は /sendlog が作るので「初回」の印にならない。"""
    try:
        return _history_path(ctx).stat().st_size == 0
    except OSError:
        return True


#: 起動時の確認に使ってよい時間。★待たせない。LAN なら数 ms で返る。届かないなら 1 秒で諦めて言う。
STARTUP_PROBE_TIMEOUT = 1.0


def _startup_probe(ctx: AppContext) -> None:
    """起動した時点で分かることは、**送る前に**言う（対話のときだけ・1 秒で打ち切り）。

    ★旧: 送り先に届かないことも、モデル名が食い違っていることも、**最初の依頼を送って待ったあと**に
      初めて分かった。どちらも起動時に 1 回聞けば分かる。何も問題が無ければ**何も出さない**
      （毎回出る行は読まれなくなる）。
    """
    if os.environ.get("LAPS_NO_STARTUP_PROBE") == "1":
        return
    try:
        s = slash._settings(ctx)
        backend = str(s.get("backend") or "")
        if not backend:
            return
        pr = slash.probe_backend(backend, timeout=STARTUP_PROBE_TIMEOUT)
        if not pr["ok"]:
            ctx.console.print(Text(
                f"{T.MARK_BAD} 送り先に接続できません: {pr['error'] or pr['health'] or '?'}", style=T.S_BAD))
            ctx.console.print(Text("  → /backend <URL> で切替、/status で確かめ直せます。", style=T.S_META))
            return
        bad = slash.model_mismatch(str(s.get("model") or ""),
                                   slash.served_models(backend, timeout=STARTUP_PROBE_TIMEOUT))
        if bad:
            # ★続き行にも段を付ける。Rich に任せると 2 行目が桁 0 から始まり、見出しの続きに見える。
            from .render import wrap_with_gutter
            ctx.console.print(Text(wrap_with_gutter(bad, ctx.console.width, "  ", "  "), style=T.S_META))
    except Exception:
        return          # ★確認は助言。ここで落ちて対話を始められなくしない。


def _header(ctx: AppContext) -> None:
    if card_on():
        # カード表示（録画・見せる用）だけ起動の銘を大きく出す。既定の表示は 1 行のまま。
        from .banner import banner
        ctx.console.print(banner(ctx.console.width))
        ctx.console.print()
    ctx.console.print(header_line(ctx))
    if config.migrated:
        # ★設定を書き換えたことは**必ず出す**（2026-08-29）。黙って権限を広げない。
        #   起きるのは「昔の既定のまま一度も触っていない」ときだけで、1 度きり。
        ctx.console.print(Text(
            "  設定を今の既定に合わせました: " + " / ".join(config.migrated)
            + "（戻すなら /policy と /ethics）", style=T.S_META))
    _startup_probe(ctx)
    if is_first_run(ctx):
        # ★初回だけ。毎回出る行は 2 回目から読まれなくなり、場所だけ取る。
        # ★色は S_META（注記の S_DIM ではない）: 初めての人が**読む本文**で、S_DIM は
        #   Solarized dark で 4.37:1 ＝本文には使わないと目盛りが決めている（theme.py）。
        ctx.console.print(Text(
            "  → やりたいことを、そのまま書いてください。コマンドの一覧は /help。",
            style=T.S_META))


def _make_prompt_session(ctx: AppContext):
    """prompt_toolkit の PromptSession（履歴・補完・Alt+Enter で改行）。"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory, InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _newline(event) -> None:   # Alt+Enter = 改行（Enter は送信のまま）
        event.current_buffer.insert_text("\n")

    # ★履歴は設定ファイルと同じフォルダ（--config を渡したときも一緒に動く）。
    #   ここが `config.config_path()` 固定だと、--config で別の場所を指した人の履歴だけ
    #   既定のフォルダに残り、「初回か」の判定（is_first_run）とも食い違う。
    hist_path = _history_path(ctx)
    try:
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        history: Any = FileHistory(str(hist_path))
    except OSError:
        history = InMemoryHistory()
    return PromptSession(
        message=FormattedText([(f"fg:{T.TEXT_2}", PROMPT)]),
        # ★空のときだけ出る案内。**行を増やさない**（常設の説明行は 2 回目から読まれない）。
        #   何も無い画面に `›` だけ、だと次の一手が分からない。打ち始めれば消える。
        placeholder=FormattedText([(f"fg:{T.NOTE}", PLACEHOLDER)]),
        history=history,
        completer=slash.SlashCompleter(ctx),
        complete_while_typing=True,
        # ★補完は別スレッドで。既定（False）だと `/resume ` の後、1 打鍵ごとに記録の
        #   走査（この機体で 50ms）が入力を止めていた（実測）。記録は消さない運用なので、
        #   件数が増えるほど悪化する。
        complete_in_thread=True,
        key_bindings=kb,
        # ★入力行は受け取ったら消す。残すと renderer.user の `> 本文` と二重に並ぶ（実機で確認）。
        #   記録として残るのは `> 本文` の 1 行（Claude Code と同じ）。ask の答えも同様に消えるので
        #   _ask_user が `? 答え` を自分で出す。
        erase_when_done=True,
    )


def _report_internal_error(ctx: AppContext, e: BaseException) -> None:
    """描画や送受信の途中で落ちたとき、**会話を殺さずに**理由を出す（2026-09-02）。

    ★旧: 事象 1 つで例外が出ると REPL ごと終わり、それまでの会話に戻る手段は
      `/resume <id>` を覚えていることだけだった。しかも Live が走ったままなので、
      端末はカーソルが消えた状態で戻る。
    ★握り潰さない: 型と本文を必ず画面に出し、全文は `--debug` のファイルへ落とす
      （出せる場所が無いときは、その取り方を言う）。
    """
    ctx.renderer.close()
    ctx.console.print(Text(f"{T.MARK_BAD} 内部エラー: {type(e).__name__}: {e}", style=T.S_BAD))
    log = getattr(ctx.client, "debug_log", None)
    _write_debug(log, traceback.format_exc())
    ctx.console.print(Text(
        "  → 会話は続けられます（この 1 往復だけ失われました）。"
        + (f"全文は {log} に書きました。" if log
           else "全文が要るときは --debug FILE を付けて再現させてください。"),
        style=T.S_META))


def _repl(ctx: AppContext) -> int:
    c = ctx.console
    ctx.interactive = True
    _header(ctx)
    reader: Callable[[str], str]
    if sys.stdin.isatty():
        session = _make_prompt_session(ctx)
        main_msg = session.message

        def reader(prompt: str) -> str:
            # ★prompt() に渡した message は PromptSession に**残る**（実装を確認）。
            #   ask の "? " を出した後に既定へ戻るよう、毎回明示して渡す。
            # ★案内文は**主の入力欄だけ**。ask の問い（"? "）に「やりたいことを書く」と出ると、
            #   問われているのに別のことを促す形になる。
            if prompt == PROMPT:
                return session.prompt(main_msg)
            return session.prompt(prompt, placeholder=None)
    else:
        # パイプ越し（試験・スクリプト）。prompt_toolkit は端末が無いと使えない。
        def reader(prompt: str) -> str:
            line = sys.stdin.readline()
            if not line:
                raise EOFError
            return line.rstrip("\n")
    ctx.ask_fn = reader

    while not ctx.exit_requested:
        try:
            line = reader(PROMPT)
        except EOFError:
            break
        except KeyboardInterrupt:
            c.print(Text("(終了は /exit か Ctrl-D)", style=T.S_DIM))
            continue
        text = line.strip()
        if not text:
            continue
        try:
            if text.startswith("/"):
                slash.dispatch(ctx, text)
                continue
            run_turn(ctx, text)
        except KeyboardInterrupt:
            # 走行外の Ctrl-C（dispatch 中など）。REPL は続ける。
            c.print(Text("(終了は /exit か Ctrl-D)", style=T.S_DIM))
        except Exception as e:
            _report_internal_error(ctx, e)
    return 0


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def default_debug_path(given: str | None) -> str | None:
    """`--debug` の既定を **on** にする（2026-09-03・W8 の監査）。

    ★なぜ: 中断が 73 秒止まらなかった実機走行で、cancel の往復を残す `debug_note` は
      実装済みだったのに `--debug` が未指定＝**診断は「無かったこと」と同じ**だった。
      次の 1 回から自動で残す。`LAPS_TERM_DEBUG=off` で止められる。空文字も off。
    既定の置き場: `~/.blackwall/term-debug.log`（client 側が親ディレクトリを作る）。"""
    if given is not None:
        return given or None
    if os.environ.get("LAPS_TERM_DEBUG", "").strip().lower() in ("off", "0", "no"):
        return None
    return str(_lp.dot_dir() / "term-debug.log")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="laps",
        description="L.A.P.S 端末クライアント（既定の推論先は http://127.0.0.1:8080/v1）。`/help` でコマンド一覧。")
    p.add_argument("-p", "--prompt", metavar="TEXT", help="一発実行（非対話）。終了コードは outcome")
    p.add_argument("--prompt-file", metavar="PATH",
                   help="一発実行の本文をファイルから読む（-p の代わり）。"
                        "★長い課題文はこちらを使う: argv に本文を載せると、課題文の中の"
                        "コマンド例（php -S ... 等）がエージェント自身の pkill -f に"
                        "マッチして親ごと撃たれる事故が実際に起きた（2026-08-26 run27d）")
    p.add_argument("--json", action="store_true", help="-p のとき事象をそのまま JSONL で stdout へ")
    p.add_argument("--backend", metavar="URL", help="推論先（OpenAI 互換 /v1）")
    p.add_argument("--model", metavar="NAME", help="モデル名")
    p.add_argument("--workspace", metavar="DIR", help="ワークスペース（既定 cwd）")
    p.add_argument("--policy", choices=slash.POLICIES, help="実行ゲート")
    p.add_argument("--ethics", choices=slash.ETHICS, help="倫理の刻み")
    p.add_argument("--resume", metavar="ID", help="セッション id で続きから")
    p.add_argument("--calm", action="store_true",
                   help="波を控えめに（刷新 8.3 回/s・振れ幅半分）。"
                        "止めるなら LAPS_WAVE=off か設定の wave=off")
    # ★2026-10-06 録画のための切り替え（オーナー「TUI を美麗に作り直して録画に使えないか」）。既定は今までどおり。
    p.add_argument("--card", action="store_true",
                   help="カード表示（ツールを枠つきのカード・思考を縦線の欄・依頼を ❯）")
    p.add_argument("--privacy", action="store_true",
                   help="伏せ表示（IP・ホームのパス・ユーザー名・ホスト名を伏せ字に）")
    p.add_argument("--record", action="store_true",
                   help="録画向け（カード表示＋波を止め、状態行は 10 秒に 1 回だけ描き直す）")
    p.add_argument("--demo", action="store_true", help="--record --privacy をまとめて（録画用）")
    p.add_argument("--ui", choices=config.UI_MODES,
                   help="画面: tui（btop 風の全画面・既定）| classic（従来の行の形）。設定の ui でも選べる")
    p.add_argument("--debug", metavar="FILE", help="エンジンの stderr とトレースをこのファイルへ")
    p.add_argument("--config", metavar="PATH", help="設定ファイル（既定 " + str(config.config_path()).replace("%", "%%") + "）")
    p.add_argument("--version", action="store_true", help="版を出して終わる")
    return p


def _write_debug(path: str | None, text: str) -> None:
    if not path:
        return
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        pass


def main(argv: Optional[Iterable[str]] = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] in ("login", "register", "logout", "whoami", "approve"):   # アカウント（2026-10-31・relayauth.py）
        from .. import relayauth
        return relayauth.main(raw)
    args = build_parser().parse_args(raw)
    if args.version:
        # ★形は「名前 版」の 2 語のまま（scripts/check_term_smoke.sh と tests/test_term_app.py が読む）。2026-11-14 正式名称へ。
        print(f"L.A.P.S {__version__}")
        return 0

    cfg_path = Path(args.config).expanduser() if args.config else config.config_path()
    try:
        config.ensure_file(cfg_path)      # 初回に既定（P40）で作る
    except OSError:
        pass                              # 書けなくても動く（既定で）
    cfg = config.resolve(args, path=cfg_path)
    # ★描画（render.py）は環境変数で読む＝子プロセスや再描画の経路でも同じ判断になる。
    if args.card:
        _envcompat.setenv("LAPS_TUI_STYLE", "card")
    if args.record or args.demo:
        _envcompat.setenv("LAPS_RECORD", "1")
    if args.privacy or args.demo:
        _envcompat.setenv("LAPS_PRIVACY", "1")
    cfg["calm"] = bool(args.calm or calm_from_env())
    workspace = str(Path(args.workspace).expanduser().resolve()) if args.workspace else os.getcwd()
    if getattr(args, "prompt_file", None):
        # ★-p と両方あれば file が勝つ（長い方を argv から遠ざける趣旨に合わせる）
        try:
            args.prompt = Path(args.prompt_file).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            print(f"--prompt-file を読めません: {e}", file=sys.stderr)
            return 2
    json_out = bool(args.json and args.prompt)

    # --json では stdout が事象の出口なので、人向けの文字は stderr へ
    console = Console(highlight=False, stderr=json_out)
    if args.resume:
        from ..session import is_valid_session_id
        if not is_valid_session_id(args.resume):
            console.print(Text(f"--resume の id が不正です: {args.resume!r}", style=T.S_RED))
            return 2
    # ★`--config` を渡す。渡さないと本文の読み幅（prose_width）だけが既定の設定ファイルを
    #   読み、`--config` の人には「設定したのに効かない」になる（render._prose_limit）。
    renderer = Renderer(console, config_path=cfg_path)
    client = ServeClient(
        workspace=workspace, backend=str(cfg["backend"]), model=str(cfg["model"]),
        policy=str(cfg["policy"]), ethics=str(cfg["ethics"]), tools=str(cfg["tools"]),
        max_iters=int(cfg["max_iters"]), debug_log=default_debug_path(args.debug),
    )
    ctx = AppContext(console=console, client=client, renderer=renderer, cfg=cfg,
                     session=args.resume or None, json_out=json_out,
                     install_id_path=Path(cfg_path).parent / "install_id", config_path=Path(cfg_path))
    # 波は TTY のときだけ（--json は stdout が事象の出口なので描かない）
    ctx.wave_factory = (None if json_out or record_on()
                        else make_wave_factory(console, cfg["calm"]))

    code = 1
    # ★全画面（2026-10-12 オーナー「TUI作りこんでほしいです。Btopみたいに」・term/tui.py）。対話のときだけ。
    #   -p・パイプ・TERM=dumb・録画（--card/--record/--demo）は行の形のまま。Textual が無い・壊れているときも
    #   行の形へ落とす（黙って落とさず 1 行言う）。
    use_tui = False
    if not args.prompt and str(cfg.get("ui") or "tui") == "tui" and not (args.card or args.record or args.demo):
        try:
            from . import tui as _tui
            why = _tui.usable()
            use_tui = not why
        except Exception as e:                          # Textual が入っていない等
            console.print(Text(f"全画面を使えないので行の形で開きます（{type(e).__name__}: {e}）", style=T.S_META))
    if use_tui:
        try:
            code = _tui.run(ctx, cfg=cfg, config_path=cfg_path, resume=args.resume or None)
        except Exception as e:
            console.print(Text(f"{type(e).__name__}: {e}", style=T.S_RED))
            _write_debug(args.debug, traceback.format_exc())
            code = 1
        finally:
            console.print(Text("エンジンを止めています…", style=T.S_META))
            try:
                client.quit()
            except Exception:
                pass
        return code
    try:
        if not json_out:                              # 機械向けの --json では部品を取りに行かない
            client.prepare_parts()
        client.start()
        if args.prompt:
            if json_out:
                code = _run_once_json(ctx, args.prompt)
            else:
                res = run_turn(ctx, args.prompt)
                code = exit_code(res["outcome"])
        else:
            code = _repl(ctx)
    except KeyboardInterrupt:
        code = 130
    except Exception as e:
        console.print(Text(f"{type(e).__name__}: {e}", style=T.S_RED))
        _write_debug(args.debug, traceback.format_exc())
        code = 1
    finally:
        # ★Live を畳んでからでないと、この後の後始末の出力が Live の絵と混ざり、
        #   そもそもカーソルが隠れたまま戻る（Renderer.close の申し送り）。
        renderer.close()
        try:
            client.quit()
        except Exception:
            pass
    return code


if __name__ == "__main__":
    sys.exit(main())
