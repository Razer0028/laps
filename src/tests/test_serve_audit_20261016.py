"""★2026-10-16 中核と画面の境目の監査（/tmp/audit-serve の再現台本を試験にした）。

1. 終わり際の割り込み: serve は `injected` と答えたのに、走行はもう読まず黙って消えた
   （oc の最後の拾い場所が確かめの推論の前・serve は run() が返った後も `_agent` を握っていた）。
2. 質問（AskUserQuestion）を待っている間の「止める」・強制割り込みが効かず、最長 300 秒待った。
3. 問いが閉じたことを画面へ言わなかった（`notice kind=ask_closed`）。
4〜6. Android の下書きの捨て方・cancel_applied・失敗の色（ソースの照合）。
7. 割り込みが線に二度出た（`injected` と、中核の写しの harness 行）。
8. ready/status の道具一覧が旧中核の登録だった。
9. Windows が id の無い error で走行を終え、本体を殺していた（translate.rs・Rust 側は cargo test で確かめる）。
10. 工程モードだけ常に旧中核を作っていた。
11. 細かい物（focused・走行の合間の status・gateway の running）。

偽のエージェントで serve を回す（推論機には繋がない）。oc の分は偽の推論機で本物の OcAgent.run() を回す。
"""
from __future__ import annotations

import inspect
import json
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


# ── serve を偽のエージェントで回す道具 ─────────────────────────────────────

class _Cap:
    """serve の線（stdout）を行ごとに受ける。"""

    def __init__(self):
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def write(self, s):
        for line in s.splitlines():
            if line.strip():
                with self._lock:
                    self.rows.append(json.loads(line))
        return len(s)

    def flush(self):
        pass

    def snap(self) -> list[dict]:
        with self._lock:
            return list(self.rows)

    def wait(self, pred, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            for r in self.snap():
                if pred(r):
                    return r
            time.sleep(0.02)
        raise AssertionError(f"来なかった。線: {self.snap()}")


@pytest.fixture
def serve(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path / "sess"))
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("LAPS_LIVE", "0")
    monkeypatch.chdir(tmp_path)                 # serve は作業場へ chdir する。終わったら戻す
    import laps.serve as S
    servers = []

    def make(agent_cls, *, ask_timeout=None):
        monkeypatch.setattr(S, "Agent", agent_cls)
        if ask_timeout is not None:
            monkeypatch.setattr(S, "_ASK_TIMEOUT", ask_timeout)
        out = _Cap()
        srv = S.Server(out)
        srv.start()
        servers.append(srv)
        return S, srv, out
    yield make
    for srv in servers:
        srv.stop(3)


class _BaseFake:
    engine = ""

    def __init__(self, session=None, on_event=None):
        self.session = session
        self.on_event = on_event
        self.tools = {"Read": 1}
        self.counters = {}
        self.outcome = None
        self._inj: list[str] = []
        self._cancel = False
        self.state = type("S", (), {"iter": 0, "files_written": []})()

    def inject(self, text, force=False):
        self._inj.append(text)

    def take_injections(self):
        out, self._inj = self._inj, []
        return out

    def request_cancel(self, reason=""):
        self._cancel = True

    def close(self):
        pass


def _send(srv, **kw):
    srv.handle_line(json.dumps({"type": "send", **kw}))


# ── 1. 終わり際の割り込み ──────────────────────────────────────────────────







# ── 2・3. 質問を待っている間の「止める」・強制割り込み・ask_closed ────────────────

def _ask_fake(results):
    from laps.tools import ask_user as AU

    class Fake(_BaseFake):
        tools = {"AskUserQuestion": object()}

        def run(self, prompt, max_iters=0):
            r = AU._run({"question": "pick one", "options": ["a", "b"]})
            results.append(r)
            self.outcome = "cancelled" if self._cancel else "accepted"
            return f"answer was: {r}"
    return Fake








# ── 7. 割り込みが線に二度出ない ────────────────────────────────────────────



# ── 8・11. status ───────────────────────────────────────────────────────────







# ── 10. 工程モードも同じ中核の選び方 ────────────────────────────────────────




# ── 1. oc の中核の拾い場所 ─────────────────────────────────────────────────

class _FakeBackend:
    def __init__(self, script):
        self.script = list(script)
        self.usage = {"completion_tokens": 0}
        self.last_prompt_tokens = 1000
        self.cfg = type("C", (), {"model": "fake", "base_url": "http://127.0.0.1:9/v1"})()
        self.sent = []

    def soft_reset(self):
        pass

    def close(self):
        pass

    def chat_with_tools(self, send, tools, **kw):
        self.sent.append("tools")
        step = self.script.pop(0) if self.script else ("(no more)", [])
        return step() if callable(step) else step

    def chat(self, send, **kw):
        return "## Goal\nsummary"


@pytest.fixture
def oc_agent(monkeypatch, tmp_path):
    from laps.oc import loop
    monkeypatch.setenv("LAPS_MODEL_FAMILY", "qwen")
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path / "sess"))
    monkeypatch.setenv("LAPS_N_CTX", "131072")
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)

    def make(script):
        a = loop.OcAgent()
        a._n_ctx = 131072
        a.backend = _FakeBackend(script)
        return a
    return make


DONE = {"name": "Done", "input": {}}








# ── 3〜6・9・11. 画面側（ソースの照合。Kotlin はこの機械で組み立てられない）──────────

ANDROID_REDUCER = ROOT / "android/app/src/main/kotlin/jp/example/laps/ui/model/EventReducer.kt"
APP_JS = ROOT / "app/ui/app.js"
TRANSLATE_RS = ROOT / "app/src-tauri/blackwall-core/src/translate.rs"
WIRE_RS = ROOT / "app/src-tauri/blackwall-core/src/wire.rs"
SIDECAR_RS = ROOT / "app/src-tauri/src/sidecar.rs"


def test_Windows_は_done_の来る失敗で走行を終えない():
    wire = WIRE_RS.read_text(encoding="utf-8")
    assert 'pub const TERMINAL_ERROR_KINDS: &[&str] = &["rejected", "fatal"];' in wire
    assert "TERMINAL_ERROR_KINDS.contains(&k)" in TRANSLATE_RS.read_text(encoding="utf-8")


def test_gateway_は走っている依頼の_done_でだけ_running_を下ろす():
    src = (SRC / "laps/gateway.py").read_text(encoding="utf-8")
    assert 'str(_o.get("id")) == str(ses.run_id)' in src
    assert 'if _o.get("type") == "done" or' not in src


def test_端末は_focused_を案内しない():
    from laps.term import config
    src = inspect.getsource(config)
    assert "full | focused | chat" not in src
