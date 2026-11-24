"""TodoWrite（帳簿）が**現実と食い違う返事をしない**こと。

★2026-08-23 devbench api-1 の事象列で実測:
  1. モデルが {"status": "done"} で5件全部を閉じた → 旧実装は "completed" しか認めず
     "OK: TODO updated (0/5 done)" → モデルは何も終わっていないと信じ、
     Glob から**最初からやり直した**（同じ出力の繰り返しの実体）。
  2. {"complete": "[\"a\", \"b\"]"}（JSON 文字列）→ 旧実装はそれを1件の新項目として追加
     （"1/6 done"）。帳簿がさらに現実から離れた。
帳簿が嘘をつくと、モデルは帳簿のほうを信じる。
"""
from __future__ import annotations

import json

import pytest

from laps.tools import todowrite, as_result


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    # ★単独実行でも動くように自分でセッションを用意する（2026-08-23 レビュー: 他の試験が
    #   os.environ に残した LAPS_SESSION_ID に依存していて、単独では 7/7 落ちた）。
    sid = "test_ledger_truth"
    monkeypatch.setenv("LAPS_SESSION_ID", sid)
    (tmp_path / "sessions" / sid).mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(inp):
    return as_result(todowrite.SPEC.handler(inp))


@pytest.mark.parametrize("word", ["completed", "done", "Done", "finished", "complete"])
def test_done_synonyms_count_as_done(home, word):
    r = _run({"todos": [{"content": "a", "status": word}, {"content": "b", "status": "pending"}]})
    assert "1/2 done" in str(r), str(r)


def test_json_string_list_is_read_as_a_list(home):
    _run({"todos": [{"content": "a", "status": "pending"}, {"content": "b", "status": "pending"}]})
    r = _run({"complete": json.dumps(["a", "b"])})
    assert r.ok and "2/2 done" in str(r), str(r)


def test_closing_an_unknown_item_is_refused_not_added(home):
    _run({"todos": [{"content": "a", "status": "pending"}]})
    r = _run({"complete": "zzz"})
    assert not r.ok
    assert "一覧に無い" in str(r) and "zzz" in str(r)
    assert "0/1 done" in str(r), "無い項目を新規追加して分母を増やしている"
