"""実機の記録で見つかった不具合（2026-10-31）。待ちの誤判定・Check passed の誤表示・Check と棚の道具・要約の上限。"""
from __future__ import annotations

import os
import shutil
import stat
import threading
import time
import types

import pytest

from laps.engine.tools import shell
from laps.oc import checktool, evidence


# ── 確かめの判定 ──────────────────────────
@pytest.mark.parametrize("cmd", [
    'cat >> "D:/TEST/.blackwall/FINDINGS.md" <<\'EOF\'\n- prep_v11.py で切り出した\nEOF',
    "cat >> note.md <<EOF\nmake the list shorter\npytest もまだ\nEOF\n",
    "cat <<-'X' > a.txt\n\tprep_v11.py\n\tX\n",
])
def test_heredoc_body_is_not_a_check(cmd):
    assert evidence.is_check(cmd, {"prep_v11.py"}) is False


@pytest.mark.parametrize("cmd", [
    "python prep_v11.py",
    "pytest -q",
    "cat <<'EOF' > conf.txt\nx\nEOF\npython prep_v11.py",
    "x=$((1<<2)); pytest -q",
    "grep a <<< b; pytest -q",
])
def test_real_checks_still_count(cmd):
    assert evidence.is_check(cmd, {"prep_v11.py"}) is True


# ── 待ち ──────────────────────────────────
def _ctx(jobs, has_message=None):
    return types.SimpleNamespace(jobs=jobs, has_message=has_message, stopped=lambda: False, limit=lambda: 100000)


def _job(done=False):
    ev = threading.Event()
    if done:
        ev.set()
    return types.SimpleNamespace(done=ev, proc=types.SimpleNamespace(pid=4242), id="bg1", background=True)


def test_running_job_is_only_true_for_a_job_still_running():
    jobs = types.SimpleNamespace(items={"bg1": _job(), "bg2": _job(done=True)})
    ctx = _ctx(jobs)
    assert shell.running_job({"id": "bg1", "wait": 3000}, ctx) is True
    assert shell.running_job({"pid": "4242"}, ctx) is True
    assert shell.running_job({"id": "bg2"}, ctx) is False
    assert shell.running_job({"id": "nope"}, ctx) is False
    assert shell.running_job({"id": "bg1", "kill": True}, ctx) is False
    assert shell.running_job({}, ctx) is True              # id を省いても、動いているのが 1 本なら相手は決まる
    jobs.items["bg3"] = _job()
    assert shell.running_job({}, ctx) is False             # 2 本動いていたら決まらない


def test_poll_backoff_stays_under_the_cache_lifetime():
    assert max(shell.POLL_WAITS) < 300


# ── Check と棚の道具 ──────────────────────
def test_check_finds_a_tool_only_on_the_child_path(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX の実行ビットで確かめる")
    tool = tmp_path / "shelf-only-tool-xyz"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    from laps.oc import toolenv
    monkeypatch.setattr(toolenv, "child_path", lambda current=None: str(tmp_path))
    assert shutil.which("shelf-only-tool-xyz") is None
    assert checktool._which("shelf-only-tool-xyz") == str(tool)


# ── 繰り返しの見張りと走行（偽の推論機）──────
from tests.test_engine_20261030 import Fake, env  # noqa: E402,F401


def test_waiting_on_a_running_job_is_never_a_doom_loop(env):  # noqa: F811
    a, _fake, _rec, _ = env([{"text": "x"}])
    a.ctx.jobs.items["bg1"] = _job()
    call = {"name": "BashOutput", "inp": {"id": "bg1", "wait": 3000}}
    assert all(a._doom(dict(call)) is None for _ in range(10))
    assert not a.counters.get("doom_loop") and a.outcome != "loop_detected"


def test_polling_a_finished_job_is_still_a_doom_loop(env):  # noqa: F811
    a, _fake, _rec, _ = env([{"text": "x"}])
    a.ctx.jobs.items["bg1"] = _job(done=True)
    call = {"name": "BashOutput", "inp": {"id": "bg1", "wait": 3000}}
    got = [a._doom(dict(call)) for _ in range(3)]
    assert got[0] is None and got[1] is None and got[2] is not None and a.counters.get("doom_loop") == 1


def test_repeat_guard_and_look_streak_skip_waits(env):  # noqa: F811
    a, _fake, _rec, _ = env([{"text": "x"}])
    a.ctx.jobs.items["bg1"] = _job()
    from laps.engine.aids import Aids
    a.aids = Aids(a)
    inp = {"id": "bg1", "wait": 3000}
    key = a.aids._key("BashOutput", inp)
    a.aids.results[key] = (9, "same")
    assert a.aids.blocked("BashOutput", inp, "") == ""
    tail = []
    for _ in range(40):
        a.aids._look("BashOutput", inp, tail)
    assert not tail and a.aids.look_streak == 0
    a.ctx.jobs.items["bg1"] = _job(done=True)
    assert "not run" in a.aids.blocked("BashOutput", inp, "")


# ── 普通の一言 ────────────────────────────
def _wait_for(pred, secs=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_message_stops_a_long_tool_after_the_grace(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAPS_INJECT_ESCALATE", "0.1")
    a, _fake, _rec, _ = env([{"text": "x"}])
    a.stage = "Tools"
    assert a.inject("聞いて")
    assert not a.ctx.preempt.is_set()                 # すぐには止めない
    assert _wait_for(a.ctx.preempt.is_set) and a.counters.get("inject_escalated") == 1
    assert _wait_for(lambda: not a._inbox_watch)


def test_message_does_not_stop_a_subagent_or_a_question(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAPS_INJECT_ESCALATE", "0.1")
    for stage in ("Task", "AskUserQuestion", "推論機"):
        a, _fake, _rec, _ = env([{"text": "x"}])
        a.stage = stage
        assert a.inject("聞いて")
        time.sleep(0.8)
        assert not a.ctx.preempt.is_set(), stage
        with a._lock:
            a._closing = True
        assert _wait_for(lambda: not a._inbox_watch)


def test_message_read_at_the_boundary_needs_no_stopping(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LAPS_INJECT_ESCALATE", "0.1")
    a, _fake, _rec, _ = env([{"text": "x"}])
    a.stage = "Tools"
    a.first_request = a.request = "元の依頼"
    assert a.inject("聞いて")
    a._take_inbox()
    time.sleep(0.8)
    assert not a.ctx.preempt.is_set()


def test_summary_limit_leaves_room_for_thinking():
    from laps.engine import compact
    # Claude は思考も上限に入る: 本文の上限の上に、思考の分（8 千以上）を残す
    assert compact.SUMMARY_TOKENS_CLAUDE >= compact.SUMMARY_TOKENS + 8_192


# ── 思考の中のコード ──────────────────────
def _watch_feed(text):
    from laps.engine.repeat import RepeatWatch
    w = RepeatWatch(hedge=15)
    for i in range(0, len(text), 8):
        if w.feed(text[i:i + 8]):
            return w.kind
    return None


def test_drafting_an_edit_in_thought_is_not_a_repeat():
    code = ('def main(argv=None):\n    ap = argparse.ArgumentParser(description="count words")\n    ap.add_argument("path")\n'
            '    args = ap.parse_args(argv)\n    with open(args.path, encoding="utf-8") as f:\n        ws = words(f.read())\n    print(len(ws))\n')
    fenced = f"Edit を呼びます。\n\n`old_string`:\n```\n{code}```\n\n`new_string`:\n```\n{code}    extra_line = 1\n```\n\nもう一度確かめます:\n```\n{code}```\n" * 2
    assert _watch_feed("x" * 500 + "\n" + fenced) is None
    bare = (code * 4)
    assert _watch_feed("y" * 500 + "\n" + bare) is None


def test_prose_loops_are_still_cut():
    loop = "".join(f"takina_info.txt を確認しないといけないと思います{i % 2}。\n" for i in range(40))
    assert _watch_feed(loop) == "repeat"
    exact = "同じことをもう一度考えてみることにしましょう。\n" * 30
    assert _watch_feed(exact) == "repeat"
    hedge = "".join(f"しかし、おそらく {i} 番目の可能性があるのかもしれないと思います。\n" for i in range(60))
    assert _watch_feed(hedge) in ("hedge", "repeat")


# ── 下見の子は見るだけ ────────────────────
def test_look_around_helper_refuses_builds_but_can_look(env):  # noqa: F811
    from laps.engine.tools import base_tools
    a, _fake, _rec, _ = env([{"text": "x"}], tools=base_tools("full"))
    a.child_kind = "explore"
    a.ctx.spawn = None
    def run_bash(cmd):
        job = {"name": "Bash", "inp": {"command": cmd}, "error": None, "tool": a.tools["Bash"]}
        return a._run_one(job)
    assert "read-only helper" in run_bash("bash build.sh").output
    assert "read-only helper" in run_bash("python3 -m pytest -q").output
    assert "read-only helper" not in run_bash("ls -la").output
    assert "read-only helper" not in run_bash("git status").output


# ── BashOutput: id を省いたとき・tail（課題集 longbuild・2026-10-31） ──

def _bg_ctx(tmp_path):
    from laps.engine.tools import Ctx
    return Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())


def _start(ctx, cmd):
    # 番号は本文から読む（行の右の tail は 2026-11-05 から「背景で開始」）
    import re as _re
    return _re.search(r"as (bg\d+)", shell.bash({"command": cmd, "run_in_background": True}, ctx).output).group(1)


def test_bash_output_without_id_reads_the_only_job(tmp_path):
    ctx = _bg_ctx(tmp_path)
    assert "No background jobs" in shell.bash_output({}, ctx).output
    jid = _start(ctx, "sleep 1; echo step; echo 'BUILD-ID: abc'")
    assert shell.running_job({"until": "BUILD-ID:"}, ctx)        # 動いている間は繰り返しの見張りに数えない
    r = shell.bash_output({"until": "BUILD-ID:", "wait": 20}, ctx)
    assert f"[{jid} " in r.output and "BUILD-ID: abc" in r.output
    ctx.jobs.items[jid].done.wait(5)
    assert not shell.running_job({}, ctx)


def test_bash_output_tail_is_lines_and_leaves_the_rest_unread(tmp_path):
    ctx = _bg_ctx(tmp_path)
    jid = _start(ctx, "printf 'a\\nb\\nBUILD-ID: abc\\n'")
    ctx.jobs.items[jid].done.wait(5)
    r = shell.bash_output({"id": jid, "tail": 1}, ctx)
    assert "BUILD-ID: abc" in r.output and "\na\n" not in r.output and "last 1 of 3 new lines" in r.output
    r = shell.bash_output({"id": jid}, ctx)                      # tail で覗いた後も全部読める
    assert "a\nb\nBUILD-ID: abc" in r.output
    assert "no new output" in shell.bash_output({"id": jid}, ctx).output


def test_bash_output_without_id_lists_when_it_cannot_tell(tmp_path):
    ctx = _bg_ctx(tmp_path)
    a, b = _start(ctx, "sleep 30"), _start(ctx, "sleep 31")
    r = shell.bash_output({"tail": 1, "kill": True}, ctx)        # 2 本動いているときは止めない・どれか言わせる
    assert a in r.output and b in r.output and "pass id" in r.output
    assert not ctx.jobs.items[a].done.is_set() and not ctx.jobs.items[b].done.is_set()
    for j in (a, b):
        shell.bash_output({"id": j, "kill": True}, ctx)


def test_review_long_note_once_and_only_for_our_review():
    from types import SimpleNamespace
    from laps.engine import aids as A
    counts = []
    finish = SimpleNamespace(review_at=None, work_calls=lambda: 20)
    me = SimpleNamespace(a=SimpleNamespace(finish=finish, _count=counts.append), review_noted=False)
    tail = []
    A.Aids._review_long(me, tail)
    assert tail == []                                      # 見直しを頼んでいない（利用者が頼んだ見直しもここ）
    finish.review_at = 20 - A.REVIEW_CALLS + 1
    A.Aids._review_long(me, tail)
    assert tail == []                                      # まだ目安に届いていない
    finish.review_at = 20 - A.REVIEW_CALLS
    A.Aids._review_long(me, tail)
    A.Aids._review_long(me, tail)
    assert len(tail) == 1 and "enough checks" in tail[0] and counts == ["review_long_note"]
