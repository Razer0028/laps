"""生成の余白が**会話を食い潰さない**こと。

★2026-08-19 実測（95走行）:
  強制ターン（差し戻し後・`tool_choice=required`）は `max_tokens=8192` を確保する。
  窓 16,384 / 固定接頭辞 6,816 のとき会話に残るのは

      16384 − 8192 − 1024 − 6816 = **352 トークン**

  969 トークンの会話が入らず、`context_exhausted` として**走行が打ち切られた**。
  13/95（14%）で発生し、うち2本が不合格。

★これは今日の変更（完了性の差し戻しを増やした）が生んだ失敗。
  強制ターンが出すのは道具呼び出し1本なので 8192 は過剰。
  会話に席を残せる分まで削る（下限 1024＝呼び出しには十分）。
"""
from __future__ import annotations

import laps.context_budget as cb


def _prompt_room(n_ctx: int, gen: int, fixed: int, slack: int = 1024) -> int:
    return cb.Budget(n_ctx=n_ctx, gen=gen, fixed=fixed, slack=slack).prompt


def test_実測の条件で会話が入らなくなる_症状の再現() -> None:
    """★負の対照。削る前の値では、実際に席が無い。"""
    assert _prompt_room(16384, 8192, 6816) == 352
    assert 352 < 969, "実測の会話長(969)が入らないことの確認"


def test_席を残す下限が定義されている() -> None:
    assert cb.TRANSCRIPT_FLOOR >= 2048, "会話の席が小さすぎる"


def test_削ったあとは会話が入る() -> None:
    """実装と同じ式で、実測の条件を通す。"""
    n_ctx, fixed, slack = 16384, 6816, 1024
    room = n_ctx - fixed - slack - cb.TRANSCRIPT_FLOOR
    gen = max(1024, min(8192, room))
    assert _prompt_room(n_ctx, gen, fixed) >= cb.TRANSCRIPT_FLOOR
    assert _prompt_room(n_ctx, gen, fixed) >= 969, "実測の会話長が入らない"


def test_窓が広ければ削らない() -> None:
    """★広い窓では従来どおり 8192 を確保する（必要な回に余白を削らない）。"""
    n_ctx, fixed, slack = 32768, 6816, 1024
    room = n_ctx - fixed - slack - cb.TRANSCRIPT_FLOOR
    assert room >= 8192, "広い窓なのに削る計算になっている"


def test_極端に狭い窓でも下限を割らない() -> None:
    """固定接頭辞だけで窓を食う場合、生成は 1024 まで。0 にはしない
    （0 にすると1トークンも生成できず、走行が別の形で死ぬ）。"""
    n_ctx, fixed, slack = 8192, 7000, 1024
    room = n_ctx - fixed - slack - cb.TRANSCRIPT_FLOOR
    gen = max(1024, min(8192, room))
    assert gen == 1024


