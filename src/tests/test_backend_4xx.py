"""一時的でない 4xx を投げ直さない（第三者レビュー D7・2026-08-30 に再現して修正）。

`raise_for_status()` の `HTTPStatusError` を retry 節が拾うため、401（鍵違い）や
404（宛先違い）でも **1.5+3+6 秒の backoff で 3 回**投げ直していた。
待っても答えは変わらず、利用者の沈黙が 11 秒延びて理由が遅れて届くだけ。
"""
from __future__ import annotations

import time

import httpx
import pytest

from laps import backend as B


def _backend(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls += 1
        return httpx.Response(status, json={"error": "nope"})
    handler.calls = 0
    b = B.Backend(B.BackendConfig(base_url="http://x/v1", model="m"))
    b._client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://x/v1")
    return b, handler


@pytest.mark.parametrize("status", [401, 403, 404, 405, 422])
def test_一時的でない4xxは1回で返す(status):
    b, h = _backend(status)
    t0 = time.time()
    with pytest.raises(B.BackendRejected) as e:
        b.chat([{"role": "user", "content": "x"}])
    assert h.calls == 1, f"HTTP {status} を {h.calls} 回投げている"
    assert time.time() - t0 < 1.0, "backoff で待っている"
    assert str(status) in str(e.value) and "http://x/v1" in str(e.value), "宛先と状態が文面に無い"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_一時的なものは従来どおり投げ直す(status):
    """★救ってはいけない側。429 と 5xx は待って投げ直すのが正しい。"""
    b, h = _backend(status)
    b.cfg.retry_base = 0.001
    with pytest.raises(Exception):
        b.chat([{"role": "user", "content": "x"}])
    assert h.calls > 1, f"HTTP {status} を投げ直していない（{h.calls} 回）"
