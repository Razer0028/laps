"""WebFetch は文章でない中身を取り込まない。

★実機で踏んだ形（2026-08-19）: python-3.14.0-amd64.exe（29,900,480 バイト・
  application/octet-stream）を取得し、PE ヘッダ以降のバイナリを
  errors="replace" で復号して 50,175 文字を文脈へ流し込み、
  走行が context_overflow で終わった。**種類を見ていなかった。**
"""
from __future__ import annotations

import pytest

from laps.tools import webfetch


@pytest.mark.parametrize("ctype,body", [
    ("application/octet-stream", b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 200),
    ("application/zip", b"PK\x03\x04" + bytes(range(256))),
    ("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100),
    ("application/pdf", b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 50),
    ("video/mp4", b"\x00\x00\x00\x18ftypmp42"),
])
def test_binary_is_refused(ctype, body):
    assert not webfetch._looks_like_text(ctype, body)


@pytest.mark.parametrize("ctype,body", [
    ("text/html; charset=utf-8", b"<html><body>hi</body></html>"),
    ("text/plain", "日本語の本文です".encode()),
    ("application/json", b'{"a": 1}'),
    ("application/xml", b"<?xml version='1.0'?><a/>"),
    ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
    ("application/x-yaml", b"a: 1\nb: 2\n"),
    ("", b"plain text with no content-type"),
])
def test_text_is_accepted(ctype, body):
    assert webfetch._looks_like_text(ctype, body)


def test_type_says_text_but_body_is_binary():
    """★種類の申告だけでは足りない。text/plain を名乗るバイナリがある。"""
    assert not webfetch._looks_like_text("text/plain", b"\x00\x01\x02\x03" * 100)


def test_allowlist_not_denylist():
    """★「バイナリを弾く」形は必ず漏れる。見たことのない種類は断る。"""
    assert not webfetch._looks_like_text("application/vnd.some-future-format", b"ok text")


def test_out_cap_follows_the_window(monkeypatch):
    """固定 50,000 文字は窓 16,384 の推論器では窓より大きい。"""
    monkeypatch.delenv("LAPS_N_CTX", raising=False)
    assert webfetch._out_cap() == webfetch._MAX_OUT_CHARS
    monkeypatch.setenv("LAPS_N_CTX", "16384")
    cap = webfetch._out_cap()
    from laps.tools.limits import DEFAULT_OUT_CAP_FRACTION as _F
    assert cap == max(4000, int(16384 * _F * 2)), cap
    assert cap < webfetch._MAX_OUT_CHARS
    monkeypatch.setenv("LAPS_N_CTX", "1024")
    assert webfetch._out_cap() == 4000       # 下限は割らない
