"""新しい中核に戻した機能の試験（添え書き・終える前の確かめ・スマホの追う画面・背景の仕事）。偽の推論機で本物の走行を流す。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from laps.engine.aids import Aids
from laps.engine.finish import Finish
from laps.engine.llm import LLM
from laps.engine.loop import Agent
from laps.engine.memory import Memory
from laps.engine.session import Transcript
from laps.engine.tools import Ctx, base_tools, limit_bytes, shell
from laps.engine.tools.misc import load_todos
from tests.test_engine_20261030 import Fake, Rec


@pytest.fixture
def run(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    monkeypatch.setenv("LAPS_UMBRA_URL", "http://127.0.0.1:9")     # 棚へ繋ぎに行かない
    monkeypatch.setenv("LAPS_AUTO_BG_SEC", "30")
    monkeypatch.setenv("LAPS_SELF_REVIEW", "1")          # 試験の既定は切ってある（conftest）
    monkeypatch.setenv("LAPS_EXPLORE_FIRST", "0")
    monkeypatch.chdir(ws)
    fakes: list[Fake] = []
    jobs = shell.Jobs()

    def go(script, request, *, session=None, n_ctx=131072, wiring=True):
        fake = Fake(script, n_ctx)
        fakes.append(fake)
        t = Transcript(session)
        ctx = Ctx(workspace=str(ws), root=t.root, cancel=threading.Event(), preempt=threading.Event(), jobs=jobs)
        load_todos(ctx)
        rec = Rec()
        a = Agent(llm=LLM(fake.url, "north-test"), out=rec, rid="r1", transcript=t, ctx=ctx, tools=base_tools("full"), system="SYSTEM")
        ctx.spawn = a.spawn
        ctx.has_message = lambda: bool(a._inbox)
        a.memory = Memory(str(ws), t.id, a._count)
        if wiring:
            a.aids = Aids(a)
            a.finish = Finish(a, a.aids)
        text = a.run(request)
        if wiring:
            assert a.aids.last_error == "" and a.finish.last_error == ""
        return text, a, fake, rec
    yield go, ws, monkeypatch
    jobs.kill_all()
    for f in fakes:
        f.close()


def sent(fake, i=-1) -> str:
    return json.dumps(fake.bodies[i]["messages"], ensure_ascii=False)


def notices(rec, kind):
    return [f for t, f in rec.events if t == "notice" and f.get("kind") == kind]


# ── システムの設定を変える呼び出し ──────
def test_system_change_is_announced_and_listed_in_the_answer(run):
    go, ws, _ = run
    call = {"calls": [("Bash", {"command": "systemctl daemon-reload || true"})]}
    text, a, _, rec = go([call, {"text": "再読み込みしました。"}], "再読み込みして")
    assert notices(rec, "syschange")               # 止めずに知らせる（2026-10-30 オーナー）
    assert "この作業で変えたシステムの設定" in text and "systemctl" in text
    assert a.counters.get("tool_calls:Bash") == 1


def test_system_change_with_a_spoken_line_is_not_announced_again(run):
    go, ws, _ = run
    call = {"text": "設定を読み直します。", "calls": [("Bash", {"command": "systemctl daemon-reload || true"})]}
    text, _, _, rec = go([call, {"text": "しました。"}], "読み直して")
    assert not notices(rec, "syschange") and "systemctl" in text


# ── 保存した API キーを伏せる ───────────
def test_saved_key_value_is_masked_in_tool_results(run, tmp_path):
    go, ws, _ = run
    from laps import apikeys
    apikeys.set_key("MYKEY", "s3cr3t-value-0123456789", ["example.com"])
    (ws / "env.txt").write_text("KEY=s3cr3t-value-0123456789\n", encoding="utf-8")
    text, a, fake, rec = go([{"calls": [("Bash", {"command": "cat env.txt"})]}, {"text": "ok"}], "出力して")
    assert "s3cr3t-value-0123456789" not in sent(fake)
    assert "{{MYKEY}}" in sent(fake) and a.counters.get("apikey_masked") == 1


# ── 繰り返しと行き詰まり ────────────────
def test_call_repeated_with_gaps_is_stopped_at_the_sixth_time(run):
    go, ws, _ = run
    (ws / "a.txt").write_text("a\n", encoding="utf-8")
    same = ("Read", {"file_path": "a.txt"})
    script = []
    for i in range(6):
        script.append({"calls": [same]})
        script.append({"calls": [("Glob", {"pattern": f"*{i}.nothing"})]})
    script.append({"text": "やめました。"})
    _, a, fake, _ = go(script, "読んで")
    assert a.counters.get("repeat_anywhere_blocked") == 1
    assert "made this exact call 5 times" in sent(fake)


def test_two_calls_alternating_are_noted_then_held(run):
    go, ws, _ = run
    (ws / "a.txt").write_text("a\n", encoding="utf-8")
    (ws / "b.txt").write_text("b\n", encoding="utf-8")
    a_call, b_call = {"calls": [("Read", {"file_path": "a.txt"})]}, {"calls": [("Read", {"file_path": "b.txt"})]}
    script = [a_call, b_call, a_call, b_call, a_call, b_call, a_call, {"text": "別の手にします。"}]
    _, a, fake, _ = go(script, "読んで")
    assert a.counters.get("alternate_noted") == 1 and a.counters.get("alternate_blocked") == 1


def test_same_failure_arms_the_stuck_gate_and_asks_for_three_lines(run):
    go, ws, _ = run
    script = [{"calls": [("Bash", {"command": f"exit 3; echo {i}"})]} for i in range(6)]
    script.append({"calls": [("Bash", {"command": "exit 3; echo again"})]})      # 説明なしの次の呼び出しは走らせない
    script.append({"text": "目的: x\nエラー: 3\n別の手: y", "calls": [("Bash", {"command": "echo ok"})]})
    script.append({"text": "直りました。"})
    _, a, fake, _ = go(script, "動かして")
    assert a.counters.get("stuck_gate_armed") == 1 and a.counters.get("stuck_gate_blocked") == 1
    assert a.counters.get("stuck_gate_passed") == 1
    assert "failure #6 of the same kind" in sent(fake) and a.counters.get("repeat_failure") >= 2


# ── 背景の仕事 ─────────────────────────
def test_long_command_moves_to_the_background_and_the_model_is_told(run):
    go, ws, mp = run
    mp.setenv("LAPS_AUTO_BG_SEC", "1")
    script = [{"calls": [("Bash", {"command": "sleep 2; echo finished-now"})]}, {"text": "待っています。"}, {"text": "終わりました。"}]
    text, a, fake, rec = go(script, "走らせて")
    assert "Still running" in sent(fake, 1)
    assert "finished-now" in sent(fake, 2) and "exit code 0" in sent(fake, 2)
    assert notices(rec, "bg_park") and text == "終わりました。"


def test_same_command_is_not_started_twice(run):
    go, ws, mp = run
    mp.setenv("LAPS_STALL_SEC", "1")
    cmd = "sleep 30"
    script = [{"calls": [("Bash", {"command": cmd, "run_in_background": True})]},
              {"calls": [("Bash", {"command": cmd, "run_in_background": True})]}, {"text": "了解"}, {"text": "了解"}]
    _, a, fake, _ = go(script, "走らせて")
    assert "already running in the background" in sent(fake)


def test_server_job_does_not_keep_the_run_open(run):
    go, ws, _ = run
    cmd = "python3 -m http.server 0 --bind 127.0.0.1"
    t0 = time.monotonic()
    text, a, fake, rec = go([{"calls": [("Bash", {"command": cmd, "run_in_background": True})]}, {"text": "起動しました。"}], "サーバを起動して")
    assert time.monotonic() - t0 < 20 and not notices(rec, "bg_park")
    assert any("Left 1 servers" in str(f.get("message")) for f in notices(rec, "harness"))
    assert a.ctx.jobs.running()                     # サーバは残す


def test_stalled_detection_counts_cpu_as_progress(tmp_path):
    jobs = shell.Jobs()
    job = jobs.start("python3 -c 'import time\nt=time.time()\nwhile time.time()-t<4: pass'", str(tmp_path))
    job.background = True
    job.progress_at = time.monotonic() - 100
    os.environ["LAPS_STALL_SEC"] = "50"
    try:
        for _ in range(40):
            jobs.notes()
            if time.monotonic() - job.progress_at < 50:
                break
            time.sleep(0.2)
        assert time.monotonic() - job.progress_at < 50     # 何も書かなくても CPU を使っていれば進んでいる
    finally:
        os.environ.pop("LAPS_STALL_SEC", None)
        jobs.kill_all()


def test_poll_backoff_waits_longer_on_the_second_look(tmp_path, monkeypatch):
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    monkeypatch.setattr(shell, "POLL_WAITS", (1.0, 1.0, 1.0, 1.0))
    r = shell.bash({"command": "sleep 5", "run_in_background": True}, ctx)
    import re as _re
    jid = _re.search(r"as (bg\d+)", r.output).group(1)   # tail は 2026-11-05 から「背景で開始」
    t0 = time.monotonic()
    shell.bash_output({"id": jid}, ctx)
    first = time.monotonic() - t0
    t0 = time.monotonic()
    shell.bash_output({"id": jid}, ctx)
    second = time.monotonic() - t0
    assert first < 0.5 and second >= 0.9
    ctx.jobs.kill_all()


def test_message_from_the_user_ends_a_wait(tmp_path):
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    ctx.has_message = lambda: True
    t0 = time.monotonic()
    r = shell.bash({"command": "sleep 30"}, ctx)
    assert "Still running" in r.output and time.monotonic() - t0 < 5
    t0 = time.monotonic()
    shell.bash_output({"id": r.tail.split()[-1], "wait": 20}, ctx)
    assert time.monotonic() - t0 < 5
    ctx.jobs.kill_all()


def test_command_is_passed_by_file_so_pkill_does_not_hit_its_own_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    r = shell.bash({"command": "pkill -f bw-self-match-word-xyz; echo survived $?"}, ctx)
    assert "survived 1" in r.output                # 自分のシェルに当たると何も出さずに死ぬ


def test_aliases_from_the_rc_file_work(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    (home / ".bashrc").write_text("alias hello-rc='echo from-rc'\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    assert "from-rc" in shell.bash({"command": "hello-rc"}, ctx).output


def test_binary_output_is_not_decoded(tmp_path):
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    r = shell.bash({"command": "printf 'a\\0b\\0c'"}, ctx)
    assert "binary output" in r.output and "\0" not in r.output


def test_tools_folder_is_on_the_path(tmp_path, monkeypatch):
    d = tmp_path / "tools" / "thing"
    d.mkdir(parents=True)
    exe = d / "bw-thing-xyz"
    exe.write_text("#!/bin/sh\necho thing-ran\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("LAPS_TOOLS_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    assert "thing-ran" in shell.bash({"command": "bw-thing-xyz"}, ctx).output


def test_output_limit_goes_down_as_the_conversation_grows(tmp_path):
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event())
    assert limit_bytes(ctx) == 32 * 1024
    ctx.tokens = 30_000
    assert limit_bytes(ctx) == 10 * 1024
    ctx.tokens = 60_000
    assert limit_bytes(ctx) == 8 * 1024


# ── 書いた後の検査・パス ────────────────
def test_syntax_error_is_reported_after_write(run):
    go, ws, _ = run
    _, _, fake, _ = go([{"calls": [("Write", {"file_path": "bad.py", "content": "def f(:\n"})]}, {"text": "書きました。"}, {"text": "はい"}], "書いて")
    assert "Syntax errors detected" in sent(fake, 1)


def test_fullwidth_space_in_a_path_is_fixed(run):
    go, ws, _ = run
    (ws / "a b.txt").write_text("hello\n", encoding="utf-8")
    _, _, fake, _ = go([{"calls": [("Read", {"file_path": "a　b.txt"})]}, {"text": "読みました。"}], "読んで")
    assert "hello" in sent(fake, 1)


def test_name_found_elsewhere_in_the_work_folder_is_mentioned(run):
    go, ws, _ = run
    (ws / "sub").mkdir()
    (ws / "sub" / "deep-target-file.txt").write_text("x\n", encoding="utf-8")
    _, a, fake, _ = go([{"calls": [("Read", {"file_path": "deep-target-file.txt"})]}, {"text": "見つけました。"}], "読んで")
    assert a.counters.get("whereis_elsewhere") == 1 and "sub" in sent(fake, 1)


# ── 依頼に添える物 ─────────────────────
def test_git_state_and_commands_are_added_to_the_request(run):
    go, ws, _ = run
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    _, _, fake, _ = go([{"text": "はい。"}], "状況を教えて")
    assert "git status" in sent(fake, 0) and "pytest" in sent(fake, 0)


def test_criteria_note_only_for_real_work(run):
    go, ws, _ = run
    _, a, fake, _ = go([{"text": "作りました。"}], "作って")
    assert not a.counters.get("criteria_note")
    long_req = "ログイン画面と一覧画面と設定画面を持つ小さな Web アプリを作って、それぞれの画面を確かめてください。"
    _, b, fake, _ = go([{"text": "はい。"}, {"text": "はい。"}], long_req)
    assert b.counters.get("criteria_note") == 1 and "count as done" in sent(fake, 0)


def test_asking_for_a_review_uses_the_adversarial_steps(run):
    go, ws, _ = run
    (ws / "x.py").write_text("print(1)\n", encoding="utf-8")
    script = [{"text": "問題ありません。"}, {"calls": [("Bash", {"command": "python3 x.py"})]}, {"text": "✓ 動いた — python3 x.py で 1 と出た"}]
    _, a, fake, rec = go(script, "x.py をレビューして")
    assert a.counters.get("review_mode") == 1 and a.counters.get("self_review_no_tools") == 1
    assert a.counters.get("self_review_ok") == 1
    assert any("1 ✓" in str(f.get("message")) for f in notices(rec, "review"))


# ── 終える前の確かめ ───────────────────
def test_small_task_is_not_reviewed(run):
    go, ws, _ = run
    script = [{"calls": [("Write", {"file_path": "hello.txt", "content": "hi\n"})]}, {"calls": [("Read", {"file_path": "hello.txt"})]}, {"text": "作りました。"}]
    text, a, _, _ = go(script, "hello.txt を作って")
    assert not a.counters.get("self_review_asked") and text == "作りました。" and not a.counters.get("end_rounds")


def test_large_task_is_reviewed_before_the_report(run):
    """★2026-11-06 オーナー「報告してからレビューを行って更に報告を行ってくる。レビュー後に報告させる」:
    見直しの前に書いた報告は画面に出さず（下書き）、見直しの後の報告を 1 回だけ出す。"""
    go, ws, _ = run
    script = [{"calls": [("Write", {"file_path": "n.txt", "content": "x\n"})]}]
    script += [{"calls": [("Glob", {"pattern": f"*{i}.nope"})]} for i in range(8)]
    script += [{"text": "全部できました。詳しい報告です。" * 5},
               {"calls": [("Read", {"file_path": "n.txt"})]},
               {"text": "n.txt を作りました。中身は x の 1 行です。作った後に読み直して、書いた内容と同じことを確かめました。残っている作業はありません。\n✓ n.txt — 読んで確かめた"}]
    text, a, fake, rec = go(script, "n.txt を作って")
    assert a.counters.get("self_review_asked") == 1 and a.counters.get("end_rounds") == 1
    assert a.counters.get("review_draft_hidden") == 1
    assert not text.startswith("全部できました。")    # 見直しの前の報告は出さない
    assert text.startswith("n.txt を作りました。") and "✓ n.txt" in text
    shown = "".join(f.get("text", "") for t, f in rec.events if t == "token")
    assert "全部できました" not in shown              # 画面にも流れていない
    assert "Before you finish, review your own work" in sent(fake, 10)
    assert "has NOT been shown to the user" in sent(fake, 10)


def _reviewed_after_a_check():
    """書いて、走らせて通った後に見直しへ入る手順。"""
    script = [{"calls": [("Write", {"file_path": "tool.py", "content": "print('x')\n"})]},
              {"calls": [("Bash", {"command": "python3 tool.py"})]}]
    script += [{"calls": [("Glob", {"pattern": f"*{i}.nope"})]} for i in range(8)]
    return script + [{"text": "全部できました。詳しい報告です。" * 5}]


def test_a_check_that_passed_after_the_last_edit_can_be_cited_in_the_review(run):
    go, ws, _ = run
    script = _reviewed_after_a_check() + [{"calls": [("Bash", {"command": "python3 tool.py extra"})]},
                                          {"text": "tool.py を作りました。python3 tool.py で x と出ることと、余分な引数を渡しても同じように動くことを確かめました。"
                                                   "引数の扱いは今のままで問題ないと判断しました。残っている作業はありません。\n✓ tool.py — python3 tool.py → x\n✓ 余分な引数 — python3 tool.py extra → x"}]
    text, a, fake, rec = go(script, "tool.py を作って")
    review = sent(fake, 11)
    assert "Already passed after your last edit" in review and "- python3 tool.py" in review
    assert "do not run it again" in review and "checks you ran before this review do not count" not in review
    assert a.counters.get("self_review_cited") == 1 and not a.counters.get("self_review_no_tools")
    assert text.startswith("tool.py を作りました。") and "✓ 余分な引数" in text     # 見直しの後の報告だけ（2026-11-06）


def test_citing_without_a_new_check_is_sent_back_once_without_asking_to_repeat(run):
    go, ws, _ = run
    script = _reviewed_after_a_check() + [{"text": "✓ tool.py — python3 tool.py → x"},
                                          {"calls": [("Bash", {"command": "python3 tool.py extra"})]},
                                          {"text": "✓ 余分な引数 — python3 tool.py extra → x"}]
    text, a, fake, rec = go(script, "tool.py を作って")
    pushed = sent(fake, 12)
    assert a.counters.get("self_review_no_tools") == 1
    assert "The checks that already passed still count" in pushed and "Run a new check for each item" not in pushed


def test_unrun_code_is_sent_back_once(run):
    go, ws, _ = run
    script = [{"calls": [("Write", {"file_path": "tool.py", "content": "print('x')\n"})]}, {"text": "書きました。"},
              {"calls": [("Bash", {"command": "python3 tool.py"})]}, {"text": "x と出ました。"}]
    text, a, fake, rec = go(script, "tool.py を作って")
    assert a.counters.get("verify_nudge") == 1 and notices(rec, "verify_nudge")
    assert "have not run, built or tested" in sent(fake, 2)
    assert a.counters.get("verdict:verified") == 1


def test_failed_check_not_mentioned_is_sent_back_once(run):
    go, ws, _ = run
    (ws / "test_x.py").write_text("def test_a():\n    assert False\n", encoding="utf-8")
    script = [{"calls": [("Edit", {"file_path": "test_x.py", "old_string": "False", "new_string": "0"})]},
              {"calls": [("Bash", {"command": "python3 -m pytest -q test_x.py"})]},
              {"text": "直しました。"}, {"text": "まだ落ちます。失敗しています。"}]
    _, a, fake, rec = go(script, "直して")
    assert a.counters.get("verdict_pushback") == 1
    assert any("failed check" in str(f.get("message")) for f in notices(rec, "verifier"))


def test_named_file_that_does_not_exist_is_sent_back(run):
    go, ws, _ = run
    (ws / "real.txt").write_text("x\n", encoding="utf-8")
    missing = str(ws / "made-up-result.txt")
    script = [{"calls": [("Edit", {"file_path": "real.txt", "old_string": "x", "new_string": "y"})]},
              {"text": f"保存しました: {missing}"}, {"text": "訂正: real.txt に書きました。"}]
    _, a, fake, _ = go(script, "real.txt を直して")
    assert a.counters.get("claims_pushback") == 1 and "do not exist" in sent(fake, 2)


def test_output_that_is_not_what_its_extension_says_is_sent_back(run):
    go, ws, _ = run
    png = ws / "out.png"
    script = [{"calls": [("Bash", {"command": f"echo not-an-image > {png}"})]},
              {"text": f"保存しました: {png}"}, {"text": "直せませんでした。中身は PNG ではありません。"}]
    _, a, fake, _ = go(script, "画像を作って")
    assert a.counters.get("media_pushback") == 1 and "not what their extension says" in sent(fake, 2)


def test_unresearched_document_must_say_so(run):
    go, ws, _ = run
    script = [{"calls": [("Write", {"file_path": "report.md", "content": "# AI の最新動向\n- 項目\n"})]},
              {"text": "report.md を書きました。ネットに接続できず、最新の情報は取得できませんでした。"},
              {"text": "report.md を書きました。ネットに接続できず、最新の情報は取得できませんでした。"},
              {"calls": [("Write", {"file_path": "report.md", "content": "# AI の動向\n未確認: オフラインのため調べられなかった\n"})]},
              {"text": "注記を足しました。"}]
    _, a, fake, _ = go(script, "AI のニュースを report.md にまとめて")
    assert a.counters.get("caveat_pushback") == 1


def test_announcing_a_step_and_stopping_is_sent_back(run):
    go, ws, _ = run
    (ws / "a.txt").write_text("a\n", encoding="utf-8")
    script = [{"calls": [("Read", {"file_path": "a.txt"})]}, {"text": "次に a.txt を直します。"},
              {"calls": [("Edit", {"file_path": "a.txt", "old_string": "a", "new_string": "b"})]}, {"text": "直しました。"}]
    text, a, fake, rec = go(script, "a.txt を直して")
    assert a.counters.get("continue_push:announce") == 1 and notices(rec, "announce_nudge") and text == "直しました。"


def test_plan_not_updated_gets_a_note(run):
    go, ws, _ = run
    plan = {"todos": [{"content": "a", "status": "in_progress"}, {"content": "b", "status": "pending"}]}
    script = [{"calls": [("TodoWrite", plan)]}]
    script += [{"calls": [("Glob", {"pattern": f"*{i}.nope"})]} for i in range(8)]
    done = {"todos": [{"content": "a", "status": "completed"}, {"content": "b", "status": "completed"}]}
    script += [{"calls": [("TodoWrite", done)]}, {"text": "終わりました。"}]
    _, a, fake, _ = go(script, "a と b をやって")
    assert a.counters.get("plan_stale_note") == 1 and "has not been updated for 8" in sent(fake)


def test_looking_only_for_a_long_time_gets_a_note(run):
    go, ws, mp = run
    mp.setenv("LAPS_LOOK_STREAK", "3")
    script = [{"calls": [("Glob", {"pattern": f"*{i}.nope"})]} for i in range(3)] + [{"text": "分かりました。"}]
    _, a, fake, _ = go(script, "探して")
    assert a.counters.get("look_streak_nudge") == 1


def test_throwaway_script_is_moved_to_scratch(run):
    go, ws, _ = run
    script = [{"calls": [("Write", {"file_path": "debug_x.txt", "content": "keep\n"})]},
              {"calls": [("Write", {"file_path": "debug_y.py", "content": "print(1)\n"})]},
              {"calls": [("Bash", {"command": "python3 debug_y.py"})]}, {"text": "できました。"}]
    _, a, _, rec = go(script, "試して")
    assert (ws / "_scratch" / "debug_y.py").exists() and not (ws / "debug_y.py").exists()
    assert (ws / "debug_x.txt").exists()            # 台本でない物は動かさない


def test_last_turn_keeps_the_tool_list(run):
    go, ws, _ = run
    script = [{"calls": [("Glob", {"pattern": "*.a"})]}, {"text": "途中までです。", "calls": [("Glob", {"pattern": "*.b"})]}]
    fake = Fake(script)
    t = Transcript(None)
    ctx = Ctx(workspace=str(ws), root=t.root, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    from laps.engine.loop import Limits
    a = Agent(llm=LLM(fake.url, "north-test"), out=Rec(), rid="r1", transcript=t, ctx=ctx, tools=base_tools("full"), system="S",
              limits=Limits(max_tool_calls=1))
    text = a.run("探して")
    assert text == "途中までです。" and a.counters.get("last_turn_calls_dropped") == 1
    assert all("tools" in b for b in fake.bodies)   # 一覧を外すと全部読み直しになる
    fake.close()


# ── 最初の下見 ─────────────────────────
def test_first_look_runs_in_a_folder_with_files(run):
    go, ws, mp = run
    mp.setenv("LAPS_EXPLORE_FIRST", "1")
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    for i in range(25):                # 小さいフォルダでは下見をしない
        (ws / f"f{i}.txt").write_text("x\n", encoding="utf-8")
    script = [{"text": "main.py があります。"}, {"calls": [("Read", {"file_path": "main.py"})]}, {"text": "読みました。"}]
    _, a, fake, rec = go(script, "main.py を読んで")
    assert a.counters.get("explore_first:ran") == 1
    assert "main.py があります" in sent(fake, 1) and "Task" in sent(fake, 1)
    assert not a.counters.get("explore_first:empty_folder")


def test_no_first_look_for_questions_or_empty_folders(run):
    go, ws, mp = run
    mp.setenv("LAPS_EXPLORE_FIRST", "1")
    _, a, _, _ = go([{"text": "はい。"}], "作って")
    assert a.counters.get("explore_first:empty_folder") == 1
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    _, b, fake, _ = go([{"text": "2 です。"}], "1+1 はなぜ 2 ですか？")
    assert not b.counters.get("explore_first:ran") and len(fake.bodies) == 1


def test_explore_child_cannot_write(run):
    go, ws, mp = run
    mp.setenv("LAPS_EXPLORE_FIRST", "1")
    (ws / "main.py").write_text("print(1)\n", encoding="utf-8")
    for i in range(25):                # 小さいフォルダでは下見をしない
        (ws / f"f{i}.txt").write_text("x\n", encoding="utf-8")
    script = [{"calls": [("Bash", {"command": "echo x > made-by-child.txt"})]}, {"text": "見ました。"}, {"text": "はい。"}, {"text": "はい。"}]
    go(script, "main.py を読んで")
    assert not (ws / "made-by-child.txt").exists()


# ── スマホの追う画面（live.ndjson・inbox.ndjson） ──
def _serve(tmp_path, fake_url, ws):
    env = {**os.environ, "LAPS_HOME": str(tmp_path / "home"), "LAPS_BACKEND": fake_url, "LAPS_MODEL": "north-test",
           "LAPS_WORKSPACE": str(ws), "HOME": str(tmp_path / "userhome"), "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "LAPS_POLICY": "trusted", "LAPS_UMBRA_URL": "http://127.0.0.1:9", "LAPS_EXPLORE_FIRST": "0"}
    env.pop("LAPS_ENGINE", None)
    return subprocess.Popen([sys.executable, "-u", "-m", "laps.serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env, text=True, encoding="utf-8")


def _read_until(p, want, secs=30):
    out = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < secs:
        line = p.stdout.readline()
        if not line:
            break
        o = json.loads(line)
        out.append(o)
        if want(o):
            return out
    raise AssertionError(f"not seen; got {[x.get('type') for x in out][-8:]}")


def test_live_copy_and_inbox_follow_a_run(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = Fake([{"calls": [("Bash", {"command": "sleep 20"})]}, {"text": "止められました。"}])
    p = _serve(tmp_path, fake.url, ws)
    try:
        p.stdin.write(json.dumps({"type": "run", "id": "r1", "prompt": "眠って", "source": "android"}) + "\n")
        p.stdin.flush()
        evs = _read_until(p, lambda o: o.get("type") == "tool_call")
        sid = next(o for o in evs if o.get("kind") == "run_start")["session"]
        d = tmp_path / "home" / "sessions" / sid
        rows = [json.loads(x) for x in (d / "live.ndjson").read_text(encoding="utf-8").splitlines()]
        start = next(o for o in rows if o.get("kind") == "run_start")
        assert start["source"] == "android" and start["prompt"] == "眠って"
        first = json.loads((d / "events.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert first["type"] == "run_spec" and first["payload"]["source"] == "android"
        # 受け箱に書いた中止は、古い id でも効く
        with open(d / "inbox.ndjson", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "cancel", "id": "from-phone"}) + "\n")
        done = _read_until(p, lambda o: o.get("type") == "done")[-1]
        assert done["outcome"] == "cancelled"
        rows = [json.loads(x) for x in (d / "live.ndjson").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["type"] == "done"
        # 受け箱に書いてはいけない型は無視する
        p.stdin.write(json.dumps({"type": "ping", "id": "p"}) + "\n")
        p.stdin.flush()
        _read_until(p, lambda o: o.get("kind") == "pong")
    finally:
        p.stdin.close()
        p.wait(timeout=15)
        fake.close()


def test_stdout_writer_flushes_on_probe(tmp_path):
    env = {**os.environ, "LAPS_HOME": str(tmp_path / "home"), "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "LAPS_WORKSPACE": str(tmp_path)}
    env.pop("LAPS_ENGINE", None)
    r = subprocess.run([sys.executable, "-u", "-m", "laps.serve", "--probe"], capture_output=True, text=True, env=env, timeout=60)
    assert json.loads(r.stdout.splitlines()[0])["kind"] == "ready"


def test_blocked_reader_does_not_freeze_the_writer():
    from laps.engine.wire import Out
    r, w = os.pipe()
    stream = os.fdopen(w, "w", encoding="utf-8")
    out = Out(stream)
    t0 = time.monotonic()
    for i in range(20000):                          # パイプが詰まっても token は捨てて進む
        out.emit("token", "r", text="x" * 200)
    assert time.monotonic() - t0 < 10
    os.close(r)
    out._closed = True


# ── Bash の前後の塞ぎ ──────────────────
def _ctx(tmp_path, monkeypatch, policy="trusted"):
    monkeypatch.setenv("LAPS_POLICY", policy)
    return Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())


def test_grep_with_no_match_is_not_a_failure_but_false_is(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    r = shell.bash({"command": "cat a.txt | grep nothing-here"}, ctx)
    assert r.ok and r.kind == "empty" and "matched no lines" in r.output
    r = shell.bash({"command": "false"}, ctx)
    assert not r.ok and "exit code: 1" in r.output


def test_killing_every_python_does_not_kill_blackwall(tmp_path):
    from laps.engine.tools import shellaid
    # 走らせない（同じ機械の別の仕事を巻き込まないように）。書き換えた命令と一言だけを見る。
    if os.name != "nt":
        _, _, refused = shellaid.guard("pkill python3", str(tmp_path), "bash", str(tmp_path), False)
        assert "includes L.A.P.S itself" in refused
    from laps.oc import shellfix
    guarded, note = shellfix.guard_self_kill("taskkill /F /IM python.exe", windows=True)
    assert guarded != "taskkill /F /IM python.exe" and note


def test_user_deny_patterns_are_honoured(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    from laps.engine.tools import shellaid
    from laps import settings
    monkeypatch.setattr(settings.Settings, "load", classmethod(lambda cls: type("S", (), {"deny_patterns": ["git push*"]})()))
    _, _, refused = shellaid.guard("git push origin main", str(tmp_path), "bash", str(tmp_path), False)
    assert "deny_patterns" in refused
    r = shell.bash({"command": "git push origin main"}, ctx)
    assert not r.ok and r.kind == "denied"


def test_server_command_starts_in_the_background_at_once(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    t0 = time.monotonic()
    r = shell.bash({"command": "python3 -m http.server 0 --bind 127.0.0.1"}, ctx)
    assert time.monotonic() - t0 < 5 and "starts a server" in r.output
    assert ctx.jobs.running()[0].server or ctx.jobs.is_server(ctx.jobs.running()[0])
    ctx.jobs.kill_all()


def test_workdir_the_command_creates_runs_in_the_parent(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    r = shell.bash({"command": "mkdir newdir && cd newdir && pwd", "workdir": "newdir"}, ctx)
    assert r.ok and "newdir" in r.output and "did not exist yet" in r.output


def test_cd_does_not_carry_over_note(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch)
    r = shell.bash({"command": "ls no-such-dir-xyz"}, ctx)
    assert not r.ok and "a `cd` in an earlier call does not" in r.output


def test_wrong_argument_name_is_read_as_the_right_one(run):
    go, ws, _ = run
    (ws / "a.txt").write_text("hello-arg\n", encoding="utf-8")
    _, a, fake, _ = go([{"calls": [("Read", {"file": "a.txt"})]}, {"text": "読みました。"}], "読んで")
    assert "hello-arg" in sent(fake, 1) and a.counters.get("args_renamed") == 1


def test_broken_image_is_dropped_and_the_run_goes_on(run):
    go, ws, _ = run
    from laps.engine.llm import BackendError
    t = Transcript(None)
    ctx = Ctx(workspace=str(ws), root=t.root, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    a = Agent(llm=LLM("http://127.0.0.1:9/v1", "m"), out=Rec(), rid="r", transcript=t, ctx=ctx, tools={}, system="S")
    t.append({"role": "user", "content": "[image] x.png", "bw_image": str(ws / "x.png"), "bw_note": True})
    assert a._drop_bad_image(BackendError("HTTP 400: Failed to load image or audio file"))
    assert "could not decode" in str(t.raw[-1]["content"]) and "bw_image" not in t.raw[-1]
    assert not a._drop_bad_image(BackendError("HTTP 400: other"))


def test_broken_output_file_is_reported_in_the_same_step(run):
    go, ws, _ = run
    script = [{"calls": [("Bash", {"command": "echo not-an-image > out.png"})]}, {"text": "直しました。"}, {"text": "はい。"}]
    _, a, fake, _ = go(script, "画像を変換して")
    assert a.counters.get("media_auto_bad") == 1 and "are broken" in sent(fake, 1)


def test_short_final_after_much_work_asks_for_a_report(run):
    go, ws, _ = run
    script = [{"calls": [("Glob", {"pattern": f"*{i}.nope"})]} for i in range(6)]
    script += [{"text": "完了。"}, {"text": "何をしたか・何が分かったか・何が残っているかを全部書いた、十分に長い報告です。" * 3}]
    text, a, fake, _ = go(script, "探して")
    assert a.counters.get("final_report:too_short") == 1 and "write the report" in sent(fake)


def test_english_drift_gets_a_note(run):
    go, ws, _ = run
    (ws / "a.txt").write_text("a\n", encoding="utf-8")
    body = "I will read the file now and then look at what it contains before going on with the rest of it."
    script = [{"text": body, "calls": [("Read", {"file_path": "a.txt"})]}, {"text": "読みました。"}]
    _, a, fake, _ = go(script, "読んで")
    assert a.counters.get("reply_lang_nudge") == 1


# ── 最後の照合で見つけた物（0.1.617） ────
def test_webfetch_does_not_go_to_internal_addresses(tmp_path, monkeypatch):
    from laps.engine.tools import web
    monkeypatch.delenv("LAPS_ALLOW_PRIVATE_FETCH", raising=False)
    ctx = _ctx(tmp_path, monkeypatch)
    for url in ("http://127.0.0.1:9/x", "http://192.0.2.1/", "http://169.254.169.254/latest/meta-data/"):
        r = web.webfetch({"url": url}, ctx)
        assert not r.ok and r.kind == "denied" and "blocked private" in r.output


def test_webfetch_checks_every_redirect(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from laps.engine.tools import web

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.send_header("Content-Length", "0")
            self.end_headers()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ctx = _ctx(tmp_path, monkeypatch)
    # 最初の 1 本だけ通す（手元の偽の公開サーバ）。転送先の検査が本物の判定で止める。
    real = web._blocked
    monkeypatch.setattr(web, "_blocked", lambda u: "" if "127.0.0.1" in u else real(u))
    r = web.webfetch({"url": f"http://127.0.0.1:{srv.server_address[1]}/"}, ctx)
    srv.shutdown()
    assert not r.ok and "blocked private" in r.output


def test_queued_done_does_not_close_the_live_copy(tmp_path):
    import io

    from laps.engine.wire import Out
    out = Out(io.StringIO())
    p = tmp_path / "live.ndjson"
    out.tee_open(p, rid="r1")
    out.emit("token", "r1", text="a")
    out.emit("done", "r2", outcome="cancelled")     # 順番待ちの依頼を取り消した
    out.emit("token", "r1", text="b")
    out.emit("done", "r1", outcome="accepted")
    out.emit("token", "r3", text="c")               # 閉じた後は書かない
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert [(x["type"], x["id"]) for x in rows] == [("token", "r1"), ("token", "r1"), ("done", "r1")]


def test_stop_also_cancels_queued_requests(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake = Fake([{"calls": [("Bash", {"command": "sleep 20"})]}, {"text": "止められました。"}])
    p = _serve(tmp_path, fake.url, ws)
    try:
        p.stdin.write(json.dumps({"type": "run", "id": "r1", "prompt": "眠って"}) + "\n")
        p.stdin.flush()
        _read_until(p, lambda o: o.get("type") == "tool_call")
        p.stdin.write(json.dumps({"type": "run", "id": "r2", "prompt": "次の依頼"}) + "\n")
        p.stdin.flush()
        _read_until(p, lambda o: o.get("kind") == "queued")
        p.stdin.write(json.dumps({"type": "cancel", "id": "r1"}) + "\n")
        p.stdin.flush()
        rows = _read_until(p, lambda o: o.get("type") == "done" and o.get("id") == "r1")
        assert any(o.get("type") == "done" and o.get("id") == "r2" and o.get("outcome") == "cancelled" for o in rows)
        assert any(o.get("kind") == "queue_cleared" for o in rows)
    finally:
        p.stdin.close()
        p.wait(timeout=15)
        fake.close()


def test_broken_output_is_generated_again_without_the_cache(run):
    go, ws, _ = run
    script = [{"think": "!" * 3000}, {"text": "はい、答えです。"}]
    text, a, fake, rec = go(script, "答えて")
    assert a.counters.get("degenerate_cut") == 1 and notices(rec, "degenerate_retry")
    assert fake.bodies[1]["cache_prompt"] is False and text == "はい、答えです。"


def test_english_analysis_in_a_gemma_answer_goes_to_thinking(run):
    go, ws, mp = run
    mp.setenv("LAPS_MODEL_FAMILY", "gemma")
    text, a, fake, rec = go([{"text": "Let me look at what the user wants here.\nThey want a greeting.\nこんにちは。"}], "挨拶して")
    tokens = "".join(f.get("text", "") for t, f in rec.events if t == "token")
    thinks = "".join(f.get("text", "") for t, f in rec.events if t == "think")
    assert tokens == "こんにちは。" and "Let me look" in thinks and text == "こんにちは。"


def test_gemma_answer_without_japanese_is_kept_as_the_answer(run):
    go, ws, mp = run
    mp.setenv("LAPS_MODEL_FAMILY", "gemma")
    text, *_ = go([{"text": "print('hello')\nprint('world')"}], "コードを見せて")
    assert text == "print('hello')\nprint('world')"


def test_gemma_call_written_in_thinking_is_run(run):
    go, ws, _ = run
    (ws / "a.txt").write_text("gemma-read\n", encoding="utf-8")
    script = [{"think": "I will read it.\n<|tool_call>call:Read{file_path:<|\"|>a.txt<|\"|>}<tool_call|>"}, {"text": "読みました。"}]
    _, a, fake, _ = go(script, "読んで")
    assert a.counters.get("thought_call_salvaged") == 1 and "gemma-read" in sent(fake, 1)


def test_review_turn_narration_is_not_added_to_the_answer(run):
    go, ws, _ = run
    first = "tool.py を書きました。動かしてはいません。これが報告です。"
    script = [{"calls": [("Write", {"file_path": "tool.py", "content": "print(1)\n"})]}, {"text": first}, {"text": "確かめの語り"}]
    text, a, fake, rec = go(script, "tool.py を作って")
    tokens = "".join(f.get("text", "") for t, f in rec.events if t == "token")
    thinks = "".join(f.get("text", "") for t, f in rec.events if t == "think")
    assert text == first and "確かめの語り" not in tokens and "確かめの語り" in thinks


def test_backend_error_names_where_it_was_sending(run, monkeypatch):
    go, ws, _ = run
    monkeypatch.setenv("LAPS_RECONNECT_SECS", "0")
    t = Transcript(None)
    ctx = Ctx(workspace=str(ws), root=t.root, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    rec = Rec()
    a = Agent(llm=LLM("http://127.0.0.1:9/v1", "m"), out=rec, rid="r", transcript=t, ctx=ctx, tools={}, system="S")
    a.run("こんにちは")
    errs = [f for ty, f in rec.events if ty == "error"]
    assert errs and "127.0.0.1:9" in errs[0]["message"] and a.outcome == "backend_error"


def test_image_for_a_model_that_cannot_see_returns_facts(tmp_path, monkeypatch):
    from laps.engine.tools import files
    ctx = _ctx(tmp_path, monkeypatch)
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000" "1f15c4890000000a49444154789c6360000000020001e221bc330000000049454e44ae426082")
    (tmp_path / "p.png").write_bytes(png)
    r = files.read({"file_path": "p.png"}, ctx)
    assert "<type>image</type>" in r.output         # 断るだけでなく、中身の事実を返す
