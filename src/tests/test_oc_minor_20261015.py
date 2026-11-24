"""★2026-10-15 軽い物（DESIGN-improvements-20261015.md の 1-F）。推論機は使わない。

F1 Grep が rg を使うと、UTF-8 でない行が空になり、日本語が cp932 のファイルに当たらなかった。
F2 長い会話で出力を絞る仕組み（OUTPUT_BUDGET_BYTES）が Bash と Read に効いていなかった。
F3 Glob が `.github` のような点で始まるフォルダの中を見なかった（Grep は見る）。
F4 強制割り込みが Explore の子に届かず、子が終わるまで待っていた。
F5 WebFetch の format が使われず、timeout の説明（最大 120 秒）と実際（60 秒）が違った。
"""
from __future__ import annotations

import shutil
import sys
import types

import httpx
import pytest

from laps.oc import tools as oc_tools

needs_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="rg が無い機械では Python の代わりが探す")


@needs_rg
def test_F1_grep_finds_japanese_in_a_cp932_file(tmp_path):
    (tmp_path / "run.bat").write_bytes('echo 開始します\r\nset X=1\r\n'.encode("cp932"))
    (tmp_path / "u.txt").write_text("別の 開始 の行\n", encoding="utf-8")
    r = oc_tools.grep_tool({"pattern": "開始", "path": str(tmp_path)})
    assert "run.bat" in str(r) and "echo 開始します" in str(r)
    assert "u.txt" in str(r) and str(r).count("開始") == 2


@needs_rg
def test_F1_grep_keeps_the_text_of_a_line_that_is_not_utf8(tmp_path):
    (tmp_path / "log.txt").write_bytes(b"ok line\nERROR at \xff\xfe offset 12\n")
    r = oc_tools.grep_tool({"pattern": "ERROR", "path": str(tmp_path)})
    assert "ERROR at" in str(r) and "offset 12" in str(r)


@pytest.fixture
def budget(monkeypatch):
    monkeypatch.setattr(oc_tools, "OUTPUT_BUDGET_BYTES", 8 * 1024)


def test_F2_read_follows_the_output_budget(tmp_path, budget):
    p = tmp_path / "big.txt"
    p.write_text("".join(f"line {i:05d} " + "x" * 60 + "\n" for i in range(400)), encoding="utf-8")   # 約 28KB
    r = str(oc_tools.read_tool({"file_path": str(p)}))
    assert len(r.encode("utf-8")) < 10 * 1024 and "Output capped at 8 KB" in r


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX の sh")
def test_F2_bash_follows_the_output_budget(budget):
    r = str(oc_tools.bash_tool({"command": "seq 1 600 | sed 's/$/ xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/'"}))
    assert "output truncated" in r
    assert len(r.encode("utf-8")) < 10 * 1024


def test_F3_glob_looks_inside_dot_folders_but_not_git(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "x.yml").write_text("no\n")
    r = str(oc_tools.glob_tool({"pattern": "**/*.yml", "path": str(tmp_path)}))
    assert "ci.yml" in r and ".git/x.yml" not in r.replace("\\", "/")




def test_F5_webfetch_timeout_is_capped_at_what_the_fetcher_allows(monkeypatch):
    from laps.tools import webfetch as _wf
    seen = {}
    monkeypatch.setattr(_wf.SPEC, "handler", lambda q: (seen.update(q), "ok")[1])
    oc_tools.webfetch_tool({"url": "https://example.com", "timeout": 100, "format": "html"})
    assert seen["timeout"] == 60.0 and seen["format"] == "html"
    desc = oc_tools.specs()["WebFetch"].schema["properties"]["timeout"]["description"]
    assert "60" in desc and "120" not in desc


def test_F5_webfetch_format_html_returns_the_markup(monkeypatch):
    from laps.tools import webfetch as _wf
    page = b"<html><head><title>T</title></head><body><p id='a'>hello</p></body></html>"
    real = httpx.Client
    monkeypatch.setattr(_wf, "_check_hop", lambda url: None)
    monkeypatch.setattr(_wf.httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=page,
                                                                 headers={"content-type": "text/html"})),
        **{k: v for k, v in kw.items() if k != "transport"}))
    html = str(_wf._run({"url": "https://example.com", "format": "html"}))
    assert "<p id='a'>hello</p>" in html
    text = str(_wf._run({"url": "https://example.com"}))
    assert "hello" in text and "<p" not in text
