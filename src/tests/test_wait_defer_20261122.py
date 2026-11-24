"""2026-11-22 オーナー「JOB の完了まで待機する動作をバックグラウンド待機にして、その他の作業を先回りで進めておく動作にできる？」→「進めて」。

・BashOutput(wait>=30) は、予定表に未着手が残っていて仕事がまだ動いていれば、1 つの仕事につき 1 回だけ待たずに返す。
・2 回目は今までどおり待つ（結果に頼る作業は待てる＝逃げ道）。
・子・send・kill・短い wait・終わった仕事・未着手が無いとき・LAPS_WAIT_DEFER=0 では先送りしない。
・前置きの 1 行も LAPS_WAIT_DEFER に連動する。
・数え（wait_deferred・tools_while_deferred・wait_after_defer）が走行の counters に移る。
"""
from __future__ import annotations

import re
import threading
import time

from laps.engine import prompt
from laps.engine.tools import Ctx, shell


def _ctx(tmp_path, pending=True, child=False):
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    ctx.child = child
    ctx.todos = [{"content": "学習を走らせる", "status": "in_progress"}]
    if pending:
        ctx.todos.append({"content": "README を書く", "status": "pending"})
    return ctx


def _start(ctx, cmd="sleep 6; echo END"):
    return re.search(r"as (bg\d+)", shell.bash({"command": cmd, "run_in_background": True}, ctx).output).group(1)


def _timed(ctx, inp):
    t0 = time.monotonic()
    r = shell.bash_output(inp, ctx)
    return r, time.monotonic() - t0


def test_first_wait_returns_at_once_and_second_waits(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_WAIT_DEFER", raising=False)
    ctx = _ctx(tmp_path)
    jid = _start(ctx)
    r, secs = _timed(ctx, {"id": jid, "wait": 60})
    assert secs < 2 and "not waited" in r.output and "README を書く" in r.output
    assert "学習を走らせる" not in r.output.split("not started:")[1]     # 進行中の項目は挙げない
    assert ctx.counters == {"wait_deferred": 1}
    job = ctx.jobs.items[jid]
    assert job.polls == 0                                  # 先送りは見張りの回数に数えない
    r, secs = _timed(ctx, {"id": jid, "wait": 60})         # 2 回目は待つ（終われば戻る）
    assert "finished with exit code 0" in r.output and "END" in r.output and secs > 2
    assert ctx.counters == {"wait_deferred": 1}


def test_no_defer_cases(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_WAIT_DEFER", raising=False)
    for ctx, inp in ((_ctx(tmp_path, pending=False), {"wait": 60}),         # 未着手が無い
                     (_ctx(tmp_path, child=True), {"wait": 60}),            # 子
                     (_ctx(tmp_path), {"wait": 3}),                         # 短い wait（下限 30 秒）
                     (_ctx(tmp_path), {"wait": 60, "send": "x"})):          # 打ち込み
        jid = _start(ctx, "sleep 2; echo END")
        r = shell.bash_output({"id": jid, **inp}, ctx)
        assert "not waited" not in r.output, inp
        assert not ctx.counters
        shell.bash_output({"id": jid, "kill": True}, ctx)


def test_long_wait_without_pending_or_with_switch_off_waits(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, pending=False)
    jid = _start(ctx, "sleep 2; echo END")
    r, secs = _timed(ctx, {"id": jid, "wait": 60})
    assert "finished" in r.output and "not waited" not in r.output
    monkeypatch.setenv("LAPS_WAIT_DEFER", "0")
    ctx = _ctx(tmp_path)
    jid = _start(ctx, "sleep 2; echo END")
    r, secs = _timed(ctx, {"id": jid, "wait": 60})
    assert "finished" in r.output and not ctx.counters


def test_finished_job_and_kill_are_not_deferred(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_WAIT_DEFER", raising=False)
    ctx = _ctx(tmp_path)
    jid = _start(ctx, "echo END")
    ctx.jobs.items[jid].done.wait(5)
    r = shell.bash_output({"id": jid, "wait": 60}, ctx)
    assert "finished" in r.output and not ctx.counters
    jid = _start(ctx, "sleep 30")
    r = shell.bash_output({"id": jid, "wait": 60, "kill": True}, ctx)
    assert "not waited" not in r.output and not ctx.counters


def test_each_job_is_deferred_once(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_WAIT_DEFER", raising=False)
    ctx = _ctx(tmp_path)
    a, b = _start(ctx, "sleep 20"), _start(ctx, "sleep 21")
    assert "not waited" in shell.bash_output({"id": a, "wait": 60}, ctx).output
    assert "not waited" in shell.bash_output({"id": b, "wait": 60}, ctx).output
    assert a != b and ctx.counters == {"wait_deferred": 2}
    for j in (a, b):
        shell.bash_output({"id": j, "kill": True}, ctx)


def test_prompt_line_follows_the_switch(monkeypatch):
    monkeypatch.delenv("LAPS_WAIT_DEFER", raising=False)
    assert prompt._bg() == prompt.BG_NEW and "{BG}" not in prompt.BASE.replace("{BG}", prompt._bg())
    monkeypatch.setenv("LAPS_WAIT_DEFER", "0")
    assert prompt._bg() == prompt.BG_OLD


def test_loop_moves_counters_and_counts_work_while_deferred(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.delenv("LAPS_WAIT_DEFER", raising=False)
    from laps.engine.llm import LLM
    from laps.engine.loop import Agent
    from laps.engine.session import Transcript
    from laps.engine.tools import base_tools

    class Out:
        last = time.monotonic()

        def emit(self, *a, **k):
            pass

        def notice(self, *a, **k):
            pass

    t = Transcript()
    ctx = _ctx(tmp_path)
    ctx.root = t.root
    a = Agent(llm=LLM("http://127.0.0.1:9/v1", "north-test"), out=Out(), rid="r1", transcript=t, ctx=ctx,
              tools=base_tools("focused"), system="SYSTEM")
    jid = _start(ctx, "sleep 20")
    shell.bash_output({"id": jid, "wait": 60}, ctx)
    a._count_defer({"name": "BashOutput", "inp": {"id": jid, "wait": 60}})       # 先送りした本人
    a._count_defer({"name": "Edit", "inp": {}})
    a._count_defer({"name": "TodoWrite", "inp": {}})
    a._count_defer({"name": "BashOutput", "inp": {"id": jid, "wait": 60}})       # もう一度待った
    assert a.counters.get("wait_deferred") == 1 and a.counters.get("tools_while_deferred") == 1
    assert a.counters.get("wait_after_defer") == 1 and not ctx.counters
    assert jid in [j.id for j in ctx.jobs.waiting()]       # 先送りした仕事でも走行は閉じずに待つ（_park が見る）
    shell.bash_output({"id": jid, "kill": True}, ctx)
