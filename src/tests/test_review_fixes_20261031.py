"""2026-10-31 全体のレビュー（engine・relay）で見つけた不具合の試験。どれも直す前に落ちることを確かめた形。"""
from __future__ import annotations

import http.server
import json
import os
import threading
import time

import pytest

from laps.engine import endgate
from laps.engine.llm import LLM, Cancelled
from laps.engine.session import Transcript, _clean, clean_json
from laps.engine.tools import Ctx
from laps.engine.tools import files as F
from laps.engine.tools import shellaid
from laps.engine.tools.shell import KEEP_DONE_JOBS, Jobs, bash, read_only
from tests.test_engine_aids_20261030 import run, sent  # noqa: F401


PW = "hunter" + "2xx"                  # 試験の値をそのまま書かない（test_redact が実物のソースに当てて数える）
VALUE = "S3cr3t" + "Pass99"
HERE = "sudo -S ls <<< "
HERE_SSH = "ssh h 'sudo -S ls' <<< "
TO_SUDO = "' | sudo -S ls"


def _ctx(tmp_path, **kw):
    return Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=Jobs(), **kw)


# ── 伏せる処理と JSON ─────────────────────
@pytest.mark.parametrize("cmd", ['echo "login --password={}"', 'ssh h "mysql --password={}"', 'bash -c "curl --token={}"'])
def test_masking_keeps_json_valid(cmd):
    cmd = cmd.format(VALUE)
    shown = json.loads(clean_json(json.dumps({"command": cmd}, ensure_ascii=False)))
    assert VALUE not in shown["command"]
    args = json.dumps({"command": cmd}, ensure_ascii=False)     # 記録の道具の呼び出しは引数を文字列で持つ
    line = clean_json(json.dumps({"tool_calls": [{"function": {"name": "Bash", "arguments": args}}]}, ensure_ascii=False))
    assert VALUE not in json.loads(json.loads(line)["tool_calls"][0]["function"]["arguments"])["command"]


def test_a_tool_call_with_a_password_does_not_crash_and_survives_reload(run):
    go, ws, _ = run
    script = [{"calls": [("Bash", {"command": 'echo "login --password=abc"'})]}, {"text": "おわり"}]
    text, a, _, _ = go(script, "これを走らせて")
    assert a.outcome == "accepted" and text == "おわり"
    assert len(Transcript(a.t.id).raw) == len(a.t.raw)


@pytest.mark.parametrize("before, after", [(HERE + "'", "'"), ("echo '", TO_SUDO), (HERE, ""), (HERE_SSH + "'", "'"), ("printf '%s\\n' '", TO_SUDO)])
def test_sudo_password_on_stdin_is_masked(before, after):
    assert PW not in _clean(before + PW + after)


def test_variables_and_plain_pipes_are_left_alone():
    for cmd in ('echo "$PW" | sudo -S ls', "echo hello | grep x", "sudo ls"):
        assert _clean(cmd) == cmd


# ── 終わりの確かめ ─────────────────────────
@pytest.mark.parametrize("q", ["このファイルを削除するコマンドは何ですか？", "これどうやって使うの？", "削除するコマンドは何ですか"])
def test_a_question_is_not_pushed_into_doing_the_work(q):
    kind, _ = endgate.check(q, "```bash\nrm a.txt\n```\nで消せます。", tools_used=0, effects=0, ran=[], todos=[], handoffs=0, todo_nudges=0)
    assert kind == ""


@pytest.mark.parametrize("q", ["Run the tests and tell me what fails", "テストを走らせて、どれが落ちたか見せて", "Can you fix this bug?", "直してくれますか？"])
def test_a_request_with_question_words_still_has_to_be_done(q):
    kind, _ = endgate.check(q, "やり方はこうです。", tools_used=0, effects=0, ran=[], todos=[], handoffs=0, todo_nudges=0)
    assert kind == "handoff"


# ── 走行 ───────────────────────────────────
def test_repeating_a_call_ends_with_an_explanation(run):
    go, ws, _ = run
    script = [{"calls": [("Glob", {"pattern": "*.nope"})]} for _ in range(8)] + [{"text": "おわり"}]
    text, a, _, _ = go(script, "ファイルを探して報告してください")
    assert a.outcome == "loop_detected" and "繰り返して" in text and not a.ctx.cancel.is_set()


def test_a_forced_message_during_a_summary_does_not_end_the_run(run, monkeypatch):
    import laps.engine.loop as L
    go, ws, _ = run
    calls = {"n": 0}

    def summarize(*a, **k):
        calls["n"] += 1
        raise Cancelled()
    monkeypatch.setattr(L.compact, "overflowing", lambda *a, **k: calls["n"] == 0)
    monkeypatch.setattr(L.compact, "summarize", summarize)
    monkeypatch.setattr(L.compact, "prune", lambda t: 0)
    text, a, _, _ = go([{"text": "x"}], "こんにちは")
    assert a.outcome == "accepted" and text == "x" and a.counters.get("preempted_generation") == 1


def test_a_stop_during_a_summary_still_stops(run, monkeypatch):
    import laps.engine.loop as L
    go, ws, _ = run
    agent = {}

    def summarize(*a, **k):
        agent["a"].ctx.cancel.set()
        raise Cancelled()
    orig = L.Agent._compact

    def compact_(self, **k):
        agent["a"] = self
        return orig(self, **k)
    monkeypatch.setattr(L.Agent, "_compact", compact_)
    monkeypatch.setattr(L.compact, "overflowing", lambda *a, **k: True)
    monkeypatch.setattr(L.compact, "summarize", summarize)
    monkeypatch.setattr(L.compact, "prune", lambda t: 0)
    _, a, _, _ = go([{"text": "x"}], "これを調べて報告して")
    assert a.outcome == "cancelled"


def test_running_the_same_check_after_an_edit_is_not_blocked(run):
    go, ws, _ = run
    (ws / "t.py").write_text("import sys\nprint('bad');sys.exit(1)\n# 0\n", encoding="utf-8")
    script = []
    for i in range(7):
        script.append({"calls": [("Bash", {"command": "python3 t.py"})]})
        script.append({"text": "直します", "calls": [("Edit", {"file_path": "t.py", "old_string": f"# {i}", "new_string": f"# {i + 1}"})]})
    text, a, _, _ = go(script + [{"text": "おわり"}], "t.py を直してください")
    assert not a.counters.get("repeat_anywhere_blocked")


def test_a_scout_that_looked_around_but_wrote_nothing_still_passes_on_what_it_read(run):
    go, ws, _ = run
    for i in range(25):
        (ws / f"f{i}.txt").write_text("x\n", encoding="utf-8")
    (ws / "a.txt").write_text("x\n", encoding="utf-8")
    os.environ["LAPS_EXPLORE_FIRST"] = "1"
    script = [{"calls": [("Read", {"file_path": "a.txt"})]}, {"text": "ANSWER: nothing to look at for this request"}, {"text": "おわり"}]
    try:
        text, a, fake, _ = go(script, "a.txt を整理して、内容を報告してください")
    finally:
        os.environ["LAPS_EXPLORE_FIRST"] = "0"
    assert a.counters.get("explore_first:ran") == 1 and "It looked at:" in sent(fake)


def test_a_failed_child_is_a_failure_for_the_parent(run):
    from laps.engine.llm import BackendError
    go, ws, _ = run
    agent = {}
    script = [{"calls": [("Task", {"description": "x", "prompt": "y", "subagent_type": "general"})]}, {"text": "おわり"}]
    import laps.engine.loop as L
    orig = L.Agent._generate

    def gen(self, **k):
        if self.child:
            raise BackendError("the server is gone")
        agent["a"] = self
        return orig(self, **k)
    L.Agent._generate = gen
    try:
        _, a, _, _ = go(script, "これを任せて報告して")
    finally:
        L.Agent._generate = orig
    result = next(m for m in a.t.raw if m.get("role") == "tool")["content"]
    assert "the server is gone" in result and a.counters.get("tool_failed") == 1


def test_a_child_system_change_reaches_the_parent_footer(run, monkeypatch):
    from laps.oc import disclose
    monkeypatch.setattr(disclose, "call_changes", lambda real, inp: ["ufw allow 22"] if "ufw" in str(inp) else [])
    go, ws, _ = run
    script = [{"calls": [("Task", {"description": "x", "prompt": "y", "subagent_type": "general"})]},
              {"calls": [("Bash", {"command": "echo ufw"})]}, {"text": "済みました"}, {"text": "おわり"}]
    _, a, _, _ = go(script, "これを任せて報告して")
    assert ("ufw allow 22", True) in a.aids.sys_changes


def test_review_does_not_check_the_plan_of_an_earlier_request(run, tmp_path):
    go, ws, _ = run
    first = [{"calls": [("TodoWrite", {"todos": [{"content": "古い依頼の項目", "status": "completed"}]})]}, {"text": "済みました"}]
    _, a, _, _ = go(first, "古い依頼です。予定を書いてください", session="sess1")
    script = [{"calls": [("Write", {"file_path": "n.txt", "content": "x\n"})]}]
    script += [{"calls": [("Glob", {"pattern": f"*{i}.nope"})]} for i in range(8)]
    script += [{"text": "全部できました。詳しい報告です。" * 5}, {"calls": [("Read", {"file_path": "n.txt"})]}, {"text": "✓ n.txt — 読んだ"}]
    _, _, fake, _ = go(script, "n.txt を作って", session="sess1")
    assert "古い依頼の項目" not in "\n".join(m["content"] for m in fake.bodies[-1]["messages"] if isinstance(m.get("content"), str) and "review your own work" in m["content"])


def test_a_requested_script_is_not_tidied_away(run):
    go, ws, _ = run
    script = [{"calls": [("Write", {"file_path": "parse_config.py", "content": "print(1)\n"})]},
              {"calls": [("Bash", {"command": "python3 parse_config.py"})]}, {"text": "作りました。動きます。"}]
    go(script, "parse_config.py を作ってください")
    assert (ws / "parse_config.py").is_file() and not (ws / "_scratch").exists()


# ── Bash ───────────────────────────────────
def test_finished_commands_do_not_keep_file_descriptors(tmp_path):
    jobs = Jobs()
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(KEEP_DONE_JOBS + 8):
        jobs.start("true", str(tmp_path)).done.wait(5)
    deadline = time.monotonic() + 3
    while len(os.listdir("/proc/self/fd")) > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(os.listdir("/proc/self/fd")) <= before
    assert len(jobs.items) <= KEEP_DONE_JOBS + 1


@pytest.mark.parametrize("cmd", ["find . -delete", "find . -exec rm {} ;", "env rm -rf x", "echo $(rm -rf x)", "echo `rm x`", "sed --in-place s/a/b/ f",
                                 "sed -i.bak s/a/b/ f", "ip link set eth0 down", "date -s 2020-01-01", "sort -o f f", "hostname evil",
                                 "awk 'BEGIN{system(\"rm\")}'"])
def test_commands_that_change_things_are_not_read_only(cmd):
    assert not read_only(cmd)


@pytest.mark.parametrize("cmd", ["ls -la", "find . -name x", "env", "sed -n 1,5p f", "ip addr", "date", "sort f", "hostname", "git status", "cd d && grep x f | wc -l"])
def test_reading_commands_stay_read_only(cmd):
    assert read_only(cmd)


@pytest.mark.parametrize("cmd", ["grep x f", "cd d && grep x f", "cd d; grep x f", 'cd "a b" && rg x', "cd a && cd b && grep x f"])
def test_a_grep_that_finds_nothing_is_not_a_failure(cmd):
    assert shellaid.no_match(cmd, 1, "") in ("grep", "rg")


def test_other_failures_after_cd_stay_failures():
    assert not shellaid.no_match("cd d && make", 1, "")
    assert not shellaid.no_match("cd nope && grep x f", 1, "bash: cd: nope: No such file or directory")


def test_a_timeout_written_in_seconds_is_read_as_seconds(tmp_path):
    ctx = _ctx(tmp_path, child=True)
    r = bash({"command": "sleep 0.3; echo ok", "timeout": 5}, ctx)
    assert r.ok and "ok" in r.output


# ── ファイル ───────────────────────────────
def test_edit_refuses_a_file_it_cannot_write_back(tmp_path):
    p = tmp_path / "l1.txt"
    raw = b"caf\xe9 \xff\xfe hello\n"
    p.write_bytes(raw)
    r = F.edit({"file_path": "l1.txt", "old_string": "hello", "new_string": "world"}, _ctx(tmp_path))
    assert not r.ok and p.read_bytes() == raw


def test_edit_refuses_a_character_the_encoding_cannot_hold(tmp_path):
    p = tmp_path / "s.txt"
    raw = "日本語 hello\n".encode("cp932")
    p.write_bytes(raw)
    r = F.edit({"file_path": "s.txt", "old_string": "hello", "new_string": "✓"}, _ctx(tmp_path))
    assert not r.ok and p.read_bytes() == raw
    assert F.edit({"file_path": "s.txt", "old_string": "hello", "new_string": "世界"}, _ctx(tmp_path)).ok
    assert p.read_bytes() == "日本語 世界\n".encode("cp932")


def test_edit_keeps_symlinks(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("hello\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    os.symlink(real, link)
    assert F.edit({"file_path": "link.txt", "old_string": "hello", "new_string": "world"}, _ctx(tmp_path)).ok
    assert link.is_symlink() and real.read_text(encoding="utf-8") == "world\n"


@pytest.mark.parametrize("pat", ["foo(?=bar)", r"(o)\1", "foo"])
def test_grep_does_not_say_nothing_when_rg_refuses_the_pattern(tmp_path, pat):
    (tmp_path / "a.txt").write_text("foobar\n", encoding="utf-8")
    assert "a.txt:1:foobar" in F.grep({"pattern": pat}, _ctx(tmp_path)).output


@pytest.mark.parametrize("make", [lambda d: "./src/*.py", lambda d: f"{d}/src/*.py", lambda d: "src/*.py", lambda d: "*.py", lambda d: f"{d}/src/a.py"])
def test_glob_finds_files_with_any_form_of_the_pattern(tmp_path, make):
    os.makedirs(tmp_path / "src")
    (tmp_path / "src" / "a.py").write_text("x\n", encoding="utf-8")
    assert "a.py" in F.glob({"pattern": make(tmp_path)}, _ctx(tmp_path)).output


def test_a_new_sibling_folder_is_not_a_typo(tmp_path):
    ws = tmp_path / "projects-with-a-long-name" / "myapp-version-1"
    ws.mkdir(parents=True)
    sibling = str(ws.parent / "myapp-version-2" / "main.py")
    assert F._fix_typo(sibling, str(ws)) == sibling


def test_a_typo_in_the_folder_above_the_workspace_is_still_fixed(tmp_path):
    ws = tmp_path / "bwau-abcdefghijkl" / "proj"
    ws.mkdir(parents=True)
    typed = str(tmp_path / "bwau-abcdef-ghijkl" / "proj" / "x.py")
    assert F._fix_typo(typed, str(ws)) == str(ws / "x.py")


def test_saved_full_output_is_masked(tmp_path):
    from laps.engine.tools import clip
    body, cut = clip(_ctx(tmp_path), HERE + PW + "\n" + "x" * 100_000)
    saved = next((tmp_path / "tool-output").iterdir()).read_text(encoding="utf-8")
    assert cut and PW not in saved


# ── 推論機の線 ─────────────────────────────
class _Slow(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_error(_Slow.props)

    def do_POST(self):
        time.sleep(8)

    def log_message(self, *a):
        pass


@pytest.fixture
def slow():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    srv.daemon_threads = True
    _Slow.props = 404
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def test_stop_works_while_waiting_for_the_inference_server(slow):
    llm = LLM(slow, "m")
    threading.Timer(0.3, llm.abort).start()
    t0 = time.monotonic()
    with pytest.raises(Cancelled):
        llm.stream(llm.payload([{"role": "user", "content": "x"}], None))
    assert time.monotonic() - t0 < 3


def test_a_temporary_props_failure_keeps_what_we_knew(slow):
    from laps.engine.llm import Info
    llm = LLM(slow, "m")
    llm._info = Info(model_path="x.gguf", n_ctx=1000, llama=True)
    _Slow.props = 503
    got = llm.info(fresh=True)
    assert got.llama and got.n_ctx == 1000
    _Slow.props = 404
    assert not llm.info(fresh=True).llama


# ── 前置き ─────────────────────────────────
def test_a_short_english_message_does_not_change_the_system_prompt(tmp_path, monkeypatch):
    from laps.engine.server import Server, Spec
    from laps.engine.wire import Out
    import io
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    srv = Server(Out(io.StringIO()))
    t = Transcript()
    spec = Spec(id="r", prompt="ファイルを整理してください", workspace=str(tmp_path))
    first = srv._system(t, spec, "bash", True)
    t.append({"role": "user", "content": spec.prompt})
    assert "Japanese" in first
    assert srv._system(t, Spec(id="r", prompt="ok", workspace=str(tmp_path)), "bash", True) == first


def test_stop_and_messages_during_setup_reach_the_run(tmp_path):
    import io
    from laps.engine.server import Server, Spec
    from laps.engine.wire import Out
    srv = Server(Out(io.StringIO()))
    srv.current = Spec(id="r", prompt="x", workspace=str(tmp_path))
    srv._accept_inject({"type": "inject", "text": "まって"})
    srv._accept_cancel({"type": "cancel", "id": "r"})
    assert srv._early_cancel and srv._early_inject == [("まって", False)]


# ── 中継 ───────────────────────────────────
def _relay(tmp_path, **kw):
    from laps import relay as R
    return R, R.Relay(token=None, device_key=None, state_dir=tmp_path, auth_mode="optional", **kw)


class _Req:
    def __init__(self, **headers):
        self.headers = {k.replace("_", "-"): v for k, v in headers.items()}


def test_other_sites_cannot_open_lines_to_the_relay(tmp_path):
    R, _ = _relay(tmp_path)
    assert R.Relay._foreign_origin(_Req(Origin="http://evil.example", Host="192.0.2.160:18400"))
    assert R.Relay._foreign_origin(_Req(Origin="null", Host="192.0.2.160:18400"))
    assert not R.Relay._foreign_origin(_Req(Origin="https://192.0.2.160:18400", Host="192.0.2.160:18400"))
    assert not R.Relay._foreign_origin(_Req(Host="192.0.2.160:18400"))


def test_signup_is_for_the_first_account_only_by_default(tmp_path):
    _, rel = _relay(tmp_path, signup="first")
    assert rel._signup_state() == "open"
    rel.accounts.create("a@example.com", "password-a")
    assert rel._signup_state() == "closed"


def test_a_401_from_another_relay_keeps_the_real_sign_in(tmp_path, monkeypatch):
    from laps import relayauth as A
    f = tmp_path / "auth.json"
    monkeypatch.setenv("LAPS_AUTH_FILE", str(f))
    monkeypatch.delenv("LAPS_RELAY_TOKEN", raising=False)
    monkeypatch.setattr(A, "rpc", lambda *a, **k: (_ for _ in ()).throw(A.AuthError("unauthorized", "x")))
    A.save({"token": "t", "relay": "ws://192.0.2.160:18400"})
    A.status("ws://127.0.0.1:9")
    assert A.json_call({"op": "sessions"}, "ws://127.0.0.1:9")["error"] == "unauthorized"
    assert f.exists()
    monkeypatch.setenv("LAPS_RELAY_TOKEN", "old")
    A.status("ws://192.0.2.160:18400")
    assert f.exists()
    monkeypatch.delenv("LAPS_RELAY_TOKEN")
    A.status("ws://192.0.2.160:18400")
    assert not f.exists()
