"""読めなかったとき、「では何があるか」を返す。

★実機で見えた形（2026-08-22・v0.1.79）: Read が **9回中9回**失敗した。
  Glob(**) で一覧を取っているのに、そこに無いパス
  （README.md / core/agent.py / frontend/ui.py …）を読もうとしていた。
  返るのは「file not found」だけなので、モデルは次に何を読めばよいか分からず、
  **また別の想像したパスを試す**。9手番が消えた。

★行き止まりを訂正に変える。
"""
from __future__ import annotations

import pytest

from laps.tools import read


@pytest.fixture
def ws(tmp_path, monkeypatch):
    (tmp_path / "core").mkdir()
    for f in ("core/inference_engine.py", "core/model_config.json", "structure.md"):
        (tmp_path / f).write_text("x")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    return tmp_path


def test_close_name_is_suggested(ws):
    """綴り違い。"""
    out = read._run({"file_path": str(ws / "structuer.md")})
    assert "structure.md" in out


def test_same_name_elsewhere_is_suggested(ws):
    out = read._run({"file_path": str(ws / "inference_engine.py")})
    assert "core/inference_engine.py" in out.replace("\\", "/")


def test_when_nothing_is_close_it_lists_what_is_there(ws):
    """★実機の失敗はこの形。名前は似ていないので、場所の中身を出す。"""
    out = read._run({"file_path": str(ws / "README.md")})
    assert "structure.md" in out and "core" in out


def test_it_never_says_only_not_found(ws):
    """★「無い」だけで終わらせない。次の一手が無いと同じ想像を繰り返す。"""
    for name in ("README.md", "core/agent.py", "frontend/ui.py", "a/b/c/d.py"):
        out = read._run({"file_path": str(ws / name)})
        assert out.count("\n") >= 1, f"{name}: 次の一手が書かれていない"


def test_it_does_not_leave_the_workspace(ws, tmp_path, monkeypatch):
    """★作業フォルダの外を漏らさない。存在の有無すら機微な場所がある。"""
    # ★見るのは**提案の部分だけ**。1行目は要求されたパスの反射で、
    #   それは利用者自身の入力なので漏洩ではない。
    #   （最初この試験は1行目を数えて誤検知した。判定の範囲を間違えると、
    #     正しい実装を「漏れている」と読む。）
    outside = tmp_path.parent / "secret_outside.txt"
    outside.write_text("x")
    out = read._run({"file_path": str(ws / "secret_outside.txt")})
    suggestion = "\n".join(out.splitlines()[1:])
    assert "secret_outside" not in suggestion, f"提案に作業フォルダ外が入っている: {suggestion}"
    assert str(tmp_path.parent) not in suggestion or str(ws) in suggestion


def test_a_successful_read_is_untouched(ws):
    out = read._run({"file_path": str(ws / "structure.md")})
    assert "近いもの" not in out and "にあるのは" not in out
