"""書き込み系の道具が、UTF-8 でないファイルを**壊さない**こと（2026-09-20・敵対レビュー）。

★実際に起きていたこと: cp932 の .bat を `Edit` で ASCII の語だけ直すと「OK: 1 replacement」と返りながら、
  日本語が全部 U+FFFD になって書き戻されていた（92→147 バイト）。バイナリは 256→512 バイト。
  `Read` は cp932 を正しく見せるので、モデルにも利用者にも気づく材料が無かった。
"""
from __future__ import annotations

import pytest

from laps.tools import apply_diff as AD
from laps.tools import edit as E
from laps.tools import textcodec as TC
from laps.tools import write as W

BAT = "@echo off\r\nrem 設定: 接続先を変えるときはここを直す\r\nset HOST=localhost\r\necho 完了しました\r\n"


@pytest.fixture(autouse=True)
def _trusted(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))


def test_cp932のファイルをASCIIの語で直しても日本語が残る(tmp_path):
    f = tmp_path / "run.bat"
    f.write_bytes(BAT.encode("cp932"))
    r = str(E._run({"file_path": str(f), "old_string": "HOST=localhost", "new_string": "HOST=192.0.2.160"}))
    assert r.startswith("OK"), r
    after = f.read_bytes()
    assert after == BAT.replace("HOST=localhost", "HOST=192.0.2.160").encode("cp932")
    assert "�" not in after.decode("cp932")


def test_cp932のファイルに日本語で直せる(tmp_path):
    f = tmp_path / "run.bat"
    f.write_bytes(BAT.encode("cp932"))
    r = str(E._run({"file_path": str(f), "old_string": "完了しました", "new_string": "終わりました"}))
    assert r.startswith("OK"), r
    assert "終わりました".encode("cp932") in f.read_bytes()


def test_cp932で表せない字は書かずに断る(tmp_path):
    f = tmp_path / "run.bat"
    raw = BAT.encode("cp932")
    f.write_bytes(raw)
    r = str(E._run({"file_path": str(f), "old_string": "完了しました", "new_string": "完了 ✅"}))
    assert r.startswith("Error") and "cp932" in r, r
    assert f.read_bytes() == raw, "断ったのに書き換えている"


def test_バイナリは編集せずに断る(tmp_path):
    f = tmp_path / "bin.dat"
    raw = bytes(range(256))
    f.write_bytes(raw)
    r = str(E._run({"file_path": str(f), "old_string": "A", "new_string": "B"}))
    assert r.startswith("Error") and "バイナリ" in r, r
    assert f.read_bytes() == raw, "旧: 256→512 バイトに壊していた"


def test_どの候補でも往復できないファイルは断る(tmp_path):
    f = tmp_path / "weird.txt"
    raw = b"key=value\n\xff\xfe\xfd broken \x81\n"     # NUL は無いが、どれでも厳密には読めない
    f.write_bytes(raw)
    r = str(E._run({"file_path": str(f), "old_string": "key=value", "new_string": "key=other"}))
    assert r.startswith("Error"), r
    assert f.read_bytes() == raw


def test_UTF8のBOMは保つ(tmp_path):
    f = tmp_path / "bom.ps1"
    f.write_bytes(b"\xef\xbb\xbf" + "Write-Host '開始'\r\n".encode("utf-8"))
    r = str(E._run({"file_path": str(f), "old_string": "開始", "new_string": "終了"}))
    assert r.startswith("OK"), r
    assert f.read_bytes() == b"\xef\xbb\xbf" + "Write-Host '終了'\r\n".encode("utf-8")


def test_UTF16のPowerShellも直せる(tmp_path):
    f = tmp_path / "u16.ps1"
    f.write_bytes(b"\xff\xfe" + "Write-Host 'a'\r\n".encode("utf-16-le"))
    r = str(E._run({"file_path": str(f), "old_string": "'a'", "new_string": "'b'"}))
    assert r.startswith("OK"), r
    assert f.read_bytes() == b"\xff\xfe" + "Write-Host 'b'\r\n".encode("utf-16-le")


def test_ApplyDiffは読んだ符号化で書き戻す(tmp_path):
    f = tmp_path / "run.bat"
    f.write_bytes(BAT.encode("cp932"))
    diff = f"{f}\n<<<<<<< SEARCH\nset HOST=localhost\n=======\nset HOST=10.0.0.1\n>>>>>>> REPLACE\n"
    r = str(AD._run({"diff": diff}))
    assert "✓" in r, r
    assert f.read_bytes() == BAT.replace("HOST=localhost", "HOST=10.0.0.1").encode("cp932"), "旧: UTF-8 に変わっていた"


def test_ApplyDiffはバイナリを壊さない(tmp_path):
    f = tmp_path / "bin.dat"
    raw = b"HEAD\x00\x01\x02" + bytes(range(200, 256))
    f.write_bytes(raw)
    diff = f"{f}\n<<<<<<< SEARCH\nHEAD\n=======\nTAIL\n>>>>>>> REPLACE\n"
    r = str(AD._run({"diff": diff}))
    assert "✗" in r, r
    assert f.read_bytes() == raw


def test_Writeで上書きしても元の符号化を保つ(tmp_path):
    f = tmp_path / "run.bat"
    f.write_bytes(BAT.encode("cp932"))
    new = BAT.replace("localhost", "example").replace("\r\n", "\n")
    r = str(W._run({"file_path": str(f), "content": new}))
    assert r.startswith("OK") and "cp932" in r, r
    assert f.read_bytes() == new.encode("cp932")


def test_Writeは表せない字があればUTF8にしてそう言う(tmp_path):
    f = tmp_path / "run.bat"
    f.write_bytes(BAT.encode("cp932"))
    r = str(W._run({"file_path": str(f), "content": "echo ✅\n"}))
    assert r.startswith("OK") and "UTF-8 で書きました" in r, r
    assert f.read_bytes() == "echo ✅\n".encode("utf-8")


def test_新規と普通のUTF8は今までどおり(tmp_path):
    f = tmp_path / "a.txt"
    assert str(W._run({"file_path": str(f), "content": "日本語\n"})).startswith("OK")
    assert f.read_bytes() == "日本語\n".encode("utf-8")
    assert str(E._run({"file_path": str(f), "old_string": "日本語", "new_string": "にほんご"})).startswith("OK")
    assert f.read_bytes() == "にほんご\n".encode("utf-8")


def test_Readが見せる字とEditが突き合わせる字は同じ順で決まる():
    """★候補の順が食い違うと、Read で見えた語が Edit で「見つからない」になる。"""
    from laps.tools.read import decode_bytes
    for enc in ("utf-8", "cp932", "euc-jp"):
        raw = "設定=値\n".encode(enc)
        text, _e, _b = TC.decode_for_edit(raw)
        assert text == decode_bytes(raw)
