"""「入らない 1 本」が、他の束を巻き添えにしていた（2026-08-31 実測・線D の続き）。

`fold.round_robin` は「どの束からも最低 1 本は見える」ために書かれた配分だが、
`return` が内側のループの中にあり、**予算に入らない行に当たった時点で配分そのものを
打ち切って**いた。長い行がどこに来るかは走査順で決まるので、結果はこうなっていた
（実物の Grep 結果 168 行・予算 30,000 バイト・並び順だけを変えて 6 通り）:

    旧: 表示 29/17/18/21/16/17 行   予算の使い残し 907〜2,392 バイト
    新: 表示 33/27/27/22/30/16 行   予算の使い残し    1〜   52 バイト

同じ 168 件の一致が、**並び順しだいで 16〜29 行**になっていた。しかも打ち切るので
予算は毎回余っていた——公平どころか、たまたま長い行を持つ束が他を巻き添えにしていた。

実機での見え方（同じ問い `tool_failed`・同じ木）:
    rg 31 行 / grep 19 行 / Python 11 行  →  直して 35 / 34 / 21 行。
"""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import pytest

from laps.tools import fold

_SRC = Path(__file__).resolve().parents[1]
_TOOLS = _SRC / "laps" / "tools"
_REPORTS = _SRC / "REPORTS"


def _real_lines() -> list[str]:
    """実物のファイルから `file:line:本文` の形の行を作る（作り物の文字列は使わない）。

    短い行（道具のソース・最長 159 バイト）と長い行（REPORTS の日本語散文・
    最長 18,167 バイト）を**両方**入れる。実機で予算を食い潰していたのは後者の形で、
    しかもどちらもこの現場が毎日 Grep している実物。
    """
    out = []
    for p in sorted(_TOOLS.glob("*.py")) + sorted(_REPORTS.glob("*.md")):
        try:
            body = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, ln in enumerate(body.splitlines(), 1):
            if "★" in ln:
                out.append(f"{p}:{i}:{ln}")
    return out


@pytest.fixture(scope="module")
def real():
    lines = _real_lines()
    assert len(lines) > 200, "実物の入力が集まらない（この試験の前提が崩れている）"
    return lines


def _groups(lines):
    g: "OrderedDict[str, list[str]]" = OrderedDict()
    for ln in lines:
        g.setdefault(ln.split(":", 1)[0], []).append(ln)
    return g


def test_入らない一本が他の束を巻き添えにしない(real):
    """先頭の束の 1 本目が予算に入らないとき、**他の束は続く**。"""
    longest = max(real, key=fold.nbytes)
    shorts = sorted(real, key=fold.nbytes)[:6]
    groups = OrderedDict([
        ("A", [longest, shorts[0]]),
        ("B", [shorts[1]]),
        ("C", [shorts[2]]),
    ])
    cap = fold.nbytes(longest) - 1          # 長い1本は入らず、短い3本は入る
    assert cap > sum(fold.nbytes(s) + 1 for s in shorts[:3])
    shown, omitted = fold.round_robin(groups, cap)
    assert shorts[1] in shown and shorts[2] in shown, "他の束が巻き添えで消えている"
    assert longest not in shown, "入らない行を無理に出している（予算を超える）"


def test_予算を超えない(real):
    for cap in (500, 3000, 30000):
        groups = _groups(real)
        shown, _ = fold.round_robin(groups, cap, reserve=200)
        used = 200 + sum(fold.nbytes(s) + 1 for s in shown)
        assert used <= cap, f"cap={cap} を {used - cap} バイト超えた"


def test_省いた件数は最後まで正確(real):
    """打ち切りをやめると、`omitted` が実際の残りと合わなくなりやすい。
    **表示した数＋省いた数＝全部** を押さえる（件数行がここから作られる）。"""
    for cap in (500, 3000, 30000):
        groups = _groups(real)
        total = sum(len(v) for v in groups.values())
        shown, omitted = fold.round_robin(groups, cap, reserve=200)
        assert len(shown) + sum(omitted.values()) == total, f"cap={cap}"


def test_予算を使い切る(real):
    """旧実装は打ち切るので予算が 900〜2,400 バイト余っていた。

    ★これは「多く見せる」ための試験ではなく、**見せられたのに見せなかった**
      ことを捕まえるための試験。残りが最短の 1 行より小さいところまで詰める。
    """
    groups = _groups(real)
    cap = 30000
    shown, omitted = fold.round_robin(groups, cap, reserve=200)
    used = 200 + sum(fold.nbytes(s) + 1 for s in shown)
    assert omitted, "この予算では全部は入らないはず（前提の確認）"
    left = [ln for k, v in groups.items() for ln in v if ln not in shown]
    assert cap - used < min(fold.nbytes(ln) + 1 for ln in left), \
        f"まだ入る行があるのに止めている（残り {cap - used} バイト）"


def test_並び順で見える量が大きく変わらない(real):
    """同じ一致・同じ予算なら、走査順が変わっても見える量はおおむね同じであるべき。
    実測（旧）: 16〜29 行。予算の使い残しが順番で変わることが原因だった。"""
    import random
    used_left = []
    for seed in range(6):
        ls = real[:]
        random.Random(seed).shuffle(ls)
        groups = _groups(ls)
        shown, _ = fold.round_robin(groups, 30000, reserve=200)
        used_left.append(30000 - (200 + sum(fold.nbytes(s) + 1 for s in shown)))
    assert max(used_left) < 200, f"並び順によって予算が余っている: {used_left}"
