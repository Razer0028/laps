"""既定のエンジンの Bash 道具が、課題（利用者）の `~/.bashrc` を拾う（2026-09-13）。

★実測で確定した機構:
  Terminal-Bench の課題 `create-bucket` は Dockerfile の最終行
  `RUN echo 'alias aws="awslocal"' >> ~/.bashrc` **だけ**で `aws` を差し替えている。
  この道具は `bash <ファイル>` で**非対話**に走るので
    ・`~/.bashrc` を読まない（bash は対話シェルでしか読まない）
    ・**alias を展開しない**（非対話の既定）
  の二重で前提が消え、モデルは諦めて「README を書いて完了」と言った（実機 pane）。
  最小ループ側（`bash -i`）は同日直して `create-bucket` が通った（腕D 6/24 → 腕G 11/24）。
  ここは**既定のエンジンに同じ手当てを当てる**もの。
"""
from __future__ import annotations

import pathlib

import pytest

from laps.tools import bash as bash_mod


def _rc(tmp_path, body: str):
    (tmp_path / ".bashrc").write_text(body)
    return str(tmp_path)


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    """★既定の `workspace` は許可リストに無い語を弾く（`bwx` は弾かれる）。
    ここで見たいのは rc を拾うかどうかなので、関門は開けておく。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")


def test_alias_が効く(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", _rc(tmp_path, 'alias bwx="echo BW_RC_ALIAS"\n'))
    monkeypatch.delenv("LAPS_BASH_RC", raising=False)
    out = str(bash_mod._run({"command": "bwx", "description": "alias"}))
    assert "BW_RC_ALIAS" in out, out


def test_関数も拾う(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", _rc(tmp_path, "bwfn() { echo BW_RC_FUNC; }\n"))
    monkeypatch.delenv("LAPS_BASH_RC", raising=False)
    assert "BW_RC_FUNC" in str(bash_mod._run({"command": "bwfn", "description": "関数"}))


def test_RC_0_で従来の挙動に戻せる(tmp_path, monkeypatch):
    """★rc が壊れている現場の逃げ道。無いと直せない現場がありうる。"""
    monkeypatch.setenv("HOME", _rc(tmp_path, 'alias bwx="echo BW_RC_ALIAS"\n'))
    monkeypatch.setenv("LAPS_BASH_RC", "0")
    assert "BW_RC_ALIAS" not in str(bash_mod._run({"command": "bwx", "description": "切る"}))


def test_rc_の挨拶は出力に混ざらない(tmp_path, monkeypatch):
    """★rc が何か print する現場は普通にある。道具の出力を汚さないこと。"""
    monkeypatch.setenv("HOME", _rc(tmp_path, 'echo "ようこそ（rc の挨拶）"\n'))
    monkeypatch.delenv("LAPS_BASH_RC", raising=False)
    out = str(bash_mod._run({"command": "echo BW_ONLY", "description": "挨拶"}))
    assert "BW_ONLY" in out
    assert "ようこそ" not in out, out


def test_壊れた_rc_でも命令は走る(tmp_path, monkeypatch):
    """★`|| true` で続ける。rc の失敗で道具が使えなくなってはいけない。"""
    monkeypatch.setenv("HOME", _rc(tmp_path, "this-command-does-not-exist-bw\nsyntax ( error\n"))
    monkeypatch.delenv("LAPS_BASH_RC", raising=False)
    assert "BW_STILL_RUNS" in str(bash_mod._run({"command": "echo BW_STILL_RUNS", "description": "壊れた rc"}))


def test_対話ガードのある普通の_rc_は無害(tmp_path, monkeypatch):
    """★Debian 既定の `case $- in *i*) ;; *) return;; esac` があると早期 return する。
    ＝**rc 自身の意図を尊重する**形になっている（こちらで判断しない）。"""
    monkeypatch.setenv("HOME", _rc(
        tmp_path,
        'case $- in *i*) ;; *) return;; esac\nalias bwx="echo BW_SHOULD_NOT_APPEAR"\n'))
    monkeypatch.delenv("LAPS_BASH_RC", raising=False)
    out = str(bash_mod._run({"command": "echo BW_OK; bwx 2>/dev/null || true", "description": "ガード"}))
    assert "BW_OK" in out
    assert "BW_SHOULD_NOT_APPEAR" not in out


def test_命令文は_argv_に載せない():
    """★自己マッチ対策（命令文をファイルに書く設計）を壊していないこと。
    前置きはファイルの中に入れる。argv に足したら `pkill -f` が自分を撃つ形に戻る。"""
    import inspect
    src = inspect.getsource(bash_mod)
    assert "_rc_prelude()" in src
    i = src.index("fh.write(_rc_prelude())")
    # 書き込みはスクリプトファイルへの fh.write であること（argv 組み立てではない）
    assert "fh.write(cmd" in src[i:i + 200]
