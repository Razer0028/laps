"""起動の銘（カード表示のときだけ）。

2026-10-06 オーナー「かっこいい TUI にしておいてください。Blackwall をテーマに」。
★字体は figlet の ANSI Shadow（6 段・74 桁）。`█` と罫線は East Asian Ambiguous＝曖昧幅を 2 桁に
  している端末では崩れる。カード表示はもともと罫線の枠を使う（render.card_on の注記）ので条件は同じ。
★幅が足りないときは大きな銘を出さず 1 行の銘に落とす（折り返した銘は銘に見えない）。
"""
from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from . import theme as T

LOGO = (
    "██╗         █████╗    ██████╗    ███████╗",
    "██║        ██╔══██╗   ██╔══██╗   ██╔════╝",
    "██║        ███████║   ██████╔╝   ███████╗",
    "██║        ██╔══██║   ██╔═══╝    ╚════██║",
    "███████╗██╗██║  ██║██╗██║     ██╗███████║",
    "╚══════╝╚═╝╚═╝  ╚═╝╚═╝╚═╝     ╚═╝╚══════╝",
)
LOGO_WIDTH = max(len(s) for s in LOGO)
#: 大きな銘を出す最小の幅。名前を L.A.P.S にして銘は 41 桁になったが、切り替わる幅は前のまま（会話の枠 72 桁では 1 行の銘）。
LOGO_MIN_WIDTH = 77
TAGLINE = "ローカルで動く自律エージェント ─ 開発とインフラのために"


def banner(width: int) -> Text:
    """起動時に 1 度だけ出す銘。width は端末の桁数。"""
    width = int(width or 80)
    t = Text()
    if width >= LOGO_MIN_WIDTH:
        pad = " "
        for row, color in zip(LOGO, T.LOGO_ROWS):
            # 塗り（█）と影（罫線）で明るさを変える＝立体に見せる。影は 1 段暗い色。
            t.append(pad)
            shade = T.LOGO_ROWS[min(len(T.LOGO_ROWS) - 1, T.LOGO_ROWS.index(color) + 2)]
            for ch in row.rstrip():
                t.append(ch, style=f"bold {color}" if ch == "█" else shade)
            t.append("\n")
        rule = "─" * max(0, LOGO_WIDTH - cell_len(TAGLINE) - 1)
        t.append(pad + rule + " ", style=T.S_WALL_RULE)
        t.append(TAGLINE, style=T.S_DIM)
    else:
        t.append("▐ ", style=T.S_BAD)
        t.append("L.A.P.S", style=T.S_BAD)
        # ★副題は入る分だけ（2026-10-21 TUI の会話の枠 72 桁で「…インフラのため / に」と 1 字だけ折れていた）。
        for tag in (TAGLINE, TAGLINE.split(" ─ ", 1)[0], ""):
            if not tag or cell_len(t.plain) + 2 + cell_len(tag) <= width - 1:
                if tag:
                    t.append("  " + tag, style=T.S_DIM)
                break
    return t
