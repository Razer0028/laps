"""配色の目盛りの門 — 値を変えたら落ちる。

★この試験の役割は「色が綺麗か」ではなく、**色を落としても意味が残るか**を機械で押さえること。
  1) contrast（本文 4.5:1 / 図形 3:1）を**計算する**（値を書き写さない）
  2) 色が無い経路（NO_COLOR / dumb / 16 色）の退避先が実際にそうなることを測る
  3) 目盛りから名前を消したら、まだ使っている場所を必ず捕まえる（import ごと死ぬ事故の門）
★色システムの降格は **色ごとに別プロセス**で測る。Rich の `Style._ansi` は最初に描いた
  色システムを焼き付けるので、同一プロセスで truecolor → standard と測ると嘘が出る
  （`scratch/tui/audit-color/wave_downgrade.py` の教訓）。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

from laps.term import theme as T

TERM_DIR = Path(T.__file__).resolve().parent
THEME_PY = TERM_DIR / "theme.py"

# 想定する地。Solarized dark が最悪値になる。
GROUNDS = {
    "app #131011": "#131011",
    "純黒 #000000": "#000000",
    "既定 #1C1C1C": "#1C1C1C",
    "Solarized dark #002B36": "#002B36",
}

AA_BODY = 4.5      # 本文（読ませる文字）
AA_SHAPE = 3.0     # 図形（点・線・波）
NOTE_FLOOR = 4.0   # 注記は本文ではないので一段緩い床（★本文に使わないことが前提）


# -- contrast を計算する（WCAG 2.x 相対輝度） --------------------------------

def _srgb(v: str) -> tuple[float, float, float]:
    v = v.lstrip("#")
    assert len(v) == 6, f"6 桁の #RRGGBB でない: {v}"
    return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hexcolor: str) -> float:
    r, g, b = (_lin(c) for c in _srgb(hexcolor))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_function_matches_known_values():
    """物差しそのものを先に確かめる（WCAG の既知値）。ここが狂うと以下が全部無意味。"""
    assert contrast("#FFFFFF", "#000000") == pytest.approx(21.0, abs=0.01)
    assert contrast("#000000", "#000000") == pytest.approx(1.0, abs=0.01)
    assert contrast("#767676", "#FFFFFF") == pytest.approx(4.54, abs=0.02)


# -- 1) 目盛りの contrast ----------------------------------------------------

@pytest.mark.parametrize("name", ["TEXT", "META", "ALERT", "GOOD"])
def test_body_colors_meet_aa_on_every_ground(name):
    """読ませる文字は 4 地すべてで AA(4.5:1)。値を変えたらここが落ちる。"""
    color = getattr(T, name)
    for label, bg in GROUNDS.items():
        cr = contrast(color, bg)
        assert cr >= AA_BODY, f"{name} {color} は {label} で {cr:.2f}:1 ＝ AA({AA_BODY}) 未満"


def test_note_color_meets_relaxed_floor_but_not_body():
    """NOTE は注記専用。本文の床は通らなくてよいが、図形の床(3:1)は必ず通る。
    ★ここが 4.5 を通り始めたら「本文に使ってよい」と誤読されるので、上限も見ない
      （上げるのは自由）。押さえるのは下限だけ。"""
    for label, bg in GROUNDS.items():
        cr = contrast(T.NOTE, bg)
        assert cr >= NOTE_FLOOR, f"NOTE {T.NOTE} は {label} で {cr:.2f}:1 ＝ {NOTE_FLOOR} 未満"
        assert cr >= AA_SHAPE


@pytest.mark.parametrize("name", ["ALERT", "GOOD", "NOTE"])
def test_shape_colors_meet_3to1(name):
    """点・波・線として使う色は 4 地すべてで 3:1。"""
    color = getattr(T, name)
    for label, bg in GROUNDS.items():
        cr = contrast(color, bg)
        assert cr >= AA_SHAPE, f"{name} {color} は {label} で {cr:.2f}:1 ＝ 図形の 3:1 未満"


def test_rejected_values_would_fail_the_gate():
    """★門が本当に噛むことの証拠。捨てた値を入れると落ちる、を明示しておく。
    （そうでないと「全部通った」が門の緩さなのか値の良さなのか分からない）"""
    old_red, old_ok, old_line = "#D0342C", "#6E8A7C", "#2C2325"
    assert min(contrast(old_red, b) for b in GROUNDS.values()) < AA_BODY   # 旧赤は本文で落ちる
    assert min(contrast(old_ok, b) for b in GROUNDS.values()) < AA_BODY    # 旧緑も落ちる
    assert max(contrast(old_line, b) for b in GROUNDS.values()) < AA_SHAPE  # 旧 LINE は線ですらない


def test_accent_is_a_single_red():
    """アクセントは 1 色。赤を名乗る目盛りが 2 つ以上の値を持ったら落ちる。
    （2026-08-29: RED_2 は削除済み＝除外はもう要らない。合流のときに消した）"""
    reds = {T.ALERT, T.RED, T.RED_TEXT, T.S_WAVE_CREST, T.S_BAD_BODY}
    assert reds == {T.ALERT}, f"赤が 1 本になっていない: {reds}"
    assert T.S_BAD == f"bold {T.ALERT}"


def test_styles_paint_no_background():
    """端末の地は利用者のもの。どのスタイルも背景を塗らない（`on` を持たない）。"""
    for name in dir(T):
        if not name.startswith("S_"):
            continue
        value = getattr(T, name)
        assert isinstance(value, str)
        assert " on " not in value, f"{name} が背景を塗っている: {value!r}"


# -- 2) 色が無いときの退避先 -------------------------------------------------

def test_marks_are_distinct():
    """成功・失敗・実行中は**字**で割れている。色が 1 バイトも出なくても残る本体。"""
    marks = [T.MARK_OK, T.MARK_BAD, T.MARK_RUN]
    assert len(set(marks)) == 3, f"印が重複している: {marks}"
    assert set(T.MARKS.values()) == set(marks)


@pytest.mark.parametrize("mark_name", ["MARK_OK", "MARK_BAD", "MARK_RUN"])
def test_marks_have_stable_width(mark_name):
    """印は East Asian Width = N/Na（幅 1 で揺れない）。A（曖昧）は端末ごとに 1 か 2 に
    化けて罫線がずれるので使わない。★`●` `·` はどちらも A だった。"""
    ch = getattr(T, mark_name)
    assert len(ch) == 1
    eaw = unicodedata.east_asian_width(ch)
    assert eaw in ("N", "Na"), f"{mark_name}={ch!r} の East Asian Width が {eaw}（幅が揺れる）"


def _downgrade_in_subprocess(color_system: str, colors: dict[str, str]) -> dict[str, str]:
    """★別プロセスで測る（Rich が最初の色システムを焼き付けるため）。"""
    src = (
        "import json,sys\n"
        "from rich.color import Color, ColorSystem\n"
        f"cs = ColorSystem.{color_system}\n"
        f"cols = {colors!r}\n"
        "print(json.dumps({k: ','.join(Color.parse(v).downgrade(cs).get_ansi_codes())"
        " for k, v in cols.items()}))\n"
    )
    out = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, check=True)
    import json
    return json.loads(out.stdout)


def test_16color_alert_stays_in_the_red_family():
    """16 色端末で赤が**赤のまま**落ちること。旧 #D0342C は 33（黄）に落ちていた。"""
    got = _downgrade_in_subprocess("STANDARD", {"alert": T.ALERT, "old_red": "#D0342C"})
    assert got["alert"] == "91", f"ALERT が 16 色で {got['alert']} に落ちた（91 bright red のはず）"
    assert got["old_red"] == "33"  # 旧値がなぜ駄目だったかの記録


def test_16color_green_collapses_so_marks_must_carry_meaning():
    """★16 色では緑は色として立たない（GOOD も META も NOTE も 37）。
    だから成否は色ではなく MARK_* が担う、という前提をここで固定する。
    もし将来ここが割れる値にしたなら、この試験ごと書き換えて理由を残すこと。"""
    got = _downgrade_in_subprocess("STANDARD", {"good": T.GOOD, "meta": T.META, "note": T.NOTE})
    assert got["good"] == got["meta"] == got["note"] == "37"
    assert T.MARK_OK != T.MARK_BAD  # 色が潰れても字は割れている


def test_256color_keeps_red_and_green_apart():
    """tmux の既定は 256 色＝このラボで一番踏まれる経路。ここでは色が立つこと。
    旧 OK #6E8A7C は 243（灰）に落ちて緑に見えなかった。"""
    got = _downgrade_in_subprocess(
        "EIGHT_BIT", {"alert": T.ALERT, "good": T.GOOD, "note": T.NOTE, "old_ok": "#6E8A7C"})
    assert got["alert"] != got["good"] != got["note"]
    assert got["good"] == "38,5,108"   # 緑の帯
    assert got["old_ok"] == "38,5,243"  # 旧値は灰の帯に落ちていた


def test_no_color_leaves_the_marks_readable():
    """`NO_COLOR=1` の Console は色を 1 バイトも出さない。
    それでも成功と失敗が**バイト単位で違う**ことを、印だけで確かめる。"""
    import io
    from rich.console import Console
    from rich.text import Text

    def render(mark: str, style: str) -> bytes:
        buf = io.StringIO()
        c = Console(file=buf, width=40, no_color=True, force_terminal=True, color_system="truecolor")
        c.print(Text(f"{mark} Bash (systemctl status x)", style=style))
        return buf.getvalue().encode()

    ok = render(T.MARK_OK, T.S_MARK_OK)
    bad = render(T.MARK_BAD, T.S_MARK_BAD)
    assert ok != bad, "NO_COLOR で成功と失敗がバイト単位で同一になっている"
    assert b"38;2;" not in ok  # 色が本当に出ていないことの確認（出ていたら試験が緩い）


# -- 3) 名前を消したら、使っている場所を捕まえる ------------------------------

def _names_defined_in_theme() -> set[str]:
    tree = ast.parse(THEME_PY.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {n for n in names if not n.startswith("_")}


def test_every_name_used_in_term_still_exists():
    """★これが本丸の門。SPEC の反証で実際に踏んだ事故:
    `theme.RED_2` を消すと `wave.py` のモジュール直下の実行文が AttributeError で死に、
    `app.py` の import ごと落ちて test_term_* が collection の時点で全滅する。
    「使っている場所を同じコミットで直せないなら名前を消すな」を機械で押さえる。"""
    defined = _names_defined_in_theme()
    used: dict[str, list[str]] = {}
    pat = re.compile(r"\b(?:T|theme)\.([A-Z][A-Z0-9_]*)\b")
    for py in sorted(TERM_DIR.glob("*.py")):
        if py.name == "theme.py":
            continue
        for name in pat.findall(py.read_text(encoding="utf-8")):
            used.setdefault(name, []).append(py.name)
    missing = {n: v for n, v in used.items() if n not in defined}
    assert not missing, f"theme に無い名前を term/ が使っている（import が落ちる）: {missing}"


def test_no_dead_names_in_the_scale():
    """目盛りに死んだ名前を置かない（旧 LINE は定義だけで使用 0 件だった）。
    別名・申し送りは対象外＝下の許可表に載っているものだけが「使われていなくてよい」。"""
    allowed_unused = {
        # 移行のための別名・次の担当が使う名前・データ（すべて theme.py 内で理由つき）
        "MARKS", "MARK_OK", "MARK_BAD", "MARK_RUN",
        "S_WAVE_LOW", "S_WAVE_CREST",   # 項目 21 が wave.py から使う
        "S_MARK_OK", "S_MARK_BAD", "S_MARK_RUN", "S_BAD",  # 項目 6 が render.py から使う
        "TEXT", "META", "NOTE", "ALERT", "GOOD",           # 役割名（別名経由で使われている）
        "TEXT_2", "TEXT_3", "RED", "RED_TEXT", "OK",
        "S_RED", "S_DOT_RUN", "S_DOT_OK", "S_DOT_BAD",
    }
    defined = _names_defined_in_theme()
    text = "\n".join(p.read_text(encoding="utf-8") for p in TERM_DIR.glob("*.py")
                     if p.name != "theme.py")
    text += "\n".join(p.read_text(encoding="utf-8")
                      for p in (Path(__file__).parent).glob("test_term_*.py"))
    dead = sorted(n for n in defined - allowed_unused
                  if not re.search(rf"\b(?:T|theme)\.{n}\b", text))
    assert not dead, f"定義だけで使われていない名前: {dead}（消すか、理由つきで許可表へ）"
