"""字数カウンタは、1回の失敗でセッション全体を近似に落とさない。

★実機で踏んだ形（2026-08-19）: 推論器の起動直後に最初の /tokenize が落ち、
  以後ずっと近似（UTF-8 バイト数＝安全側の上界）になった。見積りは
  実測 7,713 tok に対して 97,898 tok ＝ **約12倍**。その結果
  文脈の整理が「使えない」と判定されて働かず、整理されないまま送って
  本物の 400 で溢れた。画面には「固定分 24,151・使えるのは約0」という
  嘘の数字が出た。
"""
from __future__ import annotations

from laps import context_budget as cb


class _Counter(cb.TokenCounter):
    """/tokenize の成否を差し替えられるようにしたもの。"""

    def __init__(self, results):
        super().__init__("http://example.invalid/v1")
        self._results = list(results)
        self.calls = 0

    def _post_tokenize(self, text):
        self.calls += 1
        r = self._results.pop(0) if self._results else None
        return r


def test_one_failure_does_not_latch():
    """★起動直後の1回の取りこぼしで、セッション全体を壊さない。"""
    c = _Counter([None, 7, 9])
    c.count("a")            # 失敗 → まだ固定しない
    assert c.exact() is False or c._exact is None
    c.count("b")            # 成功 → 実測へ
    assert c.exact() is True


def test_latches_after_consecutive_failures():
    """★続けて駄目なら固定する（実測と近似が毎ターン入れ替わると
    投影のバイトが揺れて接頭辞キャッシュが飛ぶ）。"""
    c = _Counter([None] * 10)
    for i in range(cb._EXACT_FAIL_LATCH):
        c.count(f"t{i}")
    assert c._exact is False
    before = c.calls
    c.count("after-latch")
    assert c.calls == before, "固定後も /tokenize を叩いている"


def test_success_resets_the_streak():
    c = _Counter([None, None, 5, None, None, 3])
    for i in range(6):
        c.count(f"u{i}")
    assert c._exact is True, "成功を挟んでいるのに固定された"


def test_approximation_is_an_upper_bound():
    """近似は UTF-8 バイト数＝証明できる上界。過小に倒さない。"""
    for s in ("hello", "日本語", "1234567890 " * 10, "\n" * 50):
        assert cb.approx_tokens(s) == len(s.encode("utf-8"))
