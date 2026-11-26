"""リモートで開いたときに思考と本文の 1 字ずつを 1 行にまとめる（2026-11-27 オーナー「長期タスクのセッションをリモートのデバイスから
開くと最初からロードされるからめちゃくちゃ重くなる（クラッシュもする）」）。gateway.coalesce_stream。"""
from __future__ import annotations

import json

from laps.gateway import coalesce_stream


def _ev(seq, type_, text=None, **kw):
    d = {"v": 1, "seq": seq, "ts": 1.0 + seq, "type": type_, "id": "r1", **kw}
    if text is not None:
        d["text"] = text
    return json.dumps(d, ensure_ascii=False)


def test_runs_of_think_and_token_become_one_line_each():
    lines = [_ev(1, "iter_start", iter=0), _ev(2, "think", "The"), _ev(3, "think", " user"), _ev(4, "think", " asks"),
             _ev(5, "tool_call", name="Read"), _ev(6, "token", "Hel", iter=1), _ev(7, "token", "lo", iter=1),
             _ev(8, "token", "!", iter=2), _ev(9, "done")]
    out = [json.loads(x) for x in coalesce_stream(lines)]
    assert [o["type"] for o in out] == ["iter_start", "think", "tool_call", "token", "token", "done"]
    assert out[1]["text"] == "The user asks" and out[1]["seq"] == 4 and out[1]["seq_from"] == 2
    assert out[3]["text"] == "Hello" and out[3]["seq"] == 7, "同じ手番の本文はまとめる"
    assert out[4]["text"] == "!" and "seq_from" not in out[4], "手番が違えば分ける・1 行だけの物は元の形"


def test_other_ids_and_bad_lines_are_kept_in_order():
    lines = [_ev(1, "think", "a"), json.dumps({"v": 1, "seq": 2, "type": "think", "id": "r2", "text": "b"}),
             "not json", _ev(3, "think", "c")]
    out = coalesce_stream(lines)
    assert len(out) == 4 and out[2] == "not json"
    assert [json.loads(x)["text"] for x in (out[0], out[1], out[3])] == ["a", "b", "c"]


def test_text_is_unchanged_after_merging():
    lines = [_ev(i, "think", f"w{i} ") for i in range(1, 2001)] + [_ev(2001, "done")]
    out = coalesce_stream(lines)
    assert len(out) == 2
    assert json.loads(out[0])["text"] == "".join(f"w{i} " for i in range(1, 2001))
