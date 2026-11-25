"""端末クライアントの入口（laps/term/app.py）の試験。

ServeClient は偽物（事象列を返すスタブ）に差し替え、run_turn の流れだけを見る:
session の引き継ぎ・ask→answer・_exit→再起動 1 回・Ctrl-C→cancel・終了コード。
本物の serve は test_term_client.py と煙試験（scripts/check_term_smoke.sh）が起動する。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
from rich.console import Console

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps.term import app  # noqa: E402
from laps.term.app import AppContext, exit_code, record_event, run_turn, wave_allowed  # noqa: E402
from laps.term.render import Renderer  # noqa: E402


# --------------------------------------------------------------------------
# 偽の ServeClient
# --------------------------------------------------------------------------

class Interrupt:
    """事象列の中に置くと、その位置で KeyboardInterrupt（Ctrl-C）が起きる。"""


class FakeClient:
    """send ごとに 1 本の事象列を返す。events() は列を順に yield し、尽きたら None を 1 つ。"""

    def __init__(self, scripts: list[list] | None = None, *, restart_fail: bool = False) -> None:
        self.scripts = list(scripts or [])
        self.restart_fail = restart_fail
        self.workspace = "/tmp/ws"
        self.backend = "http://127.0.0.1:9/v1"
        self.model = "test-model"
        self.policy = "workspace"
        self.ethics = "standard"
        self.home = None
        self.tools = "full"
        self.max_iters = 0
        self.python = sys.executable
        self.debug_log = None
        self.extra_env = {}
        self.module = "laps.serve"
        self.status = {"kind": "ready", "tools": ["Bash", "Read"], "model": "test-model",
                       "policy": "workspace", "toolset": "full", "native_tools": True}
        self.alive = True
        self.pid = 4242
        self.sent: list[dict] = []
        self.answers: list[tuple[str, str]] = []
        self.cancels: list[str] = []
        self.restarts: list[dict] = []
        self.quits = 0
        self._seq = 0
        self._pending: list = []
        self.debug_notes: list[str] = []

    @property
    def settings(self) -> dict:
        return {k: getattr(self, k) for k in (
            "workspace", "backend", "model", "policy", "ethics", "home",
            "tools", "max_iters", "python", "debug_log", "extra_env", "module")}

    def prepare_parts(self) -> None:          # 本物は裏で部品をそろえる（試験では何もしない）
        pass

    def start(self, timeout: float = 20.0) -> dict:
        self.alive = True
        return self.status

    def send(self, messages, *, rid=None, session=None, **_kw) -> str:
        self._seq += 1
        rid = rid or f"r{self._seq}"
        self.sent.append({"rid": rid, "messages": list(messages), "session": session})
        script = self.scripts.pop(0) if self.scripts else [{"type": "done", "outcome": "accepted", "session": "s-auto"}]
        # id を埋める（本物は send の id を全事象に付ける）
        self._pending = [dict(e, id=rid) if isinstance(e, dict) and "id" not in e else e for e in script]
        return rid

    def events(self, timeout=None):
        while self._pending:
            ev = self._pending.pop(0)
            if isinstance(ev, Interrupt):
                raise KeyboardInterrupt
            yield ev
            if isinstance(ev, dict) and ev.get("type") == "_exit":
                return
        if timeout is not None:
            yield None

    def cancel(self, rid: str) -> None:
        self.cancels.append(rid)
        # 本物は cancel の後に done(cancelled) が来る。列に無ければ足す。
        if not any(isinstance(e, dict) and e.get("type") == "done" for e in self._pending):
            self._pending.append({"type": "done", "id": rid, "outcome": "cancelled", "session": "s-cancel"})

    def answer(self, ask_id: str, text: str) -> None:
        self.answers.append((ask_id, text))

    def debug_note(self, text: str) -> None:
        self.debug_notes.append(text)

    def ping(self, timeout: float = 5.0) -> dict:
        return self.status

    def quit(self, timeout: float = 5.0) -> None:
        self.quits += 1
        self.alive = False

    def restart(self, timeout: float = 20.0, **overrides):
        if self.restart_fail:
            raise RuntimeError("起動できない（試験）")
        self.restarts.append(dict(overrides))
        for k, v in overrides.items():
            setattr(self, k, v)
        self.alive = True
        return self.status


def _console() -> Console:
    return Console(record=True, width=80, force_terminal=False, file=io.StringIO())


def _ctx(client: FakeClient, tmp_path: Path, **kw) -> AppContext:
    c = _console()
    return AppContext(console=c, client=client, renderer=Renderer(c), cfg={"telemetry": ""},
                      install_id_path=tmp_path / "install_id", **kw)


def _text(ctx: AppContext) -> str:
    return ctx.console.export_text(clear=False)


# --------------------------------------------------------------------------
# 小さな判定
# --------------------------------------------------------------------------

def test_exit_code_mapping():
    assert exit_code("accepted") == 0
    assert exit_code("accepted_after_pushback") == 0
    assert exit_code("cancelled") == 130
    assert exit_code("backend_error") == 1
    assert exit_code("crashed") == 1
    assert exit_code(None) == 1


def test_wave_not_allowed_on_non_tty():
    assert wave_allowed(Console(file=io.StringIO(), force_terminal=False)) is False
    assert app.make_wave_factory(Console(file=io.StringIO(), force_terminal=False), False) is None


def test_wave_allowed_on_tty(monkeypatch, tmp_path):
    """★設定を隔離する（2026-09-01・波班の指摘）。

    `make_wave_factory` は `wave=off` で **None** を返すようになった。この試験は
    実機の `~/.config/blackwall/config.toml` を読んでいたので、そこに `wave = "off"`
    と書いた日から**この試験だけが落ちる**（原因が実装ではなく利用者の設定になる）。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LAPS_WAVE", raising=False)
    c = Console(file=io.StringIO(), force_terminal=True, width=60)
    assert wave_allowed(c) is True
    f = app.make_wave_factory(c, True)
    assert f is not None
    w = f()
    assert w.width == 60 and w.calm is True


def test_record_event_merges_consecutive_tokens(tmp_path):
    ctx = _ctx(FakeClient(), tmp_path)
    record_event(ctx, {"type": "token", "id": "r1", "text": "ab"})
    record_event(ctx, {"type": "token", "id": "r1", "text": "cd"})
    record_event(ctx, {"type": "tool_call", "id": "r1", "name": "Bash"})
    record_event(ctx, {"type": "token", "id": "r1", "text": "ef"})
    kinds = [(e["type"], e.get("text")) for e in ctx.events]
    assert kinds == [("token", "abcd"), ("tool_call", None), ("token", "ef")]


# --------------------------------------------------------------------------
# run_turn
# --------------------------------------------------------------------------

def test_first_send_has_no_session_and_done_session_is_kept(tmp_path):
    cl = FakeClient([
        [{"type": "token", "text": "hi"}, {"type": "done", "outcome": "accepted", "session": "sess-1",
                                           "secs": 2.0, "tool_calls": {"Bash": 2}, "total_tokens": 30}],
        [{"type": "done", "outcome": "accepted", "session": "sess-1", "secs": 1.0}],
    ])
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "最初")
    assert res["outcome"] == "accepted"
    assert ctx.session == "sess-1"
    assert cl.sent[0]["session"] is None
    assert cl.sent[0]["messages"] == [{"role": "user", "content": "最初"}]
    run_turn(ctx, "続き")
    # 2 回目は session 付きで、利用者発言 1 件だけ
    assert cl.sent[1]["session"] == "sess-1"
    assert cl.sent[1]["messages"] == [{"role": "user", "content": "続き"}]
    assert ctx.stats["turns"] == 2 and ctx.stats["tools"] == 2
    assert ctx.stats["secs"] == pytest.approx(3.0) and ctx.stats["tokens"] == 30
    assert ctx.running_rid is None
    assert ctx.last_outcome == "accepted" and ctx.last_input == "続き"
    out = _text(ctx)
    assert "> 最初" in out and "hi" in out and "done" in out


def test_resumed_session_is_sent_from_the_start(tmp_path):
    cl = FakeClient([[{"type": "done", "outcome": "accepted", "session": "old"}]])
    ctx = _ctx(cl, tmp_path, session="old")
    run_turn(ctx, "続き")
    assert cl.sent[0]["session"] == "old"


def test_ask_is_answered_by_number_and_turn_continues(tmp_path):
    cl = FakeClient([[
        {"type": "ask", "ask": "a1", "question": "どっち？", "options": ["りんご", "みかん"]},
        {"type": "token", "text": "了解"},
        {"type": "done", "outcome": "accepted", "session": "s1"},
    ]])
    ctx = _ctx(cl, tmp_path)
    ctx.ask_fn = lambda prompt: "2"
    res = run_turn(ctx, "選んで")
    assert cl.answers == [("a1", "みかん")]
    assert res["outcome"] == "accepted"
    out = _text(ctx)
    assert "どっち？" in out and "1. りんご" in out and "了解" in out


def test_ask_free_text_and_empty(tmp_path):
    cl = FakeClient([
        [{"type": "ask", "ask": "a1", "question": "q", "options": []},
         {"type": "done", "outcome": "accepted", "session": "s1"}],
        [{"type": "ask", "ask": "a2", "question": "q2", "options": ["x"]},
         {"type": "done", "outcome": "accepted", "session": "s1"}],
    ])
    ctx = _ctx(cl, tmp_path)
    ctx.ask_fn = lambda prompt: "自由に"
    run_turn(ctx, "1")
    ctx.ask_fn = lambda prompt: ""
    run_turn(ctx, "2")
    assert cl.answers == [("a1", "自由に"), ("a2", "")]


def test_ask_without_tty_answers_empty_without_blocking(tmp_path, monkeypatch):
    cl = FakeClient([[{"type": "ask", "ask": "a1", "question": "q", "options": ["x"]},
                      {"type": "done", "outcome": "accepted", "session": "s1"}]])
    ctx = _ctx(cl, tmp_path)
    ctx.ask_fn = None
    monkeypatch.setattr("sys.stdin", io.StringIO(""))   # isatty() False
    run_turn(ctx, "x")
    assert cl.answers == [("a1", "")]


def test_exit_event_restarts_once(tmp_path):
    cl = FakeClient([
        [{"type": "token", "text": "途中"}, {"type": "_exit", "code": 1, "pid": 1}],
        [{"type": "_exit", "code": 1, "pid": 2}],
    ])
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "a")
    assert res["outcome"] == "crashed"
    assert len(cl.restarts) == 1
    out = _text(ctx)
    assert "エンジンが終了しました" in out and "立て直しました" in out
    # 2 度目は再起動しない（1 回だけ）
    res = run_turn(ctx, "b")
    assert res["outcome"] == "crashed"
    assert len(cl.restarts) == 1
    assert "1 回だけ" in _text(ctx)
    assert ctx.running_rid is None


def test_exit_event_restart_failure_is_reported(tmp_path):
    cl = FakeClient([[{"type": "_exit", "code": 2, "pid": 1}]], restart_fail=True)
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "a")
    assert res["outcome"] == "crashed"
    assert "再起動できません" in _text(ctx)
    assert ctx.restarts == 0


def test_ctrl_c_once_cancels_and_waits_for_done(tmp_path):
    cl = FakeClient([[{"type": "token", "text": "はじめ"}, Interrupt(),
                      {"type": "token", "text": "…"}]])   # cancel() が done(cancelled) を足す
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "長い仕事")
    assert cl.cancels == ["r1"]
    assert res["outcome"] == "cancelled"
    assert ctx.exit_requested is False
    assert ctx.session == "s-cancel"
    assert "中断を要求" in _text(ctx)


def test_ctrl_c_twice_exits(tmp_path):
    cl = FakeClient([[{"type": "token", "text": "a"}, Interrupt(), Interrupt(),
                      {"type": "done", "outcome": "accepted", "session": "s"}]])
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "x")
    assert cl.cancels == ["r1"]
    assert res["outcome"] == "cancelled"
    assert ctx.exit_requested is True
    assert ctx.running_rid is None


def test_send_failure_is_reported_not_raised(tmp_path):
    cl = FakeClient()

    def boom(*a, **k):
        raise RuntimeError("子プロセスが動いていない")
    cl.send = boom
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "x")
    assert res["outcome"] == "send_failed"
    assert "送れません" in _text(ctx)


def test_timeout_none_from_events_is_ignored(tmp_path):
    class Slow(FakeClient):
        def events(self, timeout=None):
            yield None          # 1 回目: 何も来ない
            yield from super().events(timeout)
    cl = Slow([[{"type": "done", "outcome": "accepted", "session": "s"}]])
    ctx = _ctx(cl, tmp_path)
    assert run_turn(ctx, "x")["outcome"] == "accepted"


# --------------------------------------------------------------------------
# main（引数・非対話）
# --------------------------------------------------------------------------

def test_version_flag(capsys):
    assert app.main(["--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("L.A.P.S ") and len(out.split()) == 2      # 2026-11-14 正式名称（形は「名前 版」のまま）


def test_parser_accepts_design_flags():
    p = app.build_parser()
    a = p.parse_args(["-p", "x", "--json", "--backend", "http://h/v1", "--model", "m",
                      "--workspace", "/tmp", "--policy", "readonly", "--ethics", "raw",
                      "--resume", "id1", "--calm", "--debug", "/tmp/d.log", "--config", "/tmp/c.toml"])
    assert a.prompt == "x" and a.json and a.backend == "http://h/v1" and a.model == "m"
    assert a.policy == "readonly" and a.ethics == "raw" and a.resume == "id1" and a.calm
    with pytest.raises(SystemExit):
        p.parse_args(["--policy", "god"])


@pytest.fixture
def fake_serve(monkeypatch, tmp_path):
    """app.ServeClient を偽物にし、作られたインスタンスを返す。設定ファイルは tmp へ。"""
    made: list[FakeClient] = []
    scripts: list[list] = []

    def factory(**kw):
        cl = FakeClient([list(s) for s in scripts])
        for k, v in kw.items():
            setattr(cl, k, v)
        made.append(cl)
        return cl
    monkeypatch.setattr(app, "ServeClient", factory)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    for var in ("LAPS_BACKEND", "LAPS_MODEL", "LAPS_POLICY", "LAPS_ETHICS",
                "LAPS_TOOLSET", "LAPS_MAX_ITERS", "LAPS_TELEMETRY"):
        monkeypatch.delenv(var, raising=False)
    return {"made": made, "scripts": scripts}


def test_main_json_prints_events_and_exit_code(fake_serve, capsys, tmp_path):
    fake_serve["scripts"].append([{"type": "token", "text": "a"},
                                  {"type": "done", "outcome": "backend_error", "session": "s"}])
    code = app.main(["-p", "echo ok", "--json", "--workspace", str(tmp_path / "ws"),
                     "--backend", "http://127.0.0.1:9/v1"])
    assert code == 1
    out = capsys.readouterr().out
    lines = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert [e["type"] for e in lines] == ["token", "done"]
    cl = fake_serve["made"][0]
    assert cl.quits == 1
    assert cl.backend == "http://127.0.0.1:9/v1"
    assert cl.workspace == str((tmp_path / "ws").resolve())
    # 初回起動で設定ファイルが作られる（既定 = P40）
    cfg = tmp_path / "xdg" / "blackwall" / "config.toml"
    assert cfg.is_file() and "192.0.2.160:8741" in cfg.read_text(encoding="utf-8")


def test_main_prompt_renders_and_returns_zero_on_accepted(fake_serve, capsys):
    fake_serve["scripts"].append([{"type": "token", "text": "答え"},
                                  {"type": "done", "outcome": "accepted", "session": "s"}])
    code = app.main(["-p", "1+1", "--backend", "http://127.0.0.1:9/v1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "答え" in out and '"type"' not in out


def test_main_json_answers_ask_with_empty(fake_serve, capsys):
    fake_serve["scripts"].append([{"type": "ask", "ask": "a1", "question": "q", "options": ["x"]},
                                  {"type": "done", "outcome": "accepted", "session": "s"}])
    code = app.main(["-p", "x", "--json", "--backend", "http://127.0.0.1:9/v1"])
    assert code == 0
    cl = fake_serve["made"][0]
    assert cl.answers == [("a1", "")]
    types = [json.loads(l)["type"] for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert types == ["ask", "done"]


def test_main_resume_is_passed_to_first_send(fake_serve, capsys):
    fake_serve["scripts"].append([{"type": "done", "outcome": "accepted", "session": "keep"}])
    app.main(["-p", "x", "--resume", "keep", "--backend", "http://127.0.0.1:9/v1"])
    assert fake_serve["made"][0].sent[0]["session"] == "keep"


def test_main_start_failure_prints_red_and_quits(fake_serve, capsys, tmp_path, monkeypatch):
    def bad_start(self, timeout=20.0):
        raise RuntimeError("ready が来ない")
    monkeypatch.setattr(FakeClient, "start", bad_start)
    dbg = tmp_path / "dbg.log"
    code = app.main(["-p", "x", "--debug", str(dbg), "--backend", "http://127.0.0.1:9/v1"])
    assert code == 1
    assert "ready が来ない" in capsys.readouterr().out
    assert fake_serve["made"][0].quits == 1
    assert "RuntimeError" in dbg.read_text(encoding="utf-8")


def test_repl_over_pipe_dispatches_slash_and_send(fake_serve, capsys, monkeypatch):
    fake_serve["scripts"].append([{"type": "token", "text": "返事"},
                                  {"type": "done", "outcome": "accepted", "session": "s"}])
    monkeypatch.setattr("sys.stdin", io.StringIO("/model\nこんにちは\n/exit\n"))
    code = app.main(["--backend", "http://127.0.0.1:9/v1", "--model", "mm"])
    assert code == 0
    out = capsys.readouterr().out
    assert "L.A.P.S" in out and "/help" in out
    assert "mm" in out and "返事" in out
    cl = fake_serve["made"][0]
    assert [s["messages"][0]["content"] for s in cl.sent] == ["こんにちは"]
    assert cl.quits == 1


# --------------------------------------------------------------------------
# 走行中のキー（Ctrl-C / Esc を子に届けずに cancel にする）
# --------------------------------------------------------------------------

@pytest.fixture
def pty_pair():
    import pty
    import termios
    master, slave = pty.openpty()
    saved = termios.tcgetattr(slave)
    yield master, slave
    import os
    os.close(master)
    os.close(slave)
    del saved


def _wait(cond, timeout=2.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_keywatch_reads_ctrl_c_and_esc_and_restores_termios(pty_pair):
    import os
    import termios
    master, slave = pty_pair
    before = termios.tcgetattr(slave)
    kw = app._KeyWatch(slave)
    kw.start()
    try:
        now = termios.tcgetattr(slave)
        assert not (now[3] & termios.ISIG)          # 端末は SIGINT を作らない
        assert not (now[3] & termios.ECHO)
        os.write(master, b"abc\x03")                 # 文字は捨て、Ctrl-C だけ数える
        assert _wait(lambda: kw.take() == 1)
        os.write(master, b"\x1b")                    # Esc 単独
        assert _wait(lambda: kw.take() == 1)
        os.write(master, b"\x1b[A")                  # 矢印（エスケープ列）は中断ではない
        import time
        time.sleep(0.3)
        assert kw.take() == 0
    finally:
        kw.stop()
    after = termios.tcgetattr(slave)
    assert after[3] == before[3] and after[6] == before[6]   # 戻っている
    assert kw.take() == 0


def test_keywatch_without_tty_is_noop():
    kw = app._KeyWatch(None)
    kw.start()
    assert kw.take() == 0
    kw.stop()


def test_run_turn_polls_keywatch_and_cancels(tmp_path, pty_pair, monkeypatch):
    import os
    master, slave = pty_pair
    monkeypatch.setattr(app, "_tty_fd", lambda ctx: slave)

    class Slow(FakeClient):
        """cancel が来るまで None（timeout）を出し続ける。"""
        def events(self, timeout=None):
            import time
            if not self.cancels:
                time.sleep(0.05)
                yield None
                return
            yield from super().events(timeout)
    cl = Slow([[{"type": "token", "text": "working"}]])
    ctx = _ctx(cl, tmp_path)
    import threading

    def press():
        _wait(lambda: ctx.running_rid is not None)
        os.write(master, b"\x03")
    threading.Thread(target=press, daemon=True).start()
    res = run_turn(ctx, "長い")
    assert cl.cancels == ["r1"]
    assert res["outcome"] == "cancelled"
    assert ctx.exit_requested is False
    assert "中断を要求" in _text(ctx)


# --------------------------------------------------------------------------
# ask の間はターンを閉じない（検証で見つけた見た目の傷: 質問の前に赤い `done  no done`）
# --------------------------------------------------------------------------

_ASK_SCRIPT = [
    {"type": "token", "text": "聞きます。"},
    {"type": "tool_call", "name": "AskUserQuestion", "input": {"question": "色は？"}, "call": "c1"},
    {"type": "ask", "ask": "a1", "question": "色は？", "options": ["赤", "青"]},
    {"type": "tool_result", "name": "AskUserQuestion", "call": "c1", "ok": True, "result": "赤",
     "summary": {"head": "赤", "tail": "", "n": 1}},
    {"type": "token", "text": "赤ですね。"},
    {"type": "done", "outcome": "accepted", "session": "s1", "secs": 3.0,
     "tool_calls": {"AskUserQuestion": 1}},
]


def test_ask_does_not_close_the_turn(tmp_path):
    cl = FakeClient([[dict(e) for e in _ASK_SCRIPT]])
    ctx = _ctx(cl, tmp_path)
    seen: list[bool] = []

    def ask_fn(prompt: str) -> str:
        seen.append(bool(getattr(ctx.renderer, "_open", False)))   # 聞いている間もターンは開いたまま
        return "1"

    ctx.ask_fn = ask_fn
    res = run_turn(ctx, "色を聞いて")
    assert cl.answers == [("a1", "赤")]
    assert res["outcome"] == "accepted"
    assert seen == [True]
    out = _text(ctx)
    assert "no done" not in out and "no result returned" not in out
    assert out.count("done  ") == 1
    assert "聞きます。" in out and "色は？" in out and "赤ですね。" in out
    # done の位置: 質問より後（＝質問の前に done を出していない）
    assert out.index("? 色は？") < out.index("done  ")
    assert ctx.stats["turns"] == 1 and ctx.stats["tools"] == 1


def test_ask_resumes_live_on_terminal(tmp_path):
    """端末（Live あり）では、質問の間だけ Live を止め、答えた後に作り直す。"""
    probe: list[tuple[str, bool]] = []

    class Probing(FakeClient):
        def events(self, timeout=None):
            while self._pending:
                ev = self._pending.pop(0)
                yield ev
                if isinstance(ev, dict) and ev.get("type") == "ask":
                    # ここに戻るのは app が ask を処理し終えた後（＝再開済みのはず）
                    probe.append(("after_ask", ctx.renderer._live is not None))
            if timeout is not None:
                yield None

    cl = Probing([[dict(e) for e in _ASK_SCRIPT]])
    c = Console(record=True, width=80, force_terminal=True, file=io.StringIO())
    ctx = AppContext(console=c, client=cl, renderer=Renderer(c), cfg={"telemetry": ""},
                     install_id_path=tmp_path / "install_id")
    assert ctx.renderer._use_live

    def ask_fn(prompt: str) -> str:
        probe.append(("in_ask", ctx.renderer._live is not None))   # 聞いている間は Live が無い
        return "2"

    ctx.ask_fn = ask_fn
    res = run_turn(ctx, "色を聞いて")
    assert res["outcome"] == "accepted"
    assert cl.answers == [("a1", "青")]
    assert probe == [("in_ask", False), ("after_ask", True)]
    assert ctx.renderer._live is None        # done で閉じた
    out = _text(ctx)
    assert "no done" not in out and "no result returned" not in out
    assert out.count("done  ") == 1


# --------------------------------------------------------------------------
# 止まっている子は send の前に立て直す（/status の「次の send で再起動」を本当にする・検証で発見）
# --------------------------------------------------------------------------

def test_dead_child_is_restarted_before_send(tmp_path):
    cl = FakeClient([[{"type": "done", "outcome": "accepted", "session": "s1"}]])
    cl.alive = False                      # 待機中に死んだ（kill -9 相当）
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "x")
    assert cl.restarts == [{}]            # 設定そのままで 1 回
    assert cl.alive and len(cl.sent) == 1
    assert res["outcome"] == "accepted"
    assert "再起動します" in _text(ctx)


def test_dead_child_restart_failure_does_not_send(tmp_path):
    cl = FakeClient(restart_fail=True)
    cl.alive = False
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "x")
    assert res["outcome"] == "send_failed" and cl.sent == []
    assert "再起動できません" in _text(ctx)


def test_ask_echoes_the_chosen_answer(tmp_path):
    """入力行は erase_when_done で消えるので、選んだ答えを `? 答え` の 1 行で残す。"""
    cl = FakeClient([[
        {"type": "ask", "ask": "a1", "question": "色は？", "options": ["赤", "青"]},
        {"type": "done", "outcome": "accepted", "session": "s1"},
    ]])
    ctx = _ctx(cl, tmp_path)
    ctx.ask_fn = lambda prompt: "2"
    run_turn(ctx, "選んで")
    assert "? 青" in _text(ctx)
    ctx.ask_fn = lambda prompt: ""
    cl.scripts = [[{"type": "ask", "ask": "a2", "question": "q", "options": []},
                   {"type": "done", "outcome": "accepted", "session": "s1"}]]
    run_turn(ctx, "2")
    assert "? (空)" in _text(ctx)


def test_second_ctrl_c_says_it_is_quitting(tmp_path):
    cl = FakeClient([[{"type": "token", "text": "a"}, Interrupt(), {"type": "token", "text": "b"}, Interrupt()]])
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "x")
    assert res["outcome"] == "cancelled" and ctx.exit_requested
    assert "終了します" in _text(ctx)


def test_prompt_file_flag_exists_and_reads(tmp_path):
    """★--prompt-file（2026-08-26 run27d）: 課題文を argv に載せると、文中のコマンド例が
    エージェント自身の pkill -f にマッチして親ごと撃たれる。本文はファイルで渡す。"""
    from laps.term.app import build_parser
    p = build_parser()
    f = tmp_path / "task.txt"
    f.write_text("php -S 0.0.0.0:18300 を立てよ", encoding="utf-8")
    args = p.parse_args(["--json", "--prompt-file", str(f)])
    assert args.prompt_file == str(f)
    # 消費側の規約: prompt_file があれば prompt に本文が入る（main 内で読む）。
    # ここでは属性の存在と parser の受理だけを固定する（main は端末を要するため）。


def test_two_ctrl_c_in_one_window_reach_the_second_handler():
    """★UI 監査: take() は押下の**数**を返すのに interrupted() は1回しか呼ばれず、
    素早い2連打が1回に潰れていた（「2回目で打ち切る」契約が実装で守られていない）。"""
    from laps.term.app import _apply_interrupts

    class _Keys:
        def __init__(self, n): self.n = n
        def take(self): n, self.n = self.n, 0; return n

    calls = []

    def interrupted():
        calls.append(1)
        return len(calls) >= 2      # 2回目で打ち切る契約

    assert _apply_interrupts(_Keys(2), interrupted) is True
    assert len(calls) == 2, f"2連打が {len(calls)} 回に潰れている"


def test_single_ctrl_c_only_cancels():
    from laps.term.app import _apply_interrupts

    class _Keys:
        def __init__(self, n): self.n = n
        def take(self): n, self.n = self.n, 0; return n

    calls = []

    def interrupted():
        calls.append(1)
        return len(calls) >= 2

    assert _apply_interrupts(_Keys(1), interrupted) is False
    assert len(calls) == 1


def test_no_press_calls_nothing():
    from laps.term.app import _apply_interrupts

    class _Keys:
        def take(self): return 0

    called = []
    assert _apply_interrupts(_Keys(), lambda: called.append(1) or True) is False
    assert not called


def test_empty_states_carry_a_next_step():
    """★ui-ux-design §2 が最重要と名指しする条項:「空状態に必ず次の一手を1つ置く」。
    「まだありません」だけだと、壊れているのか、これからなのかが区別できない。"""
    import inspect

    from laps.term import render, slash
    for mod in (render, slash):
        src = inspect.getsource(mod)
        for empty in ("(セッションはまだありません)", "(道具はまだ使われていません)",
                      "走行中ではありません"):
            if empty in src:
                i = src.index(empty)
                nearby = src[i:i + 400]
                assert "→" in nearby, f"{empty} に次の一手が無い"


# --------------------------------------------------------------------------
# 銘（起動画面の 1 行）— SPEC 項目 20
# --------------------------------------------------------------------------

def _header_out(ctx: AppContext) -> str:
    app._header(ctx)
    return ctx.console.export_text(clear=True)


def test_header_is_one_line_and_fits_in_both_width_models(tmp_path):
    """★幅 80 で 2 行に割れていた（86 桁）。曖昧幅を 1 と数えても 2 と数えても収まること。"""
    from laps.term.slash import disp_width
    cl = FakeClient()
    cl.model = "blackwall-coder-30b-a3b-instruct"
    cl.backend = "http://192.0.2.160:8741/v1"
    cl.workspace = "/home/user/project/scratch/tui/deep/deeper/fake-workspace"
    for width in (80, 100, 60, 40, 200):
        c = Console(record=True, width=width, force_terminal=False, file=io.StringIO())
        ctx = AppContext(console=c, client=cl, renderer=Renderer(c), cfg={},
                         install_id_path=tmp_path / "cfg" / "install_id")
        line = app.header_line(ctx)
        assert "\n" not in line.plain
        assert disp_width(line.plain) <= width, (width, line.plain)
        assert len(line.plain) == len(line.plain.rstrip())


def test_header_has_no_ambiguous_width_separator(tmp_path):
    """区切りは空白 2 桁。`·`(U+00B7) は端末によって 1 桁にも 2 桁にもなる。"""
    ctx = _ctx(FakeClient(), tmp_path)
    assert "·" not in _header_out(ctx)


def test_header_marks_trusted_raw_in_red(tmp_path):
    from laps.term import theme as T
    cl = FakeClient()
    cl.policy, cl.ethics = "trusted", "raw"
    ctx = _ctx(cl, tmp_path)
    line = app.header_line(ctx)
    assert "trusted/raw" in line.plain
    styles = {str(sp.style) for sp in line.spans if "trusted/raw" in line.plain[sp.start:sp.end]}
    assert T.S_RED in styles
    cl.policy, cl.ethics = "readonly", "strict"
    line = app.header_line(ctx)
    styles = {str(sp.style) for sp in line.spans if "readonly/strict" in line.plain[sp.start:sp.end]}
    assert T.S_RED not in styles


def test_first_run_hint_appears_once(tmp_path):
    """初回だけ「そのまま書いてください」。2 回目以降は場所を取らない。"""
    ctx = _ctx(FakeClient(), tmp_path)
    assert "そのまま書いてください" in _header_out(ctx)
    hist = (tmp_path / "history")
    hist.write_text("+こんにちは\n", encoding="utf-8")
    assert "そのまま書いてください" not in _header_out(ctx)


def test_tail_path_shortens_from_the_end(tmp_path):
    assert app._tail_path("/a/b/c/d/e", 2) == "/…/d/e"
    assert app._tail_path("/a/b/c/d/e", -1) == "e"
    assert app._short_host("192.0.2.160:8741") == ".160:8741"
    assert app._short_host("localhost:8741") == "localhost:8741"


# --------------------------------------------------------------------------
# 走行中の告知（Live が出せない端末）— SPEC 項目 14
# --------------------------------------------------------------------------

def test_run_turn_announces_running_when_live_is_unavailable(tmp_path):
    """★TERM=dumb / パイプでは波も実行中の行も出ず、3.4 秒 画面が静止していた（実測）。
    run_turn から実際に呼ばれること（＝繋がっていること）をここで見る。"""
    cl = FakeClient([[{"type": "token", "text": "はい"},
                      {"type": "done", "outcome": "accepted", "session": "s"}]])
    ctx = _ctx(cl, tmp_path)
    ctx.interactive = True
    assert wave_allowed(ctx.console) is False       # 記録用 Console は端末ではない
    run_turn(ctx, "a")
    out = _text(ctx)
    assert app.RUNNING_NOTE in out and "Ctrl-C" in out


def test_no_announcement_for_one_shot_prompt(tmp_path):
    """-p をパイプに流している人にとって、余分な 1 行は解析の邪魔にしかならない。"""
    cl = FakeClient([[{"type": "done", "outcome": "accepted"}]])
    ctx = _ctx(cl, tmp_path)          # interactive=False（既定）
    run_turn(ctx, "a")
    assert app.RUNNING_NOTE not in _text(ctx)


def test_cancel_notice_says_what_happens_next(tmp_path):
    """内部語（cancel:）ではなく、次に何が起きるかを言う。"""
    assert "もう一度 Ctrl-C" in app.CANCEL_NOTICE and "中断を要求しました" in app.CANCEL_NOTICE
    cl = FakeClient([[{"type": "token", "text": "x"}, Interrupt(),
                      {"type": "done", "outcome": "cancelled"}]])
    ctx = _ctx(cl, tmp_path)
    run_turn(ctx, "a")
    assert app.CANCEL_NOTICE in _text(ctx)


def test_cancel_from_serve_does_not_duplicate_the_client_notice(tmp_path):
    """★1 回の Ctrl-C で「中断を要求しました」が 2〜3 回並んでいた（実機・2026-09-03）:
    ①クライアント自身の即時表示 ②serve の受理（`kind=cancel, applied=…`）③agent が
    反復境界で確定（`kind=cancelled`）が同じターンに届く。1 回だけに畳む——ただし
    `applied` は情報として価値があるので続報として残す。"""
    cl = FakeClient([[
        {"type": "token", "text": "x"}, Interrupt(),
        {"type": "notice", "kind": "cancel", "accepted": True, "applied": "running"},
        {"type": "notice", "kind": "cancelled", "iter": 3},
        {"type": "done", "outcome": "cancelled", "session": "s-cancel"},
    ]])
    ctx = _ctx(cl, tmp_path)
    run_turn(ctx, "長い仕事")
    text = _text(ctx)
    assert text.count("中断を要求しました") == 1, text
    assert "実行中に届きました" in text, text


def test_cancel_stall_warns_once_when_done_is_slow(tmp_path, monkeypatch):
    """★中断が 73 秒止まらなかった走行があり、その間クライアントは何も出さず
    固まって見えた（実機・2026-09-03）。cancel を送ってから done も来ないまま
    しきい値を超えたら、1 回だけ「まだ止まっていない」を言う。"""
    monkeypatch.setattr(app, "CANCEL_STALL_SECS", 0.0)

    class StallClient(FakeClient):
        """実物と違い cancel() でも done を足さない（意図的に長引かせる）。
        events() が空振り（None）を 2 回返してから done を積む。"""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._empty_ticks = 0

        def cancel(self, rid: str) -> None:
            self.cancels.append(rid)

        def events(self, timeout=None):
            if not self._pending and self.cancels:
                self._empty_ticks += 1
                if self._empty_ticks >= 2:
                    self._pending.append({"type": "done", "id": self.cancels[-1],
                                          "outcome": "cancelled", "session": "s-cancel"})
            yield from super().events(timeout=timeout)

    cl = StallClient([[{"type": "token", "text": "はじめ"}, Interrupt()]])
    ctx = _ctx(cl, tmp_path)
    res = run_turn(ctx, "長い仕事")
    text = _text(ctx)
    assert res["outcome"] == "cancelled"
    assert text.count("まだ止まっていない") == 1, text
    assert "もう一度 Ctrl-C で強制終了" in text, text


def test_cancel_round_trip_is_written_to_debug_note(tmp_path):
    """`--debug FILE` が 0 バイトだった日があった（serve は正常系で stderr に書かない）。
    せめてクライアントが見ている cancel の往復は残す。"""
    cl = FakeClient([[
        {"type": "token", "text": "x"}, Interrupt(),
        {"type": "notice", "kind": "cancel", "accepted": True, "applied": "running"},
        {"type": "done", "outcome": "cancelled", "session": "s-cancel"},
    ]])
    ctx = _ctx(cl, tmp_path)
    run_turn(ctx, "a")
    joined = "\n".join(cl.debug_notes)
    assert "cancel 送信" in joined, cl.debug_notes
    assert "cancel の notice を受信" in joined and "applied=running" in joined, cl.debug_notes
    assert "done 受信" in joined, cl.debug_notes


# --------------------------------------------------------------------------
# 本体が落ちたとき — SPEC 項目 18
# --------------------------------------------------------------------------

def test_exit_translates_signal_and_shows_stderr_tail(tmp_path):
    """★`code=-9` のままでは打てる手が無い。訳・材料・次の一手の 3 つを出す。"""
    cl = FakeClient([[{"type": "_exit", "code": -9, "pid": 1}], [{"type": "_exit", "code": -9}]])
    cl.stderr_tail = lambda: ("ggml_backend_cuda_buffer_type_alloc_buffer: failed\n"
                              "llama_model_load: error loading model\n")
    ctx = _ctx(cl, tmp_path)
    run_turn(ctx, "a")
    out = _text(ctx)
    assert "シグナル 9" in out and "SIGKILL" in out
    assert "llama_model_load: error loading model" in out      # 材料（原因の行）
    assert "推論サーバーのコンテキスト長" in out                                   # 次の一手
    assert "code=-9" not in out


def test_exit_without_stderr_still_gives_next_step(tmp_path):
    cl = FakeClient([[{"type": "_exit", "code": 2}], [{"type": "_exit", "code": 2}]])
    ctx = _ctx(cl, tmp_path)
    run_turn(ctx, "a")
    out = _text(ctx)
    assert "終了コード 2" in out and "--debug" in out


def test_exit_reason_table():
    assert app._exit_reason(-9)[0].startswith("シグナル 9")
    assert "SIGTERM" in app._exit_reason(-15)[0]
    assert app._exit_reason(3)[0] == "終了コード 3"
    assert app._exit_reason(None)[0].startswith("code=")


# --------------------------------------------------------------------------
# 補完が打鍵を止めない — SPEC 項目 24
# --------------------------------------------------------------------------

def test_prompt_session_completes_in_thread(tmp_path, monkeypatch):
    monkeypatch.setattr("laps.term.config.config_path", lambda: tmp_path / "config.toml")
    ctx = _ctx(FakeClient(), tmp_path)
    sess = app._make_prompt_session(ctx)
    assert sess.complete_in_thread is True and sess.complete_while_typing is True


# --------------------------------------------------------------------------
# 合流（2026-08-29）— 波 → render の Live へ刷新回数が届いているか
# --------------------------------------------------------------------------

def test_wave_is_handed_over_before_the_live_is_built(tmp_path):
    """`run_turn` は Live を作る**前に**波を渡す。

    ★順番が逆だと、Live は前のターンの波（初回は None）の値で作られ、`--calm` が
      その走行では効かない。`written-but-not-wired.md` と同じ種類の穴で、
      画面は正しく見えるので目視では絶対に気づけない（帯域だけが下がらない）。
    """
    from laps.term.wave import Wave
    cl = FakeClient([[{"type": "token", "id": "r1", "text": "はい"},
                      {"type": "done", "id": "r1", "outcome": "accepted", "text": "はい",
                       "secs": 0.1, "tok_s": 10.0, "tool_calls": {}}]])
    c = Console(record=True, width=80, force_terminal=True, file=io.StringIO())
    ctx = AppContext(console=c, client=cl, renderer=Renderer(c), cfg={"telemetry": ""},
                     install_id_path=tmp_path / "install_id")
    seen: list[float] = []
    real_start = ctx.renderer._start_live

    def spy():
        real_start()
        if ctx.renderer._live is not None and not seen:
            seen.append(ctx.renderer._live.refresh_per_second)

    ctx.renderer._start_live = spy
    ctx.wave_factory = lambda: Wave(80, mode="calm")
    run_turn(ctx, "やあ")
    assert seen, "Live が作られていない"
    assert seen[0] == pytest.approx(1.0 / 0.120, rel=0.01), seen   # calm = 8.3 回/s


def test_波をoffにしたらアプリの入口でも係を作らない(monkeypatch, tmp_path):
    """★配線を**入口**で見る（2026-09-01）。

    波班の試験は `wave.make_factory` が off で None を返すことまで。だが利用者が
    触るのは `app.make_wave_factory` で、そこが `off` を無視して Wave を作っていた。
    絵は 1 行も出ないので画面は正しく見えるが、`render._refresh_rate()` が波の
    period で Live を回し続ける＝**消したはずの物が刷新回数だけ生きている**。
    ＝この現場で何度も踏んだ「書いたが繋いでいない」。
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    c = Console(file=io.StringIO(), force_terminal=True, width=60)

    monkeypatch.setenv("LAPS_WAVE", "off")
    assert app.make_wave_factory(c, False) is None, "off なのに係を作っている"

    monkeypatch.setenv("LAPS_WAVE", "on")
    assert app.make_wave_factory(c, False) is not None, "on なのに係が無い"


# --------------------------------------------------------------------------
# 例外の後始末（2026-09-02 の監査）
# --------------------------------------------------------------------------

def test_run_turn_closes_live_even_when_rendering_raises(tmp_path, monkeypatch):
    """描画の途中で例外が出ても Live を畳む（＝カーソルを戻す）。

    ★旧: `run_turn` の finally は `keys.stop()` だけで、Live は走ったまま抜けていた。
      Rich の `Live` はカーソルを start() で隠し stop() でしか戻さず atexit も持たないので、
      利用者の手元には**カーソルの見えないシェル**が残る（`reset` を打つまで直らない）。
    """
    buf = io.StringIO()
    c = Console(file=buf, force_terminal=True, width=80, height=24)
    r = Renderer(c)
    cl = FakeClient([[{"type": "token", "text": "これから落ちます"}]])
    ctx = AppContext(console=c, client=cl, renderer=r, cfg={},
                     install_id_path=tmp_path / "install_id")

    boom = {"n": 0}

    def explode(self, ev):
        boom["n"] += 1
        raise RuntimeError("描画が落ちた")

    monkeypatch.setattr(Renderer, "on_event", explode)   # monkeypatch が終わりに戻す
    with pytest.raises(RuntimeError):
        run_turn(ctx, "やってみて")
    assert boom["n"] >= 1
    assert r._live is None and r._open is False
    out = buf.getvalue()
    assert out.count("\x1b[?25l") == out.count("\x1b[?25h") >= 1, "カーソルが隠れたまま"


def test_repl_survives_an_internal_error_and_says_why(fake_serve, capsys, monkeypatch, tmp_path):
    """1 往復の内部エラーで**会話ごと**終わらせない。理由は必ず出す（握り潰さない）。

    ★旧: 事象 1 つで例外が出ると REPL ごと終わり、それまでの会話へ戻る手段は
      `/resume <id>` を覚えていることだけだった。しかも Live が走ったままなので、
      端末はカーソルが消えた状態で戻る。
    """
    dbg = tmp_path / "dbg.log"
    calls = {"n": 0}
    real = app.run_turn

    def flaky(ctx, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("壊れた事象")
        return real(ctx, text)

    fake_serve["scripts"].append([{"type": "token", "text": "二度目は通る"},
                                  {"type": "done", "outcome": "accepted", "session": "s"}])
    monkeypatch.setattr(app, "run_turn", flaky)
    monkeypatch.setattr("sys.stdin", io.StringIO("一度目\n二度目\n/exit\n"))
    code = app.main(["--debug", str(dbg)])
    out = capsys.readouterr().out
    assert code == 0
    assert calls["n"] == 2, "1 回目の例外で REPL が死んでいる（会話が続かない）"
    assert "内部エラー" in out and "RuntimeError" in out and "壊れた事象" in out, out
    assert "二度目は通る" in out
    assert dbg.exists() and "Traceback" in dbg.read_text(encoding="utf-8"), "全文が --debug に出ていない"
