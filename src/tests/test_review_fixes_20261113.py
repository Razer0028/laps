"""★2026-11-13 Monitor の数え直しと全体のレビューで直した所（オーナー「数えてみて。レビューも」「全体をレビュー…合わせて修正」
「性能が良くなりそうな改善も」）。

- Monitor のエラーの判定（`error_rate=0.012` などを ⚠ にしていた）
- 見るだけの命令の判定の穴（`echo x 1>a`・`git branch -D` など）
- 眠って覗く命令への案内（1 回だけ）
- Ask の依頼を Agent の前置きと道具の一覧のまま走らせる（切り替えのたびに会話を頭から読み直していた）
- Tools の中の道具の名前を数える
"""
from __future__ import annotations

import io
import threading

from tests.test_engine_20261030 import Fake, env  # noqa: F401 — env は fixture

from laps.engine import loop as eloop
from laps.engine.server import Server, Spec
from laps.engine.session import Transcript
from laps.engine.wire import Out
from laps.engine.tools import Ctx, base_tools, shell
from laps.oc import monitor


def test_monitor_error_lines_are_not_names_or_settings():
    quiet = ["epoch 3 loss=0.41 error_rate=0.012", "Errors: 0", "Total: 120 passed, 0 failed", "failed=0 skipped=2",
             "failures: 0", "request timeout=30s", "Using timeout 600", "[INFO] no errors found", "WARN retrying (killed=0)",
             "Saved model to error_analysis.json", "step 100: skipped_errors=0", "0 errors, 0 warnings", "error: 0",
             "Error count: 0", "wrote logs/error.log", "--timeout 30"]
    loud = ["Traceback (most recent call last):", "ValueError: bad", "tests: 12 passed, 1 failed", "CUDA out of memory",
            "エラー: ファイルがありません", "error: 0x80004005", "failed=3", "Errors: 2", "Connection timed out", "read timeout",
            "Killed", "ERROR something broke", "FAILED tests/test_x.py", "npm ERR! code 1", "Segmentation fault (core dumped)",
            "fatal: not a git repository", "Permission denied", "Connection refused"]
    assert [s for s in quiet if monitor.looks_like_error(s)] == []
    assert [s for s in loud if not monitor.looks_like_error(s)] == []


def test_monitor_has_ready_does_not_take_the_lines(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("old\n")
    monitor.begin_run()
    try:
        assert monitor.monitor_tool({"file": str(log)}).ok and not monitor.has_ready()
        with log.open("a") as fh:
            fh.write("step 1 error_rate=0.5\nValueError: boom\n")
        assert monitor.has_ready() and monitor.has_ready()          # 見るだけ。渡すのは take_events
        ev = monitor.take_events()
        assert len(ev) == 1 and ev[0]["errors"] == 1 and "ValueError: boom" in ev[0]["text"] and "error_rate" in ev[0]["text"]
        assert not monitor.has_ready()
    finally:
        monitor.end_run()


def test_read_only_holes():
    looks = ["ls -la", "git log -p", "ls 2>/dev/null", "ls 2>&1 | head", "grep x f > /dev/null", "git branch", "git branch -a",
             "git remote -v", "git remote show origin", "git status && git diff", "wc -l < a", "dir 2>nul",
             'git branch --list "feat*"']
    changes = ["echo hi > out.txt", "echo x 1>a", "ls 2>err.txt", "ls &> out", "diff <(rm a) b", "git branch -D old",
               "git branch new", "git remote remove origin", "git remote add o u", "git remote set-url origin u",
               "git diff --output=x", "cat a | tee b", "ls & rm x", "sort -o a b"]
    assert [c for c in looks if not shell.read_only(c)] == []
    assert [c for c in changes if shell.read_only(c)] == []


def test_poll_hint_once(tmp_path):
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    assert shell.poll_hint("sleep 5; tail log", ctx) == ""                  # 短い待ちは言わない
    assert shell.poll_hint("sleep 30", ctx) == ""                           # ただ眠るだけは覗いていない
    assert "Monitor" in shell.poll_hint("cd x; sleep 25; tail -3 train.log", ctx)
    assert shell.poll_hint("while ! grep -q DONE log; do sleep 30; done", ctx) == ""   # 走行に 1 回だけ


def test_tools_inner_name_is_counted(env):  # noqa: F811
    a, fake, rec, ws = env([{"calls": [("Tools", {"name": "Monitor", "input": {}})]}, {"text": "見張りはありません。"}],
                           tools=base_tools("full"))
    a.run("見張りの一覧を見せてください。どうなっていますか")
    assert a.counters.get("tool_calls:Tools") == 1 and a.counters.get("tool_calls_in:Monitor") == 1


def test_monitor_with_a_background_job_id_points_to_bashoutput(env):  # noqa: F811
    a, fake, rec, ws = env([{"calls": [("Bash", {"command": "sleep 20", "run_in_background": True})]},
                            {"calls": [("Tools", {"name": "Monitor", "input": {"pid": "bg1"}})]},
                            {"calls": [("BashOutput", {"id": "bg1", "kill": True})]}, {"text": "止めました。"}],
                           tools=base_tools("full"))
    a.run("背景で動かして見張ってから止めてください。お願いします")
    said = [m["content"] for m in a.t.raw if m.get("role") == "tool"]
    assert any("BashOutput(id=\"bg1\"" in s and "is a background job of Bash" in s for s in said)


def _serve(tmp_path, monkeypatch, script):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setenv("LAPS_LIVE", "0")
    monkeypatch.setenv("LAPS_EXPLORE_FIRST", "0")
    fake = Fake(script)
    srv = Server(Out(io.StringIO()))
    return fake, srv


def test_ask_after_agent_keeps_the_prefix_and_refuses_writes(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    fake, srv = _serve(tmp_path, monkeypatch, [
        {"text": "はい、準備できています。"},                                          # Agent の依頼
        {"calls": [("Write", {"file_path": "x.txt", "content": "no"})]},               # Ask の依頼: 書こうとする
        {"text": "x.txt は書いていません。Agent モードに切り替えてください。"},
        {"text": "作りました。"}])                                                      # また Agent
    try:
        sid = Transcript().id
        common = dict(session=sid, backend=fake.url, model="north-test", workspace=str(ws))
        srv._run(Spec(id="r1", prompt="準備はできていますか。確かめてください", toolset="full", **common))
        srv._run(Spec(id="r2", prompt="x.txt を書けますか", toolset="chat", **common))
        srv._run(Spec(id="r3", prompt="続きをお願いします。進めてください", toolset="full", **common))
    finally:
        fake.close()
    b = fake.bodies
    names = [[t["function"]["name"] for t in x.get("tools") or []] for x in b]
    assert names[0] == names[1] == names[2] == names[3] and "Write" in names[1]        # 道具の一覧は変わらない
    systems = [x["messages"][0]["content"] for x in b]
    assert len(set(systems)) == 1 and systems[0].startswith("You are L.A.P.S")       # 前置きも変わらない
    # Ask の依頼の後ろに一言が付き、書く道具は走らない
    assert eloop.ASK_NOTE in b[1]["messages"][-1]["content"]
    assert not (ws / "x.txt").exists()
    refused = [m["content"] for m in b[2]["messages"] if m["role"] == "tool"]
    assert refused and "Not run in Ask mode: Write" in refused[-1]
    # 前の依頼までの並びは、次の要求の頭と同じ（控えがそのまま使える）
    assert b[1]["messages"][:len(b[0]["messages"])] == b[0]["messages"]


def test_ask_from_the_start_uses_the_agent_prefix(tmp_path, monkeypatch):
    """Ask で始めた会話も Agent の前置きで読ませる（後で Agent に切り替えても読み直さない・2026-11-14）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    fake, srv = _serve(tmp_path, monkeypatch, [{"text": "はい。"}, {"text": "作りました。"}])
    try:
        sid = Transcript().id
        common = dict(session=sid, backend=fake.url, model="north-test", workspace=str(ws))
        srv._run(Spec(id="r1", prompt="これは何ですか", toolset="chat", **common))
        srv._run(Spec(id="r2", prompt="では作ってください。お願いします", toolset="full", **common))
    finally:
        fake.close()
    b = fake.bodies
    assert "Write" in [t["function"]["name"] for t in b[0]["tools"]] and b[0]["tools"] == b[1]["tools"]
    assert eloop.ASK_NOTE in str(b[0]["messages"])
    assert b[0]["messages"][0] == b[1]["messages"][0]
    assert b[1]["messages"][:len(b[0]["messages"])] == b[0]["messages"]


def test_app_guidance_differs_between_ask_and_agent(tmp_path, monkeypatch):
    """アプリは Umbra の 2 行を Agent にだけ足す。指示が違っても前置きを作り直さない（2026-11-14 実機で 20 万トークンの読み直し）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    fake, srv = _serve(tmp_path, monkeypatch, [{"text": "はい。"}, {"text": "答えです。"}, {"text": "続けました。"}])
    try:
        sid = Transcript().id
        common = dict(session=sid, backend=fake.url, model="north-test", workspace=str(ws))
        g = "[Environment]\nOS: Windows\nThere is no limit on the length of your output."
        srv._run(Spec(id="r1", prompt="準備はできていますか。確かめてください", toolset="full", guidance=g + "\nUmbra | cite [1].", **common))
        srv._run(Spec(id="r2", prompt="これは何ですか", toolset="chat", guidance=g, **common))
        srv._run(Spec(id="r3", prompt="続きをお願いします。進めてください", toolset="full", guidance=g + "\nUmbra | cite [1].", **common))
    finally:
        fake.close()
    b = fake.bodies
    assert len({x["messages"][0]["content"] for x in b}) == 1
    assert b[0]["tools"] == b[1]["tools"] == b[2]["tools"]
    assert b[1]["messages"][:len(b[0]["messages"])] == b[0]["messages"]
    assert b[2]["messages"][:len(b[1]["messages"])] == b[1]["messages"]


def test_conversation_started_with_the_short_ask_prefix_keeps_it(tmp_path, monkeypatch):
    """前の版が Ask の短い前置きで始めた会話は、Ask のままなら作り直さない。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    fake, srv = _serve(tmp_path, monkeypatch, [{"text": "はい。"}, {"text": "答えです。"}])
    try:
        sid = Transcript().id
        common = dict(session=sid, backend=fake.url, model="north-test", workspace=str(ws))
        monkeypatch.setenv("LAPS_ASK_KEEP_PREFIX", "0")
        srv._run(Spec(id="r1", prompt="これは何ですか", toolset="chat", **common))
        monkeypatch.delenv("LAPS_ASK_KEEP_PREFIX")
        srv._run(Spec(id="r2", prompt="もう少し教えてください", toolset="chat", guidance="x", **common))
    finally:
        fake.close()
    b = fake.bodies
    assert "Write" not in [t["function"]["name"] for t in b[1]["tools"]]
    assert b[0]["messages"][0] == b[1]["messages"][0]


def test_ask_keep_prefix_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_ASK_KEEP_PREFIX", "0")
    ws = tmp_path / "ws"
    ws.mkdir()
    fake, srv = _serve(tmp_path, monkeypatch, [{"text": "はい。"}, {"text": "答えです。"}])
    try:
        sid = Transcript().id
        common = dict(session=sid, backend=fake.url, model="north-test", workspace=str(ws))
        srv._run(Spec(id="r1", prompt="準備はできていますか。確かめてください", toolset="full", **common))
        srv._run(Spec(id="r2", prompt="これは何ですか", toolset="chat", **common))
    finally:
        fake.close()
    assert "Write" not in [t["function"]["name"] for t in fake.bodies[1]["tools"]]



# ── 中継（relay.py）──────────────────────────────────────────────

def test_relay_index_items_cannot_break_the_sort(tmp_path):
    from laps import relay as R
    rel = R.Relay(token=None, device_key=None, state_dir=tmp_path, auth_mode="optional")
    bad = [{"sid": "a", "updated": None}, {"sid": "b", "updated": "abc"}, {"sid": "c", "updated": 1731400000},
           {"sid": "d", "updated": float("nan")}, {"sid": "e", "updated": [1]}, {"sid": "f", "updated": "1731400001.5",
                                                                                 "status": "x" * 500}]
    got = [rel._clean_item(x) for x in bad]
    assert [g["updated"] for g in got] == [0, 0, 1731400000, 0, 0, 1731400001.5]
    assert len(got[5]["status"]) == 64
    sorted(got, key=lambda r: r.get("updated", 0))                       # 型が混ざらない＝落ちない


def test_relay_login_attempts_are_counted_before_the_check(tmp_path):
    from laps import relay as R
    rel = R.Relay(token=None, device_key=None, state_dir=tmp_path, auth_mode="optional")
    rel._hit("ip:1.2.3.4", "mail:a@b")
    assert rel.fails["ip:1.2.3.4"] and rel.fails["mail:a@b"]
    rel._unhit("ip:1.2.3.4", "mail:a@b")                                  # 通ったら戻す
    assert "ip:1.2.3.4" not in rel.fails and "mail:a@b" not in rel.fails
    for _ in range(R.FAIL_MAX):
        rel._hit("ip:9.9.9.9")
    assert rel._limited("ip:9.9.9.9")


def test_relay_ignores_tokens_when_accounts_are_off(tmp_path):
    from laps import relay as R

    class Req:
        headers = {"X-Blackwall-Token": "bw_something"}
    rel = R.Relay(token=None, device_key=None, state_dir=tmp_path, auth_mode="off")
    assert rel._identify(Req(), {}) == (None, False)                     # 無効と言わない（客が手元の認証情報を捨てる）


# ── 0.1.669: 残りの指摘（オーナー「直していいです」）──────────────────────

def test_relay_data_is_set_aside_when_the_owner_changes(tmp_path):
    """サインアウトで持ち主のいなくなった機械に、別のアカウントが同じ名札で繋いでも、前の持ち主の目録と写しは見えない。"""
    from laps import relay as R
    rel = R.Relay(token=None, device_key=None, state_dir=tmp_path, auth_mode="optional")

    def fill(seq=1):
        rel.seen["m1"] = {"name": "m1", "owner": "a1"}
        rel.index["m1"] = {"s1": {"sid": "s1", "title": "秘密", "updated": 1}}
        assert rel.journal.append("m1", "s1", '{"seq": %d, "type": "token"}\n' % seq, updated=1) == 1
        rel.seen["m1"].pop("owner")
        rel.seen["m1"]["was_owner"] = "a1"                             # サインアウトした後の形
    fill()
    rel._remember("m1", {"name": "m1"}, owner="a1")                    # 同じアカウントが戻った＝そのまま
    assert rel.index["m1"]["s1"]["title"] == "秘密" and rel.journal.has_copy("m1", "s1")
    assert "was_owner" not in rel.seen["m1"]
    fill(2)
    rel._remember("m1", {"name": "m1"}, owner="a2")                    # 別のアカウント＝避ける（消さない）
    assert "m1" not in rel.index and not rel.journal.has_copy("m1", "s1")
    assert rel.seen["m1"]["owner"] == "a2" and "was_owner" not in rel.seen["m1"]
    aside = [p.name for p in rel.journal.root.iterdir() if p.name.startswith(".aside-m1-")]
    assert len(aside) == 1 and (rel.journal.root / aside[0] / "s1.jsonl").is_file()
    assert rel.journal.path(aside[0], "s1") is None                    # 避けた置き場は名札としては引けない


def test_relay_journal_has_a_cap_on_new_conversations(tmp_path, monkeypatch):
    from laps import relay as R
    monkeypatch.setattr(R, "JOURNAL_MAX_CONVS", 2)
    j = R.Journal(tmp_path / "journal")
    line = '{"seq": 1}\n'
    assert j.append("m", "a", line) == 1 and j.append("m", "b", line) == 1
    assert j.append("m", "c", line) == 0 and "c" not in j.meta["m"]    # 新しい会話は増やさない
    assert j.append("m", "a", '{"seq": 2}\n') == 1                     # 持っている会話は続けて足せる


def test_relay_pairing_for_a_deleted_account_gives_no_token(tmp_path):
    import time as _t

    from laps import relay as R
    rel = R.Relay(token=None, device_key=None, state_dir=tmp_path, auth_mode="optional")
    rel.pairing["c1"] = {"user": "ABCDEFGH", "device": {}, "exp": _t.time() + 60, "aid": "a_gone"}
    before = len(rel.accounts.tokens)
    r = rel._op_pair_poll({"code": "c1"})
    assert r["ok"] is False and len(rel.accounts.tokens) == before


def test_gateway_cap_counts_sessions_that_are_still_starting(tmp_path, monkeypatch):
    import asyncio

    from laps import gateway as gwmod

    class Ses:
        def __init__(self, sid):
            self.sid, self.client, self.reaper = sid, None, None
            self.proc = type("P", (), {"returncode": None})()
    gw = gwmod.Gateway(token=None, workspace=tmp_path / "ws", home=tmp_path / "home", max_conn=2,
                       backend="http://127.0.0.1:1", model="m", policy="trusted", toolset="default", grace=1.0)

    async def fake_spawn(sid):
        await asyncio.sleep(0.1)                 # 本物は subprocess の起動で待つ（この間は sessions_live に無い）
        gw.sessions_live[sid] = Ses(sid)
        return gw.sessions_live[sid]
    monkeypatch.setattr(gw, "_spawn", fake_spawn)

    async def go():
        return await asyncio.gather(*(gw._get_or_spawn(f"s{i}") for i in range(5)), return_exceptions=True)
    res = asyncio.run(go())
    assert sum(1 for r in res if isinstance(r, gwmod.GatewayBusy)) == 3 and len(gw.sessions_live) == 2
    assert gw._spawning == 0


def test_desktop_launch_of_a_local_page_points_to_serve(tmp_path):
    """★0.1.667 の誤りの直し: Browser が開けるのは http と https だけ。手元の .html は Serve で配ってから開く。"""
    from laps.engine.tools import ext
    from laps.oc import browsercdp
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())
    for target in ("file:///D:/x/index.html", r"D:\work\page.html", "/home/u/site/index.html"):
        r = ext._web_redirect({"action": "launch", "command": target}, ctx)
        assert r is not None and "Serve" in r.output and "http and https" in r.output, target
        assert ext._web_redirect({"action": "launch", "command": target}, ctx) is None      # 2 回目は開く
    r = ext._web_redirect({"action": "launch", "command": "https://example.com/a"}, ctx)
    assert r is not None and "Browser" in r.output and "Serve" not in r.output
    assert ext._web_redirect({"action": "launch", "command": "notepad.exe"}, ctx) is None
    assert "local .html files)" not in ext.LISTED["Browser"]
    try:                                          # 案内の前提: Browser は file: を断る
        browsercdp.to_url("file:///D:/x/index.html")
        raise AssertionError("file: が通った")
    except browsercdp.CdpError:
        pass


def test_relay_set_aside_keeps_the_deleted_marks(tmp_path):
    """機械を一覧から外して繋ぎ直しても、アプリから消した会話は機械から戻らない。"""
    from laps import relay as R
    j = R.Journal(tmp_path / "journal")
    assert j.append("m", "a", '{"seq": 1}\n') == 1
    j.delete("m", "a")
    assert j.is_gone("m", "a")
    j.set_aside("m")
    assert j.is_gone("m", "a") and j.append("m", "a", '{"seq": 1}\n') == 0
