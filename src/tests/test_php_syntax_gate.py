"""php -l の Fatal（strict_types 位置ずれ等）を編集ゲートが捕まえる（2026-09-07 vdir 実用評価）。"""
import shutil
import pytest
from laps.tools import edit as E

php = pytest.mark.skipif(not shutil.which("php"), reason="php CLI が無い")

STRICT_BROKEN = "<?php\necho 'x';\ndeclare(strict_types=1);\n"  # ← declare の前に文＝「must be the very first statement」Fatal（page2.php と同型）
GOOD = "<?php\necho 'hello';\n"
PARSE_BROKEN = "<?php\nfunction f( {\n"


@php
def test_strict_types_位置ずれのFatalを捕まえる():
    err = E._syntax_error("a.php", STRICT_BROKEN)
    assert err and "書き込みは行っていません" in err, err


@php
def test_素の構文エラーも捕まえる():
    assert E._syntax_error("b.php", PARSE_BROKEN)


@php
def test_正常なphpは通す():
    assert E._syntax_error("c.php", GOOD) is None
    assert E._syntax_error("d.php", "<?php declare(strict_types=1); echo 1;\n") is None


def test_php無し環境やphp以外は素通り(monkeypatch):
    # .txt は対象外
    assert E._syntax_error("e.txt", "何でも") is None
