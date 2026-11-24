"""Read はバイナリを黙って文字にしない（2026-08-28・エージェント監査 S4）。

★実測: モデルは works.db（SQLite）を 3 走行で 14/19/23 回 Read し、run31 では
  Read 出力の 29.6% が読めないバイト列だった。復号は「成功」するので返るのは
  文字化けした本文——モデルには「読めたが意味不明」に見え、同じ読みを繰り返す。
  断るだけでなく**その形式の正しい開け方**を返す（空観測の置換と同じ作法）。
"""
from __future__ import annotations

import sqlite3
import zipfile

import pytest


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


def _read(p):
    from laps.tools import read as R
    return str(R.SPEC.handler({"file_path": str(p)}))


def test_sqlite_is_refused_with_the_right_recipe(tmp_path):
    db = tmp_path / "works.db"
    sqlite3.connect(str(db)).execute("create table works(a)")
    out = _read(db)
    assert "SQLite" in out and "sqlite3" in out
    assert "select" in out, "正しい開け方まで言えていない"


def test_zip_and_gzip_are_named(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("x.txt", "hi")
    assert "ZIP" in _read(z) and "unzip" in _read(z)


def test_nul_bytes_without_magic_are_caught(tmp_path):
    f = tmp_path / "weird"
    f.write_bytes(b"abc\x00def" * 10)
    out = _read(f)
    assert "バイナリ" in out and "file " in out


def test_text_files_are_unaffected(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("print('hi')\n", encoding="utf-8")
    assert "print" in _read(f)


def test_utf8_japanese_is_not_mistaken_for_binary(tmp_path):
    """★多バイト文字をバイナリと誤判定したら、日本語のソースが読めなくなる。"""
    f = tmp_path / "j.md"
    f.write_text("# 見出し\n本文です。絵文字も 🎯\n", encoding="utf-8")
    out = _read(f)
    assert "見出し" in out and "Error" not in out


def test_guard_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_READ_BINARY_GUARD", "0")
    # ★SQLite は 2026-10-05 から断らずに構造として返す（read.py の _read_sqlite）。断りの方だけを外したことを見る。
    monkeypatch.setenv("LAPS_READ_SQLITE", "0")
    db = tmp_path / "x.db"
    sqlite3.connect(str(db)).execute("create table t(a)")
    assert "SQLite のデータベース" not in _read(db)
