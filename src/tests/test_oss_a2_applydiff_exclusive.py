"""ApplyDiff と Edit を同時に宣言しない（2026-09-04・OSS 由来 A2）。

出典: aider/cline — 同種の編集道具は排他で出す。
`REPORTS/oss-agents-study-20260902.md` §A2。

★測定（root+bwdog の全走行履歴 498 本・`/root/.blackwall/sessions` と
  `/home/bwdog/.blackwall/sessions` の events.jsonl を実走査・2026-09-04）:
    ApplyDiff: calls=1 fails=0（生涯で1回だけ・成功）
    Edit:      calls=577 fails=119（20.6%）
  60-case bench でも ApplyDiff は 0 回（`tools/__init__.py` FOCUSED_TOOLS の
  コメントに既に記載）。2つの独立した実配列が同じ結論（実質未使用）に落ちる。
  → 既定の道具表（full）から ApplyDiff を落とし、Edit だけを広告する。

能力は消していない: `apply_diff.py` はそのまま・SPEC は `all_specs()` に残る。
"""
from __future__ import annotations

from laps import tools as tools_mod


def test_full既定でApplyDiffは広告されずEditは残る(monkeypatch):
    monkeypatch.delenv("LAPS_TOOLSET", raising=False)     # 未指定＝full
    monkeypatch.delenv("LAPS_APPLYDIFF", raising=False)
    reg = tools_mod.by_name()
    assert "ApplyDiff" not in reg, "ApplyDiff が既定の道具表に残っている"
    assert "Edit" in reg, "Edit まで巻き添えで落ちている"


def test_focusedでも元から不在で一貫している(monkeypatch):
    monkeypatch.setenv("LAPS_TOOLSET", "focused")
    monkeypatch.delenv("LAPS_APPLYDIFF", raising=False)
    reg = tools_mod.by_name()
    assert "ApplyDiff" not in reg
    assert "Edit" in reg


def test_all_specsにはApplyDiffのSPECが残る_能力は消していない():
    names = {t.name for t in tools_mod.all_specs()}
    assert "ApplyDiff" in names, "SPEC まで消してしまっている（能力を減らした）"
    assert "Edit" in names
    # ★2026-09-21: 22→23（Verify を追加。「直した」と言う前に走らせる道具が無かった）。
    #   ★道具数は選択精度に効くので、増やしたら必ずここを通す（黙って増えない）。
    assert len(tools_mod.all_specs()) == 23


def test_escape_hatchで両方戻る(monkeypatch):
    monkeypatch.delenv("LAPS_TOOLSET", raising=False)
    monkeypatch.setenv("LAPS_APPLYDIFF", "1")
    reg = tools_mod.by_name()
    assert "ApplyDiff" in reg
    assert "Edit" in reg


def test_落ちるのはApplyDiffの1本だけ(monkeypatch):
    """escape hatch の有無で差が出るのは ApplyDiff の1本だけ（巻き添えゼロ）。"""
    monkeypatch.delenv("LAPS_TOOLSET", raising=False)
    monkeypatch.setenv("LAPS_APPLYDIFF", "1")
    with_applydiff = set(tools_mod.by_name())
    monkeypatch.setenv("LAPS_APPLYDIFF", "0")
    without_applydiff = set(tools_mod.by_name())
    assert with_applydiff - without_applydiff == {"ApplyDiff"}


def test_apply_diffモジュールは生きたまま_Editの失敗経路の翻訳器として使える():
    """★誤爆止め: 道具として選ばれなくなっても、unified diff 翻訳・照合カスケード等の
    コードは `apply_diff.py` にそのまま残る（消すのは宣言だけ）。"""
    from laps.tools import apply_diff
    # SEARCH/REPLACE ブロックの分解は道具の宣言と無関係に動く。
    blocks = apply_diff._parse_blocks(
        "a.py\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n")
    assert blocks and blocks[0]["path"] == "a.py"
    # unified diff → SEARCH/REPLACE への翻訳器も生きている。
    uni = ("diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
          "@@ -1,1 +1,1 @@\n-old\n+new\n")
    ublocks = apply_diff._blocks_from_unified(uni)
    assert ublocks and ublocks[0]["path"] == "b.py"
