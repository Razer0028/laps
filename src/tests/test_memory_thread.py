"""「いま何をしているか」を見失わないための 3 つ（2026-09-13・オーナー指摘）。

  ①過去の依頼に「完了済み」の札を貼る（消さずに見分けをつける）
  ②末尾に【いまの依頼】＋【ここまで】を置く（ハーネスの事実だけで組む）
  ③帯4 が畳む assistant の語りを直近 2 本まで守る

★①の機構: `context_budget` は人が書いた user をどの段でも落とさないので、
  **過去の依頼が窓に残り続ける**（記録の実測: 2 本以上が 260 走行・最大 10 本）。
★②の置き場所: 末尾は切り詰めで消えず、注意が強く当たり、**接頭辞キャッシュを壊さない**
  （末尾追記 0.10s 対 先頭書き換え 26.00s ＝ 260 倍）。
★③の機構: 帯4 は「中間の assistant 散文」を 1 行に畳む＝モデル自身が書いた
  「いま何をしていて次に何をするか」が真っ先に消える。素材が残って筋書きだけ消える。
"""
from __future__ import annotations

from laps import context_budget


def test_直近の語りを2本守る():
    """★1 本だと 2 手前に書いた『次はこれをする』が消える。"""
    assert context_budget.KEEP_LAST_PROSE >= 2
    msgs = [{"role": "assistant", "content": f"語り{i}"} for i in range(5)]
    prot = context_budget._protected(msgs, keep_last=0)
    assert {3, 4} <= prot, prot
    assert 0 not in prot, "古い語りまで守っている（窓が埋まる）"


