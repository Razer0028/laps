"""完了条件の構造強制と偽メディア検知（2026-08-24・VDIR テスト 3 走行の実測から）。

★実測した負け筋: named-run の動詞語彙が「起動し・確かめる・通す・止める」を拾えず、
  完了条件 4 つが 1 つも強制されないまま「完了」を受理。71 バイトのテキストが .mp4 を名乗った。
"""
from __future__ import annotations


TASK = """VDIR を作ってください。

完了条件（すべて実際に行い、結果を報告に貼ること）:
- start.sh で起動し、curl で一覧ページ・/api/videos・Range 要求（206 が返ること）を確かめる
- 動画を 1 本追加して POST /api/rescan → 一覧に増えることを確かめる
- pytest を通す
- 最後に stop.sh でサーバを止める
"""


# ---- 偽メディア -----------------------------------------------------------


# ---- 権限ヒント（Bash 結果の隣に置く） -------------------------------------

def _denied_cmd(tmp_path):
    """権限失敗を出すコマンド。★root で走る試験環境では /etc への書き込みが**成功してしまう**
    （最初の版は本当に /etc にファイルを作った）ので、失敗の出力と exit 1 を自分で作る。"""
    f = tmp_path / "denied.sh"
    f.write_text("#!/bin/bash\necho 'apt: Permission denied' >&2\nexit 1\n")
    return f"bash {f}"


def test_permission_denied_result_carries_a_sudo_hint(tmp_path, monkeypatch):
    """★run1〜4 で envpack の一般論は 4 回読み飛ばされた。ヒントは失敗の隣に置く。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from laps.tools import bash
    cmd = _denied_cmd(tmp_path)
    r = bash.SPEC.handler({"command": cmd})
    assert "Permission denied" in str(r)
    assert f"sudo {cmd}" in str(r)
    assert "偽物で代用しない" in str(r)


def test_no_hint_when_already_sudo_or_not_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path)); monkeypatch.chdir(tmp_path)
    from laps.tools import bash
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    r = bash.SPEC.handler({"command": _denied_cmd(tmp_path)})
    assert "sudo bash" not in str(r)      # workspace では sudo を勧めない（policy が拒む側）


def test_write_refuses_binary_formats_with_recipe(tmp_path, monkeypatch):
    """★run12: Write→失敗→rm→Write の 3 連で打ち切り。書く前に断り、作り方を返す。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted"); monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    from laps.tools import write
    r = str(write.SPEC.handler({"file_path": "data/works.db", "content": "CREATE TABLE ..."}))
    assert r.startswith("Error") and "sqlite3" in r and "CREATE TABLE works" in r
    assert not (tmp_path / "data" / "works.db").exists()
    r2 = str(write.SPEC.handler({"file_path": "videos/x.mp4", "content": "dummy"}))
    assert r2.startswith("Error") and "ffmpeg" in r2
    assert str(write.SPEC.handler({"file_path": "a.py", "content": "x = 1\n"})).startswith("OK")


