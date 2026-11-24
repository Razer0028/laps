# -*- coding: utf-8 -*-
"""反証c: r4c の must_fix#1 が『行の枝』に届いていないことを固定する。

★出所と扱い（2026-09-05 r4c 3 巡目）: この 4 本は**査読が書いた反例**で、
  2 巡目（`71ba5b3`）の実装に対して 4 本とも赤だった。3 巡目で 4 本とも緑に
  なったが、**査読の言葉のまま残す**——同じ受け入れ条件を
  `test_tool_output_folding.py` 側にも property test として書き直してあるが、
  そちらは私が書いた形なので、私が条件を無自覚にゆるめたときに気づけない。
  ★消さないこと。消すなら「この 4 本が測っているものを、別の誰かが書いた形で
  測り続けられる」ことを先に示すこと。
  陰性対照: `laps/tools/fold.py` を `71ba5b3` に戻すと 4 本とも赤に戻る。
"""
import json, re
from pathlib import Path
from laps.tools import fold

_MARK = re.compile(r"…\s*\(中略:.*?\)\s*…")
def _real(s): return fold.nbytes(_MARK.sub("", s).strip())
def _tail_real(s):
    p = _MARK.split(s)
    return fold.nbytes(p[-1].strip()) if len(p) > 1 else -1

def test_R1_境目399は行の枝には効いていない():
    """MIN_TAIL_BYTES の門は `_fold_bytes` にしかない。4 行以上の短い行では
    cap=135 から畳んでマーカーを出す（fold.py:202-204 の申告と食い違う）。"""
    text = "\n".join("line%d" % i for i in range(2000))
    folded = [c for c in range(0, 399)
              if "中略" in fold.fold_head_tail(text, c)]
    assert not folded, f"cap<399 で {len(folded)} 個畳んでいる: {folded[:5]}"

def test_R2_短い行では畳みが切り捨てより実文字を減らす():
    text = "\n".join("line%d" % i for i in range(2000))
    for cap in (150, 177, 224, 300, 398):
        out, _ = fold.fold_head_tail_ex(text, cap)
        clip = fold.clip_bytes(text, cap)
        assert _real(out) >= fold.nbytes(clip.strip()), (
            f"cap={cap}: 畳み {_real(out)}B < 切り捨て {fold.nbytes(clip.strip())}B")

def test_R3_末尾が空行だけでも末尾を残したと言う():
    """本文が先頭・空行が末尾。head に実文字があるので real_kept>0 を通り、
    tail は空行だけなのに『末尾は残しています』と書く。"""
    text = "\n".join("本文行%d の実際の内容がここに書いてある" % i for i in range(40)) + "\n" * 800
    for cap in (300, 400, 500, 700):
        out, tail_kept = fold.fold_head_tail_ex(text, cap)
        if "末尾は残しています" in out:
            assert _tail_real(out) > 0, (
                f"cap={cap}: 末尾を残したと言うが末尾の実文字は 0 "
                f"(tail={_MARK.split(out)[-1][:20]!r})")

