"""新しい中核と記憶（作業ログ・帳面・失敗と直し方・Construct・Remember）のつながりの試験。偽の推論機で本物の走行を回す。"""
from __future__ import annotations

import json
import threading

import pytest

from laps.engine.llm import LLM
from laps.engine.loop import Agent
from laps.engine.memory import Memory
from laps.engine.session import Transcript
from laps.engine.tools import Ctx, base_tools, shell
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
    monkeypatch.chdir(ws)
    fakes: list[Fake] = []

    def go(script, request, *, session=None, n_ctx=131072):
        fake = Fake(script, n_ctx)
        fakes.append(fake)
        t = Transcript(session)
        ctx = Ctx(workspace=str(ws), root=t.root, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
        load_todos(ctx)
        rec = Rec()
        a = Agent(llm=LLM(fake.url, "north-test"), out=rec, rid="r1", transcript=t, ctx=ctx, tools=base_tools("full"), system="SYSTEM")
        a.memory = Memory(str(ws), t.id, a._count)
        text = a.run(request)
        assert a.memory.last_error == ""
        return text, a, fake, rec
    yield go, ws
    for f in fakes:
        f.close()


def sent(fake, i=-1) -> str:
    return json.dumps(fake.bodies[i]["messages"], ensure_ascii=False)


def test_work_log_is_written_and_read_by_the_next_conversation(run):
    go, ws = run
    text, a, fake, rec = go([{"calls": [("Bash", {"command": "touch made.txt"})]}, {"text": "作りました。"}], "made.txt を作って")
    log = (ws / ".laps" / "WORKLOG.md").read_text(encoding="utf-8")
    assert "made.txt を作って" in log and a.counters.get("worklog_written") == 1
    _, b, fake2, _ = go([{"text": "前回は made.txt を作りました。"}], "前に何をした？")
    assert "<worklog " in sent(fake2, 0) and b.counters.get("worklog_loaded") == 1
    # 続きの依頼には添えない・添え書きは利用者の発言として読まれない
    _, c, fake3, _ = go([{"text": "はい。"}], "続きを教えて", session=b.t.id)
    assert not c.counters.get("worklog_loaded")
    from laps.term.slash import _scan_user
    lines = (b.t.root / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    assert "worklog" not in _scan_user(lines, 200)


def test_questions_do_not_fill_the_work_log(run):
    go, ws = run
    go([{"text": "2 です。"}], "1+1 は？")
    assert not (ws / ".laps" / "WORKLOG.md").exists()


def test_compaction_keeps_facts_plan_and_full_text(run):
    go, ws = run
    plan = {"todos": [{"content": "calc を作る", "status": "in_progress"}]}
    summary = "## Objective\n- calc\n\n## Verified Facts\n- python3 は 3.12 だった（--version で確認）\n\n## Dead Ends\n- python は無い\n\n## Next Move\n1. 続ける"
    _, a, fake, rec = go([{"calls": [("TodoWrite", plan), ("Bash", {"command": "echo hi > a.txt"})], "prompt": 90_000},
                          {"text": summary},
                          {"calls": [("TodoWrite", {"todos": [{"content": "calc を作る", "status": "completed"}]})]},
                          {"text": "できました。"}], "calc を作って", n_ctx=100_000)
    assert a.compactions == 1
    ledger = (ws / ".laps" / "FINDINGS.md").read_text(encoding="utf-8")
    assert "3.12" in ledger and "python は無い" in ledger
    after = sent(fake, 2)
    # ★2026-11-26 要約の後は、要約に既にある事実を帳面から付け直さない（LAPS_COMPACT_LEAN）。事実は要約の本文で届き、帳面は場所で示す。
    assert "3.12" in after and "python は無い" in after and "FINDINGS.md" in after
    assert "calc を作る" in after and ".laps" in after and "history" in after
    assert a.counters.get("findings_added", 0) >= 2 and a.counters.get("todos_after_compaction") == 1


def test_plan_survives_into_the_next_request_without_a_false_nudge(run):
    go, ws = run
    plan = {"todos": [{"content": "残り", "status": "pending"}]}
    _, a, *_ = go([{"calls": [("TodoWrite", plan), ("Bash", {"command": "touch a"})]}, {"text": "途中です。"}, {"text": "止めます。"}, {"text": "止めます。"}], "a を作って")
    _, b, fake2, rec2 = go([{"text": "雑談です。"}], "今日の天気は？", session=a.t.id)
    assert [t["content"] for t in b.ctx.todos] == ["残り"]
    assert "todo_nudge" not in rec2.kinds()


def test_recite_every_twenty_turns(run):
    go, ws = run
    script = [{"calls": [("Bash", {"command": f"echo {i}"})]} for i in range(21)] + [{"text": "終わり。"}]
    _, a, fake, rec = go(script, "数えるのを実行して")
    assert a.counters.get("recite") == 1
    assert "Reminder of the request" in sent(fake)


def test_lessons_from_a_failure_are_recalled_later(run):
    go, ws = run
    bad = {"calls": [("Bash", {"command": "ls /no/such/dir/x"})]}
    bad2 = {"calls": [("Bash", {"command": "ls /no/such/dir/y"})]}
    good = {"calls": [("Bash", {"command": "ls /tmp"})]}
    _, a, *_ = go([bad, bad2, good, {"text": "別の場所で確かめました。"}], "フォルダを確かめて")
    assert a.counters.get("tool_failed") == 2
    from laps.oc import lessons
    assert a.memory.lessons.saved == 1 and lessons._load()
    _, b, fake2, _ = go([bad, {"text": "はい。"}], "フォルダを確かめて")
    assert b.counters.get("lesson_recalled") == 1
    assert "An earlier run hit this same error" in sent(fake2)


def test_user_correction_is_remembered_in_a_continuing_conversation(run):
    go, ws = run
    cmd = "docker system prune --volumes"
    _, a, *_ = go([{"calls": [("Bash", {"command": cmd})]}, {"text": "片付けました。"}], "docker を片付けて")
    _, b, *_ = go([{"text": "すみません。"}], "違う、docker system prune --volumes は使わないでと言ったでしょ", session=a.t.id)
    assert b.counters.get("construct_correction") == 1
    # 同じ会話では止めない（言われたことを守る操作まで止めないため）。別の会話で同じ操作をしようとしたら一度だけ止める。
    call = {"calls": [("Bash", {"command": cmd})]}
    _, c, fake3, rec3 = go([call, call, {"text": "理由を書いて進めました。"}], "docker を片付けて")
    assert c.counters.get("construct_correction_gate") == 1
    assert "The user corrected this kind of action" in sent(fake3, 1)
    assert "The user corrected this kind of action" not in json.dumps(fake3.bodies[2]["messages"][-1], ensure_ascii=False)


def test_work_folder_changes_are_noted_and_reported(run):
    go, ws = run
    _, a, fake, rec = go([{"calls": [("Bash", {"command": "touch by_shell.txt"})]}, {"text": "はい。"}], "by_shell.txt を作って")
    assert "Work folder changes" in sent(fake) and "by_shell.txt" in sent(fake)
    assert a.memory.new_files() == ["by_shell.txt"] and a.done_fields("x")["changed"] is True
    _, b, fake2, _ = go([{"calls": [("Read", {"file_path": "by_shell.txt"})]}, {"text": "空です。"}], "by_shell.txt を読んで教えて")
    assert "Work folder changes" not in sent(fake2)


def test_failure_type_does_not_collapse_into_the_exit_code(run):
    go, ws = run
    from laps.oc import evidence
    m = Memory(str(ws), "s", lambda k, n=1: None)
    from laps.engine.memory import _EXIT_LINE
    a = _EXIT_LINE.sub("", "bash: foo: command not found\n\n<shell_metadata>\nexit code: 127\n</shell_metadata>")
    assert "not found" in evidence.failure_signature("Bash", a)[0] and m.last_error == ""


def test_remember_and_construct_are_tools(run):
    go, ws = run
    _, a, fake, rec = go([{"calls": [("Remember", {"action": "add", "text": "返事は日本語で。"})]}, {"text": "覚えました。"}], "返事は日本語で、と覚えておいて")
    names = {t["function"]["name"] for t in fake.bodies[0]["tools"]}
    assert {"Remember", "Construct"} <= names
    from laps.engine import prompt
    assert "返事は日本語で。" in prompt.system(str(ws), "m", "bash")
    key = prompt.remembered_key()
    assert key and key == prompt.remembered_key()
    _, b, fake2, _ = go([{"calls": [("Construct", {"query": "made.txt"})]}, {"text": "覚えていません。"}], "前の作業を思い出して教えて")
    assert b.counters.get("construct_recall") == 1
