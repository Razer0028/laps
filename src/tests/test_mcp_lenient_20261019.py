"""MCP 呼び出しの取り違えを直す（★2026-10-19 走行記録 10-12〜19 の Mcp 失敗から）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.oc import mcp  # noqa: E402


class _Conn:
    def __init__(self, tools):
        self.tools = tools
        self.sent = []

    def request(self, method, params, timeout):
        self.sent.append(params)
        return {"content": [{"type": "text", "text": "ok"}]}


def _patch(monkeypatch, tools):
    conn = _Conn(tools)
    monkeypatch.setattr(mcp, "load_config", lambda: {"browser": {"command": "x"}})
    monkeypatch.setattr(mcp, "connect", lambda s, c: conn)
    return conn


SHOT = {"name": "browser_take_screenshot", "inputSchema": {
    "type": "object", "required": ["type", "scale"],
    "properties": {"type": {"type": "string", "default": "png"},
                   "scale": {"type": "string", "enum": ["css", "device"]}}}}
EVAL = {"name": "browser_evaluate", "inputSchema": {
    "type": "object", "required": ["function"],
    "properties": {"function": {"type": "string"}}}}
NET = {"name": "browser_network_requests", "inputSchema": {
    "type": "object", "required": ["static"], "properties": {"static": {"type": "boolean"}}}}


def test_choice_args_are_filled(monkeypatch):
    conn = _patch(monkeypatch, [SHOT, NET])
    r = mcp.call_tool("browser", "browser_take_screenshot", {})
    assert r.ok, str(r)
    assert conn.sent[-1]["arguments"] == {"type": "png", "scale": "css"}
    assert "filled in" in str(r)
    r = mcp.call_tool("browser", "browser_network_requests", {})
    assert r.ok and conn.sent[-1]["arguments"] == {"static": False}


def test_free_args_still_rejected(monkeypatch):
    conn = _patch(monkeypatch, [EVAL])
    r = mcp.call_tool("browser", "browser_evaluate", {})
    assert not r.ok and "function" in str(r)
    assert conn.sent == []


def test_fill_does_not_override_given():
    args = {"scale": "device"}
    assert mcp._fill_choice_args(args, ["type"], SHOT["inputSchema"]) == {"type": "png"}
    assert args == {"scale": "device", "type": "png"}


def test_nested_server_tool_lifted():
    inp, how = mcp.lift_call({"action": "call", "input": {"arguments": {"function": "() => 1"},
                                                          "server": "browser", "tool": "browser_evaluate"}})
    assert inp["server"] == "browser" and inp["tool"] == "browser_evaluate"
    assert inp["arguments"] == {"function": "() => 1"} and how
