"""要約の後に残す量を絞る（2026-11-26 オーナー「会話が長くなるとコンテキスト圧縮率が落ちるけどどうにかなります？」）。
- 新しい依頼の頭の要約では、そのまま残す最近の分を 1.5 万 → 6 千（compact.BOUNDARY_TAIL）。
- 要約の後ろに付ける帳面は、要約の Verified Facts・Dead Ends と同じ行と、ほかの会話の行を付けない。
- 作業ログの塊は付け直さず、場所だけ。
- LAPS_COMPACT_LEAN=0 で前の形に戻る。
★覚え書きは設定の置き場（~/.config）に書かれるので、試験では XDG_CONFIG_HOME を一時フォルダーへ向ける。
"""
from __future__ import annotations

import json

import pytest

from tests.test_engine_20261030 import Fake, _serve  # noqa: F401

from laps.oc import findings


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    for k in ("LAPS_COMPACT_EARLY", "LAPS_COMPACT_AT", "LAPS_COMPACT_LEAN"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def _history() -> list[dict]:
    h = []
    for i in range(5):                                  # 古くて大きい分（約 10 万トークン）
        h.append({"role": "user", "content": f"q{i} " + "a" * 40_000})
        h.append({"role": "assistant", "content": f"r{i} " + "b" * 40_000})
    for i in range(10):                                 # 最近の小さな分（1 件 約 500 トークン・20 件で約 1 万）
        h.append({"role": "user", "content": f"small-q{i} " + "c" * 2_000})
        h.append({"role": "assistant", "content": f"small-r{i} " + "d" * 2_000})
    return h


def _kept_small(body: dict) -> int:
    return sum(1 for m in body["messages"] if str(m.get("content") or "").startswith(("small-q", "small-r")))


def _work_after_boundary(cfg, name: str) -> dict:
    fake = Fake([{"text": "## Objective\n- keep going\n"}, {"text": "答え。"}], n_ctx=262144)
    home, ws = cfg / f"h-{name}", cfg / f"w-{name}"
    ws.mkdir()
    ev = _serve(fake, home, ws, [{"type": "run", "id": "r1", "messages": _history() + [{"role": "user", "content": "次の依頼"}]}], 1)
    assert ev[-1]["type"] == "done" and ev[-1]["counters"].get("compaction_boundary") == 1
    return fake.bodies[1]


def test_boundary_summary_keeps_a_smaller_recent_tail(cfg, monkeypatch):
    monkeypatch.setenv("LAPS_COMPACT_EARLY", "100000")   # 境目の要約は 2026-11-27 から既定で切ってある
    lean = _work_after_boundary(cfg, "lean")
    monkeypatch.setenv("LAPS_COMPACT_LEAN", "0")
    old = _work_after_boundary(cfg, "old")
    assert _kept_small(old) >= 18, "前の形は最近の 1.5 万を残す（小さな分はほぼ全部入る・残す分は答えから始まる）"
    assert 0 < _kept_small(lean) <= 12 < _kept_small(old), "新しい依頼の頭の要約では最近の分を約 6 千だけ残す"
    assert "次の依頼" in json.dumps(lean["messages"], ensure_ascii=False), "新しい依頼は残る"


SUMMARY = """## Objective
- fix the parser

## Verified Facts
- parser.py line 40 raises KeyError on empty input
- tests/test_parser.py has 12 cases

## Dead Ends
- pinning lxml to 4.9 did not help
"""


def test_ledger_after_summary_skips_lines_the_summary_already_has(tmp_path):
    ws = str(tmp_path / "ws")
    findings.absorb(ws, SUMMARY, session_id="me")
    findings.absorb(ws, "## Verified Facts\n- build.sh needs GCC 12 on the server\n", session_id="other")
    # この会話の新しい事実（要約には無い）
    findings.absorb(ws, "## Verified Facts\n- config.yaml sets retries to 3\n", session_id="me")
    full = findings.context_block(ws, session_id="me")
    assert "KeyError" in full and "GCC 12" in full, "前の形はこの会話の行もほかの会話の行も付ける"
    lean = findings.context_block(ws, findings.CTX_CHARS_LEAN, session_id="me", others=False, skip=SUMMARY)
    assert "retries to 3" in lean, "要約に無いこの会話の事実は付ける"
    assert "KeyError" not in lean and "lxml" not in lean, "要約に既にある行は付け直さない"
    assert "GCC 12" not in lean, "ほかの会話の行は要約の後には付けない（会話の始めには付く）"


def test_ledger_pointer_when_everything_is_in_the_summary(tmp_path):
    ws = str(tmp_path / "ws")
    findings.absorb(ws, SUMMARY, session_id="me")
    lean = findings.context_block(ws, findings.CTX_CHARS_LEAN, session_id="me", others=False, skip=SUMMARY)
    assert str(findings.ledger_path(ws)) in lean and "already in the summary" in lean
    assert "KeyError" not in lean
