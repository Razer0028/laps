"""`/` コマンド（laps/term/slash.py）の試験。

偽の ctx（Console(record=True)・偽 client・本物の Renderer）で各コマンドの出力を見る。
HTTP（/status の疎通・/sendlog）は httpx.MockTransport を差して、送った本文の形を見る。
"""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from prompt_toolkit.document import Document
from rich.console import Console

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))

from laps.term import slash  # noqa: E402
from laps.term.app import AppContext, record_event  # noqa: E402
from laps.term.render import Renderer  # noqa: E402
from tests.test_term_app import FakeClient  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """試験が本物の線に出ないように、既定は「繋がらない」transport。"""
    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", httpx.MockTransport(boom))


def _ctx(tmp_path: Path, client: FakeClient | None = None, **kw) -> AppContext:
    c = Console(record=True, width=100, force_terminal=False, file=io.StringIO())
    cl = client or FakeClient()
    kw.setdefault("cfg", {"telemetry": "http://127.0.0.1:1/ingest"})
    return AppContext(console=c, client=cl, renderer=Renderer(c),
                      install_id_path=tmp_path / "cfg" / "install_id", **kw)


def _out(ctx: AppContext) -> str:
    return ctx.console.export_text(clear=True)


# --------------------------------------------------------------------------
# 表と dispatch
# --------------------------------------------------------------------------

def test_all_design_commands_exist():
    want = {"help", "status", "sendlog", "model", "backend", "workspace", "policy", "ethics",
            "tools", "clear", "resume", "expand", "verbose", "cancel", "exit", "quit"}
    assert want <= set(slash.COMMANDS)
    for c in slash.COMMANDS.values():
        assert c.help and callable(c.handler)


def test_help_lists_every_command(tmp_path):
    """★隠しコマンド（/quit）以外は必ず載る。群に足し忘れても「その他」に落ちる。"""
    ctx = _ctx(tmp_path)
    assert slash.dispatch(ctx, "/help") is True
    out = _out(ctx)
    for name, c in slash.COMMANDS.items():
        if c.hidden:
            assert f"/{name} " not in out          # /quit は一覧に出さない（受け付けはする）
        else:
            assert f"/{name}" in out


def test_dispatch_unknown_and_prefix(tmp_path):
    ctx = _ctx(tmp_path)
    assert slash.dispatch(ctx, "/nope") is True
    assert "知らないコマンド" in _out(ctx)
    slash.dispatch(ctx, "/ver")            # 前方一致で 1 つ → /verbose
    assert "verbose on" in _out(ctx)
    slash.dispatch(ctx, "/e")              # ethics / exit / expand で曖昧
    out = _out(ctx)
    assert "候補" in out and "/ethics" in out
    assert slash.dispatch(ctx, "hello") is False
    assert slash.dispatch(ctx, "/") is True   # "/" だけで一覧


def test_handler_exception_is_reported_not_raised(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)

    def bad(ctx, args):
        raise ValueError("boom")
    monkeypatch.setitem(slash.COMMANDS, "boom", slash.Command("boom", "x", bad))
    slash.dispatch(ctx, "/boom")
    assert "/boom が失敗" in _out(ctx)


# --------------------------------------------------------------------------
# /status
# --------------------------------------------------------------------------

def test_status_shows_settings_and_unreachable(tmp_path):
    cl = FakeClient()
    cl.backend = "http://127.0.0.1:9/v1"
    cl.model = "blackwall-coder"
    cl.ethics = "direct"
    ctx = _ctx(tmp_path, cl, session="sess-9")
    ctx.stats.update({"turns": 2, "tools": 5, "secs": 12.0, "tokens": 345})
    slash.dispatch(ctx, "/status")
    out = _out(ctx)
    assert "http://127.0.0.1:9/v1" in out
    assert "blackwall-coder" in out
    assert "direct" in out and "workspace" in out
    assert "sess-9" in out
    assert "ターン 2" in out and "道具 5" in out and "345 トークン" in out
    assert "届きません" in out
    # ★並びは「いま変わるもの」から。版番号は最後（起動から一度も変わらない）。
    keys = [ln.split()[0] for ln in out.splitlines() if ln.strip()]
    assert keys[0] == "reach" and keys.index("policy") < keys.index("version")


def test_status_reachable_shows_health_and_slots(tmp_path, monkeypatch):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/slots":
            return httpx.Response(200, json=[{"id": 0, "is_processing": True},
                                             {"id": 1, "is_processing": False}])
        return httpx.Response(404)
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    cl = FakeClient()
    cl.backend = "http://192.0.2.1:8741/v1"
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, "/status")
    out = _out(ctx)
    assert "health ok" in out and "slots 2" in out and "処理中 1" in out
    assert "届きません" not in out
    # /v1 を外した根を叩く
    assert "http://192.0.2.1:8741/health" in seen and "http://192.0.2.1:8741/slots" in seen


def test_backend_root_strips_v1():
    assert slash.backend_root("http://h:1/v1") == "http://h:1"
    assert slash.backend_root("http://h:1/v1/") == "http://h:1"
    assert slash.backend_root("http://h:1") == "http://h:1"
    assert slash.backend_root("http://h:1/api/v1") == "http://h:1/api"


def test_probe_handles_http_error_and_missing_slots(monkeypatch):
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(503, json={"status": "loading"})
        return httpx.Response(501)
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    pr = slash.probe_backend("http://h/v1")
    assert pr["ok"] is False and "503" in pr["error"]

    def ok_no_slots(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(501, text="disabled")
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", httpx.MockTransport(ok_no_slots))
    pr = slash.probe_backend("http://h/v1")
    assert pr["ok"] is True and pr["slots"] is None


# --------------------------------------------------------------------------
# 切替
# --------------------------------------------------------------------------

def test_model_and_backend_update_settings_without_restart(tmp_path):
    cl = FakeClient()
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, "/model")
    assert "test-model" in _out(ctx)
    slash.dispatch(ctx, "/model qwen-x")
    assert cl.settings["model"] == "qwen-x" and cl.restarts == []
    assert "次の send" in _out(ctx)
    slash.dispatch(ctx, "/backend nonsense")
    assert "URL の形" in _out(ctx) and cl.backend == "http://127.0.0.1:9/v1"
    slash.dispatch(ctx, "/backend http://10.0.0.5:8080/v1")
    assert cl.settings["backend"] == "http://10.0.0.5:8080/v1" and cl.restarts == []


def test_workspace_sets_absolute_path(tmp_path):
    cl = FakeClient()
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, f"/workspace {tmp_path}/proj")
    assert cl.workspace == str((tmp_path / "proj").resolve())
    assert "まだ無い" in _out(ctx)
    slash.dispatch(ctx, "/workspace")
    assert str(tmp_path / "proj") in _out(ctx)


@pytest.mark.parametrize("key,good,bad", [("policy", "trusted", "god"), ("ethics", "raw", "loose")])
def test_policy_and_ethics_validate_and_restart(tmp_path, key, good, bad):
    cl = FakeClient()
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, f"/{key}")
    assert getattr(cl, key) in _out(ctx)
    slash.dispatch(ctx, f"/{key} {bad}")
    assert "どれか" in _out(ctx) and cl.restarts == []
    slash.dispatch(ctx, f"/{key} {good}")
    assert cl.restarts == [{key: good}]
    assert cl.settings[key] == good
    assert "子を再起動" in _out(ctx)


def test_policy_refused_while_running_and_restart_failure(tmp_path):
    cl = FakeClient()
    ctx = _ctx(tmp_path, cl, running_rid="r1")
    slash.dispatch(ctx, "/policy readonly")
    assert "走行中" in _out(ctx) and cl.restarts == []
    ctx.running_rid = None
    cl.restart_fail = True
    slash.dispatch(ctx, "/policy readonly")
    assert "再起動に失敗" in _out(ctx)


def test_tools_lists_ready_names(tmp_path):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/tools")
    out = _out(ctx)
    assert "Bash" in out and "Read" in out and "2 件" in out


# --------------------------------------------------------------------------
# 会話の状態
# --------------------------------------------------------------------------

def test_clear_resets_session_transcript_and_events(tmp_path):
    ctx = _ctx(tmp_path, session="s1")
    ctx.renderer.transcript.append({"role": "user", "content": "x"})
    record_event(ctx, {"type": "token", "id": "r1", "text": "t"})
    slash.dispatch(ctx, "/clear")
    assert ctx.session is None and ctx.renderer.transcript == [] and len(ctx.events) == 0
    assert "新しい会話" in _out(ctx)


def test_verbose_toggles_and_expand_without_tools(tmp_path):
    ctx = _ctx(tmp_path)
    assert ctx.renderer.verbose is False
    slash.dispatch(ctx, "/verbose")
    assert ctx.renderer.verbose is True
    slash.dispatch(ctx, "/verbose")
    assert ctx.renderer.verbose is False
    slash.dispatch(ctx, "/expand")
    # ★空状態には必ず「次の一手」がある（文言そのものは render の担当）
    out = _out(ctx)
    assert "道具" in out and "→" in out


def test_cancel_when_idle_and_running(tmp_path):
    cl = FakeClient()
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, "/cancel")
    assert "走行中ではありません" in _out(ctx) and cl.cancels == []
    ctx.running_rid = "r7"
    slash.dispatch(ctx, "/cancel")
    assert cl.cancels == ["r7"]


def test_exit_and_quit_set_flag(tmp_path):
    for name in ("exit", "quit"):
        ctx = _ctx(tmp_path)
        slash.dispatch(ctx, f"/{name}")
        assert ctx.exit_requested is True


# --------------------------------------------------------------------------
# /resume
# --------------------------------------------------------------------------

def _make_session(home: Path, sid: str, user_text: str, age: float) -> None:
    d = home / "sessions" / sid
    d.mkdir(parents=True)
    mp = d / "messages.jsonl"
    with mp.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "古い発言"}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "返事"}) + "\n")
        f.write(json.dumps({"role": "user", "content": user_text}, ensure_ascii=False) + "\n")
    t = time.time() - age
    import os
    os.utime(mp, (t, t))


def test_resume_lists_newest_first_with_last_user_line(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_SESSIONS", raising=False)
    monkeypatch.delenv("LAPS_HOME", raising=False)
    home = tmp_path / "home"
    _make_session(home, "20260101_000000_aaaaaa", "いちばん古い", 3000)
    _make_session(home, "20260102_000000_bbbbbb", "真ん中 " + "長" * 100, 2000)
    _make_session(home, "20260103_000000_cccccc", "いちばん新しい", 1000)
    for i in range(12):   # 10 件に絞る
        _make_session(home, f"202602{i:02d}_x", f"埋め {i}", 10 + i)
    cl = FakeClient()
    cl.home = str(home)
    ctx = _ctx(tmp_path, cl, session="20260203_x")
    slash.dispatch(ctx, "/resume")
    out = _out(ctx)
    lines = [ln for ln in out.splitlines() if "2026" in ln]
    assert len(lines) == 10
    assert "埋め 0" in lines[0]                       # 新しい順
    assert "いちばん古い" not in out                     # 11 件目以降は出ない
    # LAPS_HOME は元に戻っている
    import os
    assert "LAPS_HOME" not in os.environ
    rows = slash.list_sessions(str(home), limit=20)
    mid = [r for r in rows if r["id"].endswith("bbbbbb")][0]
    assert mid["last"].startswith("真ん中") and mid["last"].endswith("…")
    assert len(mid["last"]) == slash.RESUME_SNIPPET + 1
    cur = [ln for ln in lines if "20260203_x" in ln][0]
    assert cur.lstrip().startswith("*")              # いまのセッションに印
    assert sum(1 for ln in lines if ln.lstrip().startswith("*")) == 1


def test_resume_with_id_sets_session(tmp_path):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/resume abc123")
    assert ctx.session == "abc123"
    assert "abc123" in _out(ctx)


def test_resume_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_SESSIONS", raising=False)
    cl = FakeClient()
    cl.home = str(tmp_path / "empty-home")
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, "/resume")
    assert "まだありません" in _out(ctx)


# --------------------------------------------------------------------------
# /sendlog
# --------------------------------------------------------------------------

def _capture_transport(responses: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=responses or {"accepted": 1, "duplicate": 0, "rejected": 0,
                                                       "reasons": {}})
    return seen, httpx.MockTransport(handler)


def test_sendlog_posts_one_ndjson_line_with_design_shape(tmp_path, monkeypatch):
    seen, tr = _capture_transport()
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", tr)
    ctx = _ctx(tmp_path, session="sess-1")
    ctx.last_input = "最後の入力"
    ctx.last_outcome = "accepted"
    ctx.renderer.transcript.extend([{"role": "user", "content": "最後の入力"},
                                    {"role": "assistant", "content": "返事"}])
    for ev in ({"type": "token", "id": "r1", "text": "返"}, {"type": "token", "id": "r1", "text": "事"},
               {"type": "done", "id": "r1", "outcome": "accepted"}):
        record_event(ctx, ev)
    slash.dispatch(ctx, "/sendlog")
    assert len(seen) == 1
    req = seen[0]
    assert str(req.url) == "http://127.0.0.1:1/ingest"
    assert req.headers["content-type"] == "application/x-ndjson"
    body = req.content.decode("utf-8")
    assert body.endswith("\n") and body.count("\n") == 1
    rec = json.loads(body)
    assert rec["rec"] == 1 and rec["kind"] == "sendlog"
    assert rec["id"].startswith("term-sess-1-")
    assert rec["ts"] and "T" in rec["ts"]
    assert rec["client"]["app"] == "blackwall-term" and rec["client"]["version"]
    assert rec["client"]["host_label"] and rec["client"]["install_id"]
    assert rec["input"] == {"text": "最後の入力"}
    assert rec["transcript"][1]["content"] == "返事"
    assert [e["type"] for e in rec["events"]] == ["token", "done"]
    assert rec["events"][0]["text"] == "返事"
    assert rec["stop"] == {"reason": "accepted"}
    # install_id は作られて保持される（2 度目も同じ値）
    p = tmp_path / "cfg" / "install_id"
    assert p.read_text(encoding="utf-8").strip() == rec["client"]["install_id"]
    assert slash.install_id(p) == rec["client"]["install_id"]
    out = _out(ctx)
    assert "accepted" in out and "HTTP 200" in out


def test_sendlog_shows_rejected_and_reasons(tmp_path, monkeypatch):
    seen, tr = _capture_transport({"accepted": 0, "duplicate": 1, "rejected": 1,
                                   "reasons": {"line_too_big": 1}})
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", tr)
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/sendlog")
    out = _out(ctx)
    assert "duplicate  1" in out.replace("  ", "  ") or "duplicate" in out
    assert "line_too_big" in out


def test_sendlog_unreachable_is_red_line(tmp_path):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/sendlog")
    assert "届きません" in _out(ctx)


def test_sendlog_without_url(tmp_path):
    ctx = _ctx(tmp_path, cfg={"telemetry": ""})
    slash.dispatch(ctx, "/sendlog")
    assert "送り先" in _out(ctx)


def test_sendlog_trims_events_to_limit(tmp_path, monkeypatch):
    seen, tr = _capture_transport()
    monkeypatch.setattr(slash, "HTTP_TRANSPORT", tr)
    ctx = _ctx(tmp_path, session="s")
    for i in range(60):
        record_event(ctx, {"type": "tool_result", "id": "r1", "call": i, "result": "x" * 10_000})
    record_event(ctx, {"type": "done", "id": "r1", "outcome": "accepted"})
    rec, body = slash.build_sendlog(ctx)
    assert len(body) <= slash.SENDLOG_MAX_BYTES
    assert rec["events"][-1]["type"] == "done"          # 新しい側が残る
    assert rec["events_dropped"] > 0
    slash.dispatch(ctx, "/sendlog")
    assert len(seen[0].content) <= slash.SENDLOG_MAX_BYTES
    assert "落とした" in _out(ctx)


# --------------------------------------------------------------------------
# 補完
# --------------------------------------------------------------------------

def _complete(comp: slash.SlashCompleter, text: str) -> list[str]:
    return [c.text for c in comp.get_completions(Document(text, len(text)), None)]


def test_completer_commands_and_args(tmp_path):
    ctx = _ctx(tmp_path)
    comp = slash.SlashCompleter(ctx)
    assert set(_complete(comp, "/")) == set(slash.COMMANDS)
    assert _complete(comp, "/st") == ["status"]
    assert _complete(comp, "/policy ") == list(slash.POLICIES)
    assert _complete(comp, "/ethics r") == ["raw"]
    assert _complete(comp, "/model ") == ["test-model", "stop"]   # stop は推論機を止める（2026-11-03）
    assert _complete(comp, "hello") == []
    assert _complete(comp, "/policy trusted x") == []


# --------------------------------------------------------------------------
# /resume の抜粋は「利用者が打った文」（serve の包みと締めの差し戻しを出さない・検証で発見）
# --------------------------------------------------------------------------

def test_resume_snippet_unwraps_serve_prompt_and_skips_pushback(tmp_path, monkeypatch):
    monkeypatch.delenv("LAPS_SESSIONS", raising=False)
    monkeypatch.delenv("LAPS_HOME", raising=False)
    home = tmp_path / "home"
    d = home / "sessions" / "20260823_100943_c66294"
    d.mkdir(parents=True)
    wrapped = ("[アプリからの指示]\n[この実行での事実]\n作業ディレクトリ: /tmp/x\nOS: Linux\n"
               "使える道具: Bash, Read\n\n\n[利用者]\nさっき書いたファイルの名前は？ 一言で")
    pushback = "依頼は**物を作ること**を求めていますが、この回ではファイルが1つも作られず…"
    with (d / "messages.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": wrapped}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "a.txt"}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"role": "user", "content": pushback}, ensure_ascii=False) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "作りました"}, ensure_ascii=False) + "\n")
    rows = slash.list_sessions(str(home))
    assert rows[0]["last"] == "さっき書いたファイルの名前は？ 一言で"
    # 印が 1 つも無い旧い記録（CLI 由来）は最後の user 行をそのまま
    d2 = home / "sessions" / "20260101_000000_legacy"
    d2.mkdir(parents=True)
    (d2 / "messages.jsonl").write_text(
        json.dumps({"role": "user", "content": "これは動作確認です"}, ensure_ascii=False) + "\n", encoding="utf-8")
    rows = {r["id"]: r for r in slash.list_sessions(str(home))}
    assert rows["20260101_000000_legacy"]["last"] == "これは動作確認です"


def test_resume_rows_do_not_wrap(tmp_path, monkeypatch):
    """日本語 60 字は 120 列＝幅 100 では折り返していた。1 行 1 セッションで切る。"""
    monkeypatch.delenv("LAPS_SESSIONS", raising=False)
    monkeypatch.delenv("LAPS_HOME", raising=False)
    home = tmp_path / "home"
    _make_session(home, "20260103_000000_cccccc", "長" * 80, 1000)
    cl = FakeClient()
    cl.home = str(home)
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, "/resume")
    lines = [ln for ln in _out(ctx).splitlines() if ln.strip()]
    assert len(lines) == 2                    # 1 行のセッション ＋ 案内
    assert "20260103_000000_cccccc" in lines[0] and lines[0].rstrip().endswith("…")


# --------------------------------------------------------------------------
# 表示幅の物差し（disp_width / _pad）— SPEC 項目 12・13・23
# --------------------------------------------------------------------------

def test_disp_width_counts_fullwidth_and_ambiguous_as_two():
    assert slash.disp_width("abc") == 3
    assert slash.disp_width("日本語") == 6
    assert slash.disp_width("·") == 2          # East Asian Ambiguous は安全側の 2
    assert slash.disp_width("") == 0


def test_kv_aligns_by_display_width(tmp_path):
    """★`f"{key:<10}"` は日本語の鍵で 2 桁ずつずれる。"""
    ctx = _ctx(tmp_path)
    slash._kv(ctx, "reach", "A")
    slash._kv(ctx, "workspace", "B")
    cols = [ln.index(v) for ln, v in zip(_out(ctx).splitlines(), ("A", "B"))]
    assert cols[0] == cols[1]


# --------------------------------------------------------------------------
# /help — SPEC 項目 12
# --------------------------------------------------------------------------

def _fits(out: str, width: int = 80) -> list[str]:
    return [ln for ln in out.splitlines() if slash.disp_width(ln) > width]


def test_help_is_grouped_and_fits_80(tmp_path):
    """★幅 80 で 4 行が桁 0 に落ちて 2 列表が崩れていた（実測）。"""
    c = Console(record=True, width=80, force_terminal=False, file=io.StringIO())
    ctx = _ctx(tmp_path)
    ctx.console = c
    slash.dispatch(ctx, "/help")
    out = c.export_text(clear=True)
    assert _fits(out) == []
    for title in ("走行中", "会話", "接続と設定", "その他"):
        assert f"\n{title}\n" in "\n" + out
    assert "Ctrl-R" in out and "blackwall --help" in out       # 起動時の引数への導線


def test_help_columns_align_and_long_usage_goes_two_lines(tmp_path):
    c = Console(record=True, width=80, force_terminal=False, file=io.StringIO())
    ctx = _ctx(tmp_path)
    ctx.console = c
    slash.dispatch(ctx, "/help")
    lines = c.export_text(clear=True).splitlines()
    # ★桁は**表示幅**で見る（`/model [名前]` は全角を含むので文字数では一致しない）
    starts = {slash.disp_width(ln[:ln.index(h)]) for ln in lines
              for h in ("モデルの表示", "セッションの一覧", "本体が告げた") if h in ln}
    assert len(starts) == 1              # ヘルプ列の開始桁が全角の有無に関わらず一致
    i = next(i for i, ln in enumerate(lines) if ln.strip().startswith("/policy"))
    assert "実行ゲート" not in lines[i] and "実行ゲート" in lines[i + 1]


def test_help_hides_quit_but_still_accepts_it(tmp_path):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/help")
    assert "/quit" not in _out(ctx)
    slash.dispatch(ctx, "/quit")
    assert ctx.exit_requested is True


# --------------------------------------------------------------------------
# /status — SPEC 項目 13
# --------------------------------------------------------------------------

def test_status_marks_dangerous_settings(tmp_path, monkeypatch):
    """trusted / raw は「いま何が起きうるか」。値だけでは分からないので語を添えて赤にする。

    ★色は**選んだスタイル**で見る（描いた ANSI では見ない）: Rich は最初に使った色システムを
      焼き付けるので、試験を全部まとめて走らせると truecolor が 256 色に落ちる（実測）。
    """
    from laps.term import theme as T
    rows: list[tuple[str, str, str]] = []
    monkeypatch.setattr(slash, "_kv",
                        lambda ctx, k, v, style=T.S_TEXT: rows.append((k, str(v), str(style))))
    cl = FakeClient()
    cl.policy, cl.ethics = "trusted", "raw"
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, "/status")
    got = {k: (v, st) for k, v, st in rows}
    assert got["policy"] == ("trusted（すべて実行できます）", T.S_RED)
    assert got["ethics"] == ("raw（刻みなし）", T.S_RED)

    rows.clear()
    cl.policy, cl.ethics = "readonly", "strict"
    slash.dispatch(ctx, "/status")
    got = {k: (v, st) for k, v, st in rows}
    assert got["policy"] == ("readonly", T.S_TEXT) and got["ethics"] == ("strict", T.S_TEXT)


def test_status_fits_80(tmp_path):
    c = Console(record=True, width=80, force_terminal=False, file=io.StringIO())
    ctx = _ctx(tmp_path, session="s" * 20)
    ctx.console = c
    slash.dispatch(ctx, "/status")
    assert _fits(c.export_text(clear=True)) == []


# --------------------------------------------------------------------------
# /tools — SPEC 項目 23
# --------------------------------------------------------------------------

def test_tools_lays_out_columns_within_width(tmp_path):
    """★1 本の長い Text は桁 0 に落ちて折り返す（段が消える）。"""
    cl = FakeClient()
    cl.status = dict(cl.status, tools=[f"Tool{i:02d}" for i in range(23)])
    c = Console(record=True, width=80, force_terminal=False, file=io.StringIO())
    ctx = _ctx(tmp_path, cl)
    ctx.console = c
    slash.dispatch(ctx, "/tools")
    out = c.export_text(clear=True)
    assert _fits(out) == []
    for i in range(23):
        assert f"Tool{i:02d}" in out
    assert all(ln.startswith("  ") for ln in out.splitlines()[1:] if ln.strip())


def test_tools_empty_has_next_step(tmp_path):
    cl = FakeClient()
    cl.status = dict(cl.status, tools=[])
    ctx = _ctx(tmp_path, cl)
    slash.dispatch(ctx, "/tools")
    out = _out(ctx)
    assert "→" in out and "/status" in out


# --------------------------------------------------------------------------
# /clear と /expand — SPEC 項目 20・22
# --------------------------------------------------------------------------

def test_clear_forgets_the_tool_bundle_and_reprints_the_header(tmp_path):
    """★`/clear` の直後の `/expand` が前の会話のコマンドと出力を全文で出していた（実測）。"""
    ctx = _ctx(tmp_path, session="s1")
    ctx.renderer.on_event({"type": "tool_call", "name": "Bash", "call": "c1",
                           "input": {"command": "cat /etc/shadow"}})
    ctx.renderer.on_event({"type": "tool_result", "call": "c1", "text": "秘密"})
    _out(ctx)
    slash.dispatch(ctx, "/clear")
    out = _out(ctx)
    assert "新しい会話" in out and "/resume" in out
    assert "L.A.P.S" in out                       # 銘の再掲（送り先・作業場・実行ゲート）
    slash.dispatch(ctx, "/expand")
    assert "cat /etc/shadow" not in _out(ctx)


def test_expand_rejects_arguments_out_loud(tmp_path):
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/expand ぜんぶ")
    assert "引数はありません" in _out(ctx)


# --------------------------------------------------------------------------
# /resume の一覧と補完 — SPEC 項目 24
# --------------------------------------------------------------------------

def test_last_user_text_reads_only_the_tail(tmp_path):
    """★全文走査は 1 打鍵あたり 50ms（この機体・1,201 件）。末尾だけ読む。"""
    p = tmp_path / "messages.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for i in range(20000):
            f.write(json.dumps({"role": "user", "content": f"[利用者]\n古い {i}"},
                               ensure_ascii=False) + "\n")
        f.write(json.dumps({"role": "user", "content": "[利用者]\n最後の発言"},
                           ensure_ascii=False) + "\n")
    assert p.stat().st_size > slash.TAIL_BYTES
    assert slash._last_user_text(p) == "最後の発言"
    lines, whole = slash._tail_lines(p)
    assert whole is False and len(lines) < 20000


def test_last_user_text_falls_back_to_full_scan(tmp_path):
    """末尾に user 行が 1 つも無いときは全文に落ちる（結果は全文走査と同じ）。"""
    p = tmp_path / "messages.jsonl"
    with p.open("w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "[利用者]\nこれだけ"},
                           ensure_ascii=False) + "\n")
        for i in range(20000):
            f.write(json.dumps({"role": "assistant", "content": f"返事 {i}"},
                               ensure_ascii=False) + "\n")
    assert slash._last_user_text(p) == "これだけ"


def test_completer_caches_the_session_list(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(slash, "list_sessions",
                        lambda home, **kw: calls.append(1) or [{"id": "s-abc"}])
    comp = slash.SlashCompleter(_ctx(tmp_path))
    for _ in range(5):
        assert _complete(comp, "/resume s") == ["s-abc"]
    assert len(calls) == 1                     # 打鍵ごとに記録を読み直さない


# --------------------------------------------------------------------------
# 合流（2026-08-29）
# --------------------------------------------------------------------------

def test_clear_uses_the_public_forget_tools(tmp_path, monkeypatch):
    """`/clear` は Renderer の**公開の口**（forget_tools）を呼ぶ。

    ★合流前は `renderer._bundle` を外から空にしていた。動きはするが、renderer 側が
      内部の持ち方を変えた日に黙って効かなくなる（`_forgot` が立たず、その後の
      `/expand` が「まだ使われていません」と嘘をつく）。呼ばれることを固定する。
    """
    ctx = _ctx(tmp_path, session="s1")
    called: list[bool] = []
    real = ctx.renderer.forget_tools
    monkeypatch.setattr(ctx.renderer, "forget_tools",
                        lambda: (called.append(True), real())[1], raising=False)
    ctx.renderer.on_event({"type": "tool_call", "name": "Bash", "call": "c1",
                           "input": {"command": "cat /etc/shadow"}})
    ctx.renderer.on_event({"type": "tool_result", "call": "c1", "text": "秘密"})
    _out(ctx)
    slash.dispatch(ctx, "/clear")
    _out(ctx)
    assert called == [True]
    slash.dispatch(ctx, "/expand")
    out = _out(ctx)
    assert "cat /etc/shadow" not in out
    assert "もう残っていません" in out        # 「まだ」ではない（起きたことを起きたと言う）


def test_help_points_at_the_way_out_on_light_terminals(tmp_path):
    """SPEC §7-1: 明るい地では本文が 1.17:1 ＝読めない。第 2 パレットは作らないと決めた以上、
    **逃げ道の存在は伝える**（手段があるのに黙っているのは、手段が無いのと変わらない）。"""
    ctx = _ctx(tmp_path)
    slash.dispatch(ctx, "/help")
    out = _out(ctx)
    assert "NO_COLOR" in out, out
    assert "✓" in out and "✗" in out and "▸" in out      # 字で分かれることも書いてある


# --------------------------------------------------------------------------
# 狭い端末（2026-09-02 の監査）
# --------------------------------------------------------------------------

def _narrow_ctx(tmp_path: Path, width: int) -> AppContext:
    c = Console(record=True, width=width, force_terminal=False, file=io.StringIO())
    cl = FakeClient()
    cl.status = dict(cl.status, tools=[
        "ApplyDiff", "AskUserQuestion", "Bash", "BashOutput", "Edit", "Glob", "Grep",
        "NotebookEdit", "Read", "RemoteRead", "SystemState", "Task", "TodoWrite", "Write"])
    return AppContext(console=c, client=cl, renderer=Renderer(c), cfg={},
                      install_id_path=tmp_path / "cfg" / "install_id")


@pytest.mark.parametrize("width", [20, 24, 30, 36, 40, 60, 80, 120])
def test_help_follows_the_real_terminal_width(tmp_path, width):
    """`/help` は**実際の**端末幅で組む（旧 `max(40, …)` は 40 桁未満で崩れていた）。

    ★旧実装は「40 桁なら収まる」と判断して 1 行に詰め、端末が自前で桁 0 まで折り返していた
      ——この関数のコメントが「桁 0 に落とすと左端が揃わない」として避けたはずの形そのもの。
      狭いときは 2 段に落ちるのが正しい退避先。
    ★見るのは「行頭が段の位置にある」こと。群の見出しだけが桁 0 に立つ。
    """
    ctx = _narrow_ctx(tmp_path, width)
    slash.dispatch(ctx, "/help")
    # 桁 0 に立ってよいのは「群の見出し」と「末尾のキー行の 1 行目」だけ
    # ★許す形は本番の折り方を写さずに書く（写すと本番が壊れても試験が一緒に緩む）。
    titles = {t for t, _ in slash.HELP_GROUPS} | {"その他"}
    key_heads = tuple(k.lstrip()[:4] for k in slash.HELP_KEYS)
    for ln in _out(ctx).split("\n"):
        if not ln.strip():
            continue
        assert slash.disp_width(ln) <= width, (width, repr(ln))
        assert ln == ln.rstrip(), ("末尾空白", width, repr(ln))
        if ln.strip() in titles or ln.startswith(key_heads):
            continue
        assert ln.startswith(" "), ("桁 0 に落ちている", width, repr(ln))


@pytest.mark.parametrize("width", [20, 24, 40, 80])
def test_tools_columns_follow_the_real_terminal_width(tmp_path, width):
    """`/tools` の表も実際の幅で列を決める（旧 `max(40, …)` は狭い端末で列が崩れた）。"""
    ctx = _narrow_ctx(tmp_path, width)
    slash.dispatch(ctx, "/tools")
    for ln in _out(ctx).split("\n"):
        assert slash.disp_width(ln) <= width, (width, repr(ln))


def test_sendlog_は計器を最上位にも載せる():
    """★2026-09-14: counters は done 事象の中にはあったが、記録の**最上位には無かった**。

    読む側（server/report.py）は最上位を見るので、端末経路の走行は常に
    「計器ゼロ」に見えていた——典型的な『書いたが繋いでいない』。
    私自身これで「門が一度も鳴っていない」と読み違えた。

    ★events より先に写すこと: 上限超過で events は古い方から削られ空にもなる。
      最上位に置いてあれば、その場合でも計器は残る。
    """
    import pathlib

    from laps.term import slash as S

    class _R:
        transcript: list = []

    class _C:
        session = "s1"
        install_id_path = pathlib.Path("/tmp/bw-iid-test")
        last_input = "依頼"
        last_outcome = "accepted"
        renderer = _R()
        events = [
            {"type": "token", "id": "r1", "text": "x"},
            {"type": "done", "id": "r1", "payload": {
                "counters": {"tool_calls:Write": 9, "tool_calls:Bash": 1},
                "unrun": ["test_vdir.py"], "changed": True, "outcome": "accepted"}},
        ]

    rec, _body = S.build_sendlog(_C())
    assert rec.get("counters", {}).get("tool_calls:Write") == 9, "計器が最上位に無い"
    assert rec.get("unrun") == ["test_vdir.py"]
    assert rec.get("changed") is True

    # ★events を全部落としても計器は残る（上限に当たった走行でも読める）。
    rec2, _ = S.build_sendlog(_C(), max_bytes=1)
    assert rec2["events"] == []
    assert rec2.get("counters", {}).get("tool_calls:Bash") == 1, "events を削ると計器も消える"
