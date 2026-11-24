"""新しい中核（blackwall/engine）の試験。偽の推論機（OpenAI 互換のストリーム）を相手に本物の走行を回す。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from laps.engine import endgate
from laps.engine.llm import LLM
from laps.engine.loop import Agent, Limits
from laps.engine.repeat import RepeatWatch
from laps.engine.session import Transcript
from laps.engine.tools import Ctx, base_tools, files, shell
from laps.engine.tools.edit_match import EditError, replace


class Fake:
    """台本どおりに答える推論機。台本の 1 項目が 1 回の生成。"""

    def __init__(self, script: list[dict], n_ctx: int = 131072) -> None:
        self.script = list(script)
        self.bodies: list[dict] = []
        self.n_ctx = n_ctx
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"model_path": "/m/north-test.gguf", "chat_template": "",
                                   "default_generation_settings": {"n_ctx": fake.n_ctx}, "modalities": {}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                fake.bodies.append(json.loads(self.rfile.read(n)))
                turn = fake.script.pop(0) if fake.script else {"text": "(no more)"}
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()

                def chunk(o):
                    self.wfile.write(f"data: {json.dumps(o)}\n\n".encode())
                    self.wfile.flush()
                try:
                    for piece in turn.get("think_pieces") or ([turn["think"]] if turn.get("think") else []):
                        chunk({"choices": [{"delta": {"reasoning_content": piece}}]})
                        if turn.get("slow"):
                            time.sleep(turn["slow"] / 3)
                    if turn.get("text"):
                        chunk({"choices": [{"delta": {"content": turn["text"]}}]})
                    for i, (name, args) in enumerate(turn.get("calls") or []):
                        chunk({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"name": name, "arguments": json.dumps(args)}}]}}]})
                    chunk({"choices": [{"delta": {}, "finish_reason": turn.get("finish") or ("tool_calls" if turn.get("calls") else "stop")}]})
                    chunk({"choices": [], "usage": {"prompt_tokens": turn.get("prompt", 100), "completion_tokens": 10,
                                                    "total_tokens": turn.get("prompt", 100) + 10}})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class Rec:
    def __init__(self):
        self.events = []

    def emit(self, type_, rid=None, **f):
        self.events.append((type_, f))

    def notice(self, kind, rid=None, **f):
        self.events.append(("notice", {"kind": kind, **f}))

    def kinds(self):
        return [f.get("kind") for t, f in self.events if t == "notice"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    fakes = []

    def make(script, n_ctx=131072, tools=None):
        fake = Fake(script, n_ctx)
        fakes.append(fake)
        t = Transcript()
        ctx = Ctx(workspace=str(tmp_path), root=t.root, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
        rec = Rec()
        a = Agent(llm=LLM(fake.url, "north-test"), out=rec, rid="r1", transcript=t, ctx=ctx,
                  tools=tools if tools is not None else base_tools("focused"), system="SYSTEM")
        return a, fake, rec, tmp_path
    yield make
    for f in fakes:
        f.close()


def test_tool_then_answer_and_thinking_is_sent_back(env):
    a, fake, rec, ws = env([{"think": "THOUGHT-1", "calls": [("Write", {"file_path": "a.txt", "content": "hi"})]},
                            {"text": "a.txt を作りました。"}])
    out = a.run("a.txt を作ってください")
    assert out == "a.txt を作りました。" and a.outcome == "accepted"
    assert (ws / "a.txt").read_text() == "hi"
    second = fake.bodies[1]["messages"]
    assert any(m.get("reasoning_content") == "THOUGHT-1" and m.get("tool_calls") for m in second)
    assert any(m["role"] == "tool" for m in second)
    assert [t for t, _ in rec.events if t == "tool_call"] and a.effects == 1


def test_describing_the_work_is_sent_back_once_with_a_required_tool(env):
    how = "手順です。\n```bash\nmkdir x\n```\nこれで完了しました。"
    a, fake, rec, ws = env([{"text": how}, {"calls": [("Bash", {"command": "mkdir x"})]}, {"text": "x を作りました。"}])
    out = a.run("x フォルダを作ってきて")
    assert out == "x を作りました。" and (ws / "x").is_dir()
    assert fake.bodies[1]["tool_choice"] == "required" and "answer_nudge" in rec.kinds()


def test_only_looking_then_saying_done_is_sent_back_when_asked_to_make(env):
    a, fake, rec, _ = env([{"calls": [("Bash", {"command": "ls"})]}, {"text": "音声を集めました。完了しました。"},
                           {"calls": [("Bash", {"command": "touch got.wav"})]}, {"text": "got.wav を保存しました。"}])
    assert a.run("音声を集めてきて") == "got.wav を保存しました。" and a.counters.get("end_handoff") == 1


def test_investigation_that_only_reads_is_accepted(env):
    a, fake, rec, _ = env([{"calls": [("Bash", {"command": "ls"})]}, {"text": "調査が完了しました。原因は X です。"}])
    assert "原因は X" in a.run("動かない理由を調べて") and not a.counters.get("end_handoff")
    assert len(fake.bodies) == 2


def test_a_question_is_not_sent_back(env):
    a, fake, rec, _ = env([{"text": "原因はキャッシュです。"}])
    assert a.run("なぜ遅いのですか？") == "原因はキャッシュです。"
    assert len(fake.bodies) == 1


def test_repeating_thought_is_cut_and_not_recorded(env):
    loop = "takina_info.txt を確認する必要があります。\n" * 40
    a, fake, rec, _ = env([{"think_pieces": [loop[i:i + 40] for i in range(0, len(loop), 40)]}, {"text": "見つかりませんでした。"}])
    out = a.run("takina の場所はどこ？")
    assert out == "見つかりませんでした。" and a.counters.get("repeat_cut") == 1
    assert not any(m.get("role") == "assistant" and "takina" in str(m.get("reasoning_content")) for m in a.t.raw)
    # 切った後も思考を減らさず、道具も強いない（2026-10-13 オーナー「モデルには自由に考えさせます」）
    assert fake.bodies[1]["reasoning_budget_tokens"] == 8192 and fake.bodies[1].get("tool_choice") != "required"


def test_same_call_three_times_is_not_run(env):
    call = {"calls": [("Bash", {"command": "echo hi >> n.txt"})]}
    a, fake, rec, ws = env([call, call, call, {"text": "やめました。"}])
    a.run("n.txt に書いて")
    assert (ws / "n.txt").read_text().count("hi") == 2 and "doom_loop" in rec.kinds()


def test_unfinished_plan_is_sent_back(env):
    plan = {"todos": [{"content": "a を作る", "status": "completed"}, {"content": "b を作る", "status": "pending"}]}
    done = {"todos": [{"content": "a を作る", "status": "completed"}, {"content": "b を作る", "status": "completed"}]}
    a, fake, rec, _ = env([{"calls": [("TodoWrite", plan), ("Bash", {"command": "touch a"})]}, {"text": "途中です。"},
                           {"calls": [("TodoWrite", done), ("Bash", {"command": "touch b"})]}, {"text": "全部終わりました。"}])
    assert a.run("a と b を作って") == "全部終わりました。" and "todo_nudge" in rec.kinds()
    assert len(fake.bodies) == 4


def test_empty_turn_is_retried(env):
    a, fake, rec, _ = env([{"think": "考えた"}, {"text": "答えです。"}])
    assert a.run("教えて") == "答えです。" and a.counters.get("empty_continue") == 1


def test_compaction_when_the_window_fills(env):
    a, fake, rec, ws = env([{"calls": [("Bash", {"command": "echo hi"})], "prompt": 90_000},
                            {"text": "## Objective\n- x\n## Next Move\n1. y"}, {"text": "完了しました。"}], n_ctx=100_000)
    assert a.run("echo して") == "完了しました。" and a.compactions == 1
    last = fake.bodies[-1]["messages"]
    assert "## Objective" in last[1]["content"] and "echo して" in last[1]["content"]


def test_declined_is_reported(env):
    a, fake, rec, _ = env([{"finish": "content_filter"}])
    assert "断りました" in a.run("やって") and a.outcome == "declined"


def test_injected_message_reaches_the_model(env):
    a, fake, rec, _ = env([{"calls": [("Bash", {"command": "echo hi"})]}, {"text": "了解しました。"}])
    orig = a._execute

    def later(tcs):
        a.inject("やっぱり止めて")
        orig(tcs)
    a._execute = later
    a.run("echo して")
    assert "やっぱり止めて" in json.dumps(fake.bodies[1]["messages"], ensure_ascii=False)


def test_cancel_stops_a_running_command(env):
    a, fake, rec, _ = env([{"calls": [("Bash", {"command": "sleep 30"})]}])
    threading.Timer(0.5, a.cancel).start()
    a.run("sleep して")
    assert a.outcome == "cancelled"


def test_background_job_keeps_the_run_open_until_it_ends(env):
    a, fake, rec, ws = env([{"calls": [("Bash", {"command": "sleep 1; echo fin", "run_in_background": True})]},
                            {"text": "待ちます。"}, {"text": "終わりました。結果は fin です。"}])
    assert a.run("sleep を走らせて") == "終わりました。結果は fin です。"
    assert "bg_park" in rec.kinds() and "fin" in json.dumps(fake.bodies[-1]["messages"], ensure_ascii=False)


def test_slow_command_moves_to_the_background_instead_of_dying(env):
    a, fake, rec, ws = env([{"calls": [("Bash", {"command": "sleep 1; touch done.txt", "timeout": 100})]},
                            {"text": "終わりました。"}, {"text": "done.txt を確かめました。"}])
    a.run("done.txt を作って")
    assert (ws / "done.txt").exists()


def test_command_that_leaves_a_daemon_returns_at_once(env):
    a, fake, rec, ws = env([{"calls": [("Bash", {"command": "sleep 20 & echo started"})]}, {"text": "はい。"}])
    t0 = time.monotonic()
    a.run("起動を実行して")
    assert time.monotonic() - t0 < 10
    assert "started" in json.dumps(fake.bodies[1]["messages"], ensure_ascii=False)


def test_broken_records_are_repaired_before_sending(env):
    a, fake, rec, ws = env([{"text": "はい。"}])
    a.t.append({"role": "user", "content": "前"})
    a.t.append({"role": "assistant", "content": "", "tool_calls": [{"id": "x1", "type": "function", "function": {"name": "Bash", "arguments": "{}"}}]})
    a.t.append({"role": "tool", "tool_call_id": "orphan", "content": "?"})
    a.run("教えて")
    msgs = fake.bodies[0]["messages"]
    tools = [m for m in msgs if m["role"] == "tool"]
    assert len(tools) == 1 and tools[0]["tool_call_id"] == "x1"


def test_progress_is_required_but_not_passed_to_the_tool(env):
    a, fake, rec, ws = env([{"calls": [("Bash", {"command": "echo hi", "progress": "確かめます"})]}, {"text": "はい。"}])
    a.run("echo を実行して")
    schema = next(t for t in fake.bodies[0]["tools"] if t["function"]["name"] == "Bash")
    assert "progress" in schema["function"]["parameters"]["required"]
    shown = next(f for t, f in rec.events if t == "tool_call")
    assert shown["input"]["progress"] == "確かめます"


def test_cancel_and_force_during_generation(env):
    slow = {"think_pieces": ["考え中です。"] * 3}
    a, fake, rec, _ = env([{**slow, "slow": 3.0}, {"text": "新しい指示を受けました。"}])
    threading.Timer(0.6, lambda: a.inject("止まって別のことを", force=True)).start()
    assert a.run("長い作業") == "新しい指示を受けました。"
    assert "preempted" in rec.kinds() and a.counters.get("preempted_generation") == 1
    assert "止まって別のことを" in json.dumps(fake.bodies[-1]["messages"], ensure_ascii=False)
    b, fake2, rec2, _ = env([{**slow, "slow": 3.0}])
    threading.Timer(0.6, b.cancel).start()
    b.run("長い作業")
    assert b.outcome == "cancelled"


def test_edit_match_and_hints():
    assert replace("a\n  b\nc\n", "b", "X") == "a\n  X\nc\n"
    assert replace("def f():\n    return 1\n", "def f():\n  return 1", "def f():\n    return 2") == "def f():\n    return 2\n"
    with pytest.raises(EditError):
        replace("a a", "a", "b")
    assert replace("a a", "a", "b", True) == "b b"


def test_policy_still_refuses_protected_paths(tmp_path):
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event())
    r = files.write({"file_path": str(tmp_path / ".ssh" / "authorized_keys"), "content": "x"}, ctx)
    assert not r.ok and r.kind == "denied" and not (tmp_path / ".ssh").exists()


def test_small_helpers():
    assert shell.read_only("ls -la | grep x && git status") and not shell.read_only("rm x") and not shell.read_only("echo a > b")
    assert endgate.unrun_commands("```bash\nmkdir x\nls\n```", ["ls"]) == ["mkdir x"]
    w = RepeatWatch()
    assert all(w.feed(f"ファイル {i} を読みました。理由は別々です、番号 {i * 7} です。\n") is None for i in range(30))


def _serve(fake: Fake, home: Path, ws: Path, lines: list[dict], expect_done: int):
    env = dict(os.environ, LAPS_BACKEND=fake.url, LAPS_HOME=str(home), LAPS_WORKSPACE=str(ws),
               LAPS_POLICY="trusted", PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    env.pop("LAPS_ENGINE", None)
    p = subprocess.Popen([sys.executable, "-u", "-m", "laps.serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, cwd=str(ws))
    events = [json.loads(p.stdout.readline())]
    for req in lines:
        p.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        p.stdin.flush()
        while True:
            e = json.loads(p.stdout.readline())
            events.append(e)
            if e["type"] == "done" or (e["type"] == "error" and e.get("kind") == "rejected"):
                break
            if e.get("type") == "notice" and req.get("type") not in ("run", "send"):
                break
    p.stdin.write('{"type":"quit"}\n')
    p.stdin.flush()
    p.wait(timeout=20)
    return events


def test_the_wire_with_a_session_that_continues(tmp_path):
    fake = Fake([{"think": "T1", "calls": [("Bash", {"command": "echo hi"})]}, {"text": "一つ目。"}, {"text": "二つ目。"}])
    home, ws = tmp_path / "h", tmp_path / "w"
    ws.mkdir()
    first = _serve(fake, home, ws, [{"type": "run", "id": "r1", "messages": [{"role": "user", "content": "やって"}]}], 1)
    assert first[0]["kind"] == "ready" and first[0]["protocol"] == 1 and "Bash" in first[0]["tools"]
    done = first[-1]
    assert done["type"] == "done" and done["outcome"] == "accepted" and done["text"] == "一つ目。" and done["counters"]["tool_calls:Bash"] == 1
    result = next(e for e in first if e["type"] == "tool_result")
    assert result["ok"] is True and result["summary"]["head"] == "echo hi"
    second = _serve(fake, home, ws, [{"type": "run", "id": "r2", "session": done["session"],
                                      "messages": [{"role": "user", "content": "やって"}, {"role": "assistant", "content": "一つ目。"},
                                                   {"role": "user", "content": "続けて"}]}], 1)
    assert second[-1]["session"] == done["session"]
    sent = fake.bodies[-1]["messages"]
    assert any(m.get("reasoning_content") == "T1" for m in sent) and any(m["role"] == "tool" for m in sent)    # 会話の記録から続く
    assert (home / "sessions" / done["session"] / "events.jsonl").exists()
    fake.close()


def test_the_wire_asks_the_user_and_takes_the_answer(tmp_path):
    fake = Fake([{"calls": [("AskUserQuestion", {"question": "どちら？", "options": ["A", "B"]})]}, {"text": "A にしました。"}])
    env = dict(os.environ, LAPS_BACKEND=fake.url, LAPS_HOME=str(tmp_path / "h"), LAPS_WORKSPACE=str(tmp_path),
               PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    env.pop("LAPS_ENGINE", None)
    p = subprocess.Popen([sys.executable, "-u", "-m", "laps.serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env, cwd=str(tmp_path))
    p.stdin.write(json.dumps({"type": "run", "id": "r1", "prompt": "選んで"}, ensure_ascii=False) + "\n")
    p.stdin.flush()
    done = None
    while done is None:
        e = json.loads(p.stdout.readline())
        if e["type"] == "ask":
            assert e["question"] == "どちら？" and e["options"] == ["A", "B"]
            p.stdin.write(json.dumps({"type": "answer", "ask": e["ask"], "text": "A"}) + "\n")
            p.stdin.flush()
        if e["type"] == "done":
            done = e
    p.stdin.write('{"type":"quit"}\n')
    p.stdin.flush()
    p.wait(timeout=20)
    assert done["text"] == "A にしました。"
    assert "The user answered: A" in json.dumps(fake.bodies[-1]["messages"], ensure_ascii=False)
    fake.close()


def test_the_wire_rejects_execution_settings(tmp_path):
    fake = Fake([])
    ev = _serve(fake, tmp_path / "h", tmp_path, [{"type": "run", "id": "r1", "policy": "off", "prompt": "x"}], 0)
    assert ev[-1]["type"] == "error" and ev[-1]["kind"] == "rejected"
    fake.close()


def test_probe_answers_ready(tmp_path):
    env = dict(os.environ, LAPS_HOME=str(tmp_path), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    env.pop("LAPS_ENGINE", None)
    r = subprocess.run([sys.executable, "-m", "laps.serve", "--probe"], capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout.splitlines()[0])["kind"] == "ready"
