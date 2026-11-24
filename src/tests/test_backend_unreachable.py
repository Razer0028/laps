"""推論機に届かないとき、**すぐに・理由を言って**失敗すること。

★2026-08-16 の敵対レビューが見つけた形:
`BackendConfig.timeout = 600.0` を httpx にスカラーで渡していたので、
**接続待ちまで 600 秒**だった。しかも接続失敗も再試行の対象で、
`max_retries=3` と合わせて最長 600×4 秒。その間アプリは1バイトも受け取らず、
画面には理由が一言も出ない（利用者からは「固まった」に見える）。

相手が居ないことは数秒で分かる。分からないふりをして待たない。
"""
from __future__ import annotations

import time

import pytest

from laps.backend import Backend, BackendConfig

#: 予約アドレス（RFC 5737 TEST-NET-1）。経路が無く、確実に接続できない。
UNREACHABLE = "http://192.0.2.1:9/v1"


def _backend() -> Backend:
    return Backend(BackendConfig(base_url=UNREACHABLE, connect_timeout=2.0, max_retries=3))


def test_届かない相手に何十分も待たない() -> None:
    t0 = time.time()
    with pytest.raises(RuntimeError) as e:
        _backend().chat([{"role": "user", "content": "x"}])
    elapsed = time.time() - t0
    # 接続上限2秒 × 再試行しない。余裕を見ても 10 秒を超えるなら回帰。
    assert elapsed < 10, f"接続できない相手に {elapsed:.0f} 秒待った（再試行が復活している）"
    assert "接続できません" in str(e.value)


def test_失敗の文面が宛先を名指しする() -> None:
    """どこへ繋ごうとして失敗したかが出ないのが、この種の障害で最も時間を溶かす。"""
    with pytest.raises(RuntimeError) as e:
        _backend().chat([{"role": "user", "content": "x"}])
    assert UNREACHABLE in str(e.value)


def test_接続と生成で上限が別々になっている() -> None:
    cfg = BackendConfig()
    assert cfg.connect_timeout < cfg.timeout, "接続待ちが生成待ちと同じ＝落ちている相手を延々と待つ"
