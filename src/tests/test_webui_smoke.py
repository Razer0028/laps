"""Web クライアントの煙試験を preflight に相乗りさせる（2026-09-07）。

★なぜ pytest から呼ぶか: `scripts/preflight.sh` は `pytest src/tests` を通す。ここに 1 本
  置けば、画面（`laps/webui/`）の煙試験が出荷の連鎖に**繋がる**——別に置いて
  「走らせるのを忘れる」のが、この現場で何度も起きた形（「書いたが繋いでいない」）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parents[1] / "laps" / "webui" / "run_smoke.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="node が無い（開発機 には在る）")
def test_webui_smoke_passes():
    assert SMOKE.is_file(), SMOKE
    r = subprocess.run(["node", str(SMOKE)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
    # 「1 件も検査していないのに緑」を防ぐ（空の試験は緑になる）。
    assert r.stdout.count("ok  ") >= 40, r.stdout[-2000:]


def _ratio(a: str, b: str) -> float:
    def lum(h: str) -> float:
        h = h.lstrip("#")
        r, g, bl = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(bl)
    la, lb = lum(a), lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def test_webui_colors_are_readable():
    """文字が実際に読める明るさか（AA 4.5:1）。

    ★`scripts/check_contrast.py` は `app/ui/styles.css` を**名指し**なので、この画面は
      その門の外にある。同じ物差しをここに置く（門の外に新しい面を作らない）。
      失敗を読み解く面ほど `--text-3` / `--red-text` を使う——いちばん読みたいときに
      いちばん読めない、を実際に踏んでいる。
    """
    import re
    css = (Path(__file__).resolve().parents[1] / "laps" / "webui" / "styles.css").read_text(encoding="utf-8")
    tok = dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9A-Fa-f]{6})", css))
    bad = []
    for t in ("--text", "--text-2", "--text-3", "--red-text", "--ok-text"):
        for b in ("--bg", "--bg-2", "--bg-3", "--bg-4"):
            r = _ratio(tok[t], tok[b])
            if r < 4.5:
                bad.append(f"{t}({tok[t]}) 対 {b}({tok[b]}) = {r:.2f}:1")
    assert not bad, bad
    # opacity で台無しにしない（色を直しても合成後で割る前例がある）。
    assert "opacity:" not in css.replace("opacity:1", ""), "文字に opacity を掛けない"
