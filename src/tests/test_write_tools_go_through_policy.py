"""パスを入力から受け取って書く道具は、**必ず実行ゲートを通る**こと。

★実機で出荷されていた穴（2026-08-21・敵対レビューで判明）:
  NotebookEdit が policy を **import すらしていなかった**。
  full（アプリの既定）に入って出荷されていたので、全モードで
  JSON として解けるファイル（settings.json 等）を書き換えられ、
  監査に1行も残らなかった。Write / Edit は同じ検査を通っている
  ＝**同じ守りの片側だけが欠けていた**。

★宣言ではなく**振る舞い**で確かめる。「policy を import しているか」を
  grep で見る形にすると、import したまま呼んでいない実装を通してしまう。
"""
from __future__ import annotations

import json

import pytest

from laps import tools as T

#: パスを入力から取って**書く**道具と、その入力の作り方。
#: 新しくそういう道具を足したら、ここに1行足すこと（足さないと下の網羅試験が落ちる）。
WRITERS = {
    "Write":        lambda p: {"file_path": p, "content": "x"},
    "Edit":         lambda p: {"file_path": p, "old_string": "a", "new_string": "b"},
    "NotebookEdit": lambda p: {"notebook_path": p, "mode": "replace",
                               "cell_index": 0, "new_source": "x"},
    # ★ApplyDiff は統一 diff ではなく SEARCH/REPLACE ブロックを取る。
    #   最初この試験は統一 diff を渡し、**書式の誤りで弾かれたのを
    #   「ゲートが効いた」と読み違えかけた**。入力の形は道具ごとに確かめる。
    "ApplyDiff":    lambda p: {"file_path": p, "diff": (
        f"{p}\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n")},
}


def _spec(name):
    for s in T.all_specs():
        if s.name == name:
            return s
    raise AssertionError(f"道具 {name} が見つかりません")


@pytest.fixture
def guarded(monkeypatch, tmp_path):
    """読むだけモードにして、作業フォルダの外を指す。"""
    monkeypatch.setenv("LAPS_POLICY", "readonly")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    outside = tmp_path.parent / "outside_target.ipynb"
    outside.write_text(json.dumps({"cells": [
        {"cell_type": "code", "source": ["a"], "outputs": [], "execution_count": None}]}))
    return str(outside)


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_refuses_when_policy_forbids(name, guarded):
    """★実際に呼んで、拒まれることを見る。"""
    # ★**文言に一致させない**（2026-08-21・自分で踏んだ）。
    #   最初 "policy denied" だけを見ていたが、実際に返るのは
    #   「Error: readonly モードでは書き込み不可」で、道具は正しく拒んでいた。
    #   拒否の文言は場所によって違う。見るべきは**拒んだという事実**。
    out = str(_spec(name).handler(WRITERS[name](guarded)))
    assert out.startswith("Error"), \
        f"{name} が実行ゲートを通らずに書けています: {out[:200]}"


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_the_file_is_not_touched(name, guarded, monkeypatch):
    """拒んだのに書いていた、が無いこと。"""
    from pathlib import Path
    before = Path(guarded).read_bytes()
    _spec(name).handler(WRITERS[name](guarded))
    assert Path(guarded).read_bytes() == before, f"{name} が拒否後に書き換えています"


def test_every_path_writing_tool_is_covered():
    """★網羅の門。新しい道具が上の表に載っていなければ落ちる。

    schema にパスらしき項目を持ち、かつ変更を伴うと宣言している道具は、
    すべて上の WRITERS に載っていなければならない。
    """
    PATHY = ("file_path", "notebook_path", "path", "target_path")
    missing = []
    for s in T.all_specs():
        props = ((s.schema or {}).get("properties") or {})
        if not any(k in props for k in PATHY):
            continue
        if not getattr(s, "mutates", False):
            continue
        if s.name not in WRITERS:
            missing.append(s.name)
    assert not missing, (
        f"パスを受け取って変更する道具が試験に載っていません: {missing}。"
        "WRITERS に足して、実行ゲートを通ることを確かめてください")


# ── 拒否された書き込みが**痕跡を残さない**（2026-09-02・再現済み）──────────
#
# Write は `p.parent.mkdir(...)` を門の**外**で呼んでいたので、policy が拒否した
# 呼び出しでも**ディレクトリだけは作られていた**。同じ形（門と、実際に触る場所が
# ずれている）を Glob の走査ゲートでも見つけたので、両方を試験で留める。

def test_denied_write_creates_no_directory(tmp_path, monkeypatch):
    from laps.tools import write as write_tool
    from laps.policy import PolicyDenied

    def deny(*a, **kw):
        raise PolicyDenied("nope")

    monkeypatch.setattr(write_tool, "authorize_path", deny)
    target = tmp_path / "made" / "up" / "a.txt"
    out = write_tool._run({"file_path": str(target), "content": "x"})
    assert "policy denied" in out
    assert not (tmp_path / "made").exists(), "拒否されたのにディレクトリが作られた"


def test_parent_is_a_regular_file_is_reported_not_raised(tmp_path, monkeypatch):
    """親が普通のファイルのとき、素の例外ではなく読める失敗を返す。"""
    from laps.tools import write as write_tool

    monkeypatch.setenv("LAPS_POLICY", "trusted")
    (tmp_path / "afile").write_text("x")
    out = write_tool._run({"file_path": str(tmp_path / "afile" / "b.txt"), "content": "y"})
    assert out.startswith("Error:")
    assert "書き込みは行われていません" in out
