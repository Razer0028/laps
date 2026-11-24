"""端末用の波（laps.term.wave）— 設計 DESIGN-linux-term.md §3 の試験。

点字の絵を文字列で検査する。色（Rich の Span）は frame() でだけ見る。
"""
from __future__ import annotations

import io
import time

import pytest
from rich.text import Text

from laps.term import theme
from laps.term.wave import (
    DELAY, MAX_WIDTH, MIN_WIDTH, MODES, TICK, TICK_CALM, Wave, resolve_mode,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """利用者の ~/.config/blackwall/config.toml も LAPS_WAVE も見ない。

    ★`Wave()` は出し方（on/calm/off）を環境変数と設定ファイルから決めるので、
      隔離しないと**この機械の設定**で結果が変わる（測るときの作法）。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("LAPS_WAVE", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

# 点字 U+2800 のビット並び（設計の★）。左列 0x01,0x02,0x04,0x40 / 右列 0x08,0x10,0x20,0x80
_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def _is_braille(ch: str) -> bool:
    return 0x2800 <= ord(ch) <= 0x28FF


def _decode(plain: str) -> list[list[int]]:
    """点字の文字列 → ドット格子（行 0 が上）。空白は 0。"""
    lines = plain.split("\n")
    width = len(lines[0])
    grid = [[0] * (width * 2) for _ in range(len(lines) * 4)]
    for row, line in enumerate(lines):
        for col, ch in enumerate(line):
            if ch == " ":
                continue
            bits = ord(ch) - 0x2800
            for dy in range(4):
                for dx in range(2):
                    if bits & _BITS[dy][dx]:
                        grid[row * 4 + dy][col * 2 + dx] = 1
    return grid


def _column_has_dots(plain: str, col: int) -> bool:
    return any(line[col] != " " for line in plain.split("\n"))


def _vspread_at_center(plain: str) -> int:
    """中央セル列のドットの縦の広がり（最上段〜最下段）。"""
    grid = _decode(plain)
    width = len(plain.split("\n")[0])
    xs = (width // 2 * 2, width // 2 * 2 + 1)
    rows = [y for y, row in enumerate(grid) if any(row[x] for x in xs)]
    return (max(rows) - min(rows) + 1) if rows else 0


# ---- 形 -------------------------------------------------------------------

def test_two_frames_differ():
    w = Wave(60)
    assert w.plain(0.0) != w.plain(0.5)


def test_width_exact_each_row():
    for width in (20, 37, 60, 120):
        w = Wave(width)
        lines = w.plain(0.0).split("\n")
        assert len(lines) == 2
        assert all(len(line) == width for line in lines), (width, [len(x) for x in lines])


def test_width_clamped():
    assert Wave(5).width == MIN_WIDTH
    assert Wave(999).width == MAX_WIDTH
    # 幅なしでも範囲内（端末幅 or 60）
    assert MIN_WIDTH <= Wave().width <= MAX_WIDTH


def test_all_chars_braille_or_space():
    w = Wave(60)
    for t in (0.0, 0.25, 1.3, 9.9):
        for ch in w.plain(t).replace("\n", ""):
            assert ch == " " or _is_braille(ch), repr(ch)


def test_edges_empty_center_drawn():
    for width in (20, 60, 120):
        w = Wave(width)
        for t in (0.0, 0.5, 2.0):
            p = w.plain(t)
            assert not _column_has_dots(p, 0), (width, t)
            assert not _column_has_dots(p, width - 1), (width, t)
            assert _column_has_dots(p, width // 2), (width, t)


def test_rows_two_lines():
    assert len(Wave(40, rows=2).plain(0.0).split("\n")) == 2
    assert len(Wave(40, rows=3).plain(0.0).split("\n")) == 3


def test_rich_console_yields_text_and_no_rich_dunder():
    """★`__rich__` を持たせない（Rich の rich_cast が先に見るので、持っていると
    `__rich_console__` の幅追随・遅延ゲート・throttle が一度も呼ばれない）。"""
    from rich.console import Console

    assert not hasattr(Wave, "__rich__")
    w = Wave(40, delay=0)
    con = Console(file=io.StringIO(), width=40, force_terminal=True)
    out = list(w.__rich_console__(con, con.options))
    assert len(out) == 1 and isinstance(out[0], Text)
    assert isinstance(w.frame(0.0), Text)
    # plain は frame と同じ絵（同じ t なら）
    assert w.frame(1.0).plain == w.plain(1.0)


# ---- calm -----------------------------------------------------------------

def test_calm_smaller_spread():
    # 1 時点では位相次第で差が出ないので、しばらく回した最大の広がりで比べる。
    ts = [i * 0.3 for i in range(40)]
    loud = max(_vspread_at_center(Wave(60).plain(t)) for t in ts)
    calm = max(_vspread_at_center(Wave(60, calm=True).plain(t)) for t in ts)
    assert loud > calm, (loud, calm)
    assert calm >= 1   # 止めない（calm でも描く）


def test_calm_period():
    assert Wave(40).period == TICK == 0.045
    assert Wave(40, calm=True).period == TICK_CALM == 0.120


# ---- 色 -------------------------------------------------------------------

def test_colors_only_two_steps_and_empty_unstyled():
    """SPEC 裁定 A: 波の色は 2 段だけ（crest だけ赤・他は注記色）。
    ★旧実装は 3 段とも赤で、画面の赤インクの大半を波が占めていた。"""
    w = Wave(60)
    text = w.frame(0.0)
    allowed = {theme.S_WAVE_LOW, theme.S_WAVE_CREST}
    assert text.spans, "色が付いていない"
    for span in text.spans:
        assert str(span.style) in allowed, span.style
        # 空セル（空白）にはスタイルを付けない＝背景を塗らない
        assert " " not in text.plain[span.start:span.end]


def test_style_by_dot_density():
    """セルのドット数で 2 段（5 ドット以上＝crest だけ赤／それ未満は注記色）。"""
    w = Wave(60)
    text = w.frame(0.3)
    by_char: dict[int, set[str]] = {}
    for span in text.spans:
        for i in range(span.start, span.end):
            ch = text.plain[i]
            if ch in (" ", "\n"):
                continue
            n = bin(ord(ch) - 0x2800).count("1")
            by_char.setdefault(n, set()).add(str(span.style))
    for n, styles in by_char.items():
        want = theme.S_WAVE_CREST if n >= 5 else theme.S_WAVE_LOW
        assert styles == {want}, (n, styles)


def test_red_ink_is_only_the_crest():
    """赤は「いま効いているもの」の色。波が塗る赤は非空白セルの一部だけであること。
    ★実測（旧実装）: 非空白セルの 100% が赤。新実装は crest（5 ドット以上）だけ。"""
    w = Wave(80)
    red = cells = 0
    for i in range(60):
        text = w.frame(i * 0.11)
        for span in text.spans:
            n = len(text.plain[span.start:span.end].replace("\n", ""))
            cells += n
            if str(span.style) == theme.S_WAVE_CREST:
                red += n
    assert cells > 0
    assert 0.0 < red / cells < 0.25, red / cells


# ---- 位相 -----------------------------------------------------------------

def test_t_none_advances_with_clock(monkeypatch):
    import laps.term.wave as mod
    now = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    w = Wave(60)
    first = w.plain()
    now[0] += 0.5
    assert w.plain() != first
    # t=None の絵は「経過秒を t に渡した絵」と同じ
    assert w.plain() == w.plain(0.5)


def test_deterministic_for_same_t():
    a, b = Wave(60), Wave(60)
    assert a.plain(2.0) == b.plain(2.0)


# ---- 検証で足した分（2026-08-23） ------------------------------------------

def test_canvas_height_fully_used():
    """縦はアプリの 34px を rows×4 ドットへ比例縮尺する。
    ★式の (MID-2) を 8 ドットにそのまま当てると ±2 ドット＝5/8 行しか使わず平らな帯になった。
    しばらく回せば最上段・最下段のドット行にも線が届くこと。"""
    w = Wave(60)
    used: set[int] = set()
    for i in range(200):
        for y, row in enumerate(w._dots(i * TICK)):
            if any(row):
                used.add(y)
    assert used == set(range(8)), sorted(used)
    assert max(_vspread_at_center(w.plain(i * TICK)) for i in range(200)) >= 7


def test_non_finite_t_does_not_raise():
    """Live の描画スレッドで例外にしない（NaN/inf は t=0 扱い）。"""
    w = Wave(60)
    for t in (float("nan"), float("inf"), -float("inf")):
        assert w.plain(t) == w.plain(0.0)
    # 負・巨大も落ちない
    assert len(w.plain(-3.0).split("\n")) == 2
    assert len(w.plain(1e12).split("\n")) == 2


def test_width_tolerant_of_bad_values():
    """設定から変な値が来ても波は出す（端末幅か 60 に落ちる）。"""
    for bad in ("abc", object(), 20.7):
        assert MIN_WIDTH <= Wave(bad).width <= MAX_WIDTH  # type: ignore[arg-type]
    assert Wave("30").width == 30
    assert Wave(40, rows=0).rows == 1       # rows も 1 未満にしない
    assert Wave(40, rows=1).plain(0.0) != Wave(40, rows=1).plain(0.7)   # 1 行でも動く


def test_calm_from_env():
    from laps.term.wave import calm_from_env
    assert calm_from_env({}) is False
    assert calm_from_env({"NO_COLOR": "1"}) is True
    assert calm_from_env({"NO_COLOR": ""}) is False          # 空は「立っていない」（no-color.org の規約）
    assert calm_from_env({"TERM": "dumb"}) is True
    assert calm_from_env({"TERM": "xterm-256color"}) is False
    # ★明示した設定が勝つ（NO_COLOR で勝手に calm にしない）
    assert calm_from_env({"NO_COLOR": "1", "LAPS_WAVE": "on"}) is False
    assert calm_from_env({"LAPS_WAVE": "calm"}) is True
    assert calm_from_env({"LAPS_WAVE": "off"}) is False   # off は calm ではない
    assert calm_from_env({"LAPS_WAVE": "kirakira"}) is False   # 語彙の外は無視


def test_live_crops_to_console_width_no_wrap():
    """Live（app の使い方）で、端末が波より狭くても折り返さず切る。広ければ各行ちょうど W。
    Live の中身は `console.render_lines(renderable)` なのでそれで測る。
    ★Rich 15 の `console.print(Text(no_wrap=True))` は Text を作り直して no_wrap を落とし**折り返す**
    （実測）。波は Live で出すこと（app.py への注意）。"""
    import io
    from rich.console import Console
    from rich.live import Live

    w = Wave(60, delay=0)
    # ★幅は毎フレーム端末に追随する（MIN..MAX に収める）。200 桁では MAX_WIDTH で頭打ち。
    for cw, want in ((30, 30), (60, 60), (200, MAX_WIDTH), (10, 10)):
        con = Console(file=io.StringIO(), width=cw, force_terminal=True, color_system=None)
        lines = con.render_lines(w, con.options, pad=False)
        assert len(lines) == 2, cw
        assert all(sum(len(seg.text) for seg in ln) == want for ln in lines), (cw, lines)
    # Live に載せて refresh しても落ちず、点字が出る
    buf = io.StringIO()
    con = Console(file=buf, width=30, force_terminal=True, color_system=None)
    with Live(w, console=con, refresh_per_second=w.refresh_per_second, transient=True,
              auto_refresh=False) as live:
        live.refresh()
    assert any(0x2800 <= ord(ch) <= 0x28FF for ch in buf.getvalue())


def test_period_drives_phase_not_refresh_rate():
    """位相は壁時計（t/period）。同じ t なら loud と calm で wt が違う＝calm は遅い。"""
    loud, calm = Wave(60), Wave(60, calm=True)
    assert loud._wt(0.45) == pytest.approx(10.0)
    assert calm._wt(0.48) == pytest.approx(4.0)


# ---- 動きの規律（SPEC 項目 21・2026-08-29） --------------------------------
#
# ★ここは「見た目」ではなく**回数と行数**を測る。
#   動きの不具合は目で見ても分からない（`--calm` が 1 回も減らしていなかったのを
#   半年間だれも気づかなかった）。

def _con(width: int = 60):
    from rich.console import Console
    return Console(file=io.StringIO(), width=width, force_terminal=True, color_system="truecolor")


def _draw(w: Wave, width: int = 60) -> list:
    con = _con(width)
    return list(w.__rich_console__(con, con.options))


def test_redraw_rate_is_capped_by_period(monkeypatch):
    """★1 秒あたりの描き直し回数を測る（決定論版・壁時計を差し替える）。

    Live が何回呼ぼうと、絵を作り直すのは `1/period` 回/s まで
    （on = 22.2 回/s ／ calm = 8.3 回/s）。
    ★旧実装にはこの上限が無く、`--calm` は実測で刷新回数を 1 回も減らしていなかった。
    """
    import laps.term.wave as mod
    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    got = {}
    for calm in (False, True):
        w = Wave(60, calm=calm, delay=0)
        con = _con(60)
        for _ in range(200):            # 1 秒ぶんを 200 回/s の勢いで呼ぶ
            now[0] += 0.005
            list(w.__rich_console__(con, con.options))
        got[calm] = w.frames_drawn
        assert w.calls == 200, w.calls
    # 5ms 刻みで呼んでいるので刻みの丸め分だけ下振れする（20 と 8 が出る）。上限は超えない。
    assert 1 / TICK - 4 <= got[False] <= 1 / TICK + 1, got     # ≈ 20〜22 回/s
    assert 1 / TICK_CALM - 2 <= got[True] <= 1 / TICK_CALM + 1, got   # ≈ 8 回/s
    assert got[True] * 2 < got[False], got               # calm は半分以下


def test_redraw_rate_on_a_real_live():
    """同じことを本物の `Live` で（配線の確認）。★`refresh_per_second` を渡す側が
    `render.py` になるまでは、この回数は wave が自分で守っている。"""
    from rich.live import Live

    got = {}
    for calm in (False, True):
        w = Wave(60, calm=calm, delay=0)
        con = _con(60)
        with Live(w, console=con, refresh_per_second=w.refresh_per_second,
                  transient=True, redirect_stdout=False, redirect_stderr=False):
            time.sleep(0.5)
        got[calm] = w.frames_drawn / 0.5
    # 機械の負荷で上下するので幅を広く取る（上限だけは超えない）
    assert 4 <= got[False] <= 1 / TICK + 6, got
    assert 1 <= got[True] <= 1 / TICK_CALM + 6, got
    assert got[True] <= got[False], got


def test_refresh_per_second_matches_period():
    assert Wave(60).refresh_per_second == pytest.approx(1 / TICK)
    assert Wave(60, calm=True).refresh_per_second == pytest.approx(1 / TICK_CALM)


def test_delay_gate_hides_short_turns(monkeypatch):
    """0.4 秒で終わるターンで波を出して消さない。★実測（旧実装）: 13 回刷新して消えた。"""
    import laps.term.wave as mod
    now = [500.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    w = Wave(60)
    assert w.delay == DELAY == 0.20
    # 0.15 秒で終わるターン: 一度も描かない（出て消えない）
    for _ in range(3):                  # Live 20 回/s 相当
        now[0] += 0.05
        assert _draw(w) == []
    assert w.frames_drawn == 0 and w.calls == 3
    # 0.4 秒のターンでも、出るのは待ちを越えた後の半分だけ
    drawn = 0
    for _ in range(5):
        now[0] += 0.05
        drawn += len(_draw(w))
    assert drawn == 5 and w.frames_drawn >= 1
    assert w.calls == 8 and w.frames_drawn < w.calls


def test_delay_zero_draws_at_once():
    assert len(_draw(Wave(60, delay=0))) == 1


def test_off_takes_no_rows_and_never_computes():
    w = Wave(60, mode="off", delay=0)
    assert w.enabled is False and w.mode == "off"
    for _ in range(5):
        assert _draw(w) == []
    assert w.frames_drawn == 0 and w.calls == 5


def test_mode_comes_from_env_and_config(tmp_path, monkeypatch):
    """★配線: 環境変数 `LAPS_WAVE` > 設定ファイル `wave` > `--calm`/NO_COLOR > on。
    値を決めるのは config.wave_mode（config.resolve と同じ「1 か所」の考え方）。"""
    from laps.term import config as C

    cfg = tmp_path / "xdg" / "blackwall" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('wave = "off"\n', encoding="utf-8")
    assert C.wave_mode(env={}, path=cfg) == "off"
    assert resolve_mode({}) == "off"                  # 設定ファイルが効く（XDG は fixture）
    assert Wave(60).mode == "off"                     # ★Wave まで届く
    monkeypatch.setenv("LAPS_WAVE", "calm")      # 環境変数が勝つ
    assert Wave(60).mode == "calm" and Wave(60).period == TICK_CALM
    monkeypatch.setenv("LAPS_WAVE", "kirakira")  # 語彙の外は無視して下の層（ファイル）へ
    with pytest.warns(UserWarning):
        assert Wave(60).mode == "off"
    monkeypatch.delenv("LAPS_WAVE")
    cfg.write_text("wave = 7\n", encoding="utf-8")    # 型違いも落とさない
    with pytest.warns(UserWarning):
        assert Wave(60).mode == "on"
    cfg.write_text("wave = = broken\n", encoding="utf-8")   # 壊れた TOML でも波は出す
    with pytest.warns(UserWarning):
        assert Wave(60, delay=0).mode == "on"


def test_mode_argument_wins_over_everything(monkeypatch):
    monkeypatch.setenv("LAPS_WAVE", "off")
    assert Wave(60, mode="on").mode == "on"
    assert Wave(60, mode="ON").mode == "on"       # 大文字・空白も受ける
    assert Wave(60, mode="ちがう").mode == "off"   # 語彙の外は無視して下の層へ


def test_calm_flag_still_works_without_settings():
    assert Wave(60, calm=True).mode == "calm"
    assert Wave(60, calm=False).mode == "on"


# ---- レイアウトを動かさない ------------------------------------------------

def test_never_wraps_and_height_is_exactly_rows():
    """★どんな幅でも行数は rows のまま・1 行も端末幅を超えない。

    超えると端末が自前で折り返し、Live が見積もった塊の高さと実際がずれて、
    **上の行を消し残す**（スクロールバックに同じ道具行が 10 回積まれた実測がある）。
    """
    for cw in (10, 20, 39, 60, 80, 137, 200):
        for rows in (1, 2, 3):
            w = Wave(60, rows=rows, delay=0)
            con = _con(cw)
            lines = con.render_lines(w, con.options, pad=False)
            assert len(lines) == rows, (cw, rows, len(lines))
            for ln in lines:
                assert sum(len(seg.text) for seg in ln) <= cw, (cw, rows)


def test_width_follows_the_terminal_after_resize():
    """作り置きの幅で描かない。140 → 60 に縮めたら次のフレームから 60 桁。"""
    w = Wave(140, delay=0)
    assert w.width == MAX_WIDTH                     # 140 は上限で頭打ち
    wide = _draw(w, 120)[0]
    assert max(len(x) for x in wide.plain.split("\n")) == 120
    narrow = _draw(w, 60)[0]
    assert w.width == 60
    assert max(len(x) for x in narrow.plain.split("\n")) == 60
    assert [len(x) for x in narrow.plain.split("\n")] == [60, 60]
    # 広げても付いていく
    assert max(len(x) for x in _draw(w, 100)[0].plain.split("\n")) == 100


def test_resize_reports_change_and_drops_cache():
    w = Wave(60, delay=0)
    _draw(w, 60)
    assert w.resize(60) is False        # 変わらなければ何もしない
    assert w.resize(80) is True and w.width == 80
    assert w._cache is None             # 古い幅の絵を再利用しない
    assert w.resize("abc") is False     # 変な値でも落とさない（幅は据え置き）
    assert w.width == 80


def test_modes_vocabulary_matches_config():
    from laps.term import config as C
    assert MODES == C.WAVE_MODES == ("on", "calm", "off")
    assert C.DEFAULTS["wave"] == "on"


def test_precedence_argument_over_env_over_file(tmp_path, monkeypatch):
    """優先順は config.resolve と同じ **引数 > 環境変数 > ファイル**。
    ただし `off`（出さない）だけは `--calm` で覆らない（弱める話ではないので）。"""
    cfg = tmp_path / "xdg" / "blackwall" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('wave = "on"\n', encoding="utf-8")
    assert Wave(60).mode == "on"
    assert Wave(60, calm=True).mode == "calm"            # 引数がファイルに勝つ
    monkeypatch.setenv("LAPS_WAVE", "on")
    assert Wave(60, calm=True).mode == "calm"            # 引数が環境変数にも勝つ
    monkeypatch.setenv("LAPS_WAVE", "off")
    assert Wave(60, calm=True).mode == "off"             # off は覆らない
    monkeypatch.delenv("LAPS_WAVE")
    cfg.write_text('wave = "off"\n', encoding="utf-8")
    assert Wave(60, calm=True).mode == "off"


def test_no_color_still_means_calm():
    """NO_COLOR / TERM=dumb は設定が何も無いときだけ効く（明示された設定が上）。"""
    assert resolve_mode({"NO_COLOR": "1"}) == "calm"
    assert resolve_mode({"TERM": "dumb"}) == "calm"
    assert resolve_mode({"NO_COLOR": "1", "LAPS_WAVE": "on"}) == "on"


def test_live_rows_reports_what_the_wave_actually_occupies(monkeypatch):
    """★`render.py` が本文の高さを見積もるための数（次の担当が読む）。
    描かないときは 0 でなければ、本文が 2 行ぶん損をする。"""
    import laps.term.wave as mod
    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    w = Wave(60, rows=2)
    assert w.live_rows == 0 and len(_draw(w)) == 0    # 遅延ゲートの最中
    now[0] += 0.25
    assert w.live_rows == 2 and len(_draw(w)) == 1
    off = Wave(60, mode="off", delay=0)
    assert off.live_rows == 0
    assert Wave(60, rows=3, delay=0).live_rows == 3


# ---- SPEC 項目 21 が名指しで求めた 4 本 -------------------------------------
#
# ★上の試験群と重なる部分があるのは承知のうえ。SPEC が名前で挙げた試験は
#   **その名前で**在ること自体が約束（次の担当が `grep` で見つけられる）。

def test_calm_lowers_refresh_rate():
    """`--calm` が**本当に**端末へ書き出す回数を減らす（SPEC 項目 21①）。

    ★実測（`scratch/tui/motion/refresh-*.log`）: 旧実装は波 19.1〜20.0 回/s に対し
      calm 19.0 回/s ＝ **1 回も減っていなかった**。原因は波が `period` を持っているのに
      `Live(refresh_per_second=20)` が固定値だったこと（「書いたが繋いでいない」）。
      ここは偽物の波ではなく**本物の `Wave` を本物の `Renderer` に載せて**測る。
    ★`Renderer.LIVE_FPS` が上限なので、`on` 側は 1/TICK(22.2) ではなく 20 で頭打ちになる
      （SPEC の「1/period になる」は calm 側で成り立つ。上限は render 側の判断）。
    """
    from laps.term.render import Renderer

    rates: dict[bool, float] = {}
    for calm in (False, True):
        r = Renderer(_con(80))
        w = Wave(60, calm=calm, delay=0)
        r.set_wave(w)
        r.start_turn(f"r-{calm}")
        assert r._live is not None, "Live が作られていない（この試験が何も測れない）"
        rates[calm] = float(r._live.refresh_per_second)
        r.end_turn()
        assert w.refresh_per_second == pytest.approx(1 / w.period)

    assert rates[True] == pytest.approx(1 / TICK_CALM), rates      # calm = 8.3 回/s
    assert rates[False] == pytest.approx(min(1 / TICK, Renderer.LIVE_FPS)), rates
    assert rates[True] < rates[False], rates
    assert rates[True] * 2 <= rates[False], rates                  # 帯域は半分以下


def test_wave_follows_width():
    """幅を変えて 2 回描き、行幅が追随する（SPEC 項目 21②）。

    ★実測: 差が出るのは**広げる方向**。縮める側は Rich が crop するので前も問題なかったが、
      ターンの最中に 80 → 120 桁にすると、旧実装は作り置きの 80 桁のまま描き、
      120 桁の端末に 79 桁の波が浮いたままだった。
    """
    w = Wave(60, delay=0)
    for want in (100, 40, 120, 60):          # 広げる・縮める・広げる・縮める
        out = _draw(w, want)
        assert len(out) == 1, want
        assert [len(x) for x in out[0].plain.split("\n")] == [want] * w.rows, want
        assert w.width == want, want
    # 端末が上限より広くても波は MAX_WIDTH で頭打ち（1 行も端末幅を超えない）
    assert [len(x) for x in _draw(w, 200)[0].plain.split("\n")] == [MAX_WIDTH] * w.rows


def test_wave_two_styles_only():
    """`_style_for` の戻り値は 2 通りだけ（SPEC 項目 21④・裁定 A）。

    ★実測（旧実装は 3 段で全セルが赤）: 赤インクが非空白セルの 100%。
      赤は「いま効いているもの」の色なので、面積の広い波に撒くと失敗の赤が埋もれる。
    """
    from laps.term.wave import CREST_DOTS, _style_for

    got = {_style_for(n) for n in range(0, 9)}          # 1 セルは 0〜8 ドット
    assert len(got) == 2, got
    assert got == {theme.S_WAVE_LOW, theme.S_WAVE_CREST}
    assert all(_style_for(n) == theme.S_WAVE_LOW for n in range(CREST_DOTS))
    assert all(_style_for(n) == theme.S_WAVE_CREST for n in range(CREST_DOTS, 9))
    # 実際に描いた絵にも 2 通りしか出ない（かつ 2 通りとも出る＝片方が死んでいない）
    seen = {str(s.style) for i in range(40) for s in Wave(80).frame(i * 0.13).spans}
    assert seen == {theme.S_WAVE_LOW, theme.S_WAVE_CREST}, seen


def test_wave_setting_off(tmp_path, monkeypatch):
    """`wave = "off"` なら**波を作る係そのものが None**（SPEC 項目 21⑤）。

    ★「作ってから黙らせる」では足りない: `Wave` があると `render.py` は毎フレーム
      `live_rows` を問い合わせ、Live の刷新回数も波の `period` に引きずられる。
      係が None なら Live は既定の刻みに戻り、波の分岐は 1 度も走らない。
    """
    from laps.term.wave import make_factory

    cfg = tmp_path / "xdg" / "blackwall" / "config.toml"
    cfg.parent.mkdir(parents=True)

    # ① 設定ファイルの wave = "off"
    cfg.write_text('wave = "off"\n', encoding="utf-8")
    assert make_factory(60) is None
    assert make_factory(60, calm=True) is None        # `off` は --calm で覆らない
    # ② 環境変数の LAPS_WAVE=off
    cfg.write_text('wave = "on"\n', encoding="utf-8")
    monkeypatch.setenv("LAPS_WAVE", "off")
    assert make_factory(60) is None
    monkeypatch.delenv("LAPS_WAVE")

    # ③ off でなければ係は本物を返す（幅は呼ぶたびに評価＝端末幅を追える）
    cols = [70]
    f = make_factory(lambda: cols[0])
    assert f is not None
    w = f()
    assert isinstance(w, Wave) and w.mode == "on" and w.width == 70
    cols[0] = 90
    assert f().width == 90
    assert make_factory(60, calm=True)().mode == "calm"   # type: ignore[misc]

    # ④ それでも作られてしまった `Wave` は 1 行も占めず 1 度も絵を作らない（二重の守り）
    off = Wave(60, mode="off", delay=0)
    assert off.enabled is False and off.live_rows == 0
    for _ in range(5):
        assert _draw(off) == []
    assert off.frames_drawn == 0 and off.calls == 5
