"""`.exe` なのに中身が実行ファイルでないときに、**中身を読んで言う**（2026-09-21）。

★実測（オーナーが goose と BLACKWALL の記録を並べて提示）。同じ課題で両方が数十手ずつ溶かした:
    goose     … 引用の付け方を 10 通り以上変えて再試行・7z・python・子エージェント 2 体
    BLACKWALL … 思考 4,433 / 7,005 トークン
★どちらも「なぜ動かないか」を一度も調べていない。実体は `UnrealPak.exe`（267,883 バイト）が
  **GitHub の HTML ページ**だった。★先頭 16 バイトを読めば一言で終わる。
"""
import laps.tools.bash as B

JA = ("このバージョンの X.exe は、実行している Windows のバージョンと"
      "互換性がありません。")
EN = "%1 is not a valid Win32 application."


def _win(monkeypatch):
    monkeypatch.setattr(B.sys, "platform", "win32", raising=False)


def test_HTML_が_exe_の名前で置かれている(tmp_path, monkeypatch):
    _win(monkeypatch)
    p = tmp_path / "UnrealPak.exe"
    p.write_bytes(b"\n\n\n\n<!DOCTYPE html>\n<html lang=\"en\">" + b"x" * 500)
    h = B._win_not_exe_hint(f'cmd /c "{p}" list', JA, 1)
    assert "実行ファイルではありません" in h
    assert "HTML" in h
    assert "引用や PATH の問題ではありません" in h


def test_英語のメッセージでも効く(tmp_path, monkeypatch):
    _win(monkeypatch)
    p = tmp_path / "t.exe"; p.write_bytes(b"<html>hello</html>")
    assert "HTML" in B._win_not_exe_hint(f'"{p}"', EN, 1)


def test_Git_LFS_のポインタ(tmp_path, monkeypatch):
    """★大きな実行ファイルを LFS 未取得のまま持ってくる形も同じ症状になる。"""
    _win(monkeypatch)
    p = tmp_path / "big.exe"
    p.write_bytes(b"version https://git-lfs.github.com/spec/v1\noid sha256:abc\n")
    assert "Git LFS" in B._win_not_exe_hint(f'"{p}"', JA, 1)


def test_本物の_PE_には何も言わない(tmp_path, monkeypatch):
    """★本物なら別の原因（アーキテクチャ違い等）。嘘の断定をしない。"""
    _win(monkeypatch)
    p = tmp_path / "real.exe"; p.write_bytes(b"MZ" + b"\x00" * 300)
    h = B._win_not_exe_hint(f'"{p}"', JA, 1)
    assert "Windows の実行ファイル（PE）" in h      # 事実は言う
    assert "HTML" not in h


def test_成功した実行には付けない(tmp_path, monkeypatch):
    _win(monkeypatch)
    p = tmp_path / "t.exe"; p.write_bytes(b"<html>")
    assert B._win_not_exe_hint(f'"{p}"', JA, 0) == ""


def test_別の失敗には付けない(tmp_path, monkeypatch):
    _win(monkeypatch)
    p = tmp_path / "t.exe"; p.write_bytes(b"<html>")
    assert B._win_not_exe_hint(f'"{p}"', "指定されたファイルが見つかりません。", 1) == ""


def test_読めないファイルには黙る(monkeypatch):
    _win(monkeypatch)
    assert B._win_not_exe_hint(r'"C:\no\such.exe"', JA, 1) == ""
