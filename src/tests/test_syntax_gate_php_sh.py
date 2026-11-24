"""編集後の構文検査を .php / .sh にも広げる（2026-08-28・監査 S10）。

★実測: 全編集の 84〜93% が php/sh なのに、構文ゲートは .py/.json/.yaml しか
  見ていなかった＝**この現場でいちばん編集される言語に検査が無い**。
  モデルが自分で php -l を打ったのは 84 回中 2 回だけ。
★注意: php は環境によっては起動時警告（拡張が読めない等）を出す。
  それを構文エラーと取り違えると、正しい編集を延々と断ることになる。
"""
from __future__ import annotations

import shutil

import pytest

from laps.tools.edit import _syntax_error


@pytest.mark.skipif(not shutil.which("php"), reason="php が無い環境")
def test_broken_php_is_caught():
    err = _syntax_error("a.php", "<?php function f( { echo 1; }\n")
    assert err and ("syntax error" in err or "Parse error" in err)


@pytest.mark.skipif(not shutil.which("php"), reason="php が無い環境")
def test_valid_php_passes_even_with_startup_warnings():
    """★環境の起動時警告を構文エラーと取り違えない（この試験がその歯止め）。"""
    assert _syntax_error("a.php", "<?php\nfunction f(){ return 1; }\n") is None


@pytest.mark.skipif(not shutil.which("bash"), reason="bash が無い環境")
def test_broken_shell_is_caught():
    err = _syntax_error("start.sh", "if [ -f x ]; then\n  echo hi\n")   # fi が無い
    assert err and ("syntax error" in err or "unexpected" in err)


@pytest.mark.skipif(not shutil.which("bash"), reason="bash が無い環境")
def test_valid_shell_passes():
    assert _syntax_error("start.sh", "#!/bin/bash\nset -e\necho hi\n") is None


def test_unknown_extension_is_untouched():
    assert _syntax_error("a.conf", "これは何でもよい {{{") is None


def test_gate_can_be_disabled(monkeypatch):
    monkeypatch.setenv("LAPS_EXT_SYNTAX", "0")
    assert _syntax_error("a.php", "<?php function f( {") is None
