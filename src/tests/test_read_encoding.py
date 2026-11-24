"""Read の符号化と、どのファイルを読んだかの表示。

★2026-08-14、実機から届いた会話の画像で判明した2件:
  1. Windows のログ（cp932）が UTF-8 として読まれ、`INFO:root:�X�N…` が
     **そのまま回答に載っていた**。サイドカーは PYTHONUTF8=1 を渡すので、
     日本語 Windows のファイルはまず化ける。
  2. 出力にパスが無く、同名ファイルの取り違えを見つけられなかった（8/12 の scraper.py）。
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from laps.tools.read import _run


@pytest.fixture(autouse=True)
def _ws(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _body(out: str) -> str:
    return "\n".join(l for l in out.splitlines() if not l.startswith("#"))


def test_cp932_のログが化けない(_ws):
    """★実機で起きたそのもの。"""
    (_ws / "scrape.log").write_bytes("INFO:root:スクレイピング開始\n".encode("cp932"))
    out = _run({"file_path": "scrape.log"})
    assert "�" not in out, out[:200]
    assert "スクレイピング開始" in out


def test_utf8_は従来どおり(_ws):
    (_ws / "a.txt").write_text("日本語のテキスト\n", encoding="utf-8")
    out = _run({"file_path": "a.txt"})
    assert "日本語のテキスト" in out and "�" not in out


def test_bom_付き_utf8(_ws):
    (_ws / "b.txt").write_bytes(b"\xef\xbb\xbf" + "BOM 付き\n".encode("utf-8"))
    out = _run({"file_path": "b.txt"})
    assert "BOM 付き" in out
    assert "﻿" not in out, "BOM が本文に残っている"


def test_読めないバイト列は落ちずに開け方を返す(_ws):
    """★2026-08-28 に契約を変えた（エージェント監査 S4）。

    旧契約は「復号できないバイトが来ても**本文を返す**（落ちない）」だった。
    しかし実測では、モデルは works.db を 3 走行で 14/19/23 回 Read し、
    run31 では Read 出力の **29.6%** が読めないバイト列だった——復号は
    「成功」するので、返るのは文字化けした本文で、モデルには「読めたが
    意味不明」に見え、**同じ読みを何度も繰り返す**。
    新契約: 落ちないのは同じ。ただし本文の代わりに「何のファイルか」と
    「その形式の正しい開け方」を返す（空観測の置換と同じ作法）。
    """
    (_ws / "bin.dat").write_bytes(bytes(range(256)) * 4)
    out = _run({"file_path": "bin.dat"})
    assert isinstance(out, str) and out, "落ちている"
    assert "バイナリ" in out, out[:160]
    assert "file " in out, "次の一手（素性の調べ方）が無い"


def test_どのファイルを読んだかを絶対パスで出す(_ws):
    (_ws / "x.py").write_text("a=1\n", encoding="utf-8")
    out = _run({"file_path": "x.py"})
    assert out.splitlines()[0] == f"# {(_ws / 'x.py').resolve()}"


def test_同名が他にあれば警告する(_ws):
    """★8/12 の取り違えの素地。読んだ瞬間に言う。"""
    (_ws / "VDIR").mkdir()
    (_ws / "scraper.py").write_text("root\n", encoding="utf-8")
    (_ws / "VDIR" / "scraper.py").write_text("vdir\n", encoding="utf-8")
    out = _run({"file_path": "VDIR/scraper.py"})
    head = "\n".join(out.splitlines()[:3])
    assert "同じ名前のファイルが他にもあります" in head, head
    assert str((_ws / "scraper.py").resolve()) in head
    assert "vdir" in out and "root" not in _body(out)


def test_同名が無ければ警告しない(_ws):
    (_ws / "only.py").write_text("z=1\n", encoding="utf-8")
    out = _run({"file_path": "only.py"})
    assert "同じ名前" not in out
