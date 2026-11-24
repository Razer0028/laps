"""推論中の波（端末用）— アプリ app/ui/app.js の `drawWave` を**式ごと**点字キャンバスへ移植。

設計: DESIGN-linux-term.md §3 ／ SPEC 項目 21（動きの規律・2026-08-29）。
  * 幅 W 列 × 高さ rows 行を点字（U+2800〜）のキャンバスにする。1 セル = 横 2 × 縦 4 ドット。
  * 5 本の線（amp/freq/phase/speed/dir はアプリと同じ定数）＋中心を通る明るい 1 本。
  * 包絡は両端で 0・中央で最大（sin(u·π)^1.6）。
  * ★出力に反応させない・止めない（アプリと同じ。止めると「固まった」と読める）。

位相は壁時計から出す（t 秒 → wt = t / period）。Live の refresh 間隔がどうであれ、
呼ぶたびに違う絵になり、遅い端末でも波の速さは変わらない。

縦はアプリのキャンバス（高さ 34px・MID 17・余白 2px）を**ドット高さへ比例縮尺**する。
★式の MID-2 を 8 ドットのキャンバスにそのまま当てると振れ幅が ±2 ドットになり、
  8 行のうち 5 行しか使わず「平らな帯」になる（検証で実際にそうなった）。
  式は同じまま単位（px→ドット）だけ合わせる。

--------------------------------------------------------------------------
動きの規律（SPEC 項目 21・実測に基づく 4 つ）
--------------------------------------------------------------------------
1. **描き直しの上限は自分で持つ**（`period` / `refresh_per_second`）。
   ★実測（`scratch/tui/motion/`）: `--calm` は 1 秒あたりの描き直しを **1 回も**
     減らしていなかった（loud 19.1〜20.0 回/s ／ calm 19.0 回/s）。原因は
     `Live(refresh_per_second=20)` が固定値だったこと。ここでは**波が自分の絵を
     period より速く作り直さない**（同じ絵を返す）。★端末へ書く回数のほうは
     `render.Renderer._refresh_rate()` が `wave.refresh_per_second` を読むことで
     **既に繋がっている**（2026-09-02 に確認。「次の担当へ」と書いたままにすると、
     読んだ人が二重に直すか、直っているのに直っていないと報告する）。
2. **幅は毎フレーム端末に追随する**（`__rich_console__` の `options.max_width`）。
   ★実測（`scratch/tui/after/`）: **縮める方向は前も問題なかった**（Rich が crop する）。
     差が出るのは**広げる方向**で、ターンの最中に 80 → 120 桁にすると、旧実装は
     作り置きの 80 桁のまま描き、120 桁の端末に 79 桁の波が浮いたままだった。
3. **遅延ゲート**（`DELAY` 秒＝既定 0.20）。短いターンで波が出て消えるのを止める。
   ★実測: 0.1 秒で終わるターンの間に端末へ降った点字が **139 個 → 0 個**。
     0.7 秒のターンでは 1776 → 1365（出るのは待ちを越えた後だけ）。
4. **色は 2 段だけ**（crest だけ赤・他は注記色）。★実測: 波の非空白セルは
   **100% が赤** → 20.6%。1 フレームの実バイト数も 536 B → 360 B。
   赤は「いま効いているもの」の色（theme の約束）なので、撒くと意味が消える。

★旧 RED_2（波の低密度セルの色）はここが最後の利用者だった。参照を外したので
  2026-08-29 の合流で theme から削除した（申し送りの条件を満たしてから消した）。
  ※この行に `theme.` 付きで書かないこと: test_term_theme の「term/ が使っている名前は
    theme に必ずある」門は**コメントも本文として数える**ので、消した名前を書くと落ちる。

★`__rich__` は**持たない**。Rich の `rich_cast` は `__rich__` を先に見るので、
  持っていると `__rich_console__`（幅の追随・遅延ゲート・throttle が入っている方）が
  一度も呼ばれない。絵だけ欲しいときは `frame(t)` / `plain(t)` を呼ぶ。
"""
from __future__ import annotations

import math
import os
import shutil
import time
from typing import Any, Callable, Iterator, Mapping

from rich.text import Text

from . import config, theme

__all__ = [
    "Wave", "TICK", "TICK_CALM", "DELAY", "MIN_WIDTH", "MAX_WIDTH", "DEFAULT_WIDTH",
    "MODES", "ENV_VAR", "calm_from_env", "resolve_mode", "make_factory",
]

# アプリの setInterval(drawWave, reduced ? 120 : 45) に相当。wt は 1 tick ごとに 1 進む。
TICK = 0.045
TICK_CALM = 0.120

# 遅延ゲート: 波を出すまでの待ち（秒）。これより短いターンでは波は 1 度も描かれない。
DELAY = 0.20

MIN_WIDTH = 20
MAX_WIDTH = 120
DEFAULT_WIDTH = 60

# 波の出し方。config.DEFAULTS["wave"] と同じ語彙（正本は config.WAVE_MODES）。
MODES = ("on", "calm", "off")
ENV_VAR = "LAPS_WAVE"

# 点字 U+2800 のビット並び。(行0:0x01,0x08) (行1:0x02,0x10) (行2:0x04,0x20) (行3:0x40,0x80)
# ——左列 0x01,0x02,0x04,0x40 / 右列 0x08,0x10,0x20,0x80。行 3 だけ並びが飛ぶ（8 点点字の歴史的事情）。
_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
_BRAILLE_BASE = 0x2800

# 線ごとの振れ幅の段（アプリの [0.34, 0.55, 0.75, 0.92, 1.0]）。
# ★近い値で重ねると帯になって 1 本ずつ見えない（アプリで実際に一度そうなった）。
_STEPS = (0.34, 0.55, 0.75, 0.92, 1.0)

# アプリのキャンバスの縦（px）。amp=step×(MID-2)・芯の 2.0 はこの単位で書かれている。
_APP_H = 34.0
_APP_MID = _APP_H / 2
_APP_MARGIN = 2.0

# 中心の明るい 1 本（アプリ: sin(u·2π·1.6 + wt·.022)·2.0）
_CORE_FREQ = 1.6
_CORE_SPEED = 0.022
_CORE_AMP = 2.0

# セルのドット数 → スタイル。**2 段だけ**（SPEC 裁定 A）。
#   5 ドット以上＝crest だけ赤（theme.S_WAVE_CREST）／それ未満は注記色（theme.S_WAVE_LOW）。
# ★旧実装は 3 段（RED_2 / RED / bold RED）で**全セルが赤**だった。赤は「いま効いている
#   もの」の色なので、面積の広い波に撒くと失敗の赤が埋もれる。
CREST_DOTS = 5
_STYLE_LOW = theme.S_WAVE_LOW
_STYLE_CREST = theme.S_WAVE_CREST


def _style_for(n_dots: int) -> str:
    return _STYLE_CREST if n_dots >= CREST_DOTS else _STYLE_LOW


def _mode_from_env(env: "Mapping[str, str]") -> str | None:
    v = str(env.get(ENV_VAR, "")).strip().lower()
    return v if v in MODES else None


def resolve_mode(env: "Mapping[str, str] | None" = None,
                 *, calm: bool = False,
                 path: Any = None) -> str:
    """波の出し方を決める。優先: 環境変数 `LAPS_WAVE` > 設定ファイル `wave`
    > 引数 `calm` / `NO_COLOR` / `TERM=dumb` > "on"。

    ★「どこから値が来たか」は `config.wave_mode` が決める（config.resolve と同じ考え方）。
      どちらにも書かれていなければ None が返るので、その時だけ calm の判定に落ちる。
      引数 `--calm` は app 側で `calm=` に畳まれて渡ってくる。
    """
    e = os.environ if env is None else env
    try:   # 設定ファイルが壊れていても波は出す（読めなければ既定へ）
        m = config.wave_mode(env=e, path=path)
    except Exception:
        m = None
    if m == "off":
        return "off"        # 「出さない」は --calm では覆らない（弱める話ではない）
    if calm:
        return "calm"       # 引数 > 環境変数 > ファイル（config.resolve と同じ順）
    if m in MODES:
        return str(m)
    return "calm" if calm_from_env(e) else "on"


def calm_from_env(environ: "os._Environ[str] | dict[str, str] | None" = None) -> bool:
    """設計 §3: `NO_COLOR` が立っている／`TERM=dumb` なら calm（120ms・振れ幅半分。止めない）。
    app 側は `calm = args.calm or calm_from_env()` とする。

    ★`LAPS_WAVE` が明示されていればそれが勝つ（`on` なら NO_COLOR でも calm にしない）。
    """
    env = os.environ if environ is None else environ
    m = _mode_from_env(env)
    if m is not None:
        return m == "calm"
    if env.get("NO_COLOR", "") != "":
        return True
    return env.get("TERM", "").strip().lower() == "dumb"


def _auto_width() -> int:
    """端末の幅。取れなければ 60。"""
    try:
        cols = shutil.get_terminal_size(fallback=(0, 0)).columns
    except Exception:  # 端末が無い・権限が無い等
        cols = 0
    return cols if cols > 0 else DEFAULT_WIDTH


def _clamp_width(width: Any) -> int:
    try:
        w = _auto_width() if width is None else int(width)
    except (TypeError, ValueError):   # 設定から変な値が来ても波は出す（落とさない）
        w = _auto_width()
    return max(MIN_WIDTH, min(MAX_WIDTH, w))


class Wave:
    """推論中の波。`Live(wave)` に渡すと refresh ごとに `__rich_console__` が呼ばれる。

    width: 列数（None なら端末幅か 60。20〜120 に収める）。Live に載せると**毎フレーム
           端末幅に追随する**（作り置きの幅で描かない）。
    calm:  振れ幅を半分・tick を 120ms に（`--calm` / NO_COLOR / TERM=dumb）。止めはしない。
    rows:  高さ（行）。設計は 2。
    mode:  "on" | "calm" | "off"。None なら resolve_mode（環境変数 > 設定 > calm）。
    delay: 波を出すまでの待ち（秒）。短いターンで出して消えるのを止める。0 で即出す。
    """

    def __init__(self, width: int | None = None, *, calm: bool = False, rows: int = 2,
                 mode: str | None = None, delay: float | None = None,
                 env: "Mapping[str, str] | None" = None) -> None:
        m = str(mode).strip().lower() if mode is not None else None
        if m not in MODES:
            m = resolve_mode(env, calm=calm)
        self.mode = m
        self.enabled = m != "off"
        self.calm = (m == "calm")
        self.period = TICK_CALM if self.calm else TICK   # 描き直しの上限（秒）
        try:
            self.delay = max(0.0, float(DELAY if delay is None else delay))
        except (TypeError, ValueError):
            self.delay = DELAY
        self.rows = max(1, int(rows))
        self._t0 = time.monotonic()

        # 数えるためのカウンタ（試験と実測が読む。表示には使わない）
        self.frames_drawn = 0     # 実際に絵を作り直した回数
        self.calls = 0            # Live から呼ばれた回数
        self._cache: Text | None = None
        self._cache_at = 0.0

        # ドット空間（アプリのキャンバス座標に相当）。
        self._dh = self.rows * 4           # 縦ドット数（= アプリの H）
        self._mid = self._dh / 2           # = アプリの MID
        # px → ドットの縮尺。アプリの 34px の絵をそのまま rows×4 ドットに縮める。
        self._scale = self._dh / _APP_H
        self._core_amp = _CORE_AMP * self._scale
        self._set_width(_clamp_width(width))
        # 線ごとの素性は一度だけ決める（アプリと同じ。毎フレーム作り直さない）。
        self._strands = tuple(
            {
                "amp": step * (_APP_MID - _APP_MARGIN) * self._scale,
                # ★周期を短くしないと「ほぼ直線」に見えてうねりが分からない。
                "freq": 1.15 + i * 0.28,
                "phase": i * 1.3,
                "speed": 0.030 + i * 0.006,   # 本ごとに速さを変えて交差が動く
                "dir": 1 if i % 2 else -1,    # 交互に逆向き
            }
            for i, step in enumerate(_STEPS)
        )

    # ---- 幅 ---------------------------------------------------------------

    @property
    def refresh_per_second(self) -> float:
        """この波を描き直してよい上限（回/s）。`Live(refresh_per_second=...)` に渡す値。
        on = 22.2 回/s ／ calm = 8.3 回/s。
        ★`render.Renderer._refresh_rate()` がこれを読み、`min(値, LIVE_FPS=20)` で頭打ちにする
          ＝実効は on 20 回/s ／ calm 8.3 回/s（2026-09-02 に実装を確認）。"""
        return 1.0 / self.period

    @property
    def live_rows(self) -> int:
        """いまこの波が実際に占める行数（0 = 1 行も占めない）。

        ★`render.py` の `_LiveView` はこれを読んで本文に割ける高さを決める
          （`getattr(wave, "live_rows", …)`・2026-09-02 に配線を確認）。波が `off` のときや
          遅延ゲートの最中は **0 行**なので、本文が理由もなく 2 行短くならない。
        """
        if not self.enabled:
            return 0
        return 0 if (time.monotonic() - self._t0) < self.delay else self.rows

    def _set_width(self, width: int) -> None:
        self.width = width
        self._dw = width * 2               # 横ドット数

    def resize(self, width: Any) -> bool:
        """幅を変える。変わったら True（キャッシュを捨てる）。

        ★読めない値のときは**いまの幅のまま**（端末幅に落とすと、リサイズの取得に
          失敗しただけで絵の幅が飛ぶ）。作るときの `width=None` とは扱いが違う。
        """
        try:
            w = max(MIN_WIDTH, min(MAX_WIDTH, int(width)))
        except (TypeError, ValueError):
            return False
        if w == self.width:
            return False
        self._set_width(w)
        self._cache = None
        return True

    # ---- 位相 -------------------------------------------------------------

    def _wt(self, t: float | None) -> float:
        """アプリの wt（tick 数）。t=None なら生成時からの経過で進める。"""
        if t is None:
            t = time.monotonic() - self._t0
        if not math.isfinite(t):   # NaN/inf を Live の描画スレッドで例外にしない
            t = 0.0
        return t / self.period

    # ---- キャンバス -------------------------------------------------------

    def _dots(self, t: float | None) -> list[list[int]]:
        """2W × 4rows のドット格子（0/1）。行 0 が上。"""
        dw, dh, mid = self._dw, self._dh, self._mid
        grid = [[0] * dw for _ in range(dh)]
        wt = self._wt(t)
        k = 0.5 if self.calm else 1.0      # 減らす設定では振れ幅だけ抑える（止めない）

        # 描く範囲はドット列 2 〜 2W-3（両端のセルは余白）。u はその中で 0→1。
        # ★包絡は両端で 0（env=0 の点は波の情報を持たないので打たない）。
        #   アプリは u=0,1 の端点に MID の 1 画素を置くが、点字では端セルが空であることを
        #   設計の試験が求めるので、端点 1 ドットを落とし、その外側の 1 セルを余白にする。
        x0, x1 = 2, dw - 3
        span = x1 - x0
        if span < 1:
            return grid

        def put(x: int, y: int) -> None:
            if 0 <= y < dh:
                grid[y][x] = 1

        def stroke(x: int, y: float, prev: int | None) -> int:
            """アプリの lineTo 相当。前の点との間の縦の隙間を埋めて線にする。"""
            # 縮尺で端に触れたとき、丸めで 1 ドットはみ出して下端が欠けないよう内側に収める
            r = min(dh - 1, max(0, int(math.floor(y + 0.5))))
            if prev is None or prev == r:
                put(x, r)
            elif r > prev:
                for yy in range(prev + 1, r + 1):
                    put(x, yy)
            else:
                for yy in range(r, prev):
                    put(x, yy)
            return r

        for s in self._strands:
            prev: int | None = None
            for x in range(x0, x1 + 1):
                u = (x - x0) / span
                # 端で 0（1 本に収束）・中央で最大（ばらけてうねる）。1.6 乗は端の収束を鋭く。
                env = math.sin(u * math.pi) ** 1.6
                if env <= 0.0:
                    prev = None
                    continue
                y = mid + math.sin(u * math.pi * 2 * s["freq"] + s["phase"] + wt * s["speed"] * s["dir"]) \
                    * s["amp"] * env * k
                prev = stroke(x, y, prev)

        # 中心を通る明るい 1 本（芯にあたる線）
        prev = None
        for x in range(x0, x1 + 1):
            u = (x - x0) / span
            env = math.sin(u * math.pi) ** 1.6
            if env <= 0.0:
                prev = None
                continue
            y = mid + math.sin(u * math.pi * 2 * _CORE_FREQ + wt * _CORE_SPEED) * self._core_amp * env * k
            prev = stroke(x, y, prev)
        return grid

    def _cells(self, t: float | None) -> list[list[tuple[str, int]]]:
        """行ごとに (点字 1 文字, ドット数) の列。空セルは (' ', 0)。"""
        grid = self._dots(t)
        out: list[list[tuple[str, int]]] = []
        for row in range(self.rows):
            line: list[tuple[str, int]] = []
            for col in range(self.width):
                bits = 0
                n = 0
                for dy in range(4):
                    grow = grid[row * 4 + dy]
                    for dx in range(2):
                        if grow[col * 2 + dx]:
                            bits |= _BITS[dy][dx]
                            n += 1
                line.append((chr(_BRAILLE_BASE + bits), n) if n else (" ", 0))
            out.append(line)
        return out

    # ---- 公開 -------------------------------------------------------------

    def frame(self, t: float | None = None) -> Text:
        """1 フレーム。t=None なら time.monotonic() から位相を進める（period ごとに wt += 1 相当）。
        ★ここは遅延ゲートも throttle も通らない（絵そのものが欲しいときの入口）。"""
        text = Text(no_wrap=True, overflow="crop")
        for i, line in enumerate(self._cells(t)):
            if i:
                text.append("\n")
            # 同じ濃さの連続セルはまとめて append（Span を減らす）。空セルは無スタイル＝背景を塗らない。
            buf = ""
            cur: str | None = None
            for ch, n in line:
                st = _style_for(n) if n else None
                if st != cur and buf:
                    text.append(buf, style=cur)
                    buf = ""
                cur = st
                buf += ch
            if buf:
                text.append(buf, style=cur)
        return text

    def __rich_console__(self, console: Any, options: Any) -> Iterator[Text]:
        """Live が毎 refresh 呼ぶ入口。ここで 3 つの規律をかける。

        1. `mode="off"` なら何も出さない（1 行も占めない）。
        2. 生成から `delay` 秒たつまで何も出さない（短いターンで出して消えない）。
        3. `options.max_width` に幅を合わせ、`period` より速くは絵を作り直さない。
        """
        self.calls += 1
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self._t0 < self.delay:
            return
        try:
            width = int(getattr(options, "max_width", None) or console.width)
        except (TypeError, ValueError, AttributeError):
            width = self.width
        self.resize(width)
        if self._cache is None or (now - self._cache_at) >= self.period:
            self._cache = self.frame()
            self._cache_at = now
            self.frames_drawn += 1
        yield self._cache

    def plain(self, t: float | None = None) -> str:
        """色なしの文字列（試験用）。rows 行を \\n で繋ぐ。"""
        return "\n".join("".join(ch for ch, _ in line) for line in self._cells(t))


def make_factory(width: Any = None, *, calm: bool = False, rows: int = 2,
                 delay: float | None = None,
                 env: "Mapping[str, str] | None" = None,
                 path: Any = None) -> "Callable[[], Wave] | None":
    """波を作る係を返す。**設定が `off` なら None**（＝呼び手は波を 1 つも持たない）。

    SPEC 項目 21 ⑤: 「出さない」は `Wave` を作ってから黙らせるのではなく、
    **作らない**ことで守る。作ってしまうと `render.py` が `live_rows` を毎フレーム
    問い合わせ、Live の刷新回数も波の `period` に引きずられる（`off` なのに
    8.3〜20 回/s で書き出す）。ここで None を返せば Live は既定の刻みに戻る。

    width は数でも「呼ぶと数を返すもの」でもよい（`lambda: console.width`）。
    ターンごとに呼び直すので、ターンの間に端末幅が変わっても次のターンは新しい幅で始まる
    （ターンの最中の追随は `__rich_console__` が `options.max_width` を見て行う）。

    ★出し方（on/calm/off）は**ここで 1 度だけ**決めて `Wave` に渡す。ターンごとに
      環境変数と設定ファイルを読み直すと、走行中に設定を書き換えた人には「効いたり
      効かなかったり」に見える（どこから値が来たかを 1 か所で決める config の考え方）。

    使う側（`app.py:70-73` の `make_wave_factory`）へ:

        def make_wave_factory(console, calm):
            if not wave_allowed(console):
                return None
            return make_factory(lambda: console.width, calm=calm)
    """
    mode = resolve_mode(env, calm=calm, path=path)
    if mode == "off":
        return None

    def factory() -> Wave:
        w = width() if callable(width) else width
        return Wave(w, mode=mode, rows=rows, delay=delay, env=env)

    return factory
