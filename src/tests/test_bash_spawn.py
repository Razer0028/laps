"""Bash の実行経路。★571 件が通ったまま壊れていたので、ここを固定する。

2026-08-12、Windows で `dir` が「exit 0・出力なし」を18回返した。
原因を追える形にする過程で `subprocess.run` の引数展開を誤り、**全コマンドが
`Error: bufsize must be an integer` を返す**状態になった。既存の試験は1件も
落ちなかった＝この経路は誰も試験していなかった。
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from laps.tools.bash import _run, _spawn_args


@pytest.fixture(autouse=True)
def _mode(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "workspace")


def test_ふつうのコマンドが出力を返す():
    out = _run({"command": "echo hello"})
    assert "hello" in out, out
    assert "Error" not in out, out


def test_出力が空なら何をどこで走らせたかを書く():
    """★「(exit 0, no output)」だけでは次の調査が始められない。"""
    out = _run({"command": "true"})
    assert "no output" in out
    for k in ("command:", "cwd:", "shell:", "platform:"):
        assert k in out, f"{k} が無い: {out}"


def test_失敗した出力も返す():
    out = _run({"command": "ls /nonexistent-xyz-zzz"})
    assert "Error: bufsize" not in out, "引数の展開が壊れている"
    assert out.strip(), "何も返っていない"


def test_windows_では_comspec_に頼らない(monkeypatch):
    """COMSPEC が cmd.exe でない環境で黙って終わるのを避ける。"""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", "/nonexistent-root")
    monkeypatch.setenv("ComSpec", "/bin/false")
    args, kw = _spawn_args("dir")
    assert kw["shell"] is False, "Windows では shell=True に頼らない"
    # ★2026-09-21: リストではなく文字列（リストだと命令の中の " が \\" に化ける）
    line = args[0]
    assert isinstance(line, str) and line.endswith('/d /s /c "dir"'), line


def test_linux_は命令文をargvに置かない(monkeypatch):
    """★2026-09-01 に契約を反転させた（元の名前は `..._の起動の仕方は変えない`）。

    旧: `shell=True` ＋ 命令文が argv[0]。
    新: 命令文は**一時ファイル**に書き、`bash <file>` で走らせる。

    ★なぜ変えたか（実機の記録で 9/9 = 100%）:
      `bash -c "<命令文>"` はその文**全体**をシェルの argv に持つ。だから
      `pkill -f php` は走らせている当のシェルに必ず一致し、**自分を撃つ**。
      居残りサーバを止めようとした 9 回すべてが exit -15 で、
      **1 回もサーバを止められていなかった**。
      実験（`shell=True` は rc=-15／ファイル渡しは rc=0 で生存）で確定。
    ★bash を明示する約束（dash に落ちない）は保つ。
    """
    monkeypatch.setattr(sys, "platform", "linux")
    args, kw = _spawn_args("pkill -f some-marker")
    assert kw["shell"] is False, "shell=True だと命令文が argv に載る"
    argv = args[0]
    assert isinstance(argv, list) and argv[0].endswith(("bash", "sh")), argv
    assert not any("some-marker" in str(a) for a in argv), f"命令文が argv に残っている: {argv}"
    script = kw.get("_bw_script")
    assert script, "台本の受け渡しが無い"
    import pathlib as _pl
    assert "pkill -f some-marker" in _pl.Path(script).read_text(encoding="utf-8")
    _pl.Path(script).unlink(missing_ok=True)
