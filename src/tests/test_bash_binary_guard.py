"""★2026-09-08 監査（tools high）: Bash の出力にバイナリ関門が無く、latin-1 フォールバックが任意のバイト列を
無傷の「文字」にしていた。Read と同じく先頭 4KB の NUL で判定する。"""
from laps.tools import bash as B


def test_nul_in_head_is_reported_as_binary():
    out = B._decode_out(b"SQLite format 3\x00" + b"\x00\x01\x02" * 2000)
    assert out.startswith("Error: 出力はバイナリです") and "sqlite3" in out


def test_plain_text_passes_through():
    assert B._decode_out("plain".encode()) == "plain"
    assert B._decode_out("日本語\n".encode()) == "日本語\n"


def test_switch_off_restores_decode(monkeypatch):
    monkeypatch.setenv("LAPS_BASH_BINARY_GUARD", "0")
    out = B._decode_out(b"ab\x00cd")
    assert not out.startswith("Error:")
