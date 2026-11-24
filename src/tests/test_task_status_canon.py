"""TaskUpdate: 状態の綴りが下流と揃うこと（2026-09-02）。

★なぜ（下流を実際に読んで確かめた）: `_update` は受け取った status をそのまま
  書いていたが、読む側は**完全一致**で見ている:
    ・`_list_tasks`  … `status == "deleted"` のときだけ隠す
    ・`agent.py:1411` … `("pending","in_progress")` を「残っている予定」、
       `"completed"` を「片付いた予定」として、文脈が切れたときに運ぶ
  なので "done" や "in-progress" が入ると、その課題は**どちらの一覧にも載らない**
  ——消えたわけではないのに、覚え書きから静かに落ちる（監査 S8 で
  「自分で書いた予定を見失う」と指摘された、まさにその経路）。
  TodoWrite は同じ問題を `_DONE_WORDS` で 08-23 に直している。**片方だけ直っていた。**

★分母と時期（正直に）: 実機の TaskUpdate 16 回はすべて正規の綴り
  （completed 12 / in_progress 4）で、**この正規化は実機記録では 0 回発火する**。
  それでも入れた理由は 2 つだけ:
    ・分母が 16 しかない（「起きていない」ではなく「まだ見ていない」）
    ・**同じモデルが TodoWrite には "done" を渡した実測がある**（08-23 の記録）
  ＝新しい判定を足したのではなく、既に宣言済みの enum に実体を合わせただけ。
"""
from __future__ import annotations

import pytest

from laps.tools import task_mgmt as T


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_SESSION_ID", "s1")
    T._create({"subject": "工程1"})
    return tmp_path


@pytest.mark.parametrize("given,want", [
    ("done", "completed"), ("finished", "completed"), ("COMPLETE", "completed"),
    ("in-progress", "in_progress"), ("active", "in_progress"),
    ("todo", "pending"), ("cancelled", "deleted"),
])
def test_下流が読める綴りに揃える(session, given, want) -> None:
    T._update({"task_id": "1", "status": given})
    assert T._load()[0]["status"] == want


def test_揃えた結果が一覧の絞り込みと噛み合う(session) -> None:
    """"cancelled" は `deleted` になって一覧から消えること（消えないと、
    モデルは閉じたつもりの課題を何度も閉じにいく＝停滞の燃料）。"""
    T._update({"task_id": "1", "status": "cancelled"})
    assert T._list_tasks({}) == "(no active tasks)"


def test_読めない状態は黙って書かない(session) -> None:
    out = T._update({"task_id": "1", "status": "たぶんおわった"})
    assert getattr(out, "ok", True) is False
    assert "pending" in out and "completed" in out      # 使える値を示す
    assert T._load()[0]["status"] == "pending"          # 書き換えていない


def test_課題ファイルはUTF8で読み書きする(session) -> None:
    """`_save` は utf-8 固定、`_load` は既定の符号化——同じファイルの読みと
    書きが別の符号化だった。日本語 Windows(cp932)では件名に日本語が
    1 文字入るだけで課題表ごと読めなくなる。"""
    T._create({"subject": "日本語の件名 → ✓"})
    raw = (session / "sessions" / "s1" / "tasks.json").read_bytes()
    assert "日本語の件名 → ✓".encode("utf-8") in raw
    assert [t["subject"] for t in T._load()][-1] == "日本語の件名 → ✓"


def test_idが欠けた行があっても一覧が死なない(session) -> None:
    """壊れた 1 行のために TaskList 全体が KeyError で落ちていた。"""
    items = T._load()
    items.append({"subject": "id の無い課題", "status": "pending"})
    T._save(items)
    out = T._list_tasks({})
    assert "id の無い課題" in out
