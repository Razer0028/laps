"""Glob が「使い方の誤り」を「0件」として返さないこと。

★2026-08-16 実機（omega_j_prime / P40）で踏んだ:
  モデルが `Glob(pattern=".py")` と呼び、rglob は当然0件を返し、モデルは
  「Python ファイルは存在しません」と**正直に**報告した——.py が3つあるフォルダで。
  捏造ではない。**道具が使い方の誤りを空の結果として返した**のが原因。
  grep で同じ欠陥（検索の失敗を0件として返す）を直したのと同じ理屈。
"""
from __future__ import annotations

import os
import pathlib

import pytest

from laps.tools.glob import _run


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("y")
    (tmp_path / "note.txt").write_text("z")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAPS_POLICY", "off")
    return tmp_path


def test_裸の拡張子は0件ではなく誤りとして返す(ws) -> None:
    out = _run({"pattern": ".py"})
    assert out.startswith("Error:")
    assert "0 件ではありません" in out
    # ★次の一手を必ず書く（診断だけだと実測で70%が黙り込む）
    assert "**/*.py" in out and "*.py" in out


def test_正しいパターンは従来どおり(ws) -> None:
    assert _run({"pattern": "*.py"}) == "a.py\nb.py"
    assert _run({"pattern": "**/*.py"}) == "a.py\nb.py"


def test_本物の0件は0件のまま(ws) -> None:
    assert _run({"pattern": "*.zzz"}) == "(no matches)"


def test_リテラル名の一致は壊さない(ws) -> None:
    assert _run({"pattern": "a.py"}) == "a.py"


def test_absolute_pattern_finds_files(tmp_path):
    """★run29: 絶対パスのパターンが必ず0件になり、モデルは「倉庫は空」と誤解した。"""
    from laps.tools import glob as g
    (tmp_path / "a.deb").write_text("x")
    (tmp_path / "b.deb").write_text("y")
    out = str(g.SPEC.handler({"pattern": str(tmp_path / "*.deb")}))
    assert "a.deb" in out and "b.deb" in out, out


def test_absolute_pattern_and_path_conflict_errors(tmp_path):
    from laps.tools import glob as g
    (tmp_path / "a.deb").write_text("x")
    other = tmp_path / "other"; other.mkdir()
    out = str(g.SPEC.handler({"pattern": str(tmp_path / "*.deb"), "path": str(other)}))
    assert "どちらか一方" in out, "黙ってどちらかを捨てている"


def test_missing_root_is_not_no_matches(tmp_path):
    from laps.tools import glob as g
    out = str(g.SPEC.handler({"pattern": str(tmp_path / "nope" / "*.deb")}))
    assert "存在しません" in out and "(no matches)" not in out


def test_dotdot_and_hidden_patterns_survive(tmp_path):
    """lstrip('./') は文字集合を剥がすので ../x や .hidden を壊していた。"""
    from laps.tools import glob as g
    (tmp_path / ".hidden.txt").write_text("x")
    out = str(g.SPEC.handler({"pattern": ".hidden*", "path": str(tmp_path)}))
    assert ".hidden.txt" in out, out


# ── 走査の許可を「実際に歩く根」に当てる（2026-09-02・再現済み）─────────────
#
# 旧実装は pattern のワイルドカードより前の部分だけを門に通していたので、
# 場所を `path=` で渡すと**門を素通り**した（このマシンで実際に一覧が返った）:
#     Glob(pattern="/srv/notes/secret/**/*.md")   → 正しく拒否
#     Glob(path="/srv/notes/secret", pattern="**/*.md") → 機微ツリーを列挙
# Grep は同じ引数を authorize_walk(path) で拒否しており、この道具だけが
# 別のものを門に通していた（「書いたが繋いでいない」＋「片方に残した」）。

def test_walk_gate_sees_the_directory_actually_walked(tmp_path, monkeypatch):
    from laps.tools import glob as glob_tool

    seen: list[str] = []

    def fake(root):
        seen.append(str(root))

    monkeypatch.setattr(glob_tool, "authorize_walk", fake)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.md").write_text("x")
    glob_tool._run({"path": str(tmp_path / "sub"), "pattern": "**/*.md"})
    assert seen == [str(tmp_path / "sub")], seen


def test_walk_gate_denial_is_reported_and_nothing_is_listed(tmp_path, monkeypatch):
    from laps.tools import glob as glob_tool
    from laps.policy import PolicyDenied

    def fake(root):
        raise PolicyDenied("no")

    monkeypatch.setattr(glob_tool, "authorize_walk", fake)
    (tmp_path / "secret.md").write_text("x")
    out = glob_tool._run({"path": str(tmp_path), "pattern": "*.md"})
    out = out if isinstance(out, str) else out.text
    assert out.startswith("Error: policy denied")
    assert "secret.md" not in out
