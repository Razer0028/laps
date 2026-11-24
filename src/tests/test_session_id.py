"""測定する側がセッション識別子を先に決められること。

★これが無いと、走行が時間切れで落ちたとき run.log に
  `session: ...` が出ず、results.tsv の sid が空になり、
  **何が起きたか後から追えない走行**ができる。
"""
from __future__ import annotations

import pytest

from laps import session as S


def test_auto_id_is_unique(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path))
    monkeypatch.delenv("LAPS_NEW_SESSION_ID", raising=False)
    assert S._new_session_id() != S._new_session_id()


def test_env_id_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path))
    monkeypatch.setenv("LAPS_NEW_SESSION_ID", "bench_arm_raw_block07")
    assert S._new_session_id() == "bench_arm_raw_block07"


def test_collision_fails_loudly_instead_of_merging(monkeypatch, tmp_path):
    """★黙って混ぜない。2走行が同じ記録に追記すると後から分離できない。"""
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path))
    monkeypatch.setenv("LAPS_NEW_SESSION_ID", "dup")
    (tmp_path / "dup").mkdir(parents=True)
    with pytest.raises(ValueError, match="既に存在"):
        S._new_session_id()


@pytest.mark.parametrize("bad", ["../escape", "a/b", "x\\y", "dot.ted"])
def test_path_traversal_is_rejected(monkeypatch, tmp_path, bad):
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path))
    monkeypatch.setenv("LAPS_NEW_SESSION_ID", bad)
    with pytest.raises(ValueError):
        S._new_session_id()


def test_agent_channel_is_not_hijacked(monkeypatch, tmp_path):
    """★`LAPS_SESSION_ID` は Agent.run が道具へ「いまどのセッションか」を
    伝える既存の経路。生成の指定に使ってはいけない。

    実機で壊した形（v0.1.74）: 生成側がこの変数を読む実装にしたところ、
    1回目の走行が自分の id をここに設定し、**2回目の生成がそれを
    「外部からの指定」と読んで「既に存在します」で落ちた**。
    実機は 0 秒・道具0本・本文0字で failed。
    ★1つの変数を「いま何か」と「これから何を作るか」の両方に使わない。
    """
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path))
    monkeypatch.delenv("LAPS_NEW_SESSION_ID", raising=False)
    # 前の走行が残した「いまのセッション」。生成はこれを見てはいけない。
    (tmp_path / "20260820_003027_321105").mkdir(parents=True)
    monkeypatch.setenv("LAPS_SESSION_ID", "20260820_003027_321105")
    sid = S._new_session_id()          # 例外を出さず、新しい id を作る
    assert sid != "20260820_003027_321105"
    assert not (tmp_path / sid).exists()
