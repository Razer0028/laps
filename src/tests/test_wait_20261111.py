"""別の機械や列の仕事を待って続きを始める（2026-11-11 オーナー「自動的にタスクを再開する機能とかって実装されてるの？」→「任せます」）。

・bwtool wait（oc/waiter.py）が確かめの命令を繰り返し、通ったら 0 で終わる・--max で 3。
・待ち受けの命令（bwtool wait・while…do…sleep・単独の sleep）は「止まって見える」とされない（tools/shell.py の is_waiter）。
・待っている物が無いのに「終わったら報告します」と書いて終えたら 1 回だけ差し戻す（engine/finish.py の _promise）。
"""
from __future__ import annotations

import sys
import time
from types import SimpleNamespace

from laps.engine import finish
from laps.engine.tools import shell
from laps.oc import waiter


def test_wait_until_and_max(tmp_path):
    flag = tmp_path / "done"
    flag.write_text("x")
    py = sys.executable
    assert waiter.main(["--until", f'"{py}" -c "import os,sys; sys.exit(0 if os.path.exists(r\'{flag}\') else 1)"', "--every", "5"]) == 0
    t0 = time.monotonic()
    assert waiter.main(["--while", f'"{py}" -c "pass"', "--every", "5", "--max", "1"]) == 3
    assert time.monotonic() - t0 < 30
    assert waiter.main(["--until", "x", "--while", "y"]) == 2


def test_waiter_commands_are_not_stalls():
    yes = ['bwtool wait --until "ssh p40 test -f /x"', "while ssh p40 kill -0 1; do sleep 60; done",
           "until [ -f d ]; do sleep 30; done; echo ok", "while true\ndo\n  sleep 5\ndone", "while ($true) { Start-Sleep 60 }",
           "sleep 1800"]
    no = ["make -j8", "sleep 600 && make", "python train.py  # while training sleep", "while read l; do echo $l; done < f"]
    assert all(shell.is_waiter(c) for c in yes)
    assert not any(shell.is_waiter(c) for c in no)


class _Jobs:
    def __init__(self, n):
        self.n = n

    def waiting(self):
        return [object()] * self.n


def _agent(waiting=0, child=False):
    counts = []
    return SimpleNamespace(child=child, rid="r", ctx=SimpleNamespace(jobs=_Jobs(waiting)), _count=counts.append,
                           out=SimpleNamespace(notice=lambda *a, **k: None), counts=counts)


def test_promise_without_waiter_is_sent_back_once():
    a = _agent()
    f = finish.Finish(a, None)
    body = "4070 のキューが終わり次第、結果を報告します。"
    note = f._promise(body)
    assert "bwtool wait" in note and "promise_pushback" in a.counts
    assert f._promise(body) == ""                       # 1 走行 1 回


def test_promise_is_fine_when_something_waits_or_is_a_request():
    assert finish.Finish(_agent(waiting=1), None)._promise("終わったら報告します") == ""
    assert finish.Finish(_agent(), None)._promise("ビルドが終わったら確かめてください。") == ""
    assert finish.Finish(_agent(child=True), None)._promise("I will report back once it finishes.") == ""
    assert finish.Finish(_agent(), None)._promise("直して試験を通しました。") == ""
