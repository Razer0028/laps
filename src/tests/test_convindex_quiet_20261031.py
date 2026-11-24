"""知らせるかの判定（convindex の quiet）と、自動の走行を一覧に載せないこと（2026-10-31 オーナー「通知がめっちゃ来ます」）。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from laps import convindex


def _session(home: Path, sid: str, *, secs: float | None, source: str = "app", outcome: str = "accepted") -> Path:
    d = home / "sessions" / sid
    d.mkdir(parents=True)
    t0 = time.time() - 500
    lines = [
        {"ts": t0, "type": "run_spec", "payload": {"prompt": "やって", "source": source}},
        {"ts": t0 + 400, "type": "result", "payload": {"outcome": outcome, "text": "できました", **({"secs": secs} if secs is not None else {})}},
    ]
    (d / "events.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return d


def _scan(home: Path) -> dict:
    changed, _ = convindex.Index(home).scan()
    return {x["sid"]: x for x in changed}


def test_short_run_is_quiet_and_long_run_is_not(tmp_path):
    _session(tmp_path, "a", secs=20.0)
    _session(tmp_path, "b", secs=300.0)
    got = _scan(tmp_path)
    assert got["a"]["quiet"] is True
    assert "quiet" not in got["b"]


def test_secs_is_the_last_run_not_the_whole_conversation(tmp_path):
    _session(tmp_path, "a", secs=5.0)                  # 記録の頭から尻までは 400 秒ある
    assert _scan(tmp_path)["a"]["secs"] == 5.0


def test_failure_of_a_short_run_is_quiet_too_but_unknown_length_rings(tmp_path):
    _session(tmp_path, "a", secs=3.0, outcome="crashed")
    d = _session(tmp_path, "b", secs=None, outcome="crashed")
    # 長さが分からないときは鳴らす（黙るほうが悪い）。記録の頭から測れてしまわないよう run_spec だけの形にする
    (d / "events.jsonl").write_text(json.dumps({"type": "result", "payload": {"outcome": "crashed", "text": "x"}}) + "\n", encoding="utf-8")
    got = _scan(tmp_path)
    assert got["a"]["status"] == "failed" and got["a"]["quiet"] is True
    assert got["b"]["status"] == "failed" and "quiet" not in got["b"]


def test_presence_silences_everything_while_it_is_fresh(tmp_path):
    _session(tmp_path, "a", secs=300.0)
    p = tmp_path / "remote" / "presence"
    p.parent.mkdir()
    p.write_bytes(b"")
    assert _scan(tmp_path)["a"]["quiet"] is True
    old = time.time() - convindex.PRESENCE_SECS - 5
    os.utime(p, (old, old))
    assert "quiet" not in _scan(tmp_path)["a"]


def _asking(home: Path, sid: str) -> None:
    d = home / "sessions" / sid
    d.mkdir(parents=True)
    lines = [{"type": "notice", "kind": "run_start", "source": "app", "prompt": "やって"},
             {"type": "ask", "question": "どちらにしますか", "ask": "a1"}]
    (d / "live.ndjson").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")


def test_a_question_always_rings_unless_someone_is_at_the_machine(tmp_path):
    _asking(tmp_path, "a")
    got = _scan(tmp_path)["a"]
    assert got["status"] == "asking" and "quiet" not in got
    p = tmp_path / "remote" / "presence"
    p.parent.mkdir()
    p.write_bytes(b"")
    assert _scan(tmp_path)["a"]["quiet"] is True


def test_batch_runs_stay_out_of_the_list(tmp_path):
    _session(tmp_path, "a", secs=300.0, source="batch")
    _session(tmp_path, "b", secs=300.0)
    ix = convindex.Index(tmp_path)
    changed, removed = ix.scan()
    assert [x["sid"] for x in changed] == ["b"] and not removed
    assert [x["sid"] for x in ix.all()] == ["b"]
    assert ix.scan() == ([], [])
