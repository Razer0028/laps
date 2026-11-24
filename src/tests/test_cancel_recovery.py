"""中断のあと、次の依頼がまともに動くか。

★実機で壊れた形（2026-08-16 v0.1.49・オーナーの試験中）:
停止ボタンを1回押したあと、

    08:02:22  問い「Glob で一覧して」 → 答えは**前の問い**（OS と作業ディレクトリ）が 0.0 秒で返る
    08:04:06  …grep "検索語" フ        ← ここで切れて
    08:04:12  ァイル.txt  06 | …       ← **次の質問の答えが、その続きから始まる**

原因は2つ、どちらも「使い回し」を想定していなかったこと:
  1. `_cancel_event` を誰も下ろさない → 以後すべての run が iter 0 で中断扱い
  2. `request_cancel` が呼ぶ `backend.close()` は `_hard_closed` を立てるので、
     推論器が二度と開き直さない

`request_cancel` の docstring はこう書いていた——
「ベンチでは test_case ごとに新しい Agent を作るので close() は次のケースへ漏れない」。
**アプリは Agent を使い回す。** ベンチの前提が実運用に持ち込まれていた。

`Backend.soft_reset` は「新しい run() の冒頭で前回の中断から回復するために使う」と
docstring に書かれていたのに、**一度も呼ばれていなかった**（grep で 0 件）。
"""
from __future__ import annotations

import threading

import pytest

from laps.backend import Backend, BackendConfig


def test_soft_resetは中断の旗を下ろして開き直す() -> None:
    b = Backend(BackendConfig())
    b.close()
    assert b._hard_closed is True, "close() が閉じた印を立てていない"
    b.soft_reset()
    assert b._hard_closed is False, "soft_reset() が開き直していない"




def test_中断していない回では旗が立たない() -> None:
    from laps.oc.base import Agent

    a = Agent.__new__(Agent)
    assert a._is_cancelled() is False, "旗が無いのに中断扱いになっている"


def test_request_cancelは旗を立てる() -> None:
    from laps.oc.base import Agent

    class FakeBackend:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    a = Agent.__new__(Agent)
    a.backend = FakeBackend()
    a.request_cancel()
    assert a._is_cancelled() is True
    assert a.backend.closed is True, "飛んでいる要求を閉じていない＝中断が効かない"
