"""bs4 が入っていない機械の代替経路（2026-09-03・W3 点検で発見した欠けている試験）。

`websearch._parse_ddg` と `webfetch._html_to_text` はどちらも「bs4 があれば使う、
無ければ正規表現の簡易版に落ちる」という2経路構造だが、この開発機には bs4 が
入っているため、**簡易版のほうは一度も試験に通っていなかった**（`grep -rn
"bs4|BeautifulSoup|ImportError" tests/` に websearch/webfetch の行が1つも無い）。
出荷先（この製品は .deb で配る）が同じ環境とは限らない以上、bs4 無しの経路が
壊れていても今の試験群では気づけない。

`sys.modules["bs4"] = None` で `import bs4` を確実に ImportError にする
（標準の作法）。ネットワークには一切出ない — どちらの関数も文字列を受けて
文字列を返す純粋関数。
"""
from __future__ import annotations

import sys

import pytest


@pytest.fixture()
def _no_bs4(monkeypatch):
    monkeypatch.setitem(sys.modules, "bs4", None)
    yield


def test_websearchのDDG解析はbs4が無くても結果を拾える(_no_bs4):
    from laps.tools import websearch as W

    html = (
        '<div class="result">'
        '<a class="result__a" href="http://x.example/a">Title A</a>'
        '<a class="result__snippet">snippet ignored by regex fallback</a>'
        '</div>'
        '<div class="result">'
        '<a class="result__a" href="http://x.example/b">Title B</a>'
        '</div>'
    )
    got = W._parse_ddg(html, 5)
    assert [r["title"] for r in got] == ["Title A", "Title B"]
    assert [r["url"] for r in got] == ["http://x.example/a", "http://x.example/b"]


def test_websearchのDDG解析はbs4が無くてもuddgの転送を剥がす(_no_bs4):
    """DDG のラップ URL（/l/?uddg=...）は bs4 の経路と同じく展開されること。"""
    from laps.tools import websearch as W
    import urllib.parse

    real = "https://example.com/real-target"
    wrapped = "/l/?uddg=" + urllib.parse.quote(real, safe="")
    html = f'<a class="result__a" href="{wrapped}">Wrapped</a>'
    got = W._parse_ddg(html, 5)
    assert got and got[0]["url"] == real


def test_websearchのDDG解析はbs4が無くてもn件で打ち切る(_no_bs4):
    from laps.tools import websearch as W

    html = "".join(
        f'<a class="result__a" href="http://x.example/{i}">T{i}</a>' for i in range(10)
    )
    got = W._parse_ddg(html, 3)
    assert len(got) == 3


def test_webfetchのHTML抽出はbs4が無くてもscriptとstyleを消す(_no_bs4):
    from laps.tools import webfetch as F

    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>evil()</script><p>Hello &amp; World</p></body></html>"
    )
    text = F._html_to_text(html)
    assert "Hello & World" in text
    assert "evil()" not in text
    assert "color:red" not in text


def test_bs4があるときと無いときで同じHTMLの結果テキストが一致する(monkeypatch):
    """簡易版が bs4 版と乖離していないか（実際に両方の経路を同じ入力で比べる）。"""
    from laps.tools import webfetch as F

    html = "<body><p>Alpha</p><p>Beta</p></body>"
    with_bs4 = F._html_to_text(html)
    monkeypatch.setitem(sys.modules, "bs4", None)
    without_bs4 = F._html_to_text(html)
    assert "Alpha" in with_bs4 and "Beta" in with_bs4
    assert "Alpha" in without_bs4 and "Beta" in without_bs4
