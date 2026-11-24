"""「止める」の3つの穴。

★敵対レビュー（2026-08-23）で見つかり、実機の記録で裏づけられた:
  今日「止める」を何度も押したのに、記録に cancelled が **0 件**。
  止めた走行は done（正常完了）として記録されていた。

穴A: run() が冒頭で中断旗を消す。公開直後の「止める」は run() 自身が消す。
穴B: 道具の実行中（Bash は最長 720 秒）は中断を一切見ていない。
穴C: （アプリ側）3秒で返事が無いと meta 空で抜け、outcome が done になる。
"""
from __future__ import annotations

import threading
import time

import pytest

from laps.oc.base import Agent
from laps.tools import bash


class _A:
    """中断まわりだけを取り出した殻。"""
    def __init__(self):
        self.counters = {}
        self.backend = type("B", (), {"close": lambda s: None, "soft_reset": lambda s: None})()
        self.request_cancel = Agent.request_cancel.__get__(self, _A)
        self._is_cancelled = Agent._is_cancelled.__get__(self, _A)

    def start_run(self):
        """run() 冒頭の旗の扱いだけを再現する（本体と同じ判定）。"""
        ev = getattr(self, "_cancel_event", None)
        prev_end = getattr(self, "_run_ended_at", None)
        cancel_at = getattr(self, "_cancel_at", None)
        for_this = cancel_at is not None and (prev_end is None or cancel_at > prev_end)
        if ev is not None and not for_this:
            ev.clear()

    def end_run(self):
        import time
        self._run_ended_at = time.monotonic()


def test_hole_a_cancel_before_run_start_survives():
    """★穴A: 公開直後に来た「止める」を、run() の冒頭が消してはいけない。"""
    a = _A()
    a.request_cancel()         # run() が始まる前に「止める」が来た
    a.start_run()              # run() の冒頭
    assert a._is_cancelled(), "run() の冒頭が中断を消している（穴A）"


def test_previous_runs_cancel_is_cleared():
    """前の走行の中断は持ち越さない（これは正しい処置。壊していないこと）。"""
    a = _A()
    a.start_run()              # 1本目が始まる
    a.request_cancel()         # 1本目を止める
    a.end_run()                # 1本目が終わった
    a.start_run()              # 2本目（中断は前の走行宛て＝消える）
    assert not a._is_cancelled(), "前の走行の中断が2本目に持ち越されている"


@pytest.mark.skipif(not hasattr(bash, "kill_running"), reason="未実装")
def test_hole_b_running_bash_is_killed(monkeypatch):
    """★穴B: 走っている Bash を中断側から殺せること。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    out = {}
    def run():
        out["r"] = str(bash._run({"command": "sleep 60", "timeout": 120}))
    t = threading.Thread(target=run); t.start()
    time.sleep(0.6)                       # 子が起動するのを待つ
    n = bash.kill_running()
    t.join(timeout=10)
    assert not t.is_alive(), "殺したのに Bash が戻ってこない"
    assert n >= 1, "走っている Bash が台帳に無い"


def test_hole_b_cancel_reaches_running_bash():
    """★穴B: 「止める」が、走っている Bash を孫ごと殺す経路に繋がっていること。

    ★最初は「道具の実行前に弾く」形で書いたが、既存の試験（道具を1回叩いた
      直後に中断→集めた結果を見せる）を壊した。返事済みの手番の道具は
      実行してよい。止めるべきは**走っている最中の Bash**のほう。
    """
    import inspect
    src = inspect.getsource(Agent.request_cancel)
    assert "kill_running" in src, "request_cancel が走っている Bash を殺していない"
