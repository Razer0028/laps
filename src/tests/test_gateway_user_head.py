"""履歴の題は利用者の文（前置きの後）から取る（2026-09-07）。"""
from laps.gateway import _user_head


def test_前置きの後の利用者の文を返す():
    c = "[アプリからの指示]\n作業ディレクトリ: /root/bw-mobile\nOS: Linux\n\n[利用者]\n現在の作業フォルダ内にあるゲームを調べてください"
    assert _user_head(c) == "現在の作業フォルダ内にあるゲームを調べてください"


def test_前置きが無ければ全文の先頭():
    assert _user_head("1+1 は？") == "1+1 は？"
    assert len(_user_head("x" * 500)) == 120
