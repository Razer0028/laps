"""★2026-10-18 道具の失敗と推論機の誤りの分類（oc/failclass.py）。

なぜ: 走行記録の道具の失敗率（Bash 29%・Lab 32%・Mcp 37%・Edit 25%）の理由を、本文を読まずに分けたい。
  分類は Blackwall 自身が書く決まった文だけで決める。★できるだけ本物の道具を呼んで、その文で確かめる
  （文の言い回しが変わったら、この試験が落ちて分類の取りこぼしに気づける）。
"""
from __future__ import annotations

import httpx
import pytest

from laps.oc import failclass as fc
from laps.oc import tools


def _cls(name, res):
    return fc.classify(name, bool(res.ok), str(getattr(res, "kind", "") or ""), str(res))


def test_success_has_no_class():
    assert fc.classify("Bash", True, "ok", "exit code: 1") is None


def test_real_edit_errors(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\nx = 1\n", encoding="utf-8")
    assert _cls("Edit", tools.edit_tool({"file_path": str(p), "old_string": "nope", "new_string": "y"})) == "not_found"
    assert _cls("Edit", tools.edit_tool({"file_path": str(p), "old_string": "x = 1", "new_string": "y"})) == "multiple"
    # ★0.1.552「同じ Edit を失敗にしない」: 変える所が無い Edit は成功（ok=True・kind=empty）＝失敗の分類は付かない。
    same = tools.edit_tool({"file_path": str(p), "old_string": "x = 1\nx = 1", "new_string": "x = 1\nx = 1"})
    assert same.ok and "No change was needed" in str(same) and _cls("Edit", same) is None
    assert _cls("Edit", tools.edit_tool({"file_path": str(tmp_path / "none.py"), "old_string": "a",
                                         "new_string": "b"})) == "no_file"
    assert _cls("Edit", tools.edit_tool({"file_path": str(tmp_path), "old_string": "a",
                                         "new_string": "b"})) == "is_dir"
    assert _cls("Edit", tools.edit_tool({"file_path": str(p), "edits": ["x"]})) == "bad_edits"


def test_real_bash_exit_and_workdir(tmp_path, monkeypatch):
    # 試験の共通設定は workspace（sh -c を断る）。ここは分類を見る試験なので既定の trusted で走らせる。
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    r = tools.bash_tool({"command": "sh -c 'exit 3'", "workdir": str(tmp_path)})
    assert _cls("Bash", r) == "exit:3"
    r2 = tools.bash_tool({"command": "true", "workdir": str(tmp_path / "missing")})
    assert _cls("Bash", r2) == "no_workdir"


def test_timeout_and_abort_wording():
    tail = ("partial\n<shell_metadata>\nshell tool terminated command after exceeding timeout 1000 ms. If this command "
            "is expected to take longer ...\n</shell_metadata>")
    assert fc.classify("Bash", False, "error", tail) == "timeout"
    assert fc.classify("Bash", False, "error", "out\nUser aborted the command") == "aborted"


def test_lab_exit_line():
    assert fc.classify("Lab", False, "error", "作業用のコンテナ:/work $ make\nboom\n[exit 2 · 0.4 s]") == "exit:2"


def test_windows_large_exit_codes_are_grouped():
    assert fc.classify("Bash", False, "error", "exit code: 3221225477") == "exit:large"


def test_mcp_wordings():
    assert fc.classify("Mcp", False, "error", "Error: MCP server 'pw' is not configured (known: a)") == "mcp_not_configured"
    assert fc.classify("Mcp", False, "error", "Error: MCP server 'pw' could not start: boom") == "mcp_start_failed"
    assert fc.classify("Mcp", False, "error", "Error: 'pw' has no tool 'x' (tools: a, b)") == "mcp_no_tool"
    assert fc.classify("Mcp", False, "error", "Error: pw/click: element not found") == "mcp_tool_error"


def test_kind_wins_for_denied_and_cancelled():
    assert fc.classify("Bash", False, "denied", "whatever") == "denied"
    assert fc.classify("Bash", False, "cancelled", "whatever") == "cancelled"


def test_command_output_words_do_not_decide_the_class():
    """★命令の出力の中の語（中身）で分けない: 出力の途中に「Could not find old_string」とあっても、Bash の分類は終了コード。"""
    text = "line\nCould not find old_string in the file\n" + "x" * 500 + "\nexit code: 1"
    assert fc.classify("Bash", False, "error", text) == "exit:1"


def test_unknown_is_other():
    assert fc.classify("Read", False, "error", "something odd happened") == "other"


@pytest.mark.parametrize("err, want", [
    (RuntimeError("the request exceeds the available context size, try increasing it"), "context"),
    (RuntimeError("failed to find free space in the KV cache"), "context"),
    (RuntimeError("no free slot available"), "slot"),
    (RuntimeError("HTTP 500 Internal Server Error"), "http_500"),
    (httpx.ConnectError("[Errno 111] Connection refused"), "connection"),
    (httpx.ReadTimeout("timed out"), "timeout"),
    (ValueError("weird"), "ValueError"),
])
def test_backend_classes(err, want):
    assert fc.backend_class(err) == want


def test_gateway_record_keeps_kind_and_class():
    """記録の道具の行に kind と cls が残る（serve が線に出す実物の形で食べさせる）。"""
    import json

    from laps.gateway_record import RunRecorder
    rec = RunRecorder(sid="s", version="0")
    for ev in ({"type": "notice", "kind": "run_start", "id": "r1"},
               {"type": "tool_call", "id": "r1", "call": "c1", "name": "Edit", "input": {}},
               {"type": "tool_result", "id": "r1", "call": "c1", "name": "Edit", "ok": False, "kind": "error",
                "result": "Error: Could not find old_string in the file."},
               {"type": "tool_call", "id": "r1", "call": "c2", "name": "Read", "input": {}},
               {"type": "tool_result", "id": "r1", "call": "c2", "name": "Read", "ok": True, "kind": "ok",
                "result": "1: x"}):
        rec.feed(json.dumps(ev))
    assert (rec.tools[0]["kind"], rec.tools[0]["cls"]) == ("error", "not_found")
    assert (rec.tools[1]["kind"], rec.tools[1]["cls"]) == ("ok", None)


def test_unknown_tool_and_bad_args_are_named():
    """★2026-10-18 オーナー「独自のツールとかもローカル LLM でもちゃんと認識できるようになっていますか？」→
    道具の名前の誤りと引数の形の誤りを数えで分ける（loop.py が書く決まった文・中身は見ない）。"""
    from laps.oc import failclass
    assert failclass.classify("Serve2", False, "error",
                              "Error: Model tried to call unavailable tool 'Serve2'. Available tools: Bash, Read.") == "unknown_tool"
    assert failclass.classify("Bash", False, "error",
                              "Error: Model tried to call unavailable tool 'Bash'. Available tools: Read.") == "unknown_tool"
    assert failclass.classify("Edit", False, "error",
                              "Error: The arguments provided to the tool are invalid: not a JSON object. Please rewrite "
                              "the input so it satisfies the expected schema.") == "bad_args"


def test_browser_errors_are_classified_20261018():
    """★アプリのブラウザ（Browser）の決まった文（2026-10-18: オーナー機の 34 走行で Browser の失敗 6 件が全部 other だった）。"""
    from laps.oc.failclass import classify
    for text, want in (
        ('Error: no element matched text "Sign in"', "browser_no_element"),
        ("Error: no input field matched (give a selector)", "browser_no_element"),
        ("Error: the app's browser could not be used: not running inside the Blackwall app", "browser_no_app"),
        ("Error: the app did not answer within 60 seconds", "browser_no_app"),
        ("Error: the page did not answer in time\nURL: https://example.com/", "browser_page_timeout"),
        ("Error: Only http and https pages can be opened (not file:)", "browser_bad_url"),
        ("Error: bad selector: SyntaxError: '#' is not valid", "browser_bad_selector"),
        ("Error: Can't open the page: navigation failed", "browser_nav_failed"),
        ("Error: the app sent an answer that could not be read", "browser_bad_reply"),
    ):
        assert classify("Browser", False, "error", text) == want, text


def test_tool_own_errors_are_classified_20261018(tmp_path):
    """★Check・Listen・Umbra・WebFetch が自分で書く決まった文（2026-10-18: 直近 10 日の走行で各 1 件が other だった）。
    ★呼べる物は本物の道具を呼んで、その返事で確かめる（試験の側で文を写すと、道具の文が変わったときに気づけない）。"""
    from laps.oc.failclass import classify
    from laps.oc import checktool, listen
    from laps.oc.tools import webfetch_tool

    def cls(name, res):
        return classify(name, bool(getattr(res, "ok", True)), getattr(res, "kind", "error"), str(res))

    assert cls("Check", checktool.check_tool({"path": str(tmp_path / "nope")})) == "no_file"
    assert cls("Check", checktool.check_tool({"path": str(tmp_path)})) == "check_no_project"
    assert cls("Check", checktool.check_tool({"kind": "lint?"})) == "bad_args"
    assert cls("Listen", listen.listen_tool({})) == "missing_arg"
    assert cls("Listen", listen.listen_tool({"path": str(tmp_path / "x.wav")})) == "no_file"
    assert cls("Listen", listen.listen_tool("x")) == "bad_args"
    assert cls("WebFetch", webfetch_tool({})) == "missing_arg"
    assert cls("WebFetch", webfetch_tool({"url": "ftp://example.com/"})) == "bad_url"
    for name, text, want in (
        ("Check", "Checked app (auto): problems found\n- ruff (lint): `ruff check` — FAILED", "check_problems"),
        ("Check", "No check command was found here (nothing installed that fits this project), so nothing ran.",
         "check_none"),
        ("Listen", "Listened to 2 file(s) in 3.1 s on cuda.\nspeech: 0 · failed: 2", "listen_all_failed"),
        ("Listen", "Error: whisper-cli did not finish within 600 s", "timeout"),
        ("WebFetch", "Error: HTTP 404 for https://example.com/x", "http_404"),
        ("WebFetch", "Error: fetch failed: [Errno -2] Name or service not known", "fetch_failed"),
        ("WebFetch", "Error: blocked private/internal URL (10.0.0.1 is private). Set ...", "blocked_private"),
        ("WebFetch", "URL: https://e.com/a.exe\nStatus: 200\nContent-Type: application/octet-stream\nBytes: 9\n---\n"
                     "Error: 文章ではない内容なので読み込みませんでした（application/octet-stream）。", "not_text"),
    ):
        assert classify(name, False, "error", text) == want, text
    # ★その道具の文は、その道具にだけ当てる（他の道具の出力に同じ書き出しがあっても分けない）
    assert classify("Grep", False, "error", "Checked something odd") == "other"
    assert classify("Mcp", False, "error", "Error: HTTP 404 for https://example.com/") != "http_404"
