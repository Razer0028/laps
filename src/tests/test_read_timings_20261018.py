"""★2026-10-18 推論機の読み直し・控え・生成の内訳を記録に残す（backend._record_usage・serve の engine:・要約の差）。

なぜ: 要約は 1 走行の中央値 163 秒・最長 33 分かかっていたが、記録には合計の秒しか無く、読み直し（控えが外れた）
のせいか生成（要約が長い）のせいかを分けられなかった。llama-server の timings は prompt_n・prompt_ms・cache_n を
返しているのに拾っていなかった。推論機は使わない（timings の形は 推論機 の llama.cpp の server-task.cpp で確かめた）。
"""
from __future__ import annotations

from laps.backend import Backend


def _bare():
    b = Backend.__new__(Backend)
    b.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reported_calls": 0}
    b.last_finish_reason = None
    b.last_prompt_tokens = None
    return b


def test_timings_accumulate_read_cached_and_generated():
    b = _bare()
    for prompt_n, cache_n in ((30000, 0), (420, 30100)):          # 1 回目は全部読む・2 回目は控えが当たる
        b._record_usage({"timings": {"prompt_n": prompt_n, "prompt_ms": prompt_n / 2.0, "cache_n": cache_n,
                                     "predicted_n": 200, "predicted_ms": 4000.0},
                         "usage": {"prompt_tokens": prompt_n + cache_n, "completion_tokens": 200,
                                   "total_tokens": prompt_n + cache_n + 200}})
    u = b.usage
    assert u["read_tokens"] == 30420 and u["cached_tokens"] == 30100
    assert u["read_ms"] == 15210 and u["gen_tokens"] == 400 and u["gen_ms"] == 8000


def test_missing_timings_do_not_break_or_invent_numbers():
    b = _bare()
    b._record_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
    b._record_usage({"timings": {"prompt_n": None, "cache_n": "x"}})
    assert "read_tokens" not in b.usage and "cached_tokens" not in b.usage


