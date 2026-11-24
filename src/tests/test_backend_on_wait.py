"""read timeout で送り直す瞬間を外へ知らせる（2026-09-02）。黙って送り直すと利用者には沈黙。"""
from __future__ import annotations

import httpx

from laps.backend import Backend


def test_on_wait_called_per_retry_when_peer_alive(monkeypatch):
    b = Backend()
    monkeypatch.setattr(b, "_peer_alive", lambda: True)
    seen: list[tuple[int, float]] = []
    b.on_wait = lambda attempt, waited: seen.append((attempt, waited))
    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx.ReadTimeout("silent")
        return "ok"
    assert b._stream_once_retry(fn) == "ok"
    assert [a for a, _ in seen] == [1, 2]
    assert all(w == b._read_to for _, w in seen)


def test_on_wait_not_called_when_peer_dead(monkeypatch):
    b = Backend()
    monkeypatch.setattr(b, "_peer_alive", lambda: False)
    seen = []
    b.on_wait = lambda a, w: seen.append(a)
    def fn():
        raise httpx.ReadTimeout("silent")
    try:
        b._stream_once_retry(fn)
    except RuntimeError:
        pass
    assert seen == []


def test_agent_turns_wait_into_engine_retry_notice():
    from laps.oc.base import Agent
    got = []
    a = Agent(on_event=lambda t, p: got.append((t, p)))
    assert a.backend.on_wait is not None
    a.backend.on_wait(2, 90.0)
    assert got and got[-1][0] == "notice"
    p = got[-1][1]
    assert p["kind"] == "engine_retry" and p["attempt"] == 2 and "90" in p["message"]
