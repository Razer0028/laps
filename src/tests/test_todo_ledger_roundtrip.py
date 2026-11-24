"""TodoWrite の帳簿が、書いた通りに読み直せること（2026-09-02）。

★なぜこの試験があるか（実機の記録で確かめた）:
`.progress.md` は `- [ ] <文言>` の 1 行 1 項目で、`scratchpad.parse` は行頭で
区切る。文言に改行が混じると **その項目だけでなく帳簿全体が壊れる**:

    render([TodoItem("a\\nb"), TodoItem("ok")])
      → "- [ ] a\\nb\\n- [ ] ok\\n"
    parse(...)  → [("a"), ("- [ ] ok")]   ← 2 件目の文言が `- [ ] ok` に化ける

化けた文言は `complete` で閉じられない（「一覧に無い項目」）ので、モデルは
終わった作業を永久に閉じられず、帳簿を信じてやり直す——この道具が既に 2 度
直している「帳簿が現実と食い違う」形そのもの。

★分母と時期（正直に）: 実機 `~/.blackwall/sessions/*/events.jsonl` の
  TodoWrite 723 回のうち、改行を含む文言は **7 件**（すべて 2026-08-19）。
  以後この道具に入った修正（08-19 の一括 complete・08-23 の JSON 文字列）は
  どちらもこの経路に触れていないので、**今日も生きていた**（下で再現している）。
  下の文言は、その 7 件のうち session 20260819_015359_5d9b2d が実際に渡した物。
"""
from __future__ import annotations

import pytest

from laps.patches import scratchpad as sp
from laps.tools import todowrite as tw


#: 実機が実際に渡した文言（add と complete の両方に同じ物を渡していた）
REAL = "[1] calc.py 作成\n[2] test_calc.py 作成\n[3] pytest 実行"


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_SESSION_ID", "s1")
    return tmp_path / "sessions" / "s1" / ".progress.md"


def test_改行を含む文言でも帳簿が壊れない(session) -> None:
    tw._run({"add": REAL})
    tw._run({"add": "あとで消す"})
    items = sp.parse(session.read_text(encoding="utf-8"))
    # 2 件のまま、2 件目の文言が `- [ ] …` に化けていないこと
    assert len(items) == 2, [i.text for i in items]
    assert items[1].text == "あとで消す"


def test_自分が足した項目を同じ文言で閉じられる(session) -> None:
    """★片側だけ均すと、ここが落ちる（登録は 1 行・照合は元の複数行）。"""
    tw._run({"add": REAL})
    out = tw._run({"complete": REAL})
    assert "1/1 done" in out, out
    assert getattr(out, "ok", True) is True


def test_文言が空の項目は登録せず件数を偽らない(session) -> None:
    """空文字は `parse` の `(.+?)` に一致せず、書いた瞬間に消える＝
    「3 件送ったのに 2 件」になる。登録しない・**黙って減らさない**。"""
    out = tw._run({"todos": [{"content": "a"}, {"content": "  "}, {"content": "b"}]})
    assert "0/2 done" in out
    assert "空の項目 1 件" in out


def test_startにlistやJSON文字列が来ても落ちない(session) -> None:
    """`complete` は 08-23 に「モデルは list / JSON 文字列を渡す」と実測して
    直したのに、すぐ下の `start` は `start.strip()` のままだった（兄弟の残り）。"""
    tw._run({"start": ["工程1", "工程2"]})
    tw._run({"start": '["工程3"]'})
    items = sp.parse(session.read_text(encoding="utf-8"))
    assert [i.text for i in items] == ["工程1", "工程2", "工程3"]


def test_台帳はUTF8で読み書きする(session) -> None:
    """既定の符号化に任せると、UTF-8 でない機械（日本語 Windows の cp932）で
    日本語の項目が読めなくなる。**ファイルの中身がバイト列で UTF-8** であること。"""
    tw._run({"add": "日本語の項目 → ✓"})
    raw = session.read_bytes()
    assert "日本語の項目 → ✓".encode("utf-8") in raw
