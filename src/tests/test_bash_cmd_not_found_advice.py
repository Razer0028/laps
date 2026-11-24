"""Bash の「そのコマンドは無い」に実在する代替を添える（2026-09-03・dogfood ⑮）。

★背景: dogfood でモデルは `python -m pytest` を先に試して `command not found`
（この機械には python3 しか無い）→ 自力で python3 に切り替えていた。旧実装は
「無い」とだけ言い、代替は一切示していなかった。固定の別名表（"python"→"python3"）
は書かない方針（その機械に実在する保証が無い助言は無いより悪い、という ssh の
教訓と同じ）なので、PATH を実際に調べて実在するものだけを言う。

単体試験（`_missing_command_name` / `_find_path_alternatives`）で判定の正しさを、
本物の Bash 実行（PATH に偽の実行ファイルを置く）で配線されていることを確かめる。
"""
from __future__ import annotations

import os
import stat

from laps.tools import bash as bash_tool


def test_bashの見つからないコマンド名を1つ取り出す():
    f = bash_tool._missing_command_name
    assert f("foocmd: command not found") == "foocmd"
    assert f("bash: line 3: foocmd: command not found") == "foocmd"
    assert f("sh: 1: foocmd: not found") == "foocmd"
    assert f("sh: foocmd: not found") == "foocmd"
    # ★実測（この機械そのもの・ja_JP.UTF-8）: bash はロケールに応じて
    #   "コマンドが見つかりません" と言い、英語の綴りだけでは検出できない。
    assert f("bash: 行 1: foocmd: コマンドが見つかりません") == "foocmd"
    # ★日本語 Windows（cp932→UTF-8 に復号済みの文面。bash.py の _decode_out を通った後の形）
    assert f("'python' は、内部コマンドまたは外部コマンド、\n"
             "操作可能なプログラムまたはバッチ ファイルとして認識されていません。") == "python"


def test_無関係なnot_foundは拾わない():
    """コロン直前の1語という形に絞ってあるので、無関係な『not found』文言には反応しない。"""
    f = bash_tool._missing_command_name
    assert f("pattern not found in file") is None
    assert f("404 Not Found") is None  # 大文字小文字が違う


def test_パスを含む取り違えは弾く():
    """`bash: ./script.sh: command not found` のような形で、道のような名前を
    そのまま PATH 探索へ渡さない（意味の無い助言になるだけ）。"""
    assert bash_tool._missing_command_name(
        "bash: ./script.sh: command not found") is None


def test_PATH上に実在する代替だけを返す(tmp_path, monkeypatch):
    shim = tmp_path / "bin"
    shim.mkdir()
    real = shim / "foocmd3"
    real.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    real.chmod(real.stat().st_mode | stat.S_IEXEC)
    # 実行できない同名の飾りは候補に入れない
    (shim / "foocmd_readme.txt").write_text("not executable", encoding="utf-8")
    monkeypatch.setenv("PATH", str(shim))
    alts = bash_tool._find_path_alternatives("foocmd")
    assert alts == ["foocmd3"]


def test_PATH上に何も無ければ空(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # 空のディレクトリ
    assert bash_tool._find_path_alternatives("definitely_missing_xyz") == []


def test_見つからないコマンドの実行結果に実在する代替が添えられる(monkeypatch, tmp_path):
    """本物の Bash 道具を、PATH に偽の実行ファイルを置いた状態で走らせる。"""
    shim = tmp_path / "bin"
    shim.mkdir()
    real = shim / "definitely_not_a_real_cmd_xyz3"
    real.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    real.chmod(real.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    # ★workspace の許可リストは実行してよいコマンド名を絞っている（policy.py）ので、
    #   実在しないコマンド名で「実行して初めて command not found になる」形を試すには
    #   trusted が要る（ssh の助言の試験・test_bash_ssh_denied_advice.py と同じ理由）。
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    out = str(bash_tool._run({"command": "definitely_not_a_real_cmd_xyz",
                              "cwd": str(tmp_path)}))
    assert "definitely_not_a_real_cmd_xyz3" in out
    assert "実際に PATH を調べた結果です" in out


def test_代替が全く無いときは正直にそう言う(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", f"{tmp_path}")  # 何も置かない空の PATH
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    out = str(bash_tool._run({"command": "totally_absent_cmd_abc999",
                              "cwd": str(tmp_path)}))
    assert "totally_absent_cmd_abc999" in out
    assert "見つかりませんでした" in out
