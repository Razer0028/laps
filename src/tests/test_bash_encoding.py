"""Bash の出力の復号。**cmd.exe は cp932 で出す。**

★なぜこの試験があるか（2026-08-16・実機の記録から）:
実機で `cmd.exe` が返した「指定されたパスが見つかりません。」が、記録の中では
U+FFFD の並びになっていた。cp932 で出したものを UTF-8 として読んだ結果と
**バイト単位で一致**することを確認した。

    "指定されたパスが見つかりません。\\n".encode("cp932").decode("utf-8","replace")
      == 記録にあった文字列        → True

`bash.py` は `text=True` を使い `encoding=` を書いていなかった。サイドカーは
PYTHONUTF8=1 を渡すので既定は常に UTF-8 になり、**日本語 Windows の失敗メッセージが
全部読めなくなる**。「パスが見つかりません」「アクセスが拒否されました」「使用中です」
——Windows で最も多い失敗が、エージェントには意味の無い記号列として届いていた。

★頻度の問題ではない（記録では 1/21 回）。**日本語が出たら必ず読めない**という種類の問題。
"""
from __future__ import annotations

import pytest

from laps.tools.bash import _decode_out
from laps.tools.read import decode_bytes


# 日本語 Windows の cmd.exe が実際に返す文面
CP932_CASES = [
    "指定されたパスが見つかりません。\n",
    "アクセスが拒否されました。\n",
    "プロセスはファイルにアクセスできません。別のプロセスが使用中です。\n",
    "'foo' は、内部コマンドまたは外部コマンド、\n操作可能なプログラムまたはバッチ ファイルとして認識されていません。\n",
]


@pytest.mark.parametrize("text", CP932_CASES)
def test_cmd_exeのcp932出力がそのまま読める(text: str) -> None:
    assert _decode_out(text.encode("cp932")) == text


@pytest.mark.parametrize("text", CP932_CASES)
def test_これを直す前は読めなかったことを示す(text: str) -> None:
    """★対照。直す前の経路（UTF-8 で読む）では文字が失われる。

    この試験が落ちるようになったら、それは「cp932 が UTF-8 として妥当になった」
    ということなので、そのときは対照の作り方を疑う。
    """
    broken = text.encode("cp932").decode("utf-8", "replace")
    assert "�" in broken
    assert broken != text


def test_utf8の出力はそのまま読める() -> None:
    s = "ファイルを作成しました。\n"
    assert _decode_out(s.encode("utf-8")) == s


def test_英数字だけの出力は壊れない() -> None:
    s = "total 0\ndrwxr-xr-x 2 root root 4096 Aug 16 00:00 .\n"
    assert _decode_out(s.encode("utf-8")) == s


def test_空とNoneは空文字になる() -> None:
    # stdout / stderr は片方が None になりうる。連結で落ちないこと。
    assert _decode_out(None) == ""
    assert _decode_out(b"") == ""


def test_すでに文字列なら素通しする() -> None:
    # 呼び出し側が text=True に戻したときの保険。二重復号で壊さない。
    assert _decode_out("すでに文字列") == "すでに文字列"


def test_復号器はreadと同じものを使っている() -> None:
    """★2つ持つと必ず片方だけ直る。実際に read.py だけが直り、bash.py が
    2日そのままだった（コミット e38e9a0 は agent+read のみ）。"""
    raw = "指定されたパスが見つかりません。\n".encode("cp932")
    assert _decode_out(raw) == decode_bytes(raw)


def test_BOM付きutf8も読める() -> None:
    s = "見出し\n"
    assert decode_bytes(b"\xef\xbb\xbf" + s.encode("utf-8")) == s
