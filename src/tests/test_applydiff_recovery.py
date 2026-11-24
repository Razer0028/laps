"""ApplyDiff が**実機で 14/14 失敗していた**形を、道具の側で受け止める。

★根拠（2026-09-02・直近 4 走行 run44/46/47/48 の記録）:
  ApplyDiff の呼び出し 14 回はすべて失敗していた。内訳は

     5 回  パスの行が `# /root/…/build_rx.py`（コメント形式）→ file not found
     3 回  `diff --git … @@` の unified diff  → no SEARCH/REPLACE blocks found
     1 回  末尾の `>>>>>>> REPLACE` が無い     → no SEARCH/REPLACE blocks found
     6 回  SEARCH が完全一致しない（照合カスケードが Edit にしか無かった）
     ※ 重複あり（同じ呼び出しが複数の形を持つ）

  ファイルは実在しており、5 回ぶんは**道具がパスを読み違えていただけ**だった。
  ここに置く試験は、その 4 つの形が二度と静かに落ちないよう固定する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.tools.apply_diff import _run, _parse_blocks, _pick_path  # noqa: E402


def _res(out) -> str:
    return out if isinstance(out, str) else out.text


def test_comment_prefixed_path_applies(tmp_path, monkeypatch):
    """`# path` を**そのファイル名**と受け取っていた（実測 5/14）。"""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "build_rx.py"
    f.write_text("x = 1\n")
    out = _res(_run({"diff": f"# {f}\n<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"}))
    assert "Applied 1/1" in out, out
    assert f.read_text() == "x = 2\n"
    # 飾りをそのままファイル名にして新規作成する、という間違いをしない
    assert not (tmp_path / f"# {f}").exists()


def test_raw_path_wins_when_it_exists(tmp_path, monkeypatch):
    """正規化が**実在する編集先をすり替えない**ことを固定する。"""
    monkeypatch.chdir(tmp_path)
    weird = tmp_path / "#a.txt"
    weird.write_text("raw\n")
    (tmp_path / "a.txt").write_text("cleaned\n")
    assert _pick_path([str(weird), str(tmp_path / "a.txt")]) == str(weird)


def test_unterminated_last_block(tmp_path, monkeypatch):
    """末尾の `>>>>>>> REPLACE` が無いまま出力が終わる形（実測 1/14）。"""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("a = 1\nb = 2\n")
    out = _res(_run({"diff": "a.py\n<<<<<<< SEARCH\na = 1\n=======\na = 9\n"}))
    assert "Applied 1/1" in out, out
    assert f.read_text() == "a = 9\nb = 2\n"
    assert "`>>>>>>> REPLACE` がありません" in out


def test_unified_diff_is_translated(tmp_path, monkeypatch):
    """`diff --git` で来ても受ける（実測 3/14）。適用の実体は共通の道。"""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "api.php"
    f.write_text("<?php\n$a = 1;\n$b = 2;\n")
    diff = (
        "diff --git a/api.php b/api.php\n"
        "index 111..222 100644\n"
        "--- a/api.php\n"
        "+++ b/api.php\n"
        "@@ -1,3 +1,3 @@\n"
        " <?php\n"
        "-$a = 1;\n"
        "+$a = 42;\n"
        " $b = 2;\n"
    )
    out = _res(_run({"diff": diff}))
    assert "Applied 1/1" in out, out
    assert f.read_text() == "<?php\n$a = 42;\n$b = 2;\n"
    assert "unified diff" in out


def test_loose_match_cascade_is_shared_with_edit(tmp_path, monkeypatch):
    """字下げの違いで落ちていた（Edit には 8/27 から入っていた救済）。"""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("def f():\n    x = 1\n    return x\n")
    # SEARCH の空白の詰め方だけが違う（実測 run47 と同じ形）
    out = _res(_run({"diff": "a.py\n<<<<<<< SEARCH\n    x  =  1\n=======\n    x = 2\n>>>>>>> REPLACE\n"}))
    assert "Applied 1/1" in out, out
    assert "x = 2" in f.read_text()
    # 黙って直さない。当てた段を必ず言う。
    assert "完全一致しませんでした" in out


def test_syntax_gate_refuses_and_keeps_file(tmp_path, monkeypatch):
    """Edit にはあった構文ゲートが ApplyDiff に無かった（同じ内容が素通り）。"""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 2\n")
    out = _res(_run({"diff": "a.py\n<<<<<<< SEARCH\ny = 2\n=======\ny = (2\n>>>>>>> REPLACE\n"}))
    assert "構文を壊します" in out, out
    assert f.read_text() == "x = 1\ny = 2\n"      # 元のまま


def test_crlf_is_preserved(tmp_path, monkeypatch):
    """CRLF のファイルが LF に化けて**全行が変わった別物**にならない。"""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "a.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    out = _res(_run({"diff": "a.txt\n<<<<<<< SEARCH\ntwo\n=======\nTWO\n>>>>>>> REPLACE\n"}))
    assert "Applied 1/1" in out, out
    assert f.read_bytes() == b"one\r\nTWO\r\nthree\r\n"


def test_non_utf8_file_does_not_explode(tmp_path, monkeypatch):
    """cp932 のファイルで `read_text()` が例外を投げて丸ごと失敗していた。"""
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "log.txt"
    f.write_bytes("見つかりません\nNEEDLE\n".encode("cp932"))
    out = _res(_run({"diff": "log.txt\n<<<<<<< SEARCH\nNEEDLE\n=======\nFOUND\n>>>>>>> REPLACE\n"}))
    assert "Applied 1/1" in out, out
    # ★2026-09-21 訂正: 旧アサーションは `read_text(encoding="utf-8")` で、
    #   **cp932 を UTF-8 に書き換える古い（壊れた）振る舞いを固定していた**。
    #   0.1.231 の F7-1（`textcodec` の損なわない往復）で書き戻しは cp932 のままに
    #   なったので、この試験だけが取り残されて赤になっていた。
    #   ★全試験を回していなかったので誰も気づいていなかった。
    #   正しい期待は「編集は効く」かつ「**文字コードは変わらない**」。
    assert "FOUND" in f.read_text(encoding="cp932")
    assert f.read_text(encoding="cp932").startswith("見つかりません")
    with pytest.raises(UnicodeDecodeError):
        f.read_text(encoding="utf-8")      # UTF-8 に化けていないことの裏取り


def test_missing_file_names_the_candidates(tmp_path, monkeypatch):
    """本当に無いときは、**どの形で探したか**を言う（黙って 1 行で終わらせない）。"""
    monkeypatch.chdir(tmp_path)
    out = _res(_run({"diff": "# nope/a.py\n<<<<<<< SEARCH\nx\n=======\ny\n>>>>>>> REPLACE\n"}))
    assert "file not found" in out
    assert "nope/a.py" in out
    assert out.startswith("Error:")


def test_backtick_wrapped_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("old\n")
    out = _res(_run({"diff": "`a.txt`\n<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE\n"}))
    assert "Applied 1/1" in out, out


def test_empty_search_still_creates_new_file(tmp_path, monkeypatch):
    """既存の約束（空 SEARCH＝新規/追記）を壊していないこと。"""
    monkeypatch.chdir(tmp_path)
    out = _res(_run({"diff": "new.txt\n<<<<<<< SEARCH\n=======\nhello\n>>>>>>> REPLACE\n"}))
    assert "Applied 1/1" in out, out
    assert (tmp_path / "new.txt").read_text() == "hello"


def test_two_files_one_bad_still_reports_both(tmp_path, monkeypatch):
    """部分成功が「何も起きなかった」に見えない（E5 の固定を壊していない）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("A\n")
    diff = ("a.txt\n<<<<<<< SEARCH\nA\n=======\nB\n>>>>>>> REPLACE\n"
            "gone.txt\n<<<<<<< SEARCH\nX\n=======\nY\n>>>>>>> REPLACE\n")
    out = _res(_run({"diff": diff}))
    assert out.startswith("Error:")
    assert "✓ a.txt" in out and "✗ gone.txt" in out
    assert "一部のファイルだけが書き換わっています" in out
