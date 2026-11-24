"""MCP サーバは、出荷で venv の中身が入れ替わったら新しい版で起動し直す（2026-11-16）。

11-14 の改名より前から動いていた MCP サーバは、desktop を呼ぶと「cannot import name 'ctlclient' from 'blackwall'」で落ちた
（古いコードが、もう無い場所から部品を後で読もうとした）。読みかけの要求は、起動し直した側が答える。
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

import pytest

from laps import mcpserver


class _Exec(Exception):
    pass


def _stdin(monkeypatch, data: bytes):
    r, w = os.pipe()
    os.write(w, data)
    os.close(w)
    monkeypatch.setattr(sys, "stdin", type("S", (), {"fileno": staticmethod(lambda: r)})())
    return r


def test_restarts_when_the_installed_version_changes_and_hands_over_the_request(monkeypatch):
    versions = iter(["0.1.1", "0.1.2"])
    monkeypatch.setattr(mcpserver, "_version", lambda: next(versions, "0.1.2"))
    seen = {}

    def fake_exec(exe, argv):
        seen["argv"] = argv
        raise _Exec()

    monkeypatch.setattr(os, "execv", fake_exec)
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.delenv(mcpserver._RESUME_ENV, raising=False)
    req = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
    _stdin(monkeypatch, json.dumps(req).encode() + b"\n")
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", type("O", (), {"buffer": out, "flush": lambda self: None})())
    with pytest.raises(_Exec):
        mcpserver.main()
    assert out.getvalue() == b""                       # 古い版では答えない
    assert seen["argv"][0] == sys.executable
    # 起動し直した側: 引き継いだ要求に答える
    monkeypatch.setattr(mcpserver, "_version", lambda: "0.1.2")
    _stdin(monkeypatch, b"")
    assert mcpserver.main() == 0
    resp = json.loads(out.getvalue().decode().strip())
    assert resp["id"] == 7 and resp["result"] == {}
    assert mcpserver._RESUME_ENV not in os.environ


def test_same_version_answers_every_line():
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    data = "".join(json.dumps(r) + "\n" for r in reqs).encode()
    p = subprocess.run([sys.executable, "-m", "laps.mcpserver"], input=data, capture_output=True, timeout=60)
    rows = [json.loads(x) for x in p.stdout.decode().splitlines() if x.strip()]
    assert [r["id"] for r in rows] == [1, 2]
    assert any(t["name"] == "desktop" for t in rows[1]["result"]["tools"])
