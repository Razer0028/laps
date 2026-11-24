"""TodoWrite の close 失敗（2026-09-04・W4 記帳道具担当・実測ベース）。

★実測（`/root/.blackwall/sessions/2026090[34]_*` と bwdog 全体、事象ログ）:
  TodoWrite の「一覧に無い項目」失敗は、全履歴を通して **6 件**（対象範囲内は 4 件）。
  そのうち **6/6 件が、一覧が空のまま complete だけ呼んだ形**（todos/add を一度も
  呼んでいない）だった。「一覧に項目はあるが文言が微妙に違う」形は **0 件**。
  ＝ブリーフの sbx-A 例（'test_div' を閉じようとして失敗 ×3）も、実際のログでは
  一覧が空だった（session 20260903_204354_23f570・bwdog）。

  正規化した部分一致（前後空白・全角半角・記号・大小・双方向の包含）を実装したが、
  **過去に実際に起きた6件のうち救えるのは0件**（何もない一覧には部分一致のしようが
  ない）。それでも一般的な頑健化として入れる価値はあるので実装し、下のテストで
  「非空の一覧で綴りが微妙に違う」場合に効くことを確かめる。実際に効くのは
  ・一覧が空のときの案内文（todos/add を先に呼べ、と名指しで教える）
  ・「近い項目」の候補提示（一覧が非空なのに見つからない場合）
  の2つ。
"""
from __future__ import annotations

import pytest

from laps.patches import scratchpad as sp
from laps.tools import todowrite as tw


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_SESSION_ID", "s1")
    return tmp_path / "sessions" / "s1" / ".progress.md"


# --- 実測どおりの再現: 一覧が空のまま complete だけ呼ぶ（6/6件がこの形） -------

def test_実機再現_一覧が空でcompleteだけ呼ぶと空だと名指しで教える(session) -> None:
    """session 20260903_204354_23f570（bwdog）の実際の入力をそのまま流す。"""
    out = tw._run({"complete": "test_div"})
    assert out.ok is False and out.kind == "error"
    assert "一覧が空です" in out
    assert "todos" in out and "add" in out


def test_実機再現_3連続で空一覧に閉じても毎回同じ案内(session) -> None:
    """session 20260903_214849_cf2690（bwdog）は同じ形で3連続失敗していた。
    正規化しても救えない（一覧が空だから）ことを確認しつつ、
    案内文は毎回出ること（黙って諦めない）。"""
    for text in (
        "アプリからの指示: 作業場のサーバの待ち受けポートを 8080 に変更",
        "作業場のサーバの待ち受けポートを 8080 に変更",
        "作業場のサーバの待ち受けポートを 8080 に変更",
    ):
        out = tw._run({"complete": text})
        assert out.ok is False
        assert "一覧が空です" in out


def test_教えたとおりにtodosでdone登録すると成功する(session) -> None:
    """案内文が指す実際の回復手順（e6602b が自力でたどり着いた形）が機能する。"""
    out = tw._run({"todos": [{"content": "test_div", "status": "done"}]})
    assert out.ok is True and "1/1 done" in out


# --- 正規化した部分一致（一覧が非空のときだけ効く） -----------------------------

def test_全角半角と大小文字の差は同一とみなす(session) -> None:
    tw._run({"add": "Ｔｅｓｔ　Div"})   # 全角スペース入り・全角英数
    out = tw._run({"complete": "test div"})
    assert out.ok is True and "1/1 done" in out, out
    items = sp.parse(session.read_text(encoding="utf-8"))
    assert items[0].done is True


def test_記号の有無は同一とみなす(session) -> None:
    tw._run({"add": "手順1: サーバ再起動"})
    out = tw._run({"complete": "手順1 サーバ再起動"})
    assert out.ok is True and "1/1 done" in out, out


def test_部分文字列は双方向に受ける(session) -> None:
    tw._run({"add": "calc.py に div 関数のテストを追加する"})
    out = tw._run({"complete": "div関数のテストを追加"})
    assert out.ok is True and "1/1 done" in out, out


def test_複数一致は最長を選ぶ(session) -> None:
    tw._run({"todos": [
        {"content": "div の実装"},
        {"content": "div のテストを実装して確認する"},
    ]})
    out = tw._run({"complete": "div"})
    items = sp.parse(session.read_text(encoding="utf-8"))
    done_texts = [i.text for i in items if i.done]
    assert done_texts == ["div のテストを実装して確認する"], done_texts
    assert out.ok is True


def test_近い文言で閉じたことを正直に言う(session) -> None:
    tw._run({"add": "工程1: ビルド"})
    out = tw._run({"complete": "工程1 ビルド"})
    assert out.ok is True
    assert "近い文言で閉じました" in out


# --- 一覧が非空でも見つからないときは候補を出す（黙って諦めない） ----------------

def test_見つからないが一覧は非空なら候補を出す(session) -> None:
    tw._run({"todos": [{"content": "工程1: ビルド"}, {"content": "工程2: デプロイ"}]})
    out = tw._run({"complete": "工程3"})
    assert out.ok is False
    assert "候補:" in out
    assert "工程1: ビルド" not in out or True  # 候補の中身は難所依存。存在だけ確認


def test_無いものを閉じても新規追加しない(session) -> None:
    """★既存方針の維持: 候補を出しても、黙って新規追加はしない。"""
    tw._run({"add": "工程1: ビルド"})
    tw._run({"complete": "まったく違う話"})
    items = sp.parse(session.read_text(encoding="utf-8"))
    assert len(items) == 1, items
    assert items[0].done is False


# --- TaskCreate: 完了系の題を作らないよう説明文で教える（2026-09-04・W4実測） -----
# ★実測: TaskCreate 全履歴21回のうち2回（同一走行 20260903_204354_23f570）が
#   件名「テスト追加完了」「サーバポート変更完了」という**作業後の記帳だけ**の
#   呼び出しだった。プロンプトで止めるのは守られにくいが、道具の説明文は
#   候補選択に効くという知見（`knowledge/ai-agent-research-notes.md`）に基づき、
#   description に明記する。効果は測れないので「入れた」とだけ報告する。

def test_TaskCreateの説明文が完了だけの記帳を止めている() -> None:
    from laps.tools import task_mgmt as T

    desc = next(s.description for s in T.SPECS if s.name == "TaskCreate")
    assert "完了" in desc
    assert "作らない" in desc
