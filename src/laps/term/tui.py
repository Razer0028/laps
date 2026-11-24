"""btop 風の全画面表示（2026-10-12 オーナー「TUI作りこんでほしいです。Btopみたいに」）。

★描画の中身は行の形（render.Renderer）と同じ物を使う。道具の行・差分・思考・締めの行は Renderer がそのまま
  作り、ここはそれを「会話」の枠へ流すだけ（Console の出口を枠に向ける＝_PaneFile）。作り直さない。
★走らせ方も同じ（app.run_turn をそのまま別スレッドで呼ぶ）。違うのは 2 つだけ:
  ①キーは端末を Textual が読むので、Ctrl-C は画面から数えて渡す（AppContext.keys_factory）
  ②ask（AskUserQuestion）は小窓で聞く（AppContext.ask_fn）
★画面は btop と同じく箱を並べる:
    ┌ 頭の帯（版・推論先・モデル・作業場・時計）
    ├ ²server（推論機の GPU・CPU・メモリ・温度・電力 = 推論機 の bw-metrics :8742 と モデル切り替えの口 :8743）
    ├ ³agent （状態・生成の速さ = 推論機の /slots・文脈の目盛り・反復・道具・要約）
    ├ ¹会話  （Renderer の出力＋生成中の本文・思考・実行中の道具・波）│ ⁴tools（道具の一覧・Enter で中身）
    ├ 依頼の欄（Enter 送信・走行中は割り込み・^T 強制割り込み）
    └ 足の帯（キーの案内）
★推論機の数字は推論機に聞く（/slots の n_decoded の差＝本当の生成の速さ・n_prompt_tokens＝本当の文脈）。
  画面で文字数から見積もらない（本文は 400 字ずつまとめて届くので事象の数は語の数ではない）。
"""
from __future__ import annotations

from laps import _envcompat
import collections
import io
import json
import os
import re
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from rich.console import Console, ConsoleOptions, RenderResult
from rich.segment import Segment
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import DataTable, Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from .. import __version__
from . import app as appmod
from . import models
from . import slash
from . import theme as T
from .render import (Renderer, ToolEntry, _md, _prose_limit, _status_target, fmt_secs, mask_private,
                     privacy_on, read_kind, wrap_think, wrap_with_gutter)

# --------------------------------------------------------------------------
# 色（theme の赤 1 本の約束はそのまま。箱の線と目盛りの段階だけ足す）
# --------------------------------------------------------------------------

BG = "#0d0a0a"
BOX = "#8f2a22"            # 箱の線（暗い赤・#4a1612 では端末によって線が見えなかった）
BOX_FOCUS = "#d0463c"      # 選んでいる箱の線
BASE = "#3a2422"           # グラフの底の線（値が 0 でも箱の中に面があると分かるように）
EMPTY = "#2b1d1c"          # 目盛りの空き
RED = ("#4a1612", "#6E1F19", "#8f2a22", T.WALL, "#d0463c", T.ALERT, "#ff7a6e", "#ffa39a")
WHITE = ("#3a3030", "#5a4d4c", "#7a6c6b", T.NOTE, T.META, "#c4bab9", T.TEXT, "#ffffff")
SEP = "・"                 # 区切り（幅 2 が決まっている字。· は幅が曖昧で、日本語の端末では 2 桁に描かれることがある）

POLL_SECS = 2.0            # bw-metrics（推論機が 2 秒ごとに測っている）
SLOTS_SECS = 1.0           # /slots（生成の速さ・文脈）
MODELCTL_SECS = 15.0       # モデル切り替えの口（載っているモデル・電力の上限・周波数）
HIST = 600                 # 履歴の長さ（点の数。幅 2 倍ぶんあれば足りる）
TOOLS_KEEP = 400           # 道具の一覧に残す数
PLACEHOLDER = "やりたいことを書く（/ でコマンド・F1 でヘルプ）"


def _pal(palette: tuple[str, ...], frac: float) -> str:
    frac = 0.0 if frac < 0 else 1.0 if frac > 1 else frac
    return palette[min(len(palette) - 1, int(frac * (len(palette) - 1) + 0.5))]


#: 点字 1 セル = 横 2 × 縦 4 ドット。[行(上から)][列(左/右)]
_DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def braille_area(values: list[float], width: int, height: int, vmax: float,
                 palette: tuple[str, ...]) -> list[Text]:
    """btop の点字の面グラフ。新しい値が右端。色は高さ（下が暗い・上が明るい）。"""
    width, height = max(1, width), max(1, height)
    n = width * 2
    vals = list(values)[-n:]
    vals = [0.0] * (n - len(vals)) + vals
    total = height * 4
    levels = []
    for v in vals:
        if vmax <= 0 or v <= 0:
            levels.append(0)
        else:
            levels.append(max(1, min(total, round(v / vmax * total))))
    rows: list[Text] = []
    for r in range(height):                       # r = 0 が上の行
        below = (height - 1 - r) * 4
        color = _pal(palette, (height - 1 - r + 0.5) / height)
        t = Text(no_wrap=True, overflow="crop")
        for c in range(width):
            code = 0
            for side in (0, 1):
                lv = levels[c * 2 + side]
                for y in range(4):
                    if lv >= below + (4 - y):
                        code |= _DOTS[y][side]
            if code:
                t.append(chr(0x2800 + code), style=color)
            elif r == height - 1:
                t.append("⣀", style=BASE)          # 底の線
            else:
                t.append(" ")
        rows.append(t)
    return rows


def meter(frac: float, width: int, palette: tuple[str, ...] = RED, *, mark: float | None = None) -> Text:
    """btop の目盛り（点字）。色は位置で段階（左が暗い）。mark は印を立てる位置（要約を始める量など）。"""
    width = max(1, width)
    frac = 0.0 if frac != frac or frac < 0 else 1.0 if frac > 1 else frac
    filled = round(frac * width)
    t = Text(no_wrap=True, overflow="crop")
    mpos = None if mark is None or not (0 < mark < 1) else min(width - 1, int(mark * width))
    for i in range(width):
        if i == mpos:
            t.append("|", style=f"bold {T.TEXT}")
            continue
        # ★点字（幅が決まっている字）で描く。■ は East Asian Ambiguous で、日本語の字形だと 2 桁に描かれ枠からはみ出す。
        t.append("⣿", style=_pal(palette, i / max(1, width - 1))) if i < filled else t.append("⣀", style=EMPTY)
    return t


def fmt_k(n: float | int | None, base: int = 1000) -> str:
    """文脈の語数は 1024 基準（窓 65,536 を 64K と書く・推論機の -c と同じ数え方）。"""
    if n is None:
        return "-"
    n = float(n)
    if n >= base * base:
        return f"{n / base / base:.1f}M"
    if n >= base * 10:
        return f"{n / base:.0f}K"
    if n >= base:
        return f"{n / base:.1f}K"
    return f"{n:.0f}"


_TARGET_KEYS = ("command", "cmd", "pattern", "query", "url", "file_path", "path", "glob", "prompt", "description",
                "name", "action", "question", "task")

#: 子を走らせる道具（子の道具の中継 notice kind=subagent を、この行に数える）。
CHILD_TOOLS = ("Explore", "Task")
_CHILD_RE = re.compile(r"^子: ([▶✓✗])\s*(\S+)")
_URL_RE = re.compile(r"https?://[^\s'\"<>|;)]+")


def child_step(stats: dict, msg: str) -> bool:
    """子の中継の 1 行（oc/tools/task._ChildRelay の「子: ▶ Read」など）を数える。▶＝呼んだ・✗＝失敗。数えたら True。
    ★2026-10-18: 全画面の表示では子の中継がどこにも出ず、子が動く 1〜2 分の間、Explore・Task の行は「-」のまま止まって見えた。"""
    m = _CHILD_RE.match(msg or "")
    if not m:
        return False
    by = stats.setdefault("by", {})
    if m.group(1) == "▶":
        stats["n"] = stats.get("n", 0) + 1
        by[m.group(2)] = by.get(m.group(2), 0) + 1
    elif m.group(1) == "✗":
        stats["bad"] = stats.get("bad", 0) + 1
    else:
        return False
    return True


def child_label(stats: dict, target: str) -> str:
    """行の「対象」の頭に付ける子の進み（子 5 ✗1 · 元の対象）。"""
    n, bad = int(stats.get("n", 0)), int(stats.get("bad", 0))
    head = f"子 {n}" + (f" ✗{bad}" if bad else "")
    return f"{head} · {target}" if target else head


def cmd_target(cmd: str) -> str:
    """命令の「対象」: 頭の `cd … &&` を外し、URL があれば「命令 方法 宛先」にする。
    ★2026-10-27 SNS の撮影（オーナー「TUI を直してもらって」）: 道具の一覧の対象が `curl -s -X PO…` で切れ、何をしたのか
      分からなかった。幅の狭い欄では、命令の頭の飾り（-s -X）より宛先のほうが意味を持つ。"""
    c = " ".join(str(cmd or "").split())
    c = re.sub(r"^(?:cd\s+\S+\s*(?:&&|;)\s*)+", "", c)
    m = _URL_RE.search(c)
    if not m:
        return c
    cut = max(c.rfind("|", 0, m.start()), c.rfind("&&", 0, m.start()), c.rfind(";", 0, m.start()))
    seg = c[cut + 1:].lstrip("& ").strip() if cut >= 0 else c
    words = seg.split()
    prog = os.path.basename(words[0]) if words else ""
    method = ""
    mm = re.search(r"(?:^|\s)(?:-X|--request)\s*([A-Za-z]+)", seg)
    if mm:
        method = mm.group(1).upper()
    elif re.search(r"(?:^|\s)(?:-d|-F|--form|--json|--data(?:-raw|-binary|-urlencode)?)(?:\s|=|$)", seg):
        method = "POST"
    url = m.group(0).split("://", 1)[1]
    return " ".join(x for x in (prog, method, url) if x)


def tool_target(name: str, inp: dict) -> str:
    """道具の一覧の「対象」。コマンドは cmd_target・探す語・URL はそのまま、パスは末尾 2 段。"""
    for k in _TARGET_KEYS:
        v = inp.get(k) if isinstance(inp, dict) else None
        if isinstance(v, str) and v.strip():
            if k in ("command", "cmd"):
                return cmd_target(v)
            v = " ".join(v.split())
            if k in ("file_path", "path") and "/" in v.rstrip("/"):
                v = "/".join(v.rstrip("/").split("/")[-2:])
            return v
    return _status_target(name, inp if isinstance(inp, dict) else {})


def shown_ctx(tel: "Telemetry", busy: bool) -> int | None:
    """agent の箱の CTX に出す語数。★推論機の枠（/slots）の数は、この会話が推論機を使っている間だけ使う
    （2026-10-27 SNS の撮影: 起動の直後に CTX 85K/128K と出て、走り出すと 9.9K に下がった＝枠に残っていた前の会話の大きさ）。
    それ以外はこの会話の数（serve の事象の ctx）。呼ぶ側が tel.lock を持つ。"""
    if tel.slots_ok and busy and tel.processing:
        return tel.ctx_used
    return tel.agent_ctx


def fmt_gib(b: float | None) -> str:
    return "-" if b is None else f"{b / 1024 ** 3:.1f}"


def _priv(s: str) -> str:
    """--privacy のときは伏せる（IP・ホームのパス・ユーザー名・ホスト名）。
    ★2026-10-27 SNS の撮影: 題の行の宛先は伏せていたのに、server の箱の中の「192.0.2.160:8742 に届きません」は生のまま出た。"""
    return mask_private(s) if (s and privacy_on()) else s


def _clip(s: str, n: int) -> str:
    from rich.cells import cell_len, set_cell_size
    if n <= 0:
        return ""
    return s if cell_len(s) <= n else set_cell_size(s, max(0, n - 1)) + "~"


# --------------------------------------------------------------------------
# 測った数（推論機・作業の両方）。スレッドから書き、画面が読む
# --------------------------------------------------------------------------

class Telemetry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        # 推論機（bw-metrics）
        self.srv_ok: bool | None = None
        self.srv_err = ""
        self.srv_t = 0.0
        self.gpu_name = ""
        # ★GPU ごとの使用率（2026-10-18 オーナー「2080super の使用率を表示する必要がありますね」＝推論機の機械が 2 枚になった）。
        self.gpu_names: list[str] = []
        self.gpus_util: list[collections.deque[float]] = []
        self.gpu_util: collections.deque[float] = collections.deque(maxlen=HIST)
        self.gpu_temp: collections.deque[float] = collections.deque(maxlen=HIST)
        self.gpu_power: collections.deque[float] = collections.deque(maxlen=HIST)
        self.cpu: collections.deque[float] = collections.deque(maxlen=HIST)
        self.last: dict = {}
        # モデル切り替えの口
        self.model_name = ""
        self.model_unit = ""
        self.power_limit: float | None = None
        self.sm_clock: float | None = None
        self.persistence = ""
        self.switching = ""
        # ★推論機を止める・起こす（2026-11-03）。models_seen は推論機の一覧を一度でも読めたとき（読めないうちは
        #   「止まっている」と言わない）。switch_action は進み中の操作（"switch" | "stop"）。
        self.models_seen = False
        self.stopping = False
        self.lease_stopped = ""                    # GPU の貸し出し の貸し出しが止めている LLM（一覧の lease.llm_stopped）
        # ★Claude API の中継（2026-10-19）。送り先が中継のとき、server の箱は GPU の代わりにモデルと今月の額を出す。
        self.claude = False
        self.spent = ""
        # /slots
        self.slots_ok: bool | None = None
        self.n_ctx: int | None = None
        self.ctx_used: int | None = None
        self.processing = False
        self.cache_pct: float | None = None
        self.gen_rate: collections.deque[float] = collections.deque(maxlen=HIST)   # 1 秒ごとの語/秒
        self._dec_prev: tuple[int, int, float] | None = None                    # (id_task, n_decoded, t)
        self.gen_now = 0.0
        # 作業（serve の事象）
        self.iter = 0
        self.agent_ctx: int | None = None
        self.agent_nctx: int | None = None
        self.compact_at: int | None = None
        self.compactions = 0
        self.compact_secs: list[int] = []
        self.compacting_since: float | None = None
        self.tools_ok = 0
        self.tools_bad = 0
        self.turns = 0
        self.out_tokens = 0
        self.turn_t0: float | None = None
        self.last_turn_secs: float | None = None
        self.last_tok_s: float | None = None
        self.injects = 0

    # -- 作業の事象 ----------------------------------------------------------

    def feed(self, ev: dict) -> None:
        et = str(ev.get("type") or "")
        with self.lock:
            if et == "iter_start":
                self.iter = int(ev.get("iter") or self.iter)
                for k, attr in (("ctx", "agent_ctx"), ("n_ctx", "agent_nctx"), ("compact_at", "compact_at")):
                    if isinstance(ev.get(k), int):
                        setattr(self, attr, int(ev[k]))
            elif et == "tool_result":
                if ev.get("ok") is False:
                    self.tools_bad += 1
                else:
                    self.tools_ok += 1
            elif et == "notice":
                kind = str(ev.get("kind") or "")
                if kind in ("compacting", "context_compacting"):
                    self.compacting_since = time.time()
                elif kind == "context_compacted":
                    self.compactions += 1
                    self.compacting_since = None
                    if isinstance(ev.get("secs"), int):
                        self.compact_secs.append(int(ev["secs"]))
                elif kind == "injected":
                    self.injects += 1
            elif et == "context_compacted":
                self.compactions += 1
                self.compacting_since = None
                if isinstance(ev.get("secs"), int):
                    self.compact_secs.append(int(ev["secs"]))
            elif et == "done":
                self.turns += 1
                try:
                    self.out_tokens += int(ev.get("completion_tokens") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    _ts = ev.get("gen_tok_s") if ev.get("gen_tok_s") is not None else ev.get("tok_s")
                    self.last_tok_s = float(_ts) if _ts is not None else None
                    self.last_turn_secs = float(ev.get("secs") or 0) or None
                except (TypeError, ValueError):
                    pass
                self.turn_t0 = None
                self.compacting_since = None

    def turn_started(self) -> None:
        with self.lock:
            self.turn_t0 = time.time()
            self.iter = 0

    def reset_conversation(self) -> None:
        with self.lock:
            self.iter = 0
            self.agent_ctx = None
            self.compactions = 0
            self.compact_secs = []
            self.tools_ok = self.tools_bad = 0
            self.turns = 0
            self.out_tokens = 0
            self.injects = 0

    # -- 推論機 --------------------------------------------------------------

    def add_metrics(self, d: dict) -> float:
        """bw-metrics の答えを取り込み、いちばん新しい時刻を返す。"""
        newest = self.srv_t
        with self.lock:
            names = d.get("gpu_names") or []
            if names:
                self.gpu_name = str(names[0])
                self.gpu_names = [str(n) for n in names]
            for s in d.get("samples") or []:
                t = float(s.get("t") or 0)
                if t <= self.srv_t:
                    continue
                gs = s.get("gpus") or [{}]
                g = gs[0] or {}
                while len(self.gpus_util) < len(gs):
                    self.gpus_util.append(collections.deque(maxlen=HIST))
                for i, gi in enumerate(gs):
                    self.gpus_util[i].append(float((gi or {}).get("util") or 0))
                self.gpu_util.append(float(g.get("util") or 0))
                self.gpu_temp.append(float(g.get("temp") or 0))
                self.gpu_power.append(float(g.get("power") or 0))
                self.cpu.append(float(s.get("cpu") or 0))
                self.last = {"t": t, "cpu": s.get("cpu"), "mem_used": s.get("mem_used"),
                             "mem_total": s.get("mem_total"), **{f"gpu_{k}": v for k, v in g.items()},
                             "gpus": [dict(x or {}) for x in gs], "llm": s.get("llm") or {}}
                newest = max(newest, t)
            self.srv_t = newest
            self.srv_ok, self.srv_err = True, ""
        return newest

    def add_models(self, d: dict) -> None:
        with self.lock:
            if d.get("kind") == "claude":
                _, self.spent = models._claude_choices(d)
            active = str(d.get("active") or "")
            self.model_unit = active
            # ★止まった（active が null）ら前のモデル名を残さない（残すと止めた後も載っているように見える）。
            self.model_name = ""
            self.models_seen = d.get("kind") != "claude"
            for u in d.get("units") or []:
                if u.get("active") or u.get("id") == active:
                    self.model_name = str(u.get("model") or u.get("id") or "")
            g = d.get("gpu") or {}
            self.power_limit = _num(g.get("power_limit"))
            self.sm_clock = _num(g.get("app_clock_gr"))
            self.persistence = str(g.get("persistence") or "")
            sw = d.get("switch") or {}
            running = sw.get("state") == "running"
            self.stopping = running and sw.get("action") == "stop"
            lease = d.get("lease") if isinstance(d.get("lease"), dict) else {}
            self.lease_stopped = str(lease.get("llm_stopped") or "")
            self.switching = str(sw.get("target") or "") if running and not self.stopping else ""

    def add_slots(self, slots: list) -> None:
        now = time.time()
        with self.lock:
            self.slots_ok = True
            if not slots:
                return
            s = next((x for x in slots if x.get("is_processing")), slots[0])
            self.n_ctx = int(s.get("n_ctx") or 0) or self.n_ctx
            self.processing = bool(s.get("is_processing"))
            nt = (s.get("next_token") or [{}])[0] or {}
            dec = int(nt.get("n_decoded") or 0)
            npt = int(s.get("n_prompt_tokens") or 0)
            self.ctx_used = npt + dec if (npt or dec) else self.ctx_used
            done_n = s.get("n_prompt_tokens_processed")
            if npt and isinstance(done_n, int):
                self.cache_pct = max(0.0, min(100.0, 100.0 * (npt - done_n) / npt))
            task = int(s.get("id_task") or 0)
            rate = 0.0
            if self._dec_prev and self._dec_prev[0] == task and self.processing:
                dt = now - self._dec_prev[2]
                if dt > 0.2 and dec >= self._dec_prev[1]:
                    rate = (dec - self._dec_prev[1]) / dt
            self._dec_prev = (task, dec, now)
            self.gen_now = rate
            self.gen_rate.append(rate)


def _num(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


class Poller(threading.Thread):
    """推論機の数を取りに行く（読むだけ）。届かなければ箱に理由を出し、取り続ける。"""

    def __init__(self, tel: Telemetry, backend: Callable[[], str], claude: Callable[[], str] | None = None) -> None:
        super().__init__(daemon=True, name="tui-poller")
        self.tel = tel
        self.backend = backend
        self.claude = claude or (lambda: "")
        self.stop = threading.Event()
        self._since = 0.0
        self._next_models = 0.0
        self._next_metrics = 0.0
        self._host = ""

    def poke(self) -> None:
        """すぐ読み直す（モデルを切り替えた直後）。"""
        self._next_models = 0.0

    def _urls(self) -> tuple[str, str, str] | None:
        b = str(self.backend() or "")
        u = urlparse(b)
        if not u.hostname:
            return None
        if u.netloc != self._host:
            self._host, self._since, self._next_models = u.netloc, 0.0, 0.0
        host = u.hostname if ":" not in u.hostname else f"[{u.hostname}]"
        root = f"{u.scheme or 'http'}://{u.netloc}"
        metrics = os.environ.get("LAPS_METRICS_URL") or f"http://{host}:8742/v1/metrics"
        modelctl = os.environ.get("LAPS_MODELCTL_URL") or f"http://{host}:8743/v1/models"
        return root, metrics, modelctl

    def _claude_round(self, http: httpx.Client, root: str, now: float) -> None:
        """送り先が Claude の中継: GPU も /slots も無い。中継の /modelctl/ からいまのモデルと今月の額だけ読む。"""
        with self.tel.lock:
            self.tel.claude = True
        if now < self._next_models:
            return
        self._next_models = now + MODELCTL_SECS
        try:
            r = http.get(f"{root}/modelctl/v1/models")
            r.raise_for_status()
            self.tel.add_models(r.json())
            with self.tel.lock:
                self.tel.srv_ok, self.tel.srv_err = True, ""
        except Exception as e:
            with self.tel.lock:
                self.tel.srv_ok = False
                self.tel.srv_err = f"Claude の中継（{urlparse(root).netloc}）に届きません（{type(e).__name__}）"

    def run(self) -> None:
        with httpx.Client(timeout=httpx.Timeout(4.0, connect=2.0), trust_env=False) as http:
            while not self.stop.is_set():
                urls = self._urls()
                now = time.time()
                if urls and models.is_claude(str(self.backend() or ""), str(self.claude() or "")):
                    self._claude_round(http, urls[0], now)
                    self.stop.wait(SLOTS_SECS)
                    continue
                with self.tel.lock:
                    self.tel.claude = False
                if urls:
                    root, metrics, modelctl = urls
                    try:
                        r = http.get(f"{root}/slots")
                        if r.status_code == 200 and isinstance(r.json(), list):
                            self.tel.add_slots(r.json())
                        else:
                            with self.tel.lock:
                                self.tel.slots_ok = False
                    except Exception:
                        with self.tel.lock:
                            self.tel.slots_ok = False
                    if now >= self._next_metrics:
                        self._next_metrics = now + POLL_SECS
                        try:
                            r = http.get(metrics, params={"since": f"{self._since or (now - 300):.1f}"})
                            r.raise_for_status()
                            self._since = self.tel.add_metrics(r.json())
                        except Exception as e:
                            with self.tel.lock:
                                self.tel.srv_ok = False
                                self.tel.srv_err = f"{urlparse(metrics).netloc} に届きません（{type(e).__name__}）"
                    if now >= self._next_models:
                        self._next_models = now + MODELCTL_SECS
                        try:
                            r = http.get(modelctl)
                            r.raise_for_status()
                            self.tel.add_models(r.json())
                        except Exception:
                            pass
                self.stop.wait(SLOTS_SECS)


# --------------------------------------------------------------------------
# Renderer の出口を「会話」の枠へ
# --------------------------------------------------------------------------

class _PaneFile(io.TextIOBase):
    """Console の書き先。1 回の print が 1 回の write で来る（Rich が溜めて書く）。"""

    def __init__(self, sink: Callable[[str], None]) -> None:
        super().__init__()
        self._sink = sink

    def write(self, s: str) -> int:
        if s:
            self._sink(s)
        return len(s)

    def isatty(self) -> bool:
        return True

    def flush(self) -> None:
        pass


class TuiRenderer(Renderer):
    """Live を持たない Renderer。生成中の本文・思考・実行中の道具は LivePane が読みに来る。"""

    def __init__(self, console: Console, *, config_path: Any = None,
                 on_event_hook: Optional[Callable[[dict], None]] = None) -> None:
        super().__init__(console, config_path=config_path)
        self._use_live = False          # 本文は確定したときに枠へ流す（流れている途中は LivePane）
        self._hook = on_event_hook
        self._child_counts: dict[str, int] = {}    # 子の道具の数（on_event の注記・子の道具が終わったら 1 行に）
        self._child_bad = 0

    def _start_live(self) -> None:
        return

    def _status_enabled(self) -> bool:
        return not (getattr(self.console, "no_color", False) or os.environ.get("NO_COLOR"))

    def on_event(self, ev: dict) -> None:
        if self._hook is not None:
            try:
                self._hook(ev)
            except Exception:
                pass
        et, kind = str(ev.get("type") or ""), str(ev.get("kind") or "")
        # ★子（Explore・Task）の道具の中継「子: ▶ Bash」「子: ✓ Bash」は会話の欄に 1 行ずつ出さない（2026-10-27 SNS の撮影:
        #   10 行以上並んで、何を調べているのか分からないまま画面が埋まった）。道具の一覧の行が「子 N」で進みを出し、
        #   子の道具が終わったら「子の道具: Bash ×9・Read ×2」と 1 行にまとめる。失敗（✗）はこれまでどおりその場で出す。
        if et == "notice" and kind == "subagent":
            m = _CHILD_RE.match(str(ev.get("message") or ""))
            if m and m.group(1) in ("▶", "✓"):
                if m.group(1) == "▶":
                    self._child_counts[m.group(2)] = self._child_counts.get(m.group(2), 0) + 1
                return
            if m and m.group(1) == "✗":
                self._child_bad += 1
        if et == "tool_result" and str(ev.get("name") or "") in CHILD_TOOLS:
            super().on_event(ev)
            self._flush_child_counts()
            return
        if et == "notice" and kind == "injected":
            self._commit_body()
            msg = str(ev.get("message") or "")
            if privacy_on():
                msg = mask_private(msg)
            head = "↳ 強制割り込み: " if ev.get("force") else "↳ 割り込み: "
            self._print(Text(wrap_with_gutter(msg, self._width(), head, "  "), style=f"bold {T.META}"))
            return
        super().on_event(ev)

    def _flush_child_counts(self) -> None:
        """子の道具の数を 1 行にまとめて出す（_CHILD_RE の注記）。数えた物が無ければ何も出さない。"""
        counts, bad = self._child_counts, self._child_bad
        self._child_counts, self._child_bad = {}, 0
        if not counts and not bad:
            return
        parts = [f"{k} ×{v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        line = "子の道具: " + (SEP.join(parts) if parts else "なし") + (f"（✗ {bad}）" if bad else "")
        self._print(Text(wrap_with_gutter(line, self._width(), "  ", "  "), style=T.S_DIM))

    def user(self, text: str) -> None:
        if self.transcript:
            self.console.print()
            self.console.print(Text("─" * max(1, self._width() - 1), style=T.S_WALL_RULE))
        self.transcript.append({"role": "user", "content": text})
        if privacy_on():
            text = mask_private(str(text))
        t = Text(wrap_with_gutter(str(text), self._width(), "❯ ", "  "), style=f"bold {T.TEXT}")
        t.stylize(f"bold {T.ALERT}", 0, 1)
        self.console.print(t)


class _LivePanel:
    """生成中の中身: 思考の末尾 → 本文の末尾 → 実行中の道具 → 状態の行 → 波。"""

    def __init__(self, r: TuiRenderer, limit: int) -> None:
        self.r = r
        self.limit = max(3, limit)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        r = self.r
        with r._lock:
            body = r._body_text
            think = r._think_buf
            running = list(r._running.values())
            waiting, quiet, t0, open_ = r._waiting, r._quiet_secs, r._t0, r._open
            out_chars = len(r._turn_text) + len(r._think_buf)
            wave = r._wave
        width = int(options.max_width or console.width)
        tail: list[Text] = []
        heads = [r._head_line(e, running=True) for e in running]
        _pn = r.plan_now(width) if open_ else None          # 予定のいまの段（2026-10-21）
        if _pn is not None:
            heads.insert(0, _pn)
        if open_ or running:
            now = time.time()
            heads.append(r._status_row(running=running, waiting=waiting, quiet=quiet,
                                       elapsed=(now - t0) if (open_ and t0) else 0.0, out_chars=out_chars,
                                       width=width, animate=True, now=now, live_turn=bool(open_ and t0)))
        room = self.limit - sum(h.plain.count("\n") + 1 for h in heads) - (2 if wave is not None else 0)
        room = max(1, room)
        lines: list[list[Segment]] = []
        if body.strip():
            pw = _prose_limit(width, r.config_path)
            lines = console.render_lines(_md(body), options.reset_height().update(width=pw), pad=False)
        elif think.strip():
            src = think.strip().splitlines()[-(room * 2):]
            wrapped = wrap_think("\n".join(src), max(8, width - 2)).splitlines()
            for ln in wrapped[-room:]:
                tail.append(Text(ln, style=f"italic {T.NOTE}", no_wrap=True, overflow="crop"))
        for ln in lines[-room:]:
            yield from ln
            yield Segment.line()
        for t in tail:
            yield t
        for h in heads:
            yield h
        if wave is not None:
            yield wave


# --------------------------------------------------------------------------
# 箱
# --------------------------------------------------------------------------

class HeaderBar(Widget):
    DEFAULT_CSS = "HeaderBar { height: 1; background: #1a0d0c; }"

    def __init__(self, tui: "BlackwallTui") -> None:
        super().__init__(id="hdr")
        self.tui = tui

    def render(self) -> Text:
        w = self.size.width
        tui = self.tui
        s = tui.settings()
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append("▐", style=T.LOGO_ROWS[2])
        name = "L.A.P.S"
        for i, ch in enumerate(name):
            t.append(ch, style=f"bold {T.LOGO_ROWS[min(5, i * 6 // len(name))]}")
        t.append(f" {__version__}", style=T.NOTE)
        host = urlparse(str(s.get("backend") or "")).hostname or "?"
        model = tui.tel.model_name or str(s.get("model") or "")
        if tui.tel.models_seen and not tui.tel.model_unit and not tui.tel.claude:
            model = "推論機は貸し出し中" if tui.tel.lease_stopped else "推論機は停止中"
        if tui.tel.claude:
            host, model = "Claude API", models.pretty(model)
        right = Text(no_wrap=True)
        if getattr(tui, "_srv_auto_hidden", False):
            right.append("server 不通 F2  ", style=T.NOTE)
        right.append(f"{s.get('policy') or '?'}/{s.get('ethics') or '?'}", style=T.NOTE)
        right.append("  " + time.strftime("%H:%M:%S"), style=T.META)
        with tui.tel.lock:
            reach = tui.tel.slots_ok or tui.tel.srv_ok
        dot_style = T.GOOD if reach else (T.ALERT if reach is False else T.NOTE)
        t.append("  ◉", style=dot_style)
        mid = f" {host}  {model}"
        ws = str(s.get("workspace") or tui.ctx.client.workspace or "")
        home = os.path.expanduser("~")
        if ws.startswith(home):
            ws = "~" + ws[len(home):]
        sess = tui.ctx.session or ""
        extra = f"  ⌂ {ws}" + (f"  ⧉ {sess[:12]}" if sess else "")
        if privacy_on():
            mid, extra = mask_private(mid), mask_private(extra)
        room = w - t.cell_len - right.cell_len - 1
        t.append(_clip(mid, room), style=T.TEXT)
        room = w - t.cell_len - right.cell_len - 1
        t.append(_clip(extra, room), style=T.META)
        pad = w - t.cell_len - right.cell_len
        if pad > 0:
            t.append(" " * pad)
        t.append_text(right)
        return t


class Box(Widget):
    """枠つきの箱（btop の箱）。中身は render で描く。"""

    can_focus = False

    def __init__(self, tui: "BlackwallTui", *, id: str, num: int, title: str) -> None:
        super().__init__(id=id, classes="box")
        self.tui = tui
        self.num = num
        self.base_title = title

    _shown: tuple[str, str] = ("", "")

    def title_markup(self, extra: str = "") -> str:
        t = f"[b {T.ALERT}]{self.num}[/] [b]{self.base_title}[/b]"
        if extra:
            t += f"  [{T.META}]{extra}[/]"
        return t

    def titles(self) -> tuple[str, str]:
        return self.title_markup(), ""

    def apply_titles(self) -> None:
        """題は描画の外で替える（render の中で替えると描き直しが止まらない）。変わったときだけ。"""
        t = self.titles()
        if t != self._shown:
            self._shown = t
            self.border_title, self.border_subtitle = t


def gpu_short(name: str) -> str:
    """欄に収まる GPU の名前（"NVIDIA GeForce RTX 2080 SUPER" → "2080S"・"Tesla P40" → "P40"）。"""
    s = str(name or "").strip()
    for pre in ("NVIDIA ", "GeForce ", "Tesla ", "Quadro ", "RTX ", "GTX "):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.replace(" SUPER", "S").replace(" Ti", "Ti") or str(name or "")


class ServerBox(Box):
    def titles(self) -> tuple[str, str]:
        tel = self.tui.tel
        with tel.lock:
            claude, model_id = tel.claude, tel.model_name
        if claude:
            host = urlparse(str(self.tui.settings().get("backend") or "")).netloc
            if privacy_on():
                host = mask_private(host)
            return self.title_markup(SEP.join(x for x in ("Claude API", host) if x)), models.pretty(model_id)
        with tel.lock:
            gpu_name, model, sw = tel.gpu_name, tel.model_name, tel.switching
            stopped = tel.models_seen and not tel.model_unit
            stopping = tel.stopping
            lease_stopped = tel.lease_stopped
            if len(tel.gpu_names) > 1:
                gpu_name = " + ".join(gpu_short(n) for n in tel.gpu_names)
        host = urlparse(str(self.tui.settings().get("backend") or "")).hostname or ""
        if privacy_on():
            host = mask_private(host)
        if stopping:
            sub = (model + "  " if model else "") + "止めています"
        elif stopped:
            sub = f"→ {sw} を起動中" if sw else models.stopped_label(lease_stopped)
        else:
            sub = model + (f"  → {sw} に切り替え中" if sw else "")
        return self.title_markup(SEP.join(x for x in (gpu_name, host) if x)), sub

    def _render_claude(self) -> Text:
        """送り先が Claude の中継のとき（GPU は無い）: いまのモデル・今月の額・切り替え方。"""
        tel = self.tui.tel
        with tel.lock:
            ok, err, model_id, spent = tel.srv_ok, tel.srv_err, tel.model_name, tel.spent
        t = Text()
        t.append("Claude API", style=f"bold {T.ALERT}")
        t.append("  開発機 の中継・費用は共有の API の枠から\n", style=T.NOTE)
        if ok is None:
            t.append("中継に聞いています…", style=T.NOTE)
            return t
        if not ok:
            t.append(_priv(err) or "中継に届きません", style=T.ALERT)
            return t
        t.append("モデル  ", style=T.META)
        t.append(models.pretty(model_id) or "?", style=f"bold {T.TEXT}")
        if model_id:
            t.append(f"  {model_id}", style=T.NOTE)
        if spent:
            t.append("\n" + spent, style=T.META)
        t.append("\nF5 でモデルを選ぶ（推論機へ戻すのもここ）", style=T.NOTE)
        return t

    def render(self) -> Any:
        """GPU 1 枚ごとに使用率の面グラフ（左に名札：名前・使用率・温度・電力）、下に GPU ごとの VRAM と推論機の枠。
        ★2026-10-18 オーナー「CPU とメモリのグラフ外して GPU 二枚のグラフでいいです」（Windows 版と揃えて CPU・MEM は出さない）。"""
        tel = self.tui.tel
        W, H = self.content_size.width, self.content_size.height
        if tel.claude:
            return self._render_claude()
        with tel.lock:
            ok, err = tel.srv_ok, tel.srv_err
            last = dict(tel.last)
            hists = [list(h) for h in tel.gpus_util] or ([list(tel.gpu_util)] if tel.gpu_util else [])
            names = list(tel.gpu_names)
            pl, clk, pers = tel.power_limit, tel.sm_clock, tel.persistence
        out: list[Text] = []
        if ok is None:
            return Text("推論機の数を取りに行っています…", style=T.NOTE)
        if not ok and not last:
            return Text(f"{_priv(err)}\n推論機と同じ機械の bw-metrics（:8742）が要ります", style=T.NOTE)
        gl = last.get("gpus") or []
        n = max(len(hists), len(gl))
        short = [gpu_short(names[i]) if i < len(names) and names[i] else f"GPU{i}" for i in range(n)]
        llm = last.get("llm") or {}
        meters: list[tuple[str, float, str]] = []
        for i in range(n):
            g = gl[i] if i < len(gl) else {}
            vt = float(g.get("vram_total") or 0) or None
            vu = _num(g.get("vram_used"))
            meters.append((f"VRAM {short[i]}", (vu or 0) / (vt or 1), f"{(vu or 0) / 1024:.1f}/{(vt or 0) / 1024:.1f}G"))
        meters.append(("SLOT", float(llm.get("busy") or 0) / max(1, int(llm.get("slots") or 1)),
                       f"{int(llm.get('busy') or 0)}/{int(llm.get('slots') or 0)}"))
        cols = 2 if W >= 66 else 1
        rows_needed = (len(meters) + cols - 1) // cols
        gh = H - rows_needed
        if gh < 2 and cols == 1:
            meters = meters[:max(2, H - 2)]
            rows_needed = len(meters)
            gh = H - rows_needed
        # GPU ごとの使用率の面グラフ（高さは枚数で分ける・余りは上から 1 行ずつ）
        lab = 7
        for gi in range(n):
            rows_this = (gh // n + (1 if gi < gh % n else 0)) if n else 0
            if rows_this < 1:
                continue
            h = hists[gi] if gi < len(hists) else []
            g = gl[gi] if gi < len(gl) else {}
            graph = braille_area(h, max(4, W - lab), rows_this, 100.0, RED)
            u = h[-1] if h else 0.0
            temp, pw = _num(g.get("temp")), _num(g.get("power"))
            labels: list[tuple[str, str]] = [
                (short[gi][:lab - 1], f"bold {T.META}"),
                (f"{u:3.0f}%", f"bold {_pal(RED, u / 100)}"),
                (f"{temp:.0f}C" if temp is not None else "", T.NOTE),
                (f"{pw:.0f}W" if pw is not None else "", T.NOTE),
            ]
            if gi == 0:                                  # 電力の上限・時計・持続は モデル切り替えの口 が 1 枚目（推論機の GPU）について返す
                if pl and pw is not None:
                    labels[3] = (f"{pw:.0f}/{pl:.0f}"[:lab - 1], T.NOTE)
                if clk:
                    labels.append((f"{clk:.0f}M", T.NOTE))
                if pers:
                    labels.append(("PM on" if pers == "Enabled" else "PM off", T.NOTE if pers == "Enabled" else T.S_BAD))
            for i, gr in enumerate(graph):
                t = Text(no_wrap=True, overflow="crop")
                if i < len(labels) and labels[i][0]:
                    t.append(labels[i][0].ljust(lab), style=labels[i][1])
                else:
                    t.append(" " * lab)
                t.append_text(gr)
                out.append(t)
        colw = (W - (cols - 1) * 2) // cols
        nw = max(5, max((len(m[0]) for m in meters), default=5) + 1)
        for r in range(rows_needed):
            t = Text(no_wrap=True, overflow="crop")
            for c in range(cols):
                i = r * cols + c
                if i >= len(meters):
                    break
                name, frac, val = meters[i]
                vw = max(len(val), 9) if colw >= 30 else len(val)
                mw = max(3, colw - nw - vw - 1)
                if c:
                    t.append("  ")
                t.append(name.ljust(nw), style=T.META)
                t.append_text(meter(frac, mw, RED))
                t.append(" " + val.rjust(vw), style=T.TEXT)
            out.append(t)
        if not ok:
            out[-1:] = [Text(_clip(_priv(err), W), style=T.S_BAD)]
        return _Lines(out)


class AgentBox(Box):
    def titles(self) -> tuple[str, str]:
        tel = self.tui.tel
        with tel.lock:
            iter_, turns = tel.iter, tel.turns
        extra = f"反復 {iter_}" if self.tui.turn_running else (f"{turns} 往復" if turns else "")
        return self.title_markup(extra), ""

    def render(self) -> Any:
        tui = self.tui
        tel = tui.tel
        r = tui.renderer
        W, H = self.content_size.width, self.content_size.height
        busy = tui.turn_running
        with tel.lock:
            rate = list(tel.gen_rate)
            gen_now = tel.gen_now
            n_ctx = tel.agent_nctx or tel.n_ctx
            used = shown_ctx(tel, busy)
            compact_at = tel.compact_at
            iter_ = tel.iter
            comp, csecs, comping = tel.compactions, list(tel.compact_secs), tel.compacting_since
            ok_n, bad_n = tel.tools_ok, tel.tools_bad
            turns, out_tok, last_tok_s = tel.turns, tel.out_tokens, tel.last_tok_s
            processing, cache = tel.processing, tel.cache_pct
            slots_ok = tel.slots_ok
            injects = tel.injects
        with r._lock:
            waiting, running = r._waiting, list(r._running.values())
            t0 = r._t0 if r._open else 0.0
            thinking = bool(r._think_buf) and not r._body_text
        # 状態の言葉
        if not busy:
            stage, style = ("待機中" if tui.ready else "本体を起動しています…"), T.META
        elif comping:
            stage, style = f"文脈を要約しています（{fmt_secs(time.time() - comping)}）", f"bold {T.ALERT}"
        elif running:
            e = running[-1]
            stage, style = f"道具 {e.name} {_status_target(e.name, e.input)}", f"bold {T.TEXT}"
        elif waiting:
            stage, style = waiting, f"bold {T.TEXT}"
        elif thinking:
            stage, style = "考えています", f"bold {T.TEXT}"
        else:
            stage, style = ("生成しています" if processing or gen_now > 0 else "推論機を待っています"), f"bold {T.TEXT}"
        out: list[Text] = []
        el = fmt_secs(time.time() - t0) if (busy and t0) else ""
        spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10] if busy else "⠂"
        line = Text(no_wrap=True, overflow="crop")
        line.append(f"{spin} ", style=T.ALERT if busy else T.NOTE)
        room = W - 2 - (len(el) + 1 if el else 0)
        line.append(_clip(stage, room), style=style)
        if el:
            line.append(" " * max(1, W - line.cell_len - len(el)))
            line.append(el, style=T.META)
        out.append(line)
        # 下の行の数を先に決め、残りを生成の速さのグラフに回す
        info: list[Text] = []
        cl = Text(no_wrap=True, overflow="crop")
        cl.append("CTX  ", style=T.META)
        val = f"{fmt_k(used, 1024)}/{fmt_k(n_ctx, 1024)}" if n_ctx else "-"
        mw = max(4, W - 5 - max(len(val), 11) - 1)
        frac = (used or 0) / n_ctx if n_ctx else 0.0
        cl.append_text(meter(frac, mw, RED, mark=(compact_at / n_ctx) if (compact_at and n_ctx) else None))
        cl.append(" " + val.rjust(max(len(val), 11)), style=T.TEXT)
        info.append(cl)
        st = Text(no_wrap=True, overflow="crop")
        st.append("道具 ", style=T.META)
        st.append(f"{ok_n}", style=T.TEXT)
        st.append(f"  ✗ {bad_n}", style=T.S_BAD if bad_n else T.NOTE)
        st.append("  要約 ", style=T.META)
        st.append(f"{comp}", style=T.TEXT)
        if csecs:
            st.append(f"（最後 {csecs[-1]}秒）", style=T.NOTE)
        if cache is not None and slots_ok and busy:
            st.append("  キャッシュ ", style=T.META)
            st.append(f"{cache:.0f}%", style=T.TEXT)
        if injects:
            st.append(f"  割り込み {injects}", style=T.META)
        info.append(st)
        se = Text(no_wrap=True, overflow="crop")
        se.append("出力 ", style=T.META)
        se.append(fmt_k(out_tok), style=T.TEXT)
        se.append(" 語", style=T.NOTE)
        if last_tok_s:
            se.append("  直前の平均 ", style=T.META)
            se.append(f"{last_tok_s:.1f}", style=T.TEXT)
            se.append(" 語/秒", style=T.NOTE)
        info.append(se)
        gh = H - 1 - len(info)
        if gh < 1:
            info = info[:max(0, H - 2)]
            gh = H - 1 - len(info)
        if gh >= 1:
            lab = 6
            vmax = max([1.0] + rate[-(W * 2):]) * 1.15
            graph = braille_area(rate, max(4, W - lab), gh, vmax, WHITE)
            for i, g in enumerate(graph):
                t = Text(no_wrap=True, overflow="crop")
                if i == 0:
                    t.append("GEN".ljust(lab), style=f"bold {T.META}")
                elif i == 1:
                    t.append((f"{gen_now:4.1f}" if slots_ok else "  -").ljust(lab), style=f"bold {T.TEXT}")
                elif i == 2:
                    t.append("語/秒".ljust(lab - 2), style=T.NOTE)
                else:
                    t.append(" " * lab)
                t.append_text(g)
                out.append(t)
        out.extend(info)
        return _Lines(out)


class _Lines:
    """Text を行として積む（Group と違い、空の行も高さを持つ）。"""

    def __init__(self, lines: list[Text]) -> None:
        self.lines = lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for i, t in enumerate(self.lines):
            yield t


class LivePane(Widget):
    DEFAULT_CSS = "LivePane { height: auto; max-height: 60%; padding: 0 1; }"

    def __init__(self, tui: "BlackwallTui") -> None:
        super().__init__(id="live")
        self.tui = tui

    def render(self) -> Any:
        parent_h = self.parent.size.height if self.parent is not None else 20
        return _LivePanel(self.tui.renderer, int(parent_h * 0.55))


class ToolsTable(DataTable):
    BINDINGS = [Binding("enter", "select_cursor", "中身", show=False)]


class PromptArea(TextArea):
    """依頼の欄。Enter で送る・Alt+Enter（Ctrl+J）で改行・↑↓ で履歴（1 行目・最終行のとき）。"""

    class Submitted(Message):
        def __init__(self, area: "PromptArea", text: str) -> None:
            super().__init__()
            self.area = area
            self.text = text

    def __init__(self, history: list[str], **kw: Any) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False,
                         tab_behavior="focus", compact=True, **kw)
        self.past = history          # 古い → 新しい
        self._hpos: int | None = None
        self._draft = ""

    async def _on_key(self, event: events.Key) -> None:
        key = event.key
        if key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted(self, self.text))
            return
        if key in ("alt+enter", "ctrl+j", "shift+enter", "ctrl+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        if key == "up" and self.cursor_location[0] == 0 and self.past:
            event.stop()
            event.prevent_default()
            if self._hpos is None:
                self._draft = self.text
                self._hpos = len(self.past)
            if self._hpos > 0:
                self._hpos -= 1
                self._set(self.past[self._hpos])
            return
        if key == "down" and self._hpos is not None and self.cursor_location[0] >= self.document.line_count - 1:
            event.stop()
            event.prevent_default()
            self._hpos += 1
            if self._hpos >= len(self.past):
                self._hpos = None
                self._set(self._draft)
            else:
                self._set(self.past[self._hpos])
            return
        await super()._on_key(event)

    def _set(self, text: str) -> None:
        self.load_text(text)
        self.move_cursor(self.document.end)

    def reset_history_pos(self) -> None:
        self._hpos = None
        self._draft = ""


class FooterBar(Widget):
    DEFAULT_CSS = "FooterBar { height: 1; background: #1a0d0c; }"

    def __init__(self, tui: "BlackwallTui") -> None:
        super().__init__(id="ftr")
        self.tui = tui

    def render(self) -> Text:
        busy = self.tui.turn_running
        # (キー, 名前, 優先度)。狭いときは優先度の低いものから落とし、並びは保つ（^Q 終了と F1 は最後まで残す）。
        keys = ([("^C", "中断", 0), ("Enter", "割り込み", 1), ("^T", "強制割り込み", 3), ("PgUp", "遡る", 4),
                 ("F1", "ヘルプ", 2), ("^Q", "終了", 0)] if busy else
                [("Enter", "送信", 0), ("Alt+Enter", "改行", 3), ("上下", "履歴", 5), ("/", "コマンド", 4),
                 ("Tab", "移動", 6), ("PgUp", "遡る", 5), ("^L", "消去", 7), ("F1", "ヘルプ", 1), ("F5", "モデル", 2),
                 ("F2-4", "箱", 6),
                 ("^Q", "終了", 0)])
        q = len(self.tui.queue)
        tail = f"  待ち {q} 件" if q else ""
        from rich.cells import cell_len
        cost = [cell_len(f" {k}  {label} ") for k, label, _ in keys]
        room = self.size.width - cell_len(tail)
        keep: set[int] = set()
        used = 0
        for i in sorted(range(len(keys)), key=lambda i: (keys[i][2], i)):
            if used + cost[i] <= room:
                keep.add(i)
                used += cost[i]
        t = Text(no_wrap=True, overflow="crop")
        for i, (k, label, _) in enumerate(keys):
            if i not in keep:
                continue
            t.append(f" {k} ", style=f"bold {T.TEXT} on {T.CHIP_BG}")
            t.append(f" {label} ", style=T.META)
        if tail:
            t.append(tail, style=f"bold {T.ALERT}")
        return t


# --------------------------------------------------------------------------
# 小窓
# --------------------------------------------------------------------------

class AskModal(ModalScreen[str]):
    """本体の問い（AskUserQuestion）。番号か自由な文。Esc は空で返す（本体に判断させる）。"""

    BINDINGS = [Binding("escape", "dismiss_empty", "閉じる")]
    DEFAULT_CSS = """
    AskModal { align: center middle; background: #0d0a0a 45%; }
    AskModal > Vertical { width: 80%; max-width: 110; height: auto; max-height: 80%;
        border: round #d0463c; background: #140e0e; padding: 1 2; }
    AskModal > Vertical > VerticalScroll { height: auto; max-height: 24; }
    AskModal Input { width: 1fr; margin-top: 1; border: tall #6E1F19; background: #0d0a0a; }
    AskModal Input:focus { border: tall #d0463c; }
    """

    def __init__(self, question: str, options: list[str], note: str = "") -> None:
        super().__init__()
        self.question = question
        self.options = options
        self.note = note or "番号か自由な文。Esc で空のまま返す（本体に任せる）"

    def compose(self) -> ComposeResult:
        t = Text()
        t.append("? ", style=f"bold {T.ALERT}")
        t.append(self.question or "（問い）", style=f"bold {T.TEXT}")
        for i, o in enumerate(self.options, 1):
            t.append(f"\n  {i}. ", style=f"bold {T.ALERT}")
            t.append(o, style=T.TEXT)
        if self.options:
            t.append("\n\n" + self.note, style=T.NOTE)
        with Vertical() as v:
            v.border_title = f"[{T.ALERT}]問い[/]"
            yield VerticalScroll(Static(t))
            yield Input(placeholder="答え", id="ans")

    def on_mount(self) -> None:
        self.query_one("#ans", Input).focus()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value)

    def action_dismiss_empty(self) -> None:
        self.dismiss("")


class ModelPickModal(ModalScreen[Optional["models.Choice"]]):
    """F5 のモデル選び（2026-10-19 オーナー「Windows や Linux 版のアプリから API のモデルを自由に切り替えられるように」）。
    Claude API（中継）と推論機のモデルを 1 つの一覧で。Enter で選ぶ・Esc で閉じる。切り替えは呼び手（slash.switch_choice）。"""

    BINDINGS = [Binding("escape", "close", "閉じる")]
    DEFAULT_CSS = """
    ModelPickModal { align: center middle; background: #0d0a0a 45%; }
    ModelPickModal > Vertical { width: 80%; max-width: 110; height: auto; max-height: 85%;
        border: round #d0463c; background: #140e0e; padding: 1 2; }
    ModelPickModal OptionList { height: auto; max-height: 26; border: none; background: #140e0e; }
    """

    def __init__(self, lst: "models.Listing") -> None:
        super().__init__()
        self.lst = lst

    def compose(self) -> ComposeResult:
        lst = self.lst
        opts: list[Any] = []
        groups = [("claude", "Claude API" + (f"  {lst.spent}" if lst.spent else "")),
                  ("server", f"推論機  {lst.server_url}")]
        for kind, title in groups:
            rows = [(i, c) for i, c in enumerate(lst.choices) if c.kind == kind]
            if not rows:
                continue
            if opts:
                opts.append(None)
            opts.append(Option(Text(title, style=f"bold {T.META}"), disabled=True))
            for i, c in rows:
                t = Text()
                t.append(c.label, style=f"bold {T.TEXT}" if c.ok else T.NOTE)
                if c.detail:
                    t.append(f"  {c.detail}", style=T.NOTE)
                m = slash._mark(lst, c)
                if m:
                    t.append(f"  {m}", style=T.GOOD if c.kind == lst.where else T.META)
                opts.append(Option(t, id=f"c{i}", disabled=not c.ok))
        foot = list(lst.notes)
        if lst.stopping:
            foot.insert(0, "推論機を止めている途中です")
        elif lst.switching:
            foot.insert(0, f"推論機は {lst.switching} へ入れ替えている途中です")
        elif lst.server_stopped and lst.lease_stopped:
            foot.insert(0, f"推論機は{lst.stopped_label()}")
        elif lst.server_stopped:
            foot.insert(0, f"推論機は{models.STOPPED_LABEL}・選ぶと起動します")
        if lst.stoppable and not lst.server_stopped and not lst.stopping:
            foot.append("推論機を止めて GPU を空けるのは /model stop")
        with Vertical() as v:
            v.border_title = f"[{T.ALERT}]F5[/] モデル"
            v.border_subtitle = "Enter で切り替え・Esc で閉じる"
            yield OptionList(*opts, id="mp")
            if foot:
                yield Static(Text("\n".join(foot), style=T.NOTE))

    def on_mount(self) -> None:
        ol = self.query_one("#mp", OptionList)
        ol.focus()
        cur = self.lst.current()
        if cur is not None:
            try:
                ol.highlighted = ol.get_option_index(f"c{self.lst.choices.index(cur)}")
            except Exception:
                pass

    def on_option_list_option_selected(self, ev: OptionList.OptionSelected) -> None:
        oid = str(ev.option.id or "")
        if oid.startswith("c") and oid[1:].isdigit():
            self.dismiss(self.lst.choices[int(oid[1:])])

    def action_close(self) -> None:
        self.dismiss(None)


class TextModal(ModalScreen[None]):
    """読むだけの小窓（道具の中身・ヘルプ）。Esc / q で閉じる。"""

    BINDINGS = [Binding("escape,q", "close", "閉じる")]
    DEFAULT_CSS = """
    TextModal { align: center middle; background: #0d0a0a 45%; }
    TextModal > VerticalScroll { width: 90%; height: 85%; border: round #B3362E; background: #140e0e; padding: 0 1; }
    """

    def __init__(self, title: str, body: Any) -> None:
        super().__init__()
        self.title_ = title
        self.body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll() as v:
            v.border_title = self.title_
            v.border_subtitle = "Esc で閉じる"
            yield Static(self.body)

    def action_close(self) -> None:
        self.dismiss(None)


class _TuiKeys:
    """run_turn から見た「キーの見張り」。Ctrl-C は画面が数え、最初の 1 回はその場で cancel を書く。"""

    def __init__(self, on_first: Callable[[], None]) -> None:
        self._on_first = on_first
        self._n = 0
        self._fired = False
        self._lock = threading.Lock()
        self.reader_error: str | None = None

    def start(self) -> None:
        return

    def stop(self) -> None:
        return

    def alive(self) -> bool:
        return True

    def press(self) -> None:
        with self._lock:
            self._n += 1
            first = not self._fired
            self._fired = True
        if first:
            try:
                self._on_first()
            except Exception:
                pass

    def take(self) -> int:
        with self._lock:
            n, self._n = self._n, 0
        return n


# --------------------------------------------------------------------------
# 本体
# --------------------------------------------------------------------------

class BlackwallTui(App):
    TITLE = "L.A.P.S"
    ENABLE_COMMAND_PALETTE = False
    CSS = f"""
    Screen {{ background: {BG}; color: {T.TEXT}; layout: vertical; }}
    AskModal, TextModal {{ background: {BG} 45%; }}
    .box {{ border: round {BOX}; border-title-color: {T.TEXT}; border-subtitle-color: {T.NOTE};
            border-title-align: left; border-subtitle-align: right; padding: 0 1; background: {BG}; }}
    #top {{ height: 11; }}
    #srv {{ width: 1fr; }}
    #agent {{ width: 1fr; }}
    #mid {{ height: 1fr; }}
    #conv {{ width: 2fr; border: round {BOX}; border-title-color: {T.TEXT}; border-subtitle-color: {T.NOTE};
             border-subtitle-align: right; background: {BG}; }}
    #conv:focus-within {{ border: round {BOX_FOCUS}; }}
    #log {{ background: {BG}; overflow-x: hidden; scrollbar-size-vertical: 1; scrollbar-color: {BOX}; scrollbar-color-hover: {T.WALL};
            scrollbar-background: {BG}; padding: 0 1; }}
    #toolsbox {{ width: 1fr; max-width: 64; border: round {BOX}; border-title-color: {T.TEXT};
                 border-subtitle-color: {T.NOTE}; border-subtitle-align: right; background: {BG}; }}
    #toolsbox:focus-within {{ border: round {BOX_FOCUS}; }}
    #tools {{ background: {BG}; scrollbar-size-vertical: 1; scrollbar-color: {BOX}; }}
    #tools > .datatable--header {{ background: {BG}; color: {T.META}; text-style: bold; }}
    #tools > .datatable--cursor {{ background: {T.CHIP_BG}; color: {T.TEXT}; }}
    #tools > .datatable--hover {{ background: #1e1312; }}
    #inbox {{ height: auto; max-height: 10; border: round {BOX}; border-title-color: {T.ALERT};
              border-subtitle-color: {T.NOTE}; border-subtitle-align: right; background: {BG}; }}
    #inbox:focus-within {{ border: round {BOX_FOCUS}; }}
    #inp {{ height: auto; min-height: 1; max-height: 8; background: {BG}; border: none; padding: 0 1; }}
    #inp .text-area--cursor-line {{ background: {BG}; }}
    """

    BINDINGS = [
        Binding("ctrl+c", "interrupt", "中断", priority=True, show=False),
        Binding("escape", "escape", "中断", show=False),
        Binding("ctrl+t", "force_inject", "強制割り込み", priority=True, show=False),
        Binding("ctrl+q", "quit_app", "終了", priority=True, show=False),
        Binding("ctrl+l", "clear_view", "消去", priority=True, show=False),
        Binding("f1", "help", "ヘルプ", priority=True, show=False),
        Binding("f2", "toggle('srv')", "server", priority=True, show=False),
        Binding("f3", "toggle('agent')", "agent", priority=True, show=False),
        Binding("f4", "toggle('toolsbox')", "tools", priority=True, show=False),
        Binding("f5", "pick_model", "モデル", priority=True, show=False),
        Binding("pageup", "scroll_log(-1)", "遡る", priority=True, show=False),
        Binding("pagedown", "scroll_log(1)", "進む", priority=True, show=False),
    ]

    def __init__(self, ctx: Any, *, cfg: dict, config_path: Any, resume: str | None = None) -> None:
        super().__init__()
        # ★本文の読み幅（prose_width・既定 96）は行の形のための物（200 桁の端末で本文が板になるのを防ぐ）。全画面では
        #   会話の枠がもう幅を区切っているので、枠いっぱいに使う（2026-10-12 オーナー「会話の所が左半分しか使われてない」）。
        #   環境変数で指定してあればそれに従う（config.resolve は 環境変数 > 設定ファイル）。
        if not os.environ.get("LAPS_PROSE_WIDTH", "").strip():
            _envcompat.setenv("LAPS_PROSE_WIDTH", "0")
        self.ctx = ctx
        self.cfg = cfg
        self.tel = Telemetry()
        self.ready = False
        self.turn_running = False
        self.slash_running = False
        self.queue: collections.deque[str] = collections.deque()
        self._keys: _TuiKeys | None = None
        self._pending_asks: list[threading.Event] = []
        self._main_tid = threading.get_ident()
        self._tool_rows: dict[Any, Any] = {}
        self._tool_entries: dict[Any, ToolEntry] = {}
        self._tool_order: list[Any] = []
        self._child_stats: dict[Any, dict] = {}       # 走っている Explore・Task の行 → 子の道具の数（child_step）
        # ★行の鍵は呼び出しごとに一意に（2026-10-30 オーナー「一回タスクが終わった後にもう一度タスクを依頼すると右側の Tools に
        #   次のタスクで実行した Tools が表示されなくなる」）: 本体の呼び出しの名札は call_<手番>_<番号> で走行ごとに 0 から数え直す。
        #   旧はそれを表の行の鍵にしていたので、2 回目の走行の最初の行が鍵の重複で足せず（例外は握り潰していた）、以後も出なかった。
        self._tool_keys: dict[str, str] = {}          # 本体の名札 → いまの行の鍵
        self._tool_seq = 0
        self._resume = resume
        self._closing = False
        self._user_hidden: set[str] = set()
        # ★推論機の数（bw-metrics）が一度も取れない間は server の箱をたたみ、agent の箱に横幅を譲る（_auto_srv）。
        #   F2 で開いたら（_srv_pinned）数が取れなくても出したまま。
        self._srv_pinned = False
        self._srv_auto_hidden = False
        self.console_out = Console(file=_PaneFile(self._post_ansi), force_terminal=True, color_system="truecolor",
                                   width=100, height=40, highlight=False, emoji=False, soft_wrap=False,
                                   legacy_windows=False)
        self.renderer = TuiRenderer(self.console_out, config_path=config_path, on_event_hook=self._on_event)
        ctx.console = self.console_out
        ctx.renderer = self.renderer
        ctx.interactive = False             # 走行中の告知（行の形の 1 行）は出さない＝状態は箱が出す
        ctx.keys_factory = self._make_keys
        ctx.ask_fn = self._ask_blocking
        ctx.confirm_fn = self._confirm_blocking
        ctx.wave_factory = appmod.make_wave_factory(self.console_out, bool(cfg.get("calm")))
        self.history = self._load_history()
        self.poller = Poller(self.tel, lambda: str(self.settings().get("backend") or ""),
                             lambda: str((self.ctx.cfg or {}).get("claude_backend") or ""))

    # -- 組み立て --------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield HeaderBar(self)
        with Horizontal(id="top"):
            yield ServerBox(self, id="srv", num=2, title="server")
            yield AgentBox(self, id="agent", num=3, title="agent")
        with Horizontal(id="mid"):
            with Vertical(id="conv"):
                yield RichLog(id="log", wrap=False, markup=False, highlight=False, max_lines=8000,
                              min_width=20, auto_scroll=True)
                yield LivePane(self)
            with Vertical(id="toolsbox"):
                yield ToolsTable(id="tools", cursor_type="row", zebra_stripes=False, show_cursor=True)
        with Vertical(id="inbox"):
            yield PromptArea(self.history, id="inp", placeholder=PLACEHOLDER)
        yield FooterBar(self)

    def on_mount(self) -> None:
        self._main_tid = threading.get_ident()
        conv = self.query_one("#conv")
        conv.border_title = f"[b {T.ALERT}]1[/] [b]会話[/b]"
        tb = self.query_one("#toolsbox")
        tb.border_title = f"[b {T.ALERT}]4[/] [b]tools[/b]"
        tb.border_subtitle = "Enter で中身"
        self.query_one("#inbox").border_title = "❯ 依頼"
        self.query_one("#inbox").border_subtitle = f"Enter 送信{SEP}Alt+Enter 改行"
        tools = self.query_one("#tools", ToolsTable)
        tools.add_column(" ", key="mark", width=1)
        tools.add_column("道具", key="name", width=9)
        tools.add_column("対象", key="target", width=24)
        tools.add_column("秒", key="secs", width=5)
        self.query_one("#live", LivePane).display = False
        self._fit_console()
        self.query_one("#inp", PromptArea).focus()
        self.set_interval(0.1, self._tick_fast)
        self.set_interval(0.25, self._tick_agent)
        self.set_interval(0.5, self._tick_slow)
        self.set_interval(1.0, self._tick_second)
        self.poller.start()
        threading.Thread(target=self._boot, daemon=True, name="tui-boot").start()

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self._fit_console)
        w, h = event.size.width, event.size.height
        top = self.query_one("#top")
        top.styles.height = 8 if h < 30 else 11 if h < 48 else 14
        self.query_one("#toolsbox").display = w >= 96 and "toolsbox" not in self._user_hidden

    def _fit_console(self) -> None:
        log = self.query_one("#log", RichLog)
        w = log.scrollable_content_region.width or log.size.width
        if w >= 10:
            self.console_out.width = max(20, w - 1)
        self.console_out.height = max(10, self.size.height)
        tools = self.query_one("#tools", ToolsTable)
        tw = tools.size.width
        if tw > 20:
            col = tools.columns.get("target")  # type: ignore[call-overload]
            if col is not None:
                col.width = max(8, tw - 1 - 9 - 5 - 4 * 2 - 1)
                tools.refresh()

    # -- 設定・履歴 ------------------------------------------------------

    def settings(self) -> dict:
        try:
            s = dict(slash._settings(self.ctx) or {})
        except Exception:
            s = {}
        for k in ("backend", "model", "policy", "ethics"):
            s.setdefault(k, self.cfg.get(k))
        return s

    def _load_history(self) -> list[str]:
        try:
            from prompt_toolkit.history import FileHistory
            hp = appmod._history_path(self.ctx)
            self._fh = FileHistory(str(hp))
            items = list(self._fh.load_history_strings())      # 新しい → 古い
            return list(reversed(items[:500]))
        except Exception:
            self._fh = None
            return []

    def _remember(self, text: str) -> None:
        if not text.strip():
            return
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        try:
            if self._fh is not None:
                self._fh.store_string(text)
        except Exception:
            pass

    # -- 会話の枠へ書く ----------------------------------------------------

    def _post_ansi(self, s: str) -> None:
        if self._closing:
            return
        if threading.get_ident() == self._main_tid:
            self._write_ansi(s)
            return
        try:
            self.call_from_thread(self._write_ansi, s)
        except Exception:
            pass

    def _write_ansi(self, s: str) -> None:
        body = s[:-1] if s.endswith("\n") else s
        t = Text.from_ansi(body) if body else Text("")
        try:
            self.query_one("#log", RichLog).write(t, scroll_end=None)
        except Exception:
            pass

    def say(self, text: str, style: str = T.META) -> None:
        self.console_out.print(Text(text, style=style))

    # -- 事象（作業スレッドから） ------------------------------------------------

    def _on_event(self, ev: dict) -> None:
        self.tel.feed(ev)
        et = str(ev.get("type") or "")
        if et in ("tool_call", "tool_result"):
            try:
                self.call_from_thread(self._tool_event, dict(ev))
            except Exception:
                pass
        elif et == "notice" and str(ev.get("kind") or "") == "subagent":
            try:
                self.call_from_thread(self._child_event, str(ev.get("message") or ""))
            except Exception:
                pass

    def _child_event(self, msg: str) -> None:
        """子の道具の中継を、走っている Explore・Task の行の「対象」に数えて出す（child_step の注記）。"""
        call = next((c for c in reversed(self._tool_order)
                     if (e := self._tool_entries.get(c)) is not None and not e.done and e.name in CHILD_TOOLS), None)
        if call is None:
            return
        st = self._child_stats.setdefault(call, {})
        if not child_step(st, msg):
            return
        e = self._tool_entries[call]
        target = tool_target(e.name, e.input)
        if privacy_on():
            target = mask_private(target)
        try:
            self.query_one("#tools", ToolsTable).update_cell(
                str(call), "target", Text(child_label(st, target), style=T.META, no_wrap=True, overflow="ellipsis"))
        except Exception:
            pass

    def _tool_event(self, ev: dict) -> None:
        tools = self.query_one("#tools", ToolsTable)
        et = ev.get("type")
        call = str(ev.get("call") or "")
        if et == "tool_call":
            self._tool_seq += 1
            key = f"{call or '_'}#{self._tool_seq}"
            if call:
                self._tool_keys[call] = key
            call = key
            name = str(ev.get("name") or "?")
            inp = ev.get("input") if isinstance(ev.get("input"), dict) else {}
            e = ToolEntry(call=call, name=name, input=inp, read=read_kind(name, inp))
            self._tool_entries[call] = e
            self._tool_order.append(call)
            at_end = tools.row_count == 0 or tools.cursor_row >= tools.row_count - 1
            target = tool_target(name, inp)
            if privacy_on():
                target = mask_private(target)
            tools.add_row(Text("▸", style=T.ALERT), Text(name, style=f"bold {T.TEXT}"),
                          Text(target, style=T.META, no_wrap=True, overflow="ellipsis"),
                          Text("-", style=T.NOTE, justify="right"), key=str(call))
            if len(self._tool_order) > TOOLS_KEEP:
                old = self._tool_order.pop(0)
                self._tool_entries.pop(old, None)
                self._child_stats.pop(old, None)
                try:
                    tools.remove_row(str(old))
                except Exception:
                    pass
            if at_end:
                tools.move_cursor(row=tools.row_count - 1)
            self._tools_title()
            return
        call = self._tool_keys.get(call, call)
        e = self._tool_entries.get(call)
        if e is None:
            for c in reversed(self._tool_order):
                x = self._tool_entries.get(c)
                if x is not None and not x.done and x.name == ev.get("name"):
                    e, call = x, c
                    break
        if e is None:
            return
        e.done = True
        e.ok = bool(ev.get("ok")) if "ok" in ev else True
        e.result = str(ev.get("result") or "")
        e.summary = ev.get("summary") if isinstance(ev.get("summary"), dict) else None
        e.secs = round(time.time() - e.t0, 1)
        try:
            tools.update_cell(str(call), "mark", Text(T.MARK_BAD if e.bad else T.MARK_OK,
                                                      style=T.S_MARK_BAD if e.bad else T.S_MARK_OK))
            tools.update_cell(str(call), "secs", Text(f"{e.secs:.1f}", style=T.NOTE, justify="right"))
        except Exception:
            pass
        self._tools_title()

    def _tools_title(self) -> None:
        n = len(self._tool_order)
        bad = sum(1 for c in self._tool_order if (e := self._tool_entries.get(c)) and e.done and e.bad)
        run = sum(1 for c in self._tool_order if (e := self._tool_entries.get(c)) and not e.done)
        tb = self.query_one("#toolsbox")
        extra = f"{n} 回" + (f"{SEP}実行中 {run}" if run else "") + (f"{SEP}✗ {bad}" if bad else "")
        tb.border_title = f"[b {T.ALERT}]4[/] [b]tools[/b]  [{T.META}]{extra}[/]"

    def on_data_table_row_selected(self, ev: DataTable.RowSelected) -> None:
        e = self._tool_entries.get(ev.row_key.value) if ev.row_key is not None else None
        if e is None:
            for c, x in self._tool_entries.items():
                if str(c) == str(ev.row_key.value):
                    e = x
                    break
        if e is None:
            return
        body = Text()
        body.append(f"{e.name}", style=f"bold {T.TEXT}")
        st = ("実行中" if not e.done else "失敗" if e.bad else "成功")
        body.append(f"  {st}", style=T.S_BAD if e.bad else T.S_OK if e.done else T.META)
        if e.secs is not None:
            body.append(f"  {e.secs:.1f} 秒", style=T.NOTE)
        body.append("\n\n入力\n", style=f"bold {T.META}")
        inp = json.dumps(e.input, ensure_ascii=False, indent=2)
        res = e.result
        if privacy_on():
            inp, res = mask_private(inp), mask_private(res)
        body.append(inp[:20000] + "\n", style=T.TEXT)
        body.append("\n結果\n", style=f"bold {T.META}")
        body.append((res[:60000] or "（まだありません）"), style=T.TEXT if not e.bad else T.S_BAD_BODY)
        self.push_screen(TextModal(f"[{T.ALERT}]道具[/] {e.name}", body))

    # -- 時計 ----------------------------------------------------------

    def _tick_fast(self) -> None:
        lp = self.query_one("#live", LivePane)
        show = self.turn_running and self.renderer._open
        if lp.display != show:
            lp.display = show
        if show:
            lp.refresh(layout=True)

    def _tick_agent(self) -> None:
        try:
            box = self.query_one("#agent", Box)
            box.apply_titles()
            box.refresh()
        except Exception:
            pass

    def _tick_slow(self) -> None:
        now = time.time()
        tools = self.query_one("#tools", ToolsTable)
        for c in self._tool_order[-50:]:
            e = self._tool_entries.get(c)
            if e is not None and not e.done:
                try:
                    tools.update_cell(str(c), "secs", Text(f"{now - e.t0:.0f}s", style=T.META, justify="right"))
                except Exception:
                    pass
        try:
            self._auto_srv()
        except Exception:
            pass
        for wid in ("#srv",):
            try:
                box = self.query_one(wid, Box)
                box.apply_titles()
                box.refresh()
            except Exception:
                pass
        try:
            self.query_one("#ftr").refresh()
        except Exception:
            pass
        inbox = self.query_one("#inbox")
        text = self.query_one("#inp", PromptArea).text
        area = self.query_one("#inp", PromptArea)
        ph = "割り込む文（Enter で区切りに渡す・Esc か ^T で今すぐ）" if self.turn_running else PLACEHOLDER
        if str(area.placeholder) != ph:
            area.placeholder = ph
        if self.turn_running:
            inbox.border_title = f"[{T.ALERT}]↳ 割り込み[/]"
            inbox.border_subtitle = f"Enter で区切りに渡す{SEP}Esc・^T で今すぐ{SEP}^C で中断"
        elif text.startswith("/"):
            name = text[1:].split(None, 1)[0].lower() if text[1:].strip() else ""
            cands = [c for c in slash.COMMANDS if c.startswith(name)][:8]
            inbox.border_title = "❯ コマンド"
            inbox.border_subtitle = "  ".join("/" + c for c in cands) if cands else "知らないコマンド（/help）"
        else:
            inbox.border_title = f"[{T.ALERT}]❯[/] 依頼"
            inbox.border_subtitle = f"Enter 送信{SEP}Alt+Enter 改行"

    def _auto_srv(self) -> None:
        """推論機の数が一度も取れない間は server の箱をたたむ（頭の帯に「server 不通 F2」とだけ残す）。
        ★2026-10-27 SNS の撮影: 中身は「bw-metrics（:8742）が要ります」という、利用者がその場で直せない話なのに、
          箱が画面のおよそ 1/4 を占め続けた。数が取れるようになったら自然に戻る。F2 で開けば出したままにする。"""
        if "srv" in self._user_hidden:
            return
        tel = self.tel
        with tel.lock:
            down = (not tel.claude) and tel.srv_ok is False and not tel.last
        want = (not down) or self._srv_pinned
        srv = self.query_one("#srv")
        if srv.display != want:
            srv.display = want
            self._srv_auto_hidden = not want
            self.query_one("#top").display = srv.display or self.query_one("#agent").display
            self.query_one("#hdr").refresh()                # 頭の帯の「server 不通 F2」をその場で（1 秒の描き直しを待たない）
            self.call_after_refresh(self._fit_console)

    def _tick_second(self) -> None:
        try:
            self.query_one("#hdr").refresh()
        except Exception:
            pass

    # -- 起動 ----------------------------------------------------------

    def _boot(self) -> None:
        ctx = self.ctx
        # ★銘の大小は会話の枠の幅で選ぶ（2026-10-21）。起動の直後は枠の幅がまだ決まっておらず、仮の 100 桁で大きな銘を選んで
        #   72 桁の枠で「BLACKWF」と右が欠けていた。枠が並ぶのを少し待ってから幅を合わせる。
        for _ in range(40):
            try:
                if self.call_from_thread(lambda: self.query_one("#log", RichLog).size.width) > 0:
                    self.call_from_thread(self._fit_console)
                    break
            except Exception:
                break
            time.sleep(0.05)
        try:
            from rich.align import Align
            from .banner import banner
            self.console_out.print(Align.center(banner(self.console_out.width)))
            self.console_out.print()
        except Exception:
            pass
        try:
            ctx.client.prepare_parts()
        except Exception:
            pass
        try:
            ctx.client.start()
        except Exception as e:
            self.console_out.print(Text(f"{T.MARK_BAD} 本体を起動できません: {e}", style=T.S_BAD))
            self.console_out.print(Text("  → --debug FILE で全文を取ってください。^Q で終了します。", style=T.S_META))
            return
        from . import config as _config
        if _config.migrated:
            self.say("設定を今の既定に合わせました: " + " / ".join(_config.migrated) + "（戻すなら /policy と /ethics）")
        if self._resume:
            self.say(f"セッション {self._resume} の続きから")
        appmod._startup_probe(ctx)
        if appmod.is_first_run(ctx):
            self.say("→ やりたいことを、そのまま書いてください。コマンドの一覧は /help、キーは F1。")
        self.ready = True
        try:
            self.call_from_thread(self._drain_queue)
        except Exception:
            pass

    # -- 入力 ----------------------------------------------------------

    def on_prompt_area_submitted(self, msg: PromptArea.Submitted) -> None:
        text = msg.text.strip()
        area = msg.area
        if not text:
            return
        area.load_text("")
        area.reset_history_pos()
        self._remember(text)
        if self.turn_running and not text.startswith("/"):
            self._inject(text, force=False)
            return
        if self.turn_running or self.slash_running or not self.ready:
            self.queue.append(text)
            self.say(f"（待ち {len(self.queue)} 件: 今の作業が終わったら送ります）", T.NOTE)
            return
        self._start(text)

    def _start(self, text: str) -> None:
        if text.startswith("/"):
            self.slash_running = True
            threading.Thread(target=self._slash_worker, args=(text,), daemon=True, name="tui-slash").start()
            return
        self.turn_running = True
        self.tel.turn_started()
        threading.Thread(target=self._turn_worker, args=(text,), daemon=True, name="tui-turn").start()

    def _drain_queue(self) -> None:
        if self.queue and not self.turn_running and not self.slash_running and self.ready:
            self._start(self.queue.popleft())

    def _turn_worker(self, text: str) -> None:
        try:
            appmod.run_turn(self.ctx, text)
        except Exception as e:
            try:
                appmod._report_internal_error(self.ctx, e)
            except Exception:
                pass
        finally:
            try:
                self.call_from_thread(self._turn_done)
            except Exception:
                pass

    def _turn_done(self) -> None:
        self.turn_running = False
        self._keys = None
        if self.ctx.exit_requested:
            self.exit()
            return
        self._drain_queue()

    def _slash_worker(self, text: str) -> None:
        name = text[1:].split(None, 1)[0].lower() if text[1:].strip() else ""
        try:
            slash.dispatch(self.ctx, text)
            if name == "help" or (name and "help".startswith(name) and len(name) >= 2):
                self.say("全画面のキーは F1 で出ます。", T.NOTE)
        except Exception as e:
            try:
                appmod._report_internal_error(self.ctx, e)
            except Exception:
                pass
        finally:
            try:
                self.call_from_thread(self._slash_done, name)
            except Exception:
                pass

    def _slash_done(self, name: str) -> None:
        self.slash_running = False
        if name == "clear":
            self.tel.reset_conversation()
            tools = self.query_one("#tools", ToolsTable)
            tools.clear()
            self._tool_rows.clear()
            self._tool_entries.clear()
            self._tool_order.clear()
            self._child_stats.clear()
            self._tool_keys.clear()
            self._tools_title()
        if self.ctx.exit_requested:
            self.exit()
            return
        self._drain_queue()

    def _inject(self, text: str, *, force: bool) -> None:
        rid = self.ctx.running_rid
        if not rid:
            self.queue.append(text)
            return
        try:
            self.ctx.client.inject(rid, text, force=force)
        except Exception as e:
            self.say(f"{T.MARK_BAD} 割り込みを送れません: {e}", T.S_BAD)

    # -- run_turn との約束 -------------------------------------------------

    def _make_keys(self, on_first: Callable[[], None]) -> _TuiKeys:
        self._keys = _TuiKeys(on_first)
        return self._keys

    def _ask_blocking(self, prompt: str) -> str:
        ev = next((e for e in reversed(self.ctx.events) if e.get("type") == "ask"), {})
        q = str(ev.get("question") or "")
        opts = [str(o) for o in (ev.get("options") or [])]
        box: dict[str, str] = {"v": ""}
        done = threading.Event()
        self._pending_asks.append(done)

        def _got(v: str | None) -> None:
            box["v"] = v or ""
            done.set()

        try:
            self.call_from_thread(self.push_screen, AskModal(q, opts), _got)
        except Exception:
            return ""
        done.wait()
        try:
            self._pending_asks.remove(done)
        except ValueError:
            pass
        return box["v"]

    def _confirm_blocking(self, question: str) -> bool:
        """y/N の確かめ（2026-11-03・/model stop）。AskModal に「はい・いいえ」を出す。Esc・空はいいえ。
        ★_ask_blocking は直近の ask の問いを出す作り（本体の問い用）なので、こちらは問いを直に渡す。"""
        if threading.get_ident() == getattr(self, "_main_tid", None):
            return False                    # 画面のスレッドで待つと固まる（slash は別のスレッドで走る）
        box: dict[str, str] = {"v": ""}
        done = threading.Event()
        self._pending_asks.append(done)

        def _got(v: str | None) -> None:
            box["v"] = v or ""
            done.set()

        try:
            self.call_from_thread(self.push_screen, AskModal(question, ["はい", "いいえ"], "番号で（Esc・空はいいえ）"), _got)
        except Exception:
            return False
        done.wait()
        try:
            self._pending_asks.remove(done)
        except ValueError:
            pass
        return box["v"].strip().lower() in ("1", "はい", "y", "yes")

    # -- 操作 ----------------------------------------------------------

    def action_interrupt(self) -> None:
        try:
            sel = self.screen.get_selected_text()
        except Exception:
            sel = None
        if sel:
            self.copy_to_clipboard(sel)
            self.notify("写しました", timeout=1.5)
            return
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss("")  # type: ignore[arg-type]
            return
        if self.turn_running and self._keys is not None:
            self._keys.press()
            return
        self.notify("終了は ^Q か /exit", timeout=2)

    def action_escape(self) -> None:
        # ★欄に文字があれば Esc は強制割り込み（止めてその一言をすぐ渡す・UI の onEscape と同じ・2026-10-19）。
        if self.turn_running:
            try:
                if self.query_one("#inp", PromptArea).text.strip():
                    self.action_force_inject()
                    return
            except Exception:
                pass
        if self.turn_running and self._keys is not None:
            self._keys.press()

    def action_force_inject(self) -> None:
        if not self.turn_running:
            return
        area = self.query_one("#inp", PromptArea)
        text = area.text.strip()
        if not text:
            self.notify("割り込む文を書いてから ^T", timeout=2)
            return
        area.load_text("")
        self._remember(text)
        self._inject(text, force=True)

    def action_quit_app(self) -> None:
        if self.turn_running and self._keys is not None:
            self._keys.press()
        self.exit()

    def action_clear_view(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_toggle(self, wid: str) -> None:
        w = self.query_one(f"#{wid}")
        w.display = not w.display
        (self._user_hidden.discard if w.display else self._user_hidden.add)(wid)
        if wid == "srv":
            self._srv_pinned = bool(w.display)   # 自分で開いたら、数が取れなくても出したまま（_auto_srv）
            self._srv_auto_hidden = False
        top = self.query_one("#top")
        top.display = self.query_one("#srv").display or self.query_one("#agent").display
        self.call_after_refresh(self._fit_console)

    def action_scroll_log(self, d: int) -> None:
        log = self.query_one("#log", RichLog)
        log.scroll_to(y=log.scroll_y + d * max(3, log.size.height - 2), animate=False)

    # -- F5 モデル（Claude API と推論機）------------------------------------
    # ★一覧と切り替えは HTTP（推論機の一覧は systemctl を何度か呼ぶので数秒かかる）＝画面のスレッドで待たない。

    def action_pick_model(self) -> None:
        if self.turn_running or self.ctx.running_rid:
            self.say("走行中はモデルを切り替えられません（^C で止めてから）", T.NOTE)
            return
        threading.Thread(target=self._pick_worker, daemon=True, name="tui-models").start()

    def _pick_worker(self) -> None:
        try:
            lst = slash.model_listing(self.ctx)
        except Exception as e:
            self.say(f"モデルの一覧を読めません: {type(e).__name__}: {e}", T.ALERT)
            return
        try:
            self.call_from_thread(self._pick_show, lst)
        except Exception:
            pass

    def _pick_show(self, lst: "models.Listing") -> None:
        if not lst.choices:
            self.say("選べるモデルがありません。" + " ".join(lst.notes), T.ALERT)
            return
        self.push_screen(ModelPickModal(lst), lambda c: self._pick_done(lst, c))

    def _pick_done(self, lst: "models.Listing", c: Optional["models.Choice"]) -> None:
        if c is None:
            return
        threading.Thread(target=self._pick_switch, args=(lst, c), daemon=True, name="tui-switch").start()

    def _pick_switch(self, lst: "models.Listing", c: "models.Choice") -> None:
        try:
            ok, msg = slash.switch_choice(self.ctx, lst, c)
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
        self.say(msg, T.GOOD if ok else T.ALERT)
        self.poller.poke()

    def action_help(self) -> None:
        tb = Table.grid(padding=(0, 2))
        tb.add_column(style=f"bold {T.ALERT}", no_wrap=True)
        tb.add_column(style=T.TEXT)
        rows = [
            ("Enter", "送る（走行中は割り込み＝区切りで本体に渡す）"),
            ("Alt+Enter / Ctrl+J", "改行"),
            ("上 / 下", "履歴（1 行目・最終行で）"),
            ("^C / Esc", "中断（1 回目）・もう一度 ^C で終了。文字を選んでいれば写す"),
            ("^T", "強制割り込み（いまの生成と Bash を止めて渡す）"),
            ("PgUp / PgDn", "会話を遡る・進む（ホイールも可）"),
            ("Tab / Shift+Tab", "依頼の欄・会話・道具の一覧を移る"),
            ("Enter（道具の一覧で）", "その道具の入力と結果の全文"),
            ("^L", "会話の枠を消す（本体の会話は続く・新しい会話は /clear）"),
            ("F2 / F3 / F4", "server・agent・tools の箱を出す・隠す"),
            ("F5", "モデルを選ぶ（Claude API と推論機を 1 つの一覧で・送り先も合わせて変わる）。止まっている推論機は選ぶと起動"),
            ("^Q", "終了"),
            ("", ""),
            ("server", "推論機の GPU（使用率・温度・電力・VRAM）（推論機 の bw-metrics :8742）。Claude API のときはモデルと今月の額"),
            ("agent", "生成の速さ（推論機の /slots・語/秒）・文脈（| は要約を始める量）・道具・要約"),
            ("行の形", "従来の画面は blackwall --ui classic（設定 ui = \"classic\"）"),
        ]
        for k, v in rows:
            tb.add_row(k, v)
        cmds = Table.grid(padding=(0, 2))
        cmds.add_column(style=f"bold {T.TEXT}", no_wrap=True)
        cmds.add_column(style=T.META)
        for name, c in sorted(slash.COMMANDS.items()):
            cmds.add_row("/" + name, str(getattr(c, "help", "") or ""))
        from rich.console import Group
        self.push_screen(TextModal(f"[{T.ALERT}]F1[/] キーとコマンド",
                                   Group(tb, Text(""), Text("コマンド", style=f"bold {T.META}"), cmds)))

    async def action_quit(self) -> None:
        self.action_quit_app()

    def on_unmount(self) -> None:
        self._closing = True
        self.poller.stop.set()
        for ev in list(self._pending_asks):
            ev.set()


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def usable(console_ok: bool = True) -> str:
    """全画面にできないなら理由を返す（空なら使える）。"""
    import sys
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "端末ではない"
    if os.environ.get("TERM", "").strip().lower() in ("dumb", ""):
        return "TERM が dumb"
    return ""


def run(ctx: Any, *, cfg: dict, config_path: Any, resume: str | None = None) -> int:
    """全画面で回す。戻り値は終了コード。本体（serve）の後始末は呼び手（app.main の finally）がする。"""
    tui = BlackwallTui(ctx, cfg=cfg, config_path=config_path, resume=resume)
    tui.run(mouse=True)
    return 0
