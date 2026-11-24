"""Browser の操作中の表示（2026-11-18 オーナー「操作されているのか・どこを操作しているのかが分かりづらい」）を本物の Chrome で確かめる。

確かめる事:
- CSP で style を止めたページでも、縁・カーソル・札が出る（閉じた Shadow DOM＋CSSOM）。
- 表示はページの部品の一覧（look・read）に入らない＝モデルが自分の「止める」を押さない。
- 札に重なる場所の部品も押せる（操作の間は札がクリックを通す）。
- look の写真を撮る間は表示を隠す。
- 人が「止める」を押すと、次の操作は denied で止まる。
- LAPS_BROWSER_INDICATOR=0 で出さない。
Chrome が無い機械では飛ばす。
"""
from __future__ import annotations

import functools
import http.server
import threading

import pytest

from laps.oc import browsercdp as bc

pytestmark = pytest.mark.skipif(not bc.pipe_ok(), reason="no Chrome")

PAGE = """<!doctype html><html lang=ja><head><meta charset=utf-8>
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; style-src 'self'; script-src 'self'">
<title>indicator</title><link rel=stylesheet href=page.css></head>
<body><h1>注文</h1><label>名前 <input id=n></label><button id=go>送信する</button><p id=out>まだ</p>
<button id=corner class=corner>右下のボタン</button><script src=page.js></script></body></html>"""
CSS = "body{margin:40px}.corner{position:fixed;right:20px;bottom:20px}"
JS = """const o=document.getElementById('out');
document.getElementById('go').addEventListener('click',()=>{o.textContent='送信: '+document.getElementById('n').value});
document.getElementById('corner').addEventListener('click',()=>{o.textContent='右下'});"""


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    d = tmp_path_factory.mktemp("site")
    (d / "page.html").write_text(PAGE, encoding="utf-8")
    (d / "page.css").write_text(CSS, encoding="utf-8")
    (d / "page.js").write_text(JS, encoding="utf-8")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(d)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/page.html"
    srv.shutdown()
    bc.close()


def test_indicator_shows_moves_and_stays_out_of_the_model_view(site):
    r = bc.run({"action": "open", "url": site})
    assert r.ok, str(r)
    pg = bc.page()
    assert pg.ev("!!document.querySelector('[data-laps-agent]')"), "表示がページに無い（CSP のページ）"
    assert pg.ev("document.querySelector('[data-laps-agent]').getAttribute('aria-hidden')") == "true"
    assert bc.run({"action": "type", "selector": "#n", "text": "太郎"}).ok
    r = bc.run({"action": "click", "text": "送信する"})
    assert r.ok and "送信: 太郎" in str(r), str(r)
    # 札に重なる右下の部品も押せる
    r = bc.run({"action": "click", "text": "右下のボタン"})
    assert r.ok and pg.ev("document.getElementById('out').textContent") == "右下", str(r)
    # 部品の一覧に「止める」が入らない・写真の間は隠れて、撮った後は戻る
    lk = bc.run({"action": "look"})
    assert lk.ok and "止める" not in str(lk)
    assert pg.ev("document.querySelector('[data-laps-agent]').style.visibility") == ""
    rd = bc.run({"action": "read"})
    assert "止める" not in str(rd) and "L.A.P.S が操作中" not in str(rd)


def test_stop_button_stops_the_next_action(site):
    assert bc.run({"action": "open", "url": site}).ok
    pg = bc.page()
    w, h = pg.ev("[innerWidth, innerHeight]")
    pg.press(w - 14 - 30, h - 14 - 16)          # 右下の札の「止める」を本物のクリックで押す
    r = bc.run({"action": "click", "text": "送信する"})
    assert not r.ok and r.kind == "denied" and "止める" in str(r), str(r)
    assert pg.ev("document.getElementById('out').textContent") == "まだ"
    r = bc.run({"action": "click", "text": "送信する"})      # 止めたのは 1 回だけ（次は動く）
    assert r.ok, str(r)


def test_indicator_can_be_turned_off(site, monkeypatch):
    monkeypatch.setenv("LAPS_BROWSER_INDICATOR", "0")
    bc.close()
    assert bc.run({"action": "open", "url": site}).ok
    assert not bc.page().ev("!!document.querySelector('[data-laps-agent]')")
