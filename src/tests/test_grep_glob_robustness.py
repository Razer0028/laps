"""Grep が不正バイトで落ちない／Glob が雑音の巣を歩かない（第三者レビュー E2・E3）。

どちらも 2026-08-30 に**実際に再現してから**直したもの。
- E2: 不正バイトが 1 つあるだけで Grep が丸ごと失敗していた
  （`Error: 'utf-8' codec can't decode byte 0xff in position 36`）。
  ログを grep する作業では普通に起きる。
- E3: `**/*` の結果が `.git` の中身と `node_modules` で埋まり、本物が沈んでいた。
  ★**隠すのではない**。パターンが名前を明示していれば従来どおり出す。
"""
from __future__ import annotations

import os
import pathlib

import pytest

from laps.tools import glob as g
from laps.tools import grep as gr


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


def test_不正バイトがあっても一致を返す(tmp_path):
    (tmp_path / "mixed.txt").write_bytes(b"\xe6\xad\xa3\n needle \xff\xfe here\n")
    out = str(gr._run({"pattern": "needle", "path": str(tmp_path)}))
    assert "needle" in out, f"落ちたか一致を返していない: {out[:200]}"
    assert "codec can't decode" not in out


def test_正常なファイルの一致は変わらない(tmp_path):
    (tmp_path / "a.txt").write_text("needle here\n", encoding="utf-8")
    assert "needle" in str(gr._run({"pattern": "needle", "path": str(tmp_path)}))


def _tree(d: pathlib.Path) -> pathlib.Path:
    (d / ".git").mkdir()
    (d / ".git" / "config").write_text("x", encoding="utf-8")
    (d / "node_modules" / "p").mkdir(parents=True)
    (d / "node_modules" / "p" / "a.js").write_text("x", encoding="utf-8")
    (d / "src").mkdir()
    (d / "src" / "main.py").write_text("x", encoding="utf-8")
    return d


def test_既定では雑音の巣を出さない(tmp_path):
    out = str(g._run({"pattern": "**/*", "path": str(_tree(tmp_path))}))
    assert "main.py" in out, "本物のファイルまで消している"
    assert ".git" not in out and "node_modules" not in out, out[:300]


def test_名前を明示すれば従来どおり出す(tmp_path):
    """★隠す機構にしない。明示的に訊かれたら答える。"""
    out = str(g._run({"pattern": "**/.git/*", "path": str(_tree(tmp_path))}))
    assert "config" in out, f"明示しても出ない（隠す機構になっている）: {out[:200]}"


# ── Windows のパスで束ね方が壊れていた（2026-09-02・再現済み）────────────────
#
# `C:\src\a.py:12:text` を素朴に先頭の `:` で切ると **"C"** になる。すると
# 束が 1 つにまとまり、「ファイルごとに公平に配る」畳み方も、
# 「一致したファイル: N 本」も、git の無視規則を当てる相手も全部おかしくなる。
# どちらの道具も説明文で「OS を問わず同じように動く」と約束している。

def test_grep_splits_windows_drive_letter_paths():
    from laps.tools.grep import _split_file, _render

    assert _split_file(r"C:\src\a.py:12:text") == r"C:\src\a.py"
    assert _split_file("C:/src/a.py:12:text") == "C:/src/a.py"
    assert _split_file("src/a.py:12:text") == "src/a.py"
    assert _split_file("区切りなし") == "(不明)"
    # 束が 2 本に分かれる（旧実装は両方 "C" で 1 本になっていた）
    out = _render([r"C:\src\a.py:1:x", r"C:\src\b.py:1:x"], 200)
    assert "一致したファイル: 2 本" in out, out


def test_glob_folds_by_directory_on_windows_paths():
    from laps.tools import glob as glob_tool

    groups = {}
    for m in [r"C:\src\a.py", r"C:\src\b.py", r"C:\other\c.py"]:
        i = max(m.rfind("/"), m.rfind("\\"))
        groups.setdefault(m[:i] if i > 0 else ".", []).append(m)
    assert set(groups) == {r"C:\src", r"C:\other"}
    # 実装が同じ切り方をしていること（畳みが要る大きさで確かめる）
    many = [rf"C:\src\f{i}.py" for i in range(3000)]
    out = glob_tool._render(many)
    assert r"C:\src" in out, out[:300]
