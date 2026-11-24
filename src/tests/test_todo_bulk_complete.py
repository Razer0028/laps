"""予定をまとめて閉じられること（2026-08-19・実機の軌跡から）。

★何が起きていたか: 複雑な課題（6段・5ファイル）の実機3走行で、
  モデルは {"complete": "1件"} を **7回**呼び、
  **反復の約40%が予定の記帳**に消えていた（作業そのものは10手ほど）。

      step 3-7:   Write ×5      ← 実作業
      step 8:     Bash（失敗）
      step 9:     Edit
      step 10-11: Bash
      step 13:    差し戻し
      step 14-19: TodoWrite ×6  ← ★ここが全部「1件ずつ閉じる」

  原因は道具が list を受けず、**説明文にも書いていなかった**こと。
  ＝**できると教えていないことは行われない**（同日の「道具の呼び方」と同じ形）。
"""
from __future__ import annotations

import laps.tools.todowrite as tw


def _session(tmp_path, monkeypatch, body: str):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_SESSION_ID", "s1")
    d = tmp_path / "sessions" / "s1"
    d.mkdir(parents=True, exist_ok=True)
    (d / ".progress.md").write_text(body)
    return d / ".progress.md"


def test_複数をまとめて閉じられる(tmp_path, monkeypatch) -> None:
    p = _session(tmp_path, monkeypatch, "- [ ] a\n- [ ] b\n- [ ] c\n")
    tw.SPEC.handler({"complete": ["a", "b"]})
    body = p.read_text()
    assert "- [x] a" in body and "- [x] b" in body and "- [ ] c" in body, body


def test_1件でも従来どおり(tmp_path, monkeypatch) -> None:
    """★後方互換。既存の呼び方を壊さない。"""
    p = _session(tmp_path, monkeypatch, "- [ ] a\n- [ ] b\n")
    tw.SPEC.handler({"complete": "a"})
    assert "- [x] a" in p.read_text() and "- [ ] b" in p.read_text()


def test_schemaが配列を受ける() -> None:
    t = tw.SPEC.schema["properties"]["complete"]["type"]
    assert "array" in t and "string" in t, t


def test_説明文でまとめ方を教えている() -> None:
    """★道具が受けられても、教えていなければ使われない。"""
    d = tw.SPEC.description
    assert "まとめて" in d, "まとめて閉じられることが説明に無い"
    assert "complete=" in d


# --- 説明だけでは足りなかったので、結果の中でも伝える（2026-08-19） -----------
#
# 道具の説明と差し戻し文に書いたら 2/3 の走行で 20手 → 13手 に減ったが、
# 残り1本は書いた直後に**5回連続で1件ずつ**閉じた。説明は助言で、従わない回がある。
# **結果は必ず読まれる**ので、そこで言う。


