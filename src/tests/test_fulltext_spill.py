"""畳んで捨てた全文をファイルに残し、場所を教える（2026-09-21・OSS 2 本から）。

出典: goose `agents/large_response_handler.rs:13-45`／opencode `tool/truncate.ts:14-15`
★畳み方は変えない。足すのは**全文の行き先**だけ——欲しいのは全部ではなく中の 1 行。
"""
import os

import pytest

from laps.tools import fulltext as F


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    return tmp_path


def test_長い出力は残して相対パスを返す(ws):
    rel = F.keep("y" * 20000, "bash")
    assert rel and rel.startswith(".blackwall-phases/fulltext/")
    assert (ws / rel).exists()
    assert (ws / rel).read_text(encoding="utf-8") == "y" * 20000


def test_短い出力は残さない(ws):
    """★畳んでいないものを残しても場所を取るだけ。"""
    assert F.keep("x" * 100, "bash") is None


def test_作業場が無ければ残さず黙る(monkeypatch):
    """★保存できないのに「保存しました」と言わない。"""
    monkeypatch.delenv("LAPS_WORKSPACE", raising=False)
    assert F.keep("z" * 20000, "bash") is None
    assert F.note(None, 20000, 3000) == ""


def test_同じ中身は書き直さない(ws):
    a = F.keep("q" * 20000, "bash")
    mt = (ws / a).stat().st_mtime_ns
    b = F.keep("q" * 20000, "bash")
    assert a == b and (ws / a).stat().st_mtime_ns == mt


def test_置き場が膨らんだら古い順に消す(ws, monkeypatch):
    monkeypatch.setattr(F, "MAX_DIR_BYTES", 60_000)
    monkeypatch.setattr(F, "MIN_BYTES", 1)
    kept = [F.keep(f"{i}" * 20000, f"n{i}") for i in range(5)]
    left = [k for k in kept if k and (ws / k).exists()]
    assert 0 < len(left) < 5, f"消えていない／消しすぎ: {len(left)}"
    assert left[-1] == kept[-1], "新しいほうを消している"


def test_案内は場所と量を言う(ws):
    rel = F.keep("w" * 20000, "bash")
    n = F.note(rel, 20000, 3000)
    assert rel in n and "20,000" in n and "3,000" in n
    assert "Grep" in n and "Read" in n       # ★次の一手を書く


def test_Bash_が畳んだら案内が付く(ws, monkeypatch):
    """★「書いたが繋いでいない」を 5 回踏んだので、実際に Bash を通して見る。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.chdir(ws)
    from laps.tools import bash
    out = str(bash._run({"command": "python3 -c \"[print('x'*200) for _ in range(400)]\"",
                         "timeout": 60}))
    assert "全文（" in out
    import re
    m = re.search(r"\.blackwall-phases/fulltext/\S+", out)
    assert m and os.path.exists(m.group(0))
