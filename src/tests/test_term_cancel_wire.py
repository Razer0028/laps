"""中断（Ctrl-C）が**線に出るまで**の時間（laps/term/app.py・W2・2026-09-04）。

背景（実機・2026-09-03 dogfood ⑳ / 0.1.180）: Ctrl-C を押してから走行が止まるまで 73 秒
かかった走行がある。第 1 回の調査（W1）は「端末が cancel を送るのが遅れた」か
「本体が受けたのに効かなかった」かを**決められずに**止まった——どちらの側にも
「いつ線に出たか」の記録が無かったからである。

ここで固定するのは端末側の契約の 1 つだけ:

    **Ctrl-C を読んでから `{"type":"cancel"}` が本体の stdin に届くまで ≤ 1 秒**、
    しかもそれは**主ループ（events のループ）が詰まっていても**成り立つ。

旧実装は「見張りスレッドは数えるだけ・線に書くのは主ループ」だったので、この 2 つ目が
成り立っていなかった（＝主ループが詰まる限り中断は出ていかない）。
"""
from __future__ import annotations

import io
import os
import sys
import termios
import threading
import time
from pathlib import Path

import pytest
from rich.console import Console

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps.term import app  # noqa: E402
from laps.term.app import AppContext, run_turn  # noqa: E402
from laps.term.client import ServeClient  # noqa: E402
from laps.term.render import Renderer  # noqa: E402


def _ctx(client, tmp_path: Path) -> AppContext:
    console = Console(file=io.StringIO(), width=100, force_terminal=False, highlight=False)
    return AppContext(console=console, client=client, renderer=Renderer(console), cfg={},
                      install_id_path=tmp_path / "install_id")


@pytest.fixture
def pty_pair():
    import pty
    master, slave = pty.openpty()
    yield master, slave
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def _wait(cond, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


# --------------------------------------------------------------------------
# 1. 主ループが詰まっていても cancel は出ていく
# --------------------------------------------------------------------------

class _BlockedClient:
    """`events()` が長く戻らない客。旧実装ではこの間 Ctrl-C はどこにも出ていかなかった。"""

    def __init__(self, block: float = 3.0) -> None:
        self.block = block
        self.cancels: list[str] = []
        self.cancel_at: list[float] = []
        self.sent: list[dict] = []
        self.alive = True
        self.status = {"kind": "ready"}
        self.debug_notes: list[str] = []
        self._done = threading.Event()

    def send(self, messages, *, rid=None, session=None, **_kw) -> str:
        self.sent.append({"messages": messages})
        return "r1"

    def cancel(self, rid: str) -> None:
        self.cancels.append(rid)
        self.cancel_at.append(time.time())
        self._done.set()          # cancel が線に出たら、次の events で done を返す

    def debug_note(self, text: str) -> None:
        self.debug_notes.append(text)

    def events(self, timeout=None):
        if self._done.wait(self.block):
            yield {"type": "done", "id": "r1", "outcome": "cancelled", "session": "s1",
                   "secs": 1.0, "tool_calls": {}}
            return
        yield None


def test_ctrl_c_reaches_the_wire_while_the_event_loop_is_blocked(tmp_path, pty_pair, monkeypatch):
    """★これが W2 の本体。主ループが 3 秒戻らない間に Ctrl-C を押しても、
    cancel は**押してから 1 秒以内**に線へ出る（旧実装では 3 秒近く待たされた）。"""
    master, slave = pty_pair
    monkeypatch.setattr(app, "_tty_fd", lambda ctx: slave)
    cl = _BlockedClient(block=3.0)
    ctx = _ctx(cl, tmp_path)

    pressed: list[float] = []

    def press():
        _wait(lambda: ctx.running_rid is not None)
        time.sleep(0.2)               # 主ループが events() の中で寝ている最中に押す
        pressed.append(time.time())
        os.write(master, b"\x03")

    threading.Thread(target=press, daemon=True).start()
    res = run_turn(ctx, "長い")
    assert cl.cancels == ["r1"]
    lag = cl.cancel_at[0] - pressed[0]
    assert lag <= 1.0, f"Ctrl-C から cancel が線に出るまで {lag:.2f} 秒（主ループ待ちに縛られている）"
    assert res["outcome"] == "cancelled"
    assert any("Ctrl-C を読んだ→cancel 送信" in n for n in cl.debug_notes), cl.debug_notes


def test_cancel_is_written_exactly_once_even_though_two_writers_exist(tmp_path, pty_pair, monkeypatch):
    """見張りと主ループの両方が書き手になった。**線に出るのは 1 回だけ**。"""
    master, slave = pty_pair
    monkeypatch.setattr(app, "_tty_fd", lambda ctx: slave)
    cl = _BlockedClient(block=0.05)   # 主ループは詰まらない＝両方が同じ窓で動く
    ctx = _ctx(cl, tmp_path)

    def press():
        _wait(lambda: ctx.running_rid is not None)
        os.write(master, b"\x03")

    threading.Thread(target=press, daemon=True).start()
    run_turn(ctx, "長い")
    assert cl.cancels == ["r1"]       # 2 本になっていない


# --------------------------------------------------------------------------
# 2. 見張りが死んだら黙って消えない（端末を戻して SIGINT に道を譲る）
# --------------------------------------------------------------------------

def test_keywatch_death_restores_isig_instead_of_swallowing_ctrl_c(pty_pair):
    """★旧実装は `os.read` の OSError で **return して終わり**だった。ISIG は切ったままなので
    端末も SIGINT を作らず、その走行の Ctrl-C は完全に無効（画面にも記録にも何も出ない）。

    ここでは死に方そのもの（`_died`）を呼んで、**端末が cooked に戻ること**を見る
    （相手（master）を閉じて EIO を起こすと、その pty はもう tcgetattr すら通らないので
    「戻ったか」を確かめられない——次の試験がその経路を別に押さえる）。"""
    master, slave = pty_pair
    before = termios.tcgetattr(slave)
    kw = app._KeyWatch(slave)
    kw.start()
    try:
        assert not (termios.tcgetattr(slave)[3] & termios.ISIG)   # いま raw
        kw._died("read: OSError: [Errno 5] Input/output error")
        assert kw.reader_error
        assert kw.alive is False
        after = termios.tcgetattr(slave)
        assert after[3] & termios.ISIG, "端末が raw のまま＝Ctrl-C がどこにも届かない"
        assert after[3] == before[3] and after[6] == before[6]
        kw.stop()                                   # 二重に戻しても壊れない
        assert termios.tcgetattr(slave)[3] == before[3]
    finally:
        kw.stop()


def test_keywatch_records_why_its_reader_died():
    """相手が消えた（EIO）ときに、理由が残ること。**黙って終わらない**。"""
    import pty
    master, slave = pty.openpty()
    kw = app._KeyWatch(slave)
    kw.start()
    try:
        os.close(master)                            # 読み手に EIO を起こす
        assert _wait(lambda: bool(kw.reader_error)), "見張りが黙って死んだ"
        assert "read" in kw.reader_error or "select" in kw.reader_error, kw.reader_error
    finally:
        kw.stop()
        try:
            os.close(slave)
        except OSError:
            pass


# --------------------------------------------------------------------------
# 3. 端から端まで: pty ＋ 本物の ServeClient ＋ 黙る偽 serve
# --------------------------------------------------------------------------

_FAKE_SERVE = '''
import json, os, sys, threading, time
LOG = os.environ["FAKE_SERVE_LOG"]
def note(k):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("%.6f %s\\n" % (time.time(), k))
def out(o):
    sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
out({"type": "notice", "kind": "ready", "model": "fake", "tools": []})
def worker(rid):
    out({"type": "token", "id": rid, "text": "..."})
    time.sleep(20)                       # ★ここが「推論機が黙っている」区間
    out({"type": "done", "id": rid, "outcome": "accepted", "session": "s1", "secs": 20.0})
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    obj = json.loads(line)
    t = obj.get("type")
    note("recv_" + str(t))
    if t == "send":
        threading.Thread(target=worker, args=(obj.get("id"),), daemon=True).start()
    elif t == "quit":
        break
'''


@pytest.mark.timeout(60) if hasattr(pytest.mark, "timeout") else (lambda f: f)
def test_cancel_line_reaches_the_child_stdin_within_one_second(tmp_path, pty_pair, monkeypatch):
    """本物の子プロセス相手に「Ctrl-C → cancel の行が子の stdin に届く」を秒で測る。

    偽 serve は run_start のあと token を 1 つ出して 20 秒黙る（実機の
    「推論機の呼び出し 1 回が 76 秒続いた」と同じ形）。"""
    mod = tmp_path / "fake_serve_for_cancel.py"
    mod.write_text(_FAKE_SERVE, encoding="utf-8")
    log = tmp_path / "fake.log"
    log.write_text("", encoding="utf-8")
    # 子は PYTHONPATH で自分を見つける（ServeClient._env が os.environ の分も足す）
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("FAKE_SERVE_LOG", str(log))

    master, slave = pty_pair
    monkeypatch.setattr(app, "_tty_fd", lambda ctx: slave)
    cl = ServeClient(workspace=str(tmp_path / "ws"), backend="http://127.0.0.1:9/v1",
                     model="fake", module="fake_serve_for_cancel",
                     debug_log=str(tmp_path / "debug.log"))
    cl.start(timeout=20.0)
    ctx = _ctx(cl, tmp_path)
    pressed: list[float] = []

    def press():
        # 依頼が子に届いてから押す（走行中であることを確かめてから）
        _wait(lambda: "recv_send" in log.read_text(encoding="utf-8"), timeout=15.0)
        time.sleep(0.3)
        pressed.append(time.time())
        os.write(master, b"\x03")

    th = threading.Thread(target=press, daemon=True)
    th.start()
    try:
        res = run_turn(ctx, "長い作業")
    finally:
        th.join(timeout=2.0)
        cl.quit()

    lines = [l.split(" ", 1) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
    recv = [float(ts) for ts, k in lines if k.strip() == "recv_cancel"]
    assert recv, "cancel の行が子の stdin に届いていない"
    assert pressed, "押せていない（試験の側の失敗）"
    lag = recv[0] - pressed[0]
    assert lag <= 1.0, f"Ctrl-C から cancel が子に届くまで {lag:.2f} 秒"
    assert res["outcome"] in ("cancelled", "accepted")
    dbg = (tmp_path / "debug.log").read_text(encoding="utf-8", errors="replace")
    assert "Ctrl-C を読んだ→cancel 送信" in dbg, dbg      # 往復が記録に残る


# --------------------------------------------------------------------------
# 4. 「止める」が何を切ったか（cancel_applied）— 内部の識別子を画面に出さない
# --------------------------------------------------------------------------

def _render(events: list[dict]) -> str:
    c = Console(record=True, width=80, force_terminal=True, file=io.StringIO())
    r = Renderer(c)
    r.start_turn("r1")
    for ev in events:
        r.on_event(ev)
    r.end_turn()
    return c.export_text()


def test_cancel_applied_does_not_print_the_internal_identifier():
    """★実測（pty で端から端まで・2026-09-04）: 画面に `cancel_applied` の 1 行が
    そのまま出ていた（`_NOTICE` 表に無く既定へ落ちる＝項目 17 違反。ws_guard と同型）。"""
    out = _render([
        {"type": "notice", "id": "r1", "kind": "cancel", "message": "中断を要求しました"},
        {"type": "notice", "id": "r1", "kind": "cancel", "accepted": True, "applied": "running"},
        {"type": "notice", "id": "r1", "kind": "cancel_applied", "applied": "running",
         "sockets": 1, "inflight": 6, "request_cancel_ms": 0.3, "bash_killed": 0},
    ])
    assert "cancel_applied" not in out
    assert "推論サーバーへの接続を 1 本切りました" in out
    assert out.count("実行中に届きました") == 1        # cancel の受理と二重に言わない


def test_cancel_applied_says_so_when_nothing_was_cut():
    """sockets=0 は「止めたのに止まらない」の入口。**利用者に見える**ようにする。"""
    out = _render([
        {"type": "notice", "id": "r1", "kind": "cancel_applied", "applied": "running",
         "sockets": 0, "inflight": 0, "connections": 0},
    ])
    assert "掴めませんでした" in out


def test_cancel_applied_stays_quiet_when_the_body_sent_no_numbers():
    """古い本体（数字を出さない）でも内部識別子は出さない——推測も書かない。"""
    out = _render([{"type": "notice", "id": "r1", "kind": "cancel_applied"}])
    assert "cancel_applied" not in out
