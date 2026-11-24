"""Edit の「境界」と「理由が届かない」を固定する（2026-09-02）。

★測ってから直した（直近 4 走行 run44/46/47/48・Edit 384 回 / 失敗 122 回）:
    73  old_string not found   … うち **70 件は本当に本文に無い**
                                 （照合を緩める段を増やしても 1 件も救えない、と実測）
                                 うち 18 件は返した「現在の内容」が先頭 2,000 字で
                                 切れていた＝**直すべき場所が一度も見えない**
    41  構文ゲートが止めた（＝正しく働いている）
     8  old_string が無く new_string だけ … 旧実装の答えは 1 行だけで次の一手が無い
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.tools.edit import _run, _nearest_region  # noqa: E402


def _text(out) -> str:
    return out if isinstance(out, str) else out.text


def test_not_found_points_at_the_nearest_region(tmp_path):
    """「無い」で終わらせず「では**どこが近いか**」を返す。"""
    f = tmp_path / "a.py"
    body = "\n".join([f"filler_{i} = {i}" for i in range(400)]
                     + ["def build_index(conn):", "    conn.execute('SELECT 1')", "    return 0"])
    f.write_text(body + "\n")
    out = _text(_run({"file_path": str(f),
                      "old_string": "def build_index(cur):\n    cur.execute('SELECT 1')",
                      "new_string": "x"}))
    assert "old_string not found" in out
    # ★先頭 2,000 字の貼り付けでは絶対に出てこない場所を指せていること
    assert "def build_index(conn):" in out, out
    assert "いちばん近いのは" in out
    assert "filler_0 = 0" not in out          # 頭からの貼り付けに戻っていない


def test_nearest_region_stays_silent_when_nothing_is_close(tmp_path):
    """近いものが無いのに「ここが近い」と言わない（間違った案内は無いより悪い）。"""
    assert _nearest_region("alpha\nbravo\ncharlie\n", "zzzzzzzzzzzz\n") == ""


def test_missing_old_string_names_the_next_step(tmp_path):
    """new_string だけ渡す形（実測 8 件）。Write を名指しする。"""
    f = tmp_path / "a.php"
    f.write_text("<?php\n")
    out = _text(_run({"file_path": str(f), "new_string": "<?php\n// 全文\n"}))
    assert "Write" in out and "content" in out, out
    assert f.read_text() == "<?php\n"          # 勝手に丸ごと書き換えない


def test_symlink_is_followed_not_replaced(tmp_path):
    """記号リンクを実ファイルで**潰さない**（sites-enabled/default の形）。"""
    real = tmp_path / "real.conf"
    real.write_text("listen 80;\n")
    link = tmp_path / "link.conf"
    link.symlink_to(real)
    out = _text(_run({"file_path": str(link), "old_string": "80", "new_string": "8080"}))
    assert "OK:" in out, out
    assert link.is_symlink(), "リンクが実ファイルに置き換わった"
    assert real.read_text() == "listen 8080;\n", "リンク先が更新されていない"


def test_crlf_new_string_does_not_double(tmp_path):
    """new_string に CRLF が混ざっても `\\r\\r\\n` にしない。"""
    f = tmp_path / "a.txt"
    f.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    out = _text(_run({"file_path": str(f), "old_string": "two", "new_string": "two\r\ntwo-b"}))
    assert "OK:" in out, out
    assert b"\r\r\n" not in f.read_bytes()
    assert f.read_bytes() == b"one\r\ntwo\r\ntwo-b\r\nthree\r\n"


def test_file_mode_is_preserved(tmp_path):
    f = tmp_path / "run.sh"
    f.write_text("echo hi\n")
    os.chmod(f, 0o755)
    _run({"file_path": str(f), "old_string": "hi", "new_string": "bye"})
    assert (f.stat().st_mode & 0o777) == 0o755


def test_non_dict_edit_entry_is_named_not_crashed(tmp_path):
    """`edits` に文字列が混ざると AttributeError で素の例外になっていた。"""
    f = tmp_path / "a.txt"
    f.write_text("x\n")
    out = _text(_run({"file_path": str(f), "edits": ["oops"]}))
    assert out.startswith("Error:")
    assert "edits[0]" in out
