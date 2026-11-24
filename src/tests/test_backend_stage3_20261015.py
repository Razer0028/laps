"""★2026-10-15 段 3（DESIGN-improvements-20261015.md の 1-E・推論機との線）。偽の推論機（httpx.MockTransport）で確かめる。

E1 思考の途中で崩れを切ると `</think>` が流れず、次の手番の本文が思考欄に出た。
E2 逐次（on_token あり）の chat() だけ、400 の溢れが要約に入らない・500 を投げ直さない・時間切れのやり直しで同じ文が二重に流れた。
E3 逐次の道具の経路で finish_reason が消え、calls が二重に数えられた。
E4 /props と /slots だけ proxy を使わず、proxy 経由でしか届かない機械で系統が "" になった（失敗も 1 分控えていた）。
E5 道具の経路が 401/403/404/422 を投げ直して待っていた。
E6 繰り返しの直後の「1 回だけ presence_penalty」が、一括へ落ちたときや点検の要求に先に使われた。
E7 繰り返しで切った後、last_prompt_tokens が前の要求の値のまま残った。
"""
from __future__ import annotations

import json
import types

import httpx
import pytest

from laps import backend as B

MSG = [{"role": "user", "content": "x"}]
TOOLS = [{"type": "function", "function": {"name": "Read", "parameters": {"type": "object", "properties": {}}}}]


def _sse(*objs, done=True) -> bytes:
    out = "".join(f"data: {json.dumps(o)}\n\n" for o in objs)
    return (out + ("data: [DONE]\n\n" if done else "")).encode()


def _delta(**d):
    return {"choices": [{"index": 0, "delta": d}]}


def _make(handler):
    b = B.Backend(B.BackendConfig(base_url="http://127.0.0.1:9/v1", model="m"))
    b.cfg.retry_base = 0.001
    b._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:9/v1")
    b._new_client = lambda: httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:9/v1")
    b._slots_busy = lambda: None
    b._await_release = lambda *a, **k: None
    return b


@pytest.fixture(autouse=True)
def _qwen(monkeypatch):
    monkeypatch.setenv("LAPS_MODEL_FAMILY", "qwen")
    monkeypatch.setenv("LAPS_THINK", "1")


class _Stream(httpx.SyncByteStream):
    """途中まで流してから読みの時間切れにする本文。"""

    def __init__(self, first: bytes):
        self.first = first

    def __iter__(self):
        yield self.first
        raise httpx.ReadTimeout("silent")


# ── E1 ─────────────────────────────────────────────────────────────────────

def test_E1_cut_inside_thinking_streams_the_close_tag(monkeypatch):
    monkeypatch.setattr(B, "_LOOPCUT_EVERY", 1)
    monkeypatch.setattr(B._loopcut, "degenerate_tail", lambda txt: "ab" if len(txt) > 20 else None)
    b = _make(lambda req: httpx.Response(200, content=_sse(*[_delta(reasoning_content="thinking ") for _ in range(8)])))
    got: list[str] = []
    with pytest.raises(B.DegenerateOutput):
        b.chat_with_tools(MSG, TOOLS, on_token=got.append)
    s = "".join(got)
    assert s.startswith(B.THINK_OPEN) and s.rstrip().endswith(B.THINK_CLOSE)




# ── E2 ─────────────────────────────────────────────────────────────────────

def test_E2_streamed_chat_turns_400_overflow_into_ContextOverflow():
    b = _make(lambda req: httpx.Response(400, json={"error": {"type": "exceed_context_size_error", "message": "x",
                                                              "n_prompt_tokens": 140000, "n_ctx": 131072}}))
    with pytest.raises(B.ContextOverflow) as e:
        b.chat(MSG, on_token=lambda s: None)
    assert e.value.n_prompt_tokens == 140000


def test_E2_streamed_chat_retries_a_500_and_still_shows_the_text():
    n = {"v": 0}

    def h(req):
        n["v"] += 1
        if n["v"] == 1:
            return httpx.Response(500, json={"error": {"message": "loading model"}})
        body = json.loads(req.content)
        if body.get("stream"):
            return httpx.Response(200, content=_sse(_delta(content="hello")))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})
    b = _make(h)
    got: list[str] = []
    assert b.chat(MSG, on_token=got.append) == "hello"
    assert "".join(got) == "hello" and n["v"] >= 2


def test_E2_timeout_after_text_was_streamed_does_not_stream_it_twice(monkeypatch):
    n = {"v": 0}

    def h(req):
        n["v"] += 1
        return httpx.Response(200, stream=_Stream(_sse(_delta(content="part one "), done=False)))
    b = _make(h)
    monkeypatch.setattr(b, "_peer_alive", lambda: True)
    got: list[str] = []
    with pytest.raises(RuntimeError):
        b.chat(MSG, on_token=got.append)
    assert "".join(got).count("part one") == 1 and n["v"] == 1


# ── E3 ─────────────────────────────────────────────────────────────────────

def test_E3_stream_keeps_finish_reason_and_counts_one_call():
    chunks = [_delta(content="hi"), {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
              {"choices": [], "usage": {"prompt_tokens": 1200, "completion_tokens": 5, "total_tokens": 1205}}]
    b = _make(lambda req: httpx.Response(200, content=_sse(*chunks)))
    b.chat_with_tools(MSG, TOOLS, on_token=lambda s: None)
    assert b.usage["calls"] == 1
    assert b.last_finish_reason == "length" and b.last_prompt_tokens == 1200


# ── E4 ─────────────────────────────────────────────────────────────────────



def test_E4_a_failed_family_lookup_is_retried_soon(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(B.time, "monotonic", lambda: clock["t"])
    monkeypatch.delenv("LAPS_MODEL_FAMILY")
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"model_path": "/m/Qwen3.6.gguf"})
    monkeypatch.setattr(httpx, "get", fake_get)
    B._FAMILY_CACHE.clear()
    assert B.model_family("http://flaky:8080/v1") == ""
    clock["t"] += 10.0
    assert B.model_family("http://flaky:8080/v1") == "qwen"


# ── E5 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_E5_tool_path_does_not_retry_permanent_4xx(status):
    n = {"v": 0}

    def h(req):
        n["v"] += 1
        return httpx.Response(status, json={"error": "nope"})
    b = _make(h)
    with pytest.raises(B.BackendRejected):
        b.chat_with_tools(MSG, TOOLS)
    assert n["v"] == 1


# ── E6 ─────────────────────────────────────────────────────────────────────

def test_E6_loop_escape_survives_a_check_request_and_a_failed_stream():
    sent = []

    def h(req):
        body = json.loads(req.content)
        sent.append((bool(body.get("tools")), bool(body.get("stream")), body.get("presence_penalty")))
        if body.get("stream"):
            return httpx.Response(500, json={"error": {"message": "busy"}})
        if body.get("tools"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "NO ISSUES"}, "finish_reason": "stop"}]})
    b = _make(h)
    b._loop_escape_next = True
    b.chat(MSG)                                                  # 点検の要求（道具なし）は旗を使わない
    assert sent[-1][2] == 0.0 and b._loop_escape_next
    b.chat_with_tools(MSG, TOOLS, on_token=lambda s: None)      # 逐次が 500 → 一括へ落ちる
    assert sent[-1] == (True, False, 1.5)                        # 一括の方に載る
    assert not b._loop_escape_next                               # 成功してから下ろす


# ── E7 ─────────────────────────────────────────────────────────────────────

def test_E7_last_prompt_tokens_is_cleared_when_the_request_reports_none():
    b = _make(lambda req: httpx.Response(200, content=_sse(_delta(content="cut short"))))   # usage の塊が来ない
    b.last_prompt_tokens = 50000
    b.last_finish_reason = "stop"
    b.chat_with_tools(MSG, TOOLS, on_token=lambda s: None)
    assert b.last_prompt_tokens is None and b.last_finish_reason is None
