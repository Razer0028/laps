"""NotebookEdit / BashOutput / AskUserQuestion の不完全な部分（2026-09-02）。

この 3 つは実機の記録に呼び出しが 1 件も無い（0 / 3,072 回）。**測れていない**ので、
直したのは「起きたら黙って壊れる」形だけに絞った:

* NotebookEdit … 読み書きが**その機械の既定の符号化**だった。`.ipynb` は nbformat の
  仕様で常に UTF-8 なので、UTF-8 が既定でない機械（日本語 Windows の cp932。
  この製品は Windows 版を出荷している）では**仕様違反のファイルを書き出す**。
  加えて `write_text` は開いた瞬間に 0 バイトへ切り詰めるので、途中で失敗すると
  notebook を丸ごと失う（`tools/atomic.py` に再現手順つきで書いてある形。
  Write / Edit / TaskUpdate は既に不可分に書いており、ここだけ素だった）。

* BashOutput … Windows の生存確認が `text=True` で、cp932 の `tasklist` 出力で
  UnicodeDecodeError → `except Exception: return False` が飲んで
  **動いている背景処理を「いない」と報告する**（例外は 1 行も残らない）。
  `remote_read` / `system_state` と同じ `text=True` の兄弟。

* AskUserQuestion … 窓口が例外を投げたとき `(利用者に聞けませんでした: …)` という
  素の文字列を返しており、`Error:` で始まらないので **ok=True** で記録されていた。
  しかもその文字列は「利用者の答え」が入る位置に返る＝モデルが返事だと読む。
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from laps.tools import ask_user as A
from laps.tools import bash_output as B
from laps.tools import notebook_edit as N


def _nb(tmp_path, cells):
    p = tmp_path / "a.ipynb"
    p.write_text(json.dumps({"cells": cells, "nbformat": 4}, ensure_ascii=False),
                 encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _open_policy(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


def test_notebookはUTF8で書かれる(tmp_path) -> None:
    p = _nb(tmp_path, [{"cell_type": "code", "source": ["x = 1\n"]}])
    N._run({"notebook_path": str(p), "mode": "replace", "cell_index": 0,
            "new_source": "print('日本語 → ✓')"})
    raw = p.read_bytes()
    assert "日本語 → ✓".encode("utf-8") in raw
    # 書いた物を自分で読み直せること（読みと書きの符号化が揃っている）
    assert "日本語" in json.loads(p.read_text(encoding="utf-8"))["cells"][0]["source"][0]


def test_書けなかったときにnotebookを失わない(tmp_path, monkeypatch) -> None:
    """★不可分に書く。旧実装は `write_text` で、書く前に 0 バイトへ切り詰めていた。"""
    p = _nb(tmp_path, [{"cell_type": "code", "source": ["x = 1\n"]}])
    before = p.read_bytes()

    import laps.tools.atomic as atomic
    monkeypatch.setattr(atomic, "write_bytes_atomic",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    out = N._run({"notebook_path": str(p), "mode": "delete", "cell_index": 0})
    assert out.startswith("Error:")
    assert p.read_bytes() == before, "書けなかったのに元の notebook が壊れている"


@pytest.mark.parametrize("bad_idx", ["二", "cell 2", {}, "1.5"])
def test_数でないcell_indexで道具ごと落ちない(tmp_path, bad_idx) -> None:
    p = _nb(tmp_path, [{"cell_type": "code", "source": []}])
    out = N._run({"notebook_path": str(p), "mode": "replace",
                  "cell_index": bad_idx, "new_source": "x"})
    assert out.startswith("Error: cell_index は整数")


def test_notebookでないJSONを名指しで断る(tmp_path) -> None:
    p = tmp_path / "b.ipynb"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    out = N._run({"notebook_path": str(p), "mode": "insert", "new_source": "x"})
    assert "notebook の形ではありません" in out


@pytest.mark.skipif(os.name == "nt", reason="Linux 側の枝を確かめる試験")
def test_背景処理の末尾は全部読まずに取る(tmp_path, monkeypatch) -> None:
    """巨大な .out を毎回丸ごとメモリに載せない（ビルドや学習の記録は GB になる）。"""
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    d = tmp_path / "bg"
    d.mkdir(parents=True)
    pid = 999_999
    (d / f"{pid}.meta.json").write_text(
        json.dumps({"pid": pid, "cmd": "make 日本語", "started_at": "x"},
                   ensure_ascii=False), encoding="utf-8")
    (d / f"{pid}.out").write_bytes(b"A" * 100_000 + b"END")
    (d / f"{pid}.err").write_bytes(b"")

    # ★「全部読んでいない」を決定論で示す: ファイル全体を読む道
    #   （`Path.read_bytes`）が使われたら、その場で落ちるようにする。
    import pathlib
    monkeypatch.setattr(pathlib.Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(
                            AssertionError(f"全部読んでいる: {self}")))
    out = B._run({"pid": pid, "tail": 10})
    assert out.rstrip().endswith("END")
    assert "make 日本語" in out          # meta も UTF-8 で読めている


def test_metaが壊れていても出力は読める(tmp_path, monkeypatch) -> None:
    """台帳のせいで**出力まで**諦めない（旧実装は json.loads が例外で道具ごと死ぬ）。"""
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    d = tmp_path / "bg"
    d.mkdir(parents=True)
    pid = 999_998
    (d / f"{pid}.meta.json").write_text("{壊れ", encoding="utf-8")
    (d / f"{pid}.out").write_text("たいせつな出力", encoding="utf-8")
    out = B._run({"pid": pid})
    assert "たいせつな出力" in out
    assert "meta を読めません" in out


def test_windowsの生存確認がcp932で嘘をつかない(monkeypatch) -> None:
    """★旧実装は `text=True`＝UnicodeDecodeError → except が飲んで False。
    「動いているのに、いない」と答えていた。"""
    monkeypatch.setattr(B.os, "name", "nt")
    pid = 4321

    class _P:
        # 日本語 Windows の tasklist が返すバイト列（cp932）
        stdout = "イメージ名 4321 コンソール".encode("cp932")
        stderr = b""
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    assert B._is_alive(pid) is True


def test_利用者に聞けなかったのを答えとして返さない(monkeypatch) -> None:
    def _boom(_q, _o):
        raise RuntimeError("窓口が落ちた")

    monkeypatch.setattr(A, "_ASKER", _boom)
    out = A._run({"question": "どちらにしますか", "options": ["A", "B"]})
    assert getattr(out, "ok", True) is False
    assert out.startswith("Error:")
    assert "This is not the user's answer" in out      # ★2026-10-16 モデルへ渡る文を英語にした
