"""Write の成功メッセージが**文字数**を「bytes」と称していた（2026-09-03・W3 点検）。

`write.py` は `write_text_atomic(p, content)` で UTF-8 に符号化してから書くのに、
返す文言は `len(content)`（Python の str の**文字数**）をそのまま「bytes」と言っていた。
この現場のコメント・文言はほぼ日本語で、UTF-8 では1文字が最大3バイトになるので、
日本語を含む本文では申告と実際にディスクへ書かれた量が大きくずれる
（例: 100文字の日本語は「wrote 100 bytes」と出るが、実際は約300バイト）。
`ls -l` / `wc -c` で確かめたモデルが「言われた量と違う」と誤読する余地を残さない。
"""
from __future__ import annotations

import pytest

from laps.tools import write as w


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


def test_日本語本文の申告バイト数が実際のバイト数と一致する(tmp_path):
    p = tmp_path / "note.txt"
    content = "日本語の本文です。文字数とバイト数がずれる例。"  # 1文字3バイト級を含む
    out = str(w._run({"file_path": str(p), "content": content}))
    assert out.startswith("OK: wrote ")
    claimed = int(out.split()[2])
    actual = p.stat().st_size
    assert claimed == actual, f"申告 {claimed} バイト ≠ 実際 {actual} バイト"
    # 文字数と一致していない（＝直す前の壊れた挙動ではない）ことも確かめる
    assert claimed != len(content)
    assert claimed == len(content.encode("utf-8"))


def test_ASCII本文では文字数とバイト数が一致する(tmp_path):
    p = tmp_path / "ascii.txt"
    content = "plain ascii content"
    out = str(w._run({"file_path": str(p), "content": content}))
    claimed = int(out.split()[2])
    assert claimed == len(content) == p.stat().st_size
