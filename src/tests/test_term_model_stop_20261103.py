"""端末版から推論機を止める・起こす（/model stop・止まっている推論機を /model <id> で起動）の試験（2026-11-03）。

オーナー「Blackwallのアプリや CLI 版から推論機を空けたり（止めて GPU を返す）推論機を起動したりできるようにしたい」。
モデル切り替えの口（:8743 の /v1/models・/v1/switch・/v1/stop）と Claude の中継を httpx.MockTransport で偽物にする
（偽物の土台は test_term_models_20261019.py の Lab と同じ）。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from rich.console import Console

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps.term import config, models, slash  # noqa: E402
from laps.term.app import AppContext  # noqa: E402
from laps.term.render import Renderer  # noqa: E402
from tests.test_term_app import FakeClient  # noqa: E402
from tests.test_term_models_20261019 import RELAY, SERVER, Lab  # noqa: E402


class StopLab(Lab):
    """Lab に /v1/stop と「止まっている」状態を足した偽物。"""

    def __init__(self) -> None:
        super().__init__()
        self.stoppable = True
        self.stop_reply: tuple[int, dict] | None = None   # None なら 202 で止める
        self.stops: list[dict] = []
        self.sw: dict = {}

    def server_view(self) -> dict:
        d = super().server_view()
        d["active"] = self.server or None
        d["switch"] = self.sw
        if self.stoppable:
            d["stoppable"] = True
        return d

    def handler(self, req: httpx.Request) -> httpx.Response:
        if (req.url.host, req.url.port) == ("p40.test", 8743) and req.method == "POST" and req.url.path == "/v1/stop":
            body = json.loads(req.content or b"{}")
            self.stops.append(body)
            self.posts.append(("stop", str(body.get("force"))))
            if not self.stoppable:
                return httpx.Response(404, json={"error": "not found"})
            if self.stop_reply:
                return httpx.Response(self.stop_reply[0], json=self.stop_reply[1])
            if not self.server:
                return httpx.Response(200, json={"ok": True, "message": "もう止まっています", "already": True})
            prev, self.server = self.server, ""
            return httpx.Response(202, json={"ok": True, "message": f"{prev} を止めます"})
        if (req.url.host, req.url.port) == ("p40.test", 8743) and req.method == "POST" and req.url.path == "/v1/switch":
            body = json.loads(req.content or b"{}")
            self.posts.append(("server", body["id"]))
            msg = f"{body['id']} を起動します" if not self.server else "切り替えを始めました"
            return httpx.Response(202, json={"ok": True, "message": msg})
        return super().handler(req)


@pytest.fixture
def lab(monkeypatch):
    lb = StopLab()
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", httpx.MockTransport(lb.handler))
    monkeypatch.delenv("LAPS_BACKEND", raising=False)
    return lb


def _ctx(tmp_path: Path, backend: str = SERVER, answers: list[str] | None = None, interactive: bool = True) -> AppContext:
    c = Console(record=True, width=160, force_terminal=False, file=io.StringIO())
    cl = FakeClient()
    cl.backend = backend
    path = tmp_path / "config.toml"
    config.save({"backend": backend, "policy": "trusted"}, path)
    ctx = AppContext(console=c, client=cl, renderer=Renderer(c), cfg={"claude_backend": RELAY, "server_backend": SERVER},
                     install_id_path=tmp_path / "install_id", config_path=path)
    ctx.interactive = interactive
    asked: list[str] = []
    ans = list(answers or [])

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return ans.pop(0) if ans else ""
    ctx.ask_fn = ask
    ctx.asked = asked  # type: ignore[attr-defined]
    return ctx


def _out(ctx: AppContext) -> str:
    return ctx.console.export_text(clear=True)


# ── models.fetch / models.stop ─────────────────────────────────────────

def test_fetch_reads_stopped_stoppable_and_stopping_20261103(lab):
    tr = httpx.MockTransport(lab.handler)
    lst = models.fetch({"claude_backend": RELAY}, SERVER, tr)
    assert lst.server_read and lst.stoppable and not lst.server_stopped and not lst.stopping
    lab.server = ""
    lab.sw = {"state": "running", "action": "stop", "target": None, "previous": "katcoder", "steps": []}
    lst = models.fetch({"claude_backend": RELAY}, SERVER, tr)
    assert lst.server_stopped and lst.stopping and lst.switching == "" and lst.current() is None
    # 古い モデル切り替えの口（stoppable も action も無い）は入れ替えとして読む
    lab.stoppable = False
    lab.sw = {"state": "running", "target": "qwen38x2"}
    lst = models.fetch({"claude_backend": RELAY}, SERVER, tr)
    assert not lst.stoppable and not lst.stopping and lst.switching == "qwen38x2"


def test_stop_never_asks_the_relay_or_old_modelctl_20261103(lab):
    tr = httpx.MockTransport(lab.handler)
    lab.stoppable = False
    lst = models.fetch({"claude_backend": RELAY}, SERVER, tr)
    r = models.stop(lst, transport=tr)
    assert not r.ok and "止める口を持っていません" in r.message and lab.stops == []
    # 推論機が読めない（届かない）ときは「止まっている」とも言わず、頼みもしない
    def down(req):
        if req.url.port == 8743:
            raise httpx.ConnectError("refused", request=req)
        return lab.handler(req)
    lst = models.fetch({"claude_backend": RELAY}, RELAY, httpx.MockTransport(down))
    assert not lst.server_read and not lst.server_stopped
    r = models.stop(lst, transport=httpx.MockTransport(down))
    assert not r.ok and "読めません" in r.message
    assert not any(p[0] == "stop" for p in lab.posts)


# ── /model stop ─────────────────────────────────────────────────────────

def test_model_stop_asks_then_stops_20261103(tmp_path, lab):
    ctx = _ctx(tmp_path, answers=["y"])
    slash.dispatch(ctx, "/model stop")
    out = _out(ctx)
    assert ctx.asked and "[y/N]" in ctx.asked[0] and "KAT-Coder-V2.5-Dev" in ctx.asked[0]
    assert lab.stops == [{"force": False}] and "katcoder を止めます" in out and "/model" in out
    assert "推論機へ送る依頼は届きません" in out                  # 送り先が推論機のまま
    assert ctx.client.backend == SERVER                              # 送り先は変えない


def test_model_stop_no_means_no_20261103(tmp_path, lab):
    ctx = _ctx(tmp_path, answers=["n"])
    slash.dispatch(ctx, "/model stop")
    assert "止めませんでした" in _out(ctx) and lab.stops == []
    ctx = _ctx(tmp_path, answers=[""])                               # Enter だけ＝いいえ
    slash.dispatch(ctx, "/model stop")
    assert lab.stops == []


def test_model_stop_yes_flag_and_non_interactive_skip_question_20261103(tmp_path, lab):
    ctx = _ctx(tmp_path, answers=["n"])
    slash.dispatch(ctx, "/model stop --yes")
    assert ctx.asked == [] and lab.stops == [{"force": False}]
    lab.server = "katcoder"
    ctx = _ctx(tmp_path, interactive=False)
    slash.dispatch(ctx, "/model stop")
    assert ctx.asked == [] and len(lab.stops) == 2


def test_model_stop_confirm_fn_is_used_in_tui_20261103(tmp_path, lab):
    ctx = _ctx(tmp_path, interactive=False)
    seen: list[str] = []
    ctx.confirm_fn = lambda q: (seen.append(q), False)[1]
    slash.dispatch(ctx, "/model stop")
    assert seen and lab.stops == [] and "止めませんでした" in _out(ctx)


def test_model_stop_busy_lease_and_force_20261103(tmp_path, lab):
    ctx = _ctx(tmp_path, answers=["y", "y", "y"])
    lab.stop_reply = (409, {"error": "生成の途中です", "busy": True})
    slash.dispatch(ctx, "/model stop")
    out = _out(ctx)
    assert "生成の途中です" in out and "/model stop --force" in out
    lab.stop_reply = (409, {"error": "GPU の仕事が動いています", "lease": True})
    slash.dispatch(ctx, "/model stop --force")
    out = _out(ctx)
    assert lab.stops[-1] == {"force": True} and "GPU の仕事が動いています" in out and "--force" not in out
    lab.stop_reply = (409, {"error": "ほかの切り替えが進んでいます"})
    slash.dispatch(ctx, "/model stop")
    assert "ほかの切り替えが進んでいます" in _out(ctx)


def test_model_stop_refusals_20261103(tmp_path, lab):
    ctx = _ctx(tmp_path, answers=["y"])
    ctx.running_rid = "r1"
    slash.dispatch(ctx, "/model stop")
    assert "走行中は止められません" in _out(ctx) and lab.stops == [] and ctx.asked == []
    ctx.running_rid = None
    slash.dispatch(ctx, "/model stop now")
    assert "分からない引数" in _out(ctx) and lab.stops == []
    lab.server = ""                                                  # もう止まっている＝聞かず・頼まず
    slash.dispatch(ctx, "/model stop")
    assert "もう止まっています（GPU は空き）" in _out(ctx) and lab.stops == [] and ctx.asked == []
    lab.server = "katcoder"
    lab.stoppable = False                                            # 止める口の無い古い モデル切り替えの口
    slash.dispatch(ctx, "/model stop")
    assert "止める口を持っていません" in _out(ctx) and lab.stops == [] and ctx.asked == []


def test_model_stop_from_claude_relay_targets_server_only_20261103(tmp_path, lab):
    ctx = _ctx(tmp_path, backend=RELAY, answers=["y"])
    slash.dispatch(ctx, "/model stop")
    out = _out(ctx)
    assert lab.stops == [{"force": False}] and "katcoder を止めます" in out
    assert "推論機へ送る依頼は届きません" not in out                 # 送り先は Claude のまま
    assert ctx.client.backend == RELAY
    assert not any(p[0] == "claude" for p in lab.posts)


# ── 止まっているときの一覧と起動 ──────────────────────────────────────────

def test_model_listing_says_stopped_20261103(tmp_path, lab):
    lab.server = ""
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/model")
    out = _out(ctx)
    assert "止まっています（GPU は空き）" in out and "推論機は止まっています（GPU は空き）" in out
    assert "← いま" not in out and "止まっている推論機に選ぶと起動します" in out
    assert "/model stop で推論機を止めて" not in out
    lab.server = "katcoder"
    slash.dispatch(ctx, "/model")
    out = _out(ctx)
    assert "止まっています" not in out and "/model stop で推論機を止めて GPU を空ける" in out


def test_model_listing_while_stopping_20261103(tmp_path, lab):
    lab.server = ""
    lab.sw = {"state": "running", "action": "stop", "target": None, "previous": "katcoder", "steps": []}
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/model")
    assert "推論機を止めている途中です" in _out(ctx)
    slash.dispatch(ctx, "/model qwen38x2")                          # 止めている途中は起こさない
    assert "止めている途中です" in _out(ctx) and lab.posts == []


def test_model_id_starts_a_stopped_server_20261103(tmp_path, lab):
    lab.server = ""
    ctx = _ctx(tmp_path, backend=RELAY)
    slash.dispatch(ctx, "/model katcoder")
    out = _out(ctx)
    assert lab.posts == [("server", "katcoder")]
    assert "KAT-Coder-V2.5-Dev を起動します" in out and "1〜3 分" in out
    assert ctx.client.backend == SERVER                              # 送り先も推論機へ


def test_help_and_completion_mention_stop_20261103(tmp_path, lab):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/help")
    out = _out(ctx)
    assert "/model [番号|名前|stop]" in out and "GPU を空ける" in out


# ── 全画面の server の箱 ────────────────────────────────────────────────

def test_tui_telemetry_forgets_model_when_stopped_20261103(lab):
    pytest.importorskip("textual")
    from laps.term import tui
    tel = tui.Telemetry()
    tel.add_models(lab.server_view())
    assert tel.models_seen and tel.model_unit == "katcoder" and tel.model_name
    lab.server = ""
    lab.sw = {"state": "running", "action": "stop", "target": None, "previous": "katcoder"}
    tel.add_models(lab.server_view())
    assert tel.model_unit == "" and tel.model_name == "" and tel.stopping and tel.switching == ""
    tel.add_models(lab.relay_view())                                  # 中継の一覧は「止まっている」と読まない
    assert not tel.models_seen


# ── 貸し出しが止めている推論機（2026-11-03 のレビュー）────────────────────────

def _leased(lab):
    lab.server = ""
    view = lab.server_view

    def with_lease():
        d = view()
        d["lease"] = {"running": [], "queued": [], "llm_stopped": "katcoder", "llm_stop_at": None}
        return d
    lab.server_view = with_lease


def test_leased_server_is_not_called_free_and_stop_still_asks_the_server_20261103(tmp_path, lab):
    _leased(lab)
    tr = httpx.MockTransport(lab.handler)
    lst = models.fetch({"claude_backend": RELAY}, SERVER, tr)
    assert lst.server_stopped and lst.lease_stopped == "katcoder"
    assert lst.stopped_label() == models.LEASED_LABEL
    ctx = _ctx(tmp_path, answers=["y"])
    slash.dispatch(ctx, "/model")
    out = _out(ctx)
    assert "貸し出し中" in out and "GPU は空き" not in out and "戻さない" in out
    # 手元で「もう止まっています」と答えず、向こうに頼む（向こうが戻す予定を消すか、仕事中なら断る）
    lab.stop_reply = (200, {"ok": True, "message": "推論機はもう止まっています（貸し出しの後に katcoder を戻す予定でしたが、戻しません）",
                            "already": True})
    slash.dispatch(ctx, "/model stop")
    out = _out(ctx)
    assert lab.stops == [{"force": False}] and ctx.asked and "戻しません" in out
    lab.stop_reply = (409, {"error": "推論機は貸し出し中です", "lease": True})
    slash.dispatch(ctx, "/model stop --yes")
    assert "推論機は貸し出し中です" in _out(ctx) and len(lab.stops) == 2


def test_tui_telemetry_reads_lease_20261103(lab):
    pytest.importorskip("textual")
    from laps.term import tui
    _leased(lab)
    tel = tui.Telemetry()
    tel.add_models(lab.server_view())
    assert tel.models_seen and tel.model_unit == "" and tel.lease_stopped == "katcoder"
