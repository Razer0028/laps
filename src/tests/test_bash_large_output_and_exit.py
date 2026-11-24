"""大出力の切り詰め位置と、非0 exit の型（ok/kind）（第3回 W2・2026-09-04）。

★大出力: `fold.fold_head_tail` はバイト単位で先頭/末尾を残し中央を畳むが、
  マルチバイト文字（日本語等）の境界をまたいで切ると文字化け（U+FFFD 等）が
  起きうる。本物の Bash 実行を通し、実際に返る文字列に置換文字が現れないこと・
  「ここで切りました/中略」の案内が本文に残ることを確かめる。

★非0 exit: `ToolResult.ok` / `.kind` が終了コードと一致すること（本文の
  `[exit=N]` タグと二重に頼らなくても、型で成否が読めること）。
"""
from __future__ import annotations

import sys

import pytest

from laps.tools import bash as bash_tool

REPLACEMENT = "�"


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のクォート前提")
def test_マルチバイトの大出力は境界で文字化けしない():
    """2,000,000 文字の日本語（3 バイト/文字）。畳みの境界がバイト単位でも、
    最終的な文字列に不完全なマルチバイト文字（置換文字）が残らないこと。"""
    cmd = "python3 -c \"print(chr(0x3042)*2_000_000)\""
    out = str(bash_tool._run({"command": cmd}))
    assert REPLACEMENT not in out, "マルチバイト境界で文字化けしている"
    # 畳んだことが本文に書かれている（黙って切らない）
    assert "中略" in out or "ここで切りました" in out, out[:300]
    # 末尾に「あ」が残っている（末尾を優先して残す設計）
    assert out.rstrip().endswith("あ"), out[-50:]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のクォート前提")
def test_マルチバイトの大出力は上限の中に収まる():
    from laps.tools import limits
    cap_bytes = limits.body_cap(30000)
    cmd = "python3 -c \"print(chr(0x3042)*2_000_000)\""
    out = str(bash_tool._run({"command": cmd}))
    # 「ここで切りました」の案内文自体も込みで、上限より極端に大きくはならない
    # （案内文の分だけ少し超えるのは許容し、元の 6,000,000 バイトには遠く及ばないことを見る）
    assert len(out.encode("utf-8")) < cap_bytes * 2, \
        f"畳みが効いていない: {len(out.encode('utf-8'))} bytes"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のクォート前提")
def test_ASCIIの大出力も境界で欠けずに末尾が残る():
    cmd = "python3 -c \"print('X'*2_000_000)\""
    out = str(bash_tool._run({"command": cmd}))
    assert REPLACEMENT not in out
    assert out.rstrip().endswith("X"), out[-50:]
    assert len(out) < 2_000_000, "畳みが効いていない"


def test_成功終了はokTrueでkindがok():
    out = bash_tool._run({"command": "echo hello"})
    assert out.ok is True
    assert out.kind == "ok"


def test_非0終了はokFalseでkindがerror():
    out = bash_tool._run({"command": "echo boom 1>&2; exit 7"})
    assert out.ok is False
    assert out.kind == "error"
    assert "[exit=7]" in out
    assert "boom" in out


def test_空出力の非0終了もokFalseでkindがerror():
    """出力が空でも、終了コードから成否は読めること（本文の言い回しに頼らない）。"""
    out = bash_tool._run({"command": "exit 3"})
    assert out.ok is False
    assert out.kind == "error"
    assert "exit 3" in out or "3" in out


def test_timeoutで打ち切られた結果もokFalseでkindがerror():
    out = bash_tool._run({"command": "sleep 30", "timeout": 1})
    assert out.ok is False
    assert out.kind == "error"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX のクォート前提")
def test_末尾が空行だけの大出力も黙って切らない(monkeypatch):
    """★2026-09-05 r4c 4 巡目（査読 must_fix#1・**実機 e2e で再現した回帰**）。

    `fold._fold_bytes` の「末尾が空白しか無いなら畳まない」枝は `clip_bytes` へ
    落ちるが、`bash.py` は 1 値版 `fold_head_tail` を呼んでいたので
    **切ったことがどこにも書かれなかった**。実測（この試験と同じ入力）:

        LAPS_N_CTX=16384 → body_cap 1,200
        6,700B → 1,200B・本文の開示語 0（「中略」も「ここで切りました」も無し）

    末尾の空行は `fold._tail_room(1200)`＝600B ぶんあれば足りる＝`print` や
    ログの末尾で普通に起きる。上の `test_マルチバイトの大出力…` と同じ意図
    （黙って切らない）を、**畳めない形**について固定する。
    """
    from laps.tools import fold, limits

    monkeypatch.setenv("LAPS_N_CTX", "16384")
    cap = limits.body_cap(30000)
    assert cap == 1200, cap
    assert fold._tail_room(cap, 0.4) == 600
    cmd = ("python3 -c \"import sys; sys.stdout.write('DATA'*1500); "
           "sys.stdout.write(chr(10)*700)\"")
    out = str(bash_tool._run({"command": cmd}))
    assert fold.nbytes(out) < 6700, "畳み/切り詰めが効いていない"
    assert any(w in out for w in ("中略", "ここで切りました", "先頭だけ")), (
        f"6,700B を {fold.nbytes(out)}B に切ったのに本文に何も書いていない:"
        f"\n…{out[-200:]!r}")
    # 嘘は書かない: 末尾は空行しか無いので「末尾は残しています」とは言わない。
    assert "末尾は残しています" not in out
