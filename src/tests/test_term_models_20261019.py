"""端末版のモデル選び（/model・/backend claude|server・全画面の F5）の試験（2026-10-19）。

オーナー「Windows や Linux 版のアプリから API のモデルを自由に切り替えられるようにしてほしい」。
Claude の中継（scripts/claude_relay.py の /modelctl/）と推論機の モデル切り替えの口（:8743）を httpx.MockTransport で
偽物にし、1 つの一覧に並ぶこと・選ぶと送り先も合わせて変わること・設定ファイルに残ることを見る。
"""
from __future__ import annotations

import asyncio
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

RELAY = "http://relay.test:8745/v1"
SERVER = "http://p40.test:8741/v1"
KNOWN = ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1")


class Lab:
    """中継と推論機の切り替え口の偽物。呼ばれた POST を数える。"""

    def __init__(self) -> None:
        self.claude = "claude-haiku-5-5"
        self.server = "katcoder"
        self.posts: list[tuple[str, str]] = []

    def relay_view(self) -> dict:
        return {"kind": "claude", "active": self.claude, "switch": {}, "gpu": None, "busy": False,
                "spent_usd": 1.234, "ordinary_usd": 100.0, "stop_usd": 190.0,
                "units": [{"id": m, "model": m, "file_ok": True, "active": m == self.claude,
                           "price": {"in": 0.1, "out": 0.5}} for m in KNOWN]}

    def server_view(self) -> dict:
        units = [("katcoder", "KAT-Coder-V2.5-Dev", 131072, True), ("qwen36x2", "Qwen3.6-35B-A3B", 131072, False),
                 ("qwen38x2", "Qwen3.8-27B", 65536, False), ("gone", "gone-model", 4096, False)]
        return {"active": self.server, "switch": {}, "gpu": {"power_limit": 200},
                "units": [{"id": u, "model": m, "ctx": c, "vision": v, "mtp": u == "katcoder",
                           "file_ok": u != "gone", "active": u == self.server} for u, m, c, v in units]}

    def handler(self, req: httpx.Request) -> httpx.Response:
        hp = (req.url.host, req.url.port)
        body = json.loads(req.content or b"{}") if req.method == "POST" else {}
        if hp == ("relay.test", 8745):
            if req.method == "GET" and req.url.path == "/modelctl/v1/models":
                return httpx.Response(200, json=self.relay_view())
            if req.method == "POST" and req.url.path == "/modelctl/v1/switch":
                self.posts.append(("claude", body["id"]))
                prev, self.claude = self.claude, body["id"]
                return httpx.Response(202, json={"ok": True, "message": f"{prev} → {self.claude} に切り替えました（次の依頼から）"})
        if hp == ("p40.test", 8743):
            if req.method == "GET" and req.url.path == "/v1/models":
                return httpx.Response(200, json=self.server_view())
            if req.method == "POST" and req.url.path == "/v1/switch":
                self.posts.append(("server", body["id"]))
                return httpx.Response(202, json={"ok": True, "message": "切り替えを始めました"})
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def lab(monkeypatch):
    lb = Lab()
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", httpx.MockTransport(lb.handler))
    monkeypatch.delenv("LAPS_BACKEND", raising=False)
    return lb


def _ctx(tmp_path: Path, backend: str = SERVER, **cfg) -> AppContext:
    c = Console(record=True, width=140, force_terminal=False, file=io.StringIO())
    cl = FakeClient()
    cl.backend = backend
    path = tmp_path / "config.toml"
    config.save({"backend": backend, "policy": "trusted", "wave": "calm"}, path)
    return AppContext(console=c, client=cl, renderer=Renderer(c), cfg={"claude_backend": RELAY, **cfg},
                      install_id_path=tmp_path / "install_id", config_path=path)


def _out(ctx: AppContext) -> str:
    return ctx.console.export_text(clear=True)


def _file(ctx: AppContext) -> dict:
    import tomllib
    return tomllib.loads(Path(ctx.config_path).read_text(encoding="utf-8"))


# ── 一覧と当て方 ─────────────────────────────────────────────────────────

def test_is_claude_and_server_url_20261019():
    assert models.is_claude(RELAY, RELAY) and models.is_claude("http://192.0.2.9:8745/v1")
    assert not models.is_claude(SERVER, RELAY) and not models.is_claude("", RELAY)
    assert models.server_url({"claude_backend": RELAY}, SERVER) == SERVER
    assert models.server_url({"claude_backend": RELAY, "server_backend": "http://x:1/v1"}, RELAY) == "http://x:1/v1"
    assert models.server_url({"claude_backend": RELAY}, RELAY) == config.DEFAULTS["backend"]
    assert models.pretty("claude-sonnet-5-5") == "Claude Sonnet 5.5" and models.pretty("katcoder") == "katcoder"


def test_fetch_lists_claude_then_server_and_pick_20261019(lab):
    lst = models.fetch({"claude_backend": RELAY}, SERVER, httpx.MockTransport(lab.handler))
    assert lst.where == "server" and not lst.notes
    assert [c.kind for c in lst.choices] == ["claude"] * 4 + ["server"] * 4
    assert lst.current().id == "katcoder" and "$1.23" in lst.spent and "$190 で止まる" in lst.spent
    assert lst.choices[-1].ok is False and "ファイルが無い" in lst.choices[-1].detail
    assert models.pick(lst, "2")[0].id == "claude-sonnet-5-5"
    assert models.pick(lst, "sonnet")[0].id == "claude-sonnet-5-5"
    assert models.pick(lst, "Claude Opus 5.5")[0].id == "claude-opus-5-5"
    assert models.pick(lst, "katcoder")[0].kind == "server"
    c, cands = models.pick(lst, "qwen")
    assert c is None and {x.id for x in cands} == {"qwen36x2", "qwen38x2"}
    assert models.pick(lst, "99") == (None, [])


def test_fetch_says_why_when_one_side_is_down_20261019(lab):
    def half(req):
        if req.url.port == 8743:
            raise httpx.ConnectError("refused", request=req)
        return lab.handler(req)
    lst = models.fetch({"claude_backend": RELAY}, SERVER, httpx.MockTransport(half))
    assert [c.kind for c in lst.choices] == ["claude"] * 4
    assert len(lst.notes) == 1 and "推論機の切り替え口" in lst.notes[0] and "ConnectError" in lst.notes[0]


# ── /model ───────────────────────────────────────────────────────────

def test_model_shows_both_groups_with_marks_20261019(tmp_path, lab):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/model")
    out = _out(ctx)
    assert "Claude API" in out and "今月 $1.23" in out and "推論機" in out
    assert "Claude Sonnet 5.5" in out and "KAT-Coder-V2.5-Dev" in out
    assert "← 中継で選ばれている" in out and "← いま" in out
    assert "/model <番号か名前>" in out


def test_model_claude_moves_backend_and_remembers_server_20261019(tmp_path, lab):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/model sonnet")
    out = _out(ctx)
    assert lab.posts == [("claude", "claude-sonnet-5-5")]
    assert ctx.client.backend == RELAY and "送り先を Claude API" in out and "費用は共有の API の枠" in out
    f = _file(ctx)
    assert f["backend"] == RELAY and f["server_backend"] == SERVER
    assert f["policy"] == "trusted" and f["wave"] == "calm"            # ほかの鍵はそのまま
    assert "model" not in f                                             # 既定で埋めない（既定を焼き付けない）
    # 推論機に載っている物を選ぶ＝推論機には頼まず、送り先だけ戻る（覚えておいた推論機へ）
    slash.dispatch(ctx, "/model katcoder")
    out = _out(ctx)
    assert lab.posts == [("claude", "claude-sonnet-5-5")]
    assert ctx.client.backend == SERVER and "もう選ばれています" in out and _file(ctx)["backend"] == SERVER


def test_model_server_switch_starts_and_keeps_backend_20261019(tmp_path, lab):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/model qwen38x2")
    out = _out(ctx)
    assert lab.posts == [("server", "qwen38x2")] and ctx.client.backend == SERVER
    assert "1〜3 分" in out
    slash.dispatch(ctx, "/model gone")
    assert "選べません" in _out(ctx) and len(lab.posts) == 1


def test_model_refuses_while_running_and_keeps_old_name_behaviour_20261019(tmp_path, lab):
    ctx = _ctx(tmp_path)
    ctx.running_rid = "r1"
    slash.dispatch(ctx, "/model sonnet")
    assert "走行中は切り替えられません" in _out(ctx) and lab.posts == [] and ctx.client.backend == SERVER
    ctx.running_rid = None
    slash.dispatch(ctx, "/model 42")
    assert "番号 42 は一覧にありません" in _out(ctx)
    slash.dispatch(ctx, "/model blackwall-think-x")                    # 一覧に無い名前＝送るモデル名だけ変える
    assert ctx.client.settings["model"] == "blackwall-think-x" and lab.posts == []


def test_backend_claude_and_server_words_20261019(tmp_path, lab):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/backend claude")
    assert ctx.client.backend == RELAY and "Claude API" in _out(ctx)
    assert _file(ctx)["server_backend"] == SERVER and lab.posts == []
    slash.dispatch(ctx, "/backend")
    assert "Claude API の中継" in _out(ctx)
    slash.dispatch(ctx, "/backend server")
    assert ctx.client.backend == SERVER and _file(ctx)["backend"] == SERVER


def test_update_file_keeps_unknown_keys_20261019(tmp_path):
    p = tmp_path / "c.toml"
    config.save({"backend": SERVER, "config_version": 2, "mine": "x"}, p)
    config.update_file({"backend": RELAY}, p)
    import tomllib
    got = tomllib.loads(p.read_text(encoding="utf-8"))
    assert got == {"backend": RELAY, "config_version": 2, "mine": "x"}


# ── 全画面（F5 と server の箱）──────────────────────────────────────────

def test_poller_claude_round_and_box_text_20261019(lab):
    pytest.importorskip("textual")
    from laps.term import tui
    tel = tui.Telemetry()
    p = tui.Poller(tel, lambda: RELAY, lambda: RELAY)
    with httpx.Client(transport=httpx.MockTransport(lab.handler)) as h:
        p._claude_round(h, "http://relay.test:8745", 100.0)
    assert tel.claude and tel.srv_ok and tel.model_name == "claude-haiku-5-5" and "$1.23" in tel.spent


def test_f5_modal_picks_a_choice_20261019(lab):
    pytest.importorskip("textual")
    from textual.app import App
    from laps.term import tui
    lst = models.fetch({"claude_backend": RELAY}, SERVER, httpx.MockTransport(lab.handler))
    got: list = []

    class _A(App):
        def on_mount(self):
            self.push_screen(tui.ModelPickModal(lst), got.append)

    async def main():
        app = _A()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            ol = app.screen.query_one("#mp", tui.OptionList)
            # 開いたときは、いま使っている物（推論機の katcoder）に印が合っている
            assert ol.get_option_at_index(ol.highlighted).id == f"c{lst.choices.index(lst.current())}"
            ol.highlighted = ol.get_option_index("c1")
            await pilot.press("enter")
            await pilot.pause()
    asyncio.run(main())
    assert got and got[0].id == "claude-sonnet-5-5"
