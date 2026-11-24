"""2026-11-12 オーナー「タスク完了待ちのときに Thinking 扱いになってまとめを出してくれない」。
・背景の仕事を待つ（_park）前に、見直しのために預かった本文を画面へ出す。
・心拍の段は「道具 <名前>」・「背景の仕事」で送る（画面・ウェブ版・端末・Android との取り決め）。"""
from __future__ import annotations

import threading
import time

from laps.engine import server as srv_mod
from laps.engine.llm import LLM
from laps.engine.loop import Agent
from laps.engine.session import Transcript
from laps.engine.tools import Ctx, base_tools, shell


class Rec:
    def __init__(self):
        self.events = []
        self.last = time.monotonic()

    def emit(self, type_, rid=None, **f):
        self.events.append((type_, f))

    def notice(self, kind, rid=None, **f):
        self.events.append(("notice", {"kind": kind, **f}))


def _agent(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    t = Transcript()
    ctx = Ctx(workspace=str(tmp_path), root=t.root, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    rec = Rec()
    a = Agent(llm=LLM("http://127.0.0.1:9/v1", "north-test"), out=rec, rid="r1", transcript=t, ctx=ctx,
              tools=base_tools("focused"), system="SYSTEM")
    return a, rec


def test_held_report_is_shown_before_waiting_for_jobs(tmp_path, monkeypatch):
    a, rec = _agent(tmp_path, monkeypatch)
    job = a.ctx.jobs.start("sleep 1", str(tmp_path))
    job.background = True
    a._held = "作成を始めました。終わったら報告します。"
    assert a._park() is True
    kinds = [(t, f.get("kind") or f.get("text")) for t, f in rec.events]
    token_at = kinds.index(("token", "作成を始めました。終わったら報告します。"))
    park_at = kinds.index(("notice", "bg_park"))
    assert token_at < park_at          # 待つ前に出す（直す前は待ちの後の次の手番まで出なかった）
    assert a._held is None


def test_heartbeat_names_the_stage_as_the_screens_expect(monkeypatch):
    sent = []

    class Out:
        last = time.monotonic() - 100

        def notice(self, kind, rid=None, **f):
            sent.append(f)

    class Ag:
        stage = ""

    class L:
        progress = "Reading 40%"

    class Spec:
        id = "r1"

    class Jobs:
        def waiting(self):
            return [1, 2]

    monkeypatch.setenv("LAPS_HEARTBEAT_SECS", "1")
    monkeypatch.setenv("LAPS_STALL_DUMP_SECS", "0")
    s = srv_mod.Server.__new__(srv_mod.Server)
    s.out, s.jobs = Out(), Jobs()
    for stage, want_stage, want_detail in (("BashOutput", "道具 BashOutput", ""), ("背景の仕事", "背景の仕事", "2 running"),
                                           ("推論機", "推論機", "Reading 40%")):
        sent.clear()
        ag = Ag()
        ag.stage = stage
        threading.Timer(1.6, lambda: setattr(ag, "stage", "")).start()
        s._heartbeat(Spec(), ag, L())
        assert sent and sent[0]["stage"] == want_stage and sent[0]["detail"] == want_detail, (stage, sent)
