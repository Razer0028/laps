"""2026-11-16 システム全体のレビューで見つけた道具の直し（WebFetch・skills.sh の検索語・Read・Edit・ApplyPatch）。"""
from __future__ import annotations

import threading

import pytest

from laps.engine.tools import Ctx, files, patch, shell
from laps.engine.tools.web import decode_page, html_to_text
from laps.oc.skilltool import _PRIVATE_Q


def _ctx(tmp_path):
    return Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())


def test_page_without_closing_head_keeps_its_body():
    assert html_to_text("<html><head><title>t</title><body><p>hello</p></body></html>") == "hello"


def test_shift_jis_page_without_charset_header_is_readable():
    page = '<html><head><meta charset="Shift_JIS"></head><body>日本語</body></html>'.encode("cp932")
    assert "日本語" in decode_page(page, "text/html")
    assert decode_page("日本語".encode("cp932"), "text/html") == "日本語"
    assert decode_page("日本語".encode("utf-8"), "text/html; charset=utf-8") == "日本語"


@pytest.mark.parametrize("q", ["token counting", "tokenizer", "secrets management", "react-native-performance-optimization"])
def test_ordinary_search_words_are_not_private(q):
    assert not _PRIVATE_Q.search(q)


@pytest.mark.parametrize("q", ["192.0.2.9 設定", "me@example.com", "/srv/notes/private", "password reset",
                               "sk_live_" + "abcdefghijklmnopqrstuvwx", "0123456789abcdef0123456789abcdef", "jwt token=abc"])
def test_private_looking_words_are_still_held_back(q):
    assert _PRIVATE_Q.search(q)


def test_patch_does_not_add_a_newline_to_a_file_without_one():
    c = patch.Chunk(old=["a", "b"], new=["a", ""], ops=[(" ", "a"), ("-", "b"), ("+", "")])
    assert patch.apply_chunks("a\nb", [c], "x") == "a\n"
    assert patch.apply_chunks("a\nb\n", [c], "x") == "a\n\n"


def test_utf16_file_is_read_edited_and_patched_in_utf16(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ctx = _ctx(tmp_path)
    f = tmp_path / "u.txt"
    f.write_bytes(b"\xff\xfe" + "一行目\r\n二行目\r\n".encode("utf-16-le"))
    r = files.read({"file_path": str(f)}, ctx)
    assert r.ok and "二行目" in r.output
    r = files.edit({"file_path": str(f), "old_string": "二行目", "new_string": "2 行目"}, ctx)
    assert r.ok, r.output
    assert f.read_bytes() == b"\xff\xfe" + "一行目\r\n2 行目\r\n".encode("utf-16-le")
    r = patch.run({"patch": "*** Begin Patch\n*** Update File: u.txt\n-一行目\n+1 行目\n*** End Patch"}, ctx)
    assert r.ok, r.output
    assert f.read_bytes() == b"\xff\xfe" + "1 行目\r\n2 行目\r\n".encode("utf-16-le")


def test_big_file_is_read_in_part(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "BIG_FILE", 1000)
    f = tmp_path / "big.log"
    f.write_text("".join(f"line {i}\n" for i in range(1, 501)), encoding="utf-8")
    ctx = _ctx(tmp_path)
    r = files.read({"file_path": str(f), "offset": 10, "limit": 5}, ctx)
    assert r.ok and "10: line 10" in r.output and "14: line 14" in r.output and "15: line 15" not in r.output
    assert "offset=15" in r.output
    r = files.read({"file_path": str(f), "offset": 498}, ctx)
    assert "500: line 500" in r.output and "total 500 lines" in r.output
    assert not files.read({"file_path": str(f), "offset": 900}, ctx).ok


def test_binary_read_gets_the_resolved_path(tmp_path, monkeypatch):
    from laps.engine.tools import ext
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    seen = {}

    def fake(name, inp, ctx, **kw):
        seen.update(inp)
        return files.Result("ok")
    monkeypatch.setattr(ext, "call_legacy", fake)
    files.read({"file_path": "blob.bin"}, _ctx(tmp_path))
    assert seen["file_path"] == str(tmp_path / "blob.bin")


def test_skill_cache_merges_prunes_and_caps(tmp_path, monkeypatch):
    import json
    import time
    from laps.oc import skilltool
    monkeypatch.setattr(skilltool, "_outside_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(skilltool, "_CACHE_CAP", {"meta": 3, "trees": 2})
    now = time.time()
    (tmp_path / "cache.json").write_text(json.dumps({"meta": {"other": {"d": "x", "t": now}, "old": {"d": "y", "t": now - 30 * 86400}}}))
    skilltool._cache_save({"meta": {f"m{i}": {"d": "z", "t": now - i} for i in range(5)}})
    got = json.loads((tmp_path / "cache.json").read_text())
    assert "old" not in got["meta"] and len(got["meta"]) == 3
    assert "other" in got["meta"]                      # ほかの走行が書いた物を消さない
    assert not list(tmp_path.glob("*.tmp"))


def test_engine_skill_frontmatter_reads_bom_and_crlf():
    from laps.engine.tools.misc import _frontmatter
    assert _frontmatter("﻿---\r\nname: x\r\ndescription: >\r\n  long\r\n  text\r\n---\r\nbody") == {"name": "x", "description": "long text"}


def test_dead_headless_chrome_is_started_again(monkeypatch):
    import http.server
    from laps.oc import browsercdp as bc
    from laps.oc import browsertool as bt
    from laps.tools import appcall
    if not bc.pipe_ok():
        pytest.skip("no Chrome")

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<!doctype html><title>t</title><p>page</p>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    monkeypatch.setattr(appcall, "can", lambda name: False)
    bc.close()
    try:
        r = bt.run({"action": "open", "url": url})
        assert r.ok, str(r)
        line = bc.page().line
        line.proc.kill()
        line.proc.wait(timeout=10)
        r = bt.run({"action": "read"})
        assert not r.ok and "browser" in str(r) and "closed" in str(r), str(r)
        r = bt.run({"action": "open", "url": url})
        assert r.ok, str(r)
        assert bc.page().line is not line and bc.page().line.alive()
    finally:
        bc.close()
        srv.shutdown()
