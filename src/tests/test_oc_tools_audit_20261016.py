"""★2026-10-16 道具の点検（oc の道具の 15 件と、旧中核からの取りこぼし）。推論機は使わない。

1  kill を作業場の中に絞っていなかった（旧中核の _scope_* が oc から呼ばれず）・自分を撃つ守りが python3.12 を通していた
2  真偽を文字列で受けると真になった（replace_all:"false" が全部を置き換え・kill:"false" が止めた）
3  1 バイトの崩れ・符号化の混ざりで、Read・Bash の結果全体が latin-1 に化けた
4  BashOutput の until= が、出力が 64KB を超えると日本語に当たらなかった
5  rg の無い機械の Glob・Grep が build/・dist/ を黙って飛ばし、4MB で黙って切っていた
6  Edit の replace_all が、別々の場所がたまたま持つ同じ 1 行を全部書き換えた
7  trusted 以外で、資格情報のファイルを Read・Grep が返した
8  settings.json の deny_patterns を oc の Bash が見ていなかった
9  前で走らせた `cmd &` の子が、番号も無く追えず、走行の終わりにも止まらなかった
10 Shift_JIS のページを WebFetch が「文章でない」と断った・化けた
11 App の cwd が黙って捨てられた
12 Api の json を文字列で渡すと二重に符号化した・save_to が書き込みの制限を見なかった
13 WebFetch が手元の http を https に上げて読めなかった
14 Read の limit="all" で道具が落ち、負の値で黙って 0 行を返した・Edit の edits の中の dict でない物で落ちた
15 切り詰めた出力の全文が消されずに溜まった
＋ 待ち（BashOutput・Monitor）が強制割り込みで抜けない・取り消しの後に Bash が起動した（走行の点検の 7・9 の道具の側）
"""
from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time

import pytest

from laps.oc import bgwait
from laps.oc import tools as oc_tools
from laps.tools import ToolSpec

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX のプロセス")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    d = tmp_path / "ws"
    d.mkdir()
    monkeypatch.chdir(d)
    monkeypatch.setenv("LAPS_WORKSPACE", str(d))
    monkeypatch.setenv("LAPS_POLICY", "trusted")         # 出荷の既定（conftest は workspace にする）。制限の試験は自分で替える
    return d


# ── 2 ──
def test_string_booleans_follow_the_schema(ws):
    spec = oc_tools.specs()["Edit"]
    inp = oc_tools.coerce_input(spec, {"file_path": "a", "old_string": "x", "new_string": "y", "replace_all": "false"})
    assert inp["replace_all"] is False
    (ws / "a.txt").write_text("foo\nfoo\n")
    r = oc_tools.edit_tool({"file_path": "a.txt", "edits": [{"old_string": "foo", "new_string": "X", "replace_all": "false"}]})
    assert not r.ok and (ws / "a.txt").read_text() == "foo\nfoo\n"
    (ws / "b.txt").write_text("Hello\nhello\n")
    r = oc_tools.grep_tool({"pattern": "hello", "path": str(ws / "b.txt"), "-i": "false"})
    assert "Line 1" not in str(r) and "Line 2" in str(r)


def test_cwd_reaches_tools_that_name_it_cwd():
    spec = ToolSpec(name="App", description="", schema={"type": "object", "properties": {"cwd": {"type": "string"}}},
                    handler=lambda i: i)
    assert oc_tools.coerce_input(spec, oc_tools.normalize_input("App", {"cwd": "/x"})) == {"cwd": "/x"}


@posix
def test_bashoutput_kill_false_does_not_kill(ws):
    pid = _bg("sleep 30")
    try:
        bgwait.bash_output_tool({"pid": pid, "kill": "false"})
        time.sleep(0.3)
        assert bgwait._alive(pid)              # 生きている（zombie は数えない）
    finally:
        _stop(pid)


def _bg(cmd: str) -> int:
    r = str(oc_tools.bash_tool({"command": cmd, "run_in_background": True}))
    return int(re.search(r"pid: (\d+)", r).group(1))


def _stop(pid: int) -> None:
    try:
        os.killpg(pid, 9)
    except ProcessLookupError:
        pass


# ── 1 ──
@posix
def test_name_and_pid_kills_stay_inside_the_workspace(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    tag = str(70000 + os.getpid() % 9000)
    v = subprocess.Popen(["sleep", tag], cwd=str(other), start_new_session=True)
    v2 = subprocess.Popen(["sleep", str(int(tag) + 1)], cwd=str(other), start_new_session=True)
    try:
        time.sleep(0.3)
        oc_tools.bash_tool({"command": f"pkill -f 'sleep {tag}'"})
        oc_tools.bash_tool({"command": f"kill {v2.pid}"})
        time.sleep(0.3)
        assert v.poll() is None and v2.poll() is None
    finally:
        v.kill()
        v2.kill()


def test_self_kill_guard_names():
    from laps.oc import shellfix as sf
    assert sf.guard_self_kill("killall python3.12", windows=False)[0] is None
    assert sf.guard_self_kill('pkill -f "python server.py"', windows=False)[0] is not None


# ── 3 ──
def test_decoding_keeps_utf8_with_a_cut_last_char_and_mixed_lines():
    from laps.tools.read import decode_bytes
    assert decode_bytes("処理中\n処理中\n".encode() + "完".encode()[:2]) == "処理中\n処理中\n"
    mixed = "テストは全部成功しました\n".encode() + "指定されたパスが見つかりません。\n".encode("cp932")
    assert decode_bytes(mixed) == "テストは全部成功しました\n指定されたパスが見つかりません。\n"


def test_read_shows_a_cut_utf8_log_readably(ws):
    (ws / "run.log").write_bytes(("処理中\n" * 50).encode() + "完".encode()[:2])
    r = str(oc_tools.read_tool({"file_path": "run.log"}))
    assert "1: 処理中" in r


# ── 4 ──
def test_until_matches_japanese_after_64kb(ws, tmp_path):
    out = tmp_path / "o.out"
    out.write_bytes(("あ" * 30000 + "\n処理が完了しました\n").encode())       # 9 万バイト・窓の頭が文字の途中
    assert "完了" in bgwait._tail_text(str(out))


# ── 5 ──
def test_fallback_search_matches_rg_folders(ws, monkeypatch):
    monkeypatch.setattr(oc_tools, "_rg", lambda: None)
    for d in ("build/gen", "dist", "logs", "src"):
        (ws / d).mkdir(parents=True)
    (ws / "build/gen/out.js").write_text("TOKEN")
    (ws / "dist/app.js").write_text("TOKEN")
    (ws / "logs/x.js").write_text("TOKEN")
    (ws / "src/main.js").write_text("TOKEN")
    (ws / ".gitignore").write_text("logs/\n")
    g = str(oc_tools.glob_tool({"pattern": "**/*.js"}))
    assert "out.js" in g and "app.js" in g and "main.js" in g and "x.js" not in g
    (ws / "src/big.log").write_bytes(b"x" * 4_100_000 + b"\nNEEDLE\n")
    r = str(oc_tools.grep_tool({"pattern": "NEEDLE"}))
    assert "first 4 MB" in r


# ── 6・14 ──
def test_replace_all_refuses_different_places_sharing_a_line(ws):
    src = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b\n"
    (ws / "m.py").write_text(src)
    r = oc_tools.edit_tool({"file_path": "m.py", "old_string": "    return a + b", "new_string": "    return a * b",
                            "replace_all": True})
    assert not r.ok and "refused" in str(r) and (ws / "m.py").read_text() == src
    (ws / "n.py").write_text("old_name = 1\nprint(old_name)\n")             # 行の一部の付け替えは今までどおり
    assert oc_tools.edit_tool({"file_path": "n.py", "old_string": "old_name", "new_string": "new_name",
                               "replace_all": True}).ok


def test_bad_arguments_are_explained_not_crashed(ws):
    (ws / "f.txt").write_text("a\nb\n")
    assert "limit must be" in str(oc_tools.read_tool({"file_path": "f.txt", "limit": "all"}))
    assert "limit must be" in str(oc_tools.read_tool({"file_path": "f.txt", "limit": -5}))
    assert "not objects" in str(oc_tools.edit_tool({"file_path": "f.txt", "edits": ["x"]}))


# ── 7 ──
def test_credentials_are_not_read_under_workspace_policy(ws, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    (ws / ".ssh").mkdir()
    (ws / ".ssh" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    assert "PRIVATE KEY" not in str(oc_tools.read_tool({"file_path": ".ssh/id_ed25519"}))
    g = str(oc_tools.grep_tool({"pattern": "BEGIN"}))
    assert "PRIVATE KEY" not in g


def test_trusted_policy_still_reads_them(ws, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    (ws / ".ssh").mkdir()
    (ws / ".ssh" / "config").write_text("Host lab\n")
    assert "Host lab" in str(oc_tools.read_tool({"file_path": ".ssh/config"}))


# ── 8 ──
def test_deny_patterns_stop_bash(ws, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"deny_patterns": [r"echo\s+FORBIDDEN"]}))
    monkeypatch.setenv("LAPS_HOME", str(home))
    r = oc_tools.bash_tool({"command": "echo FORBIDDEN > made.txt"})
    assert not r.ok and "deny_patterns" in str(r) and not (ws / "made.txt").exists()


# ── 9 ──
@posix
def test_released_background_child_is_tracked_and_stopped(ws):
    bgwait.begin_run()
    pg = None
    try:
        r = str(oc_tools.bash_tool({"command": "sleep 60 & echo started"}))
        m = re.search(r"process group (\d+)", r)
        assert m
        pg = int(m.group(1))
        assert bgwait.running_jobs()
    finally:
        bgwait.end_run()
    time.sleep(0.5)
    alive = bgwait._alive(pg)                 # 刈り取られない zombie（コンテナの 1 番が刈らない）は数えない
    if alive:
        _stop(pg)
    assert not alive


# ── 10・13 ──
class _Pages(http.server.BaseHTTPRequestHandler):
    pages: dict = {}

    def do_GET(self):  # noqa: N802
        body, ctype = self.pages[self.path]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def pages(monkeypatch):
    from laps.tools import webfetch as _wf
    monkeypatch.setattr(_wf, "_check_hop", lambda url: None)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Pages)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_webfetch_reads_shift_jis_pages(pages):
    _Pages.pages = {
        "/jp.html": (("<html><body>" + "日本語の説明です。" * 80 + "</body></html>").encode("cp932"), "text/html"),
        "/meta.html": (("<html><head><meta charset=\"Shift_JIS\"></head><body>" + "English line. " * 40
                        + "日本語の説明</body></html>").encode("cp932"), "text/html"),
    }
    a = str(oc_tools.webfetch_tool({"url": pages + "/jp.html"}))
    b = str(oc_tools.webfetch_tool({"url": pages + "/meta.html"}))
    assert "日本語の説明です" in a and "日本語の説明" in b


# ── 12 ──
def test_api_json_string_and_save_to_policy(ws, monkeypatch, tmp_path):
    from laps.oc import apitool
    got = {}

    class _Echo(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("LAPS_ALLOW_PRIVATE_FETCH", "1")
    url = f"http://127.0.0.1:{srv.server_address[1]}/x"
    try:
        apitool.api_tool({"method": "POST", "url": url, "json": '{"name": "a"}'})
        assert json.loads(got["body"]) == {"name": "a"}
        monkeypatch.setenv("LAPS_POLICY", "workspace")
        out = tmp_path / "outside.json"
        r = apitool.api_tool({"method": "POST", "url": url, "json": {"a": 1}, "save_to": str(out)})
        assert not r.ok and not out.exists()
    finally:
        srv.shutdown()


# ── 15 ──
def test_old_full_outputs_are_pruned(tmp_path):
    old = tmp_path / "tool_old"
    old.write_text("x")
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    new = tmp_path / "tool_new"
    new.write_text("y")
    oc_tools._prune_full(tmp_path)
    assert not old.exists() and new.exists()


# ── 走行の点検の 7・9（道具の側） ──
@posix
def test_wait_ends_on_a_new_message(ws, monkeypatch):
    pid = _bg("sleep 30")
    t0 = time.time()
    monkeypatch.setattr(oc_tools, "_preempt_probe", lambda: time.time() - t0 > 1.0)
    monkeypatch.setattr(oc_tools, "_cancel_probe", lambda: False)        # 前の試験の走行が付けた口を使わない
    try:
        r = str(bgwait.bash_output_tool({"pid": pid, "wait": 20}))
        assert time.time() - t0 < 8 and "new message" in r
    finally:
        _stop(pid)


def test_bash_does_not_start_after_stop(ws, monkeypatch):
    monkeypatch.setattr(oc_tools, "_cancel_probe", lambda: True)
    r = oc_tools.bash_tool({"command": "echo ran > ran.txt"})
    assert not r.ok and not (ws / "ran.txt").exists()


# ── 前置きの点検の残り（oc/tools.py の側）: Umbra を切ったら道具の説明で名指ししない ──
def test_no_umbra_mentions_when_umbra_is_off(monkeypatch):
    monkeypatch.setenv("LAPS_UMBRA", "0")
    assert "Umbra" not in oc_tools._bash_description()
    sk = oc_tools.specs().get("Skill")
    if sk is not None:
        assert "Umbra" not in json.dumps(sk.schema)
