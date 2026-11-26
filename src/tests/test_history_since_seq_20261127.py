"""ほかの機械の会話を 2 回目から続きだけ読む（2026-11-27 オーナー「Claude に寄せて」）。relayauth.history の since_seq。"""
from __future__ import annotations

import json

from laps import relayauth

LINES = [
    {"seq": 1, "ts": 1, "type": "run_spec", "payload": {"prompt": "A"}},
    {"seq": 2, "ts": 2, "type": "assistant", "payload": {"text": "a1"}},
    {"seq": 3, "ts": 3, "type": "result", "payload": {"text": "a1", "outcome": "accepted"}},
    {"seq": 4, "ts": 4, "type": "run_spec", "payload": {"prompt": "B"}},
    {"seq": 5, "ts": 5, "type": "tool_start", "payload": {"name": "Read", "input": {"p": 1}}},
    {"seq": 6, "ts": 6, "type": "assistant", "payload": {"text": "b1"}},
    {"seq": 7, "ts": 7, "type": "result", "payload": {"text": "b1", "outcome": "accepted"}},
]


def _fake(monkeypatch, seen):
    def get(path, relay=None, timeout=0):
        seen.append(path)
        since = int(path.split("since_seq=")[1]) if "since_seq=" in path else 0
        return "\n".join(json.dumps(x) for x in LINES if x["seq"] > since) + "\n"
    monkeypatch.setattr(relayauth, "http_get_text", get)


def test_full_then_tail(monkeypatch):
    seen: list = []
    _fake(monkeypatch, seen)
    r = relayauth.json_call({"op": "history", "device": "dev1", "sid": "s1"})
    assert r["ok"] and r["seq"] == 7 and [x["prompt"] for x in r["runs"]] == ["A", "B"]
    r2 = relayauth.json_call({"op": "history", "device": "dev1", "sid": "s1", "since_seq": 5})
    assert "since_seq=5" in seen[-1]
    assert r2["seq"] == 7 and len(r2["runs"]) == 1
    cont = r2["runs"][0]
    assert cont["cont"] and cont["finished"] and [e["type"] for e in cont["events"]] == ["assistant", "result"]


def test_nothing_new(monkeypatch):
    _fake(monkeypatch, [])
    r = relayauth.json_call({"op": "history", "device": "dev1", "sid": "s1", "since_seq": 7})
    assert r["ok"] and r["runs"] == [] and r["seq"] == 7
