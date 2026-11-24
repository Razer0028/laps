"""Browser の中身（oc/browsercdp）を本物の Chrome で確かめる（2026-11-03）。

なぜ本物か: 直す前の道具は「成功」と返して何も起きない形で壊れていた（el.click() は mousedown を出さない）。
偽物の相手では見えない。Chrome が無い機械では飛ばす。
アプリの線（AppLine）は Rust の代わりに Chrome へ中継する偽の窓口で通す＝本体が送る頼みの形まで確かめる。
"""
from __future__ import annotations

import http.server
import threading

import pytest

from laps.oc import browsercdp as bc
from laps.oc import browsertool as bt
from laps.tools import appcall

pytestmark = pytest.mark.skipif(not bc.pipe_ok(), reason="no Chrome")

INDEX = """<!doctype html><meta charset=utf-8><title>fixture</title>
<script>window.__hits=[];function hit(n){__hits.push(n)};addEventListener('message',e=>__hits.push(e.data))</script>
<h1>Test page</h1><p>Body text with <b>bold</b> in one line.</p>
<p><button onclick="hit('plain')">Plain</button></p>
<p id=shost></p>
<script>const s=document.getElementById('shost').attachShadow({mode:'open'});s.innerHTML='<button onclick="hit(\\'shadow\\')">InShadow</button><input aria-label="ShadowField" oninput="hit(\\'shadowtype\\')">';</script>
<iframe width=300 height=70 srcdoc="<!doctype html><button onclick=&quot;parent.hit('sameframe')&quot;>InSameFrame</button>"></iframe>
<iframe id=cross width=300 height=70></iframe>
<script>document.getElementById('cross').src='http://localhost:'+location.port+'/cross.html'</script>
<p><button onmousedown="hit('mousedown')">OpensOnMouseDown</button></p>
<p><button id=tr>NeedsTrusted</button></p>
<script>document.getElementById('tr').addEventListener('click',e=>{if(e.isTrusted)hit('trusted')})</script>
<div contenteditable=true aria-label=Editor style="border:1px solid #888;min-height:30px" oninput="hit('contenteditable')"></div>
<div id=lst style="width:200px">ListenerDiv</div>
<script>document.getElementById('lst').addEventListener('click',()=>hit('listenerdiv'))</script>
<p><button onclick="alert('hello');hit('alert')">ShowsAlert</button></p>
<p><button onclick="hit(confirm('sure?')?'confirm-yes':'confirm-no')">ShowsConfirm</button></p>
<p><input type=file aria-label=Upload onchange="hit('upload:'+this.files.length)"></p>
<p><button id=rr onclick="hit('rerender')">Rerendered</button></p>
<canvas id=cv width=200 height=50></canvas>
<script>document.getElementById('cv').addEventListener('click',()=>hit('canvas'))</script>
<p><label>Email <input oninput="hit('email:'+this.value)"></label></p>
<p><label>Date <input type=date onchange="hit('date:'+this.value)"></label></p>
<p><label>Country <select onchange="hit('select:'+this.value)"><option value=jp>Japan</option><option value=us>USA</option></select></label></p>
<form onsubmit="event.preventDefault();hit('submit');document.getElementById('msg').textContent='Saved OK'"><input aria-label=Query><button>Go</button></form>
<div id=msg></div>
<p><button onclick="console.error('boom in console')">LogsError</button></p>
<p><button>DoesNothing</button></p>
<div style="position:relative;height:40px"><button style="position:absolute;left:0;top:0">CoveredBtn</button><div style="position:absolute;left:0;top:0;width:300px;height:40px;background:#ccc">Overlay</div></div>
<div onmouseenter="document.getElementById('menu').style.display='block'" style="width:100px">HoverMe</div>
<div id=menu style="display:none"><button onclick="hit('menuitem')">MenuItem</button></div>
<a href="/two.html">GoTwo</a>
<div style="height:1500px"></div><button onclick="hit('far')">FarButton</button>
"""
PAGES = {
    "/index.html": INDEX,
    "/cross.html": """<!doctype html><meta charset=utf-8><button onclick="parent.postMessage('crossframe','*')">InCrossFrame</button>""",
    "/two.html": "<!doctype html><meta charset=utf-8><title>Two</title><h1>Second</h1><button>Back here</button>",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGES.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(params=["pipe", "app"])
def page(request, site, monkeypatch):
    """アプリの外（pipe）とアプリの線（app・Rust の代わりに Chrome へ中継）の両方で同じ試験を回す。"""
    bc.close()
    appcall.set_caller(None)
    chrome = None
    if request.param == "app":
        chrome = bc.PipeLine()

        def relay(op, args, timeout):
            assert op == "browser"
            a = args["action"]
            if a == "show":
                return {"ok": True}
            if a == "go":
                err = chrome.navigate(args["url"])
                return {"ok": not err, "error": err}
            if a == "cdp_events":
                return {"ok": True, "events": chrome.drain()}
            assert a == "cdp"
            try:
                return {"ok": True, "view": "child:1",
                        "result": chrome.call(args["method"], args.get("params"), args.get("session") or "", args.get("timeout") or 10.0)}
            except bc.CdpError as e:
                return {"ok": False, "error": str(e)}
        appcall.set_caller(relay, caps=("browser", "browser_cdp"))
    r = bt.run({"action": "open", "url": site + "/index.html"})
    assert r.ok, r
    pg = bc.page()
    assert pg.line.kind == request.param
    yield pg
    bc.close()
    appcall.set_caller(None)
    if chrome:
        chrome.close()


def hits(pg):
    return pg.ev("window.__hits") or []


@pytest.mark.parametrize("label,want", [
    ("Plain", "plain"), ("InShadow", "shadow"), ("InSameFrame", "sameframe"), ("InCrossFrame", "crossframe"),
    ("OpensOnMouseDown", "mousedown"), ("NeedsTrusted", "trusted"), ("ListenerDiv", "listenerdiv"), ("FarButton", "far"),
])
def test_click_reaches_it(page, label, want):
    r = bt.run({"action": "click", "text": label})
    assert r.ok, r
    assert want in hits(page)


def test_read_lists_everything_with_numbers(page):
    txt = str(bt.run({"action": "read", "max_chars": 60000}))
    for w in ("InShadow", "InSameFrame", "InCrossFrame", "Body text with", "ListenerDiv", "Country", "options: Japan, USA"):
        assert w in txt, w
    assert "Body text with bold in one line." in txt      # b で行を切らない
    assert '] button "Plain"' in txt


def test_numbers_stay_and_follow_a_redraw(page):
    snap = page.snapshot()
    ref = next(i.ref for i in snap.items if i.name == "Rerendered")
    assert ref == next(i.ref for i in page.snapshot().items if i.name == "Rerendered")
    page.ev("(()=>{const o=document.getElementById('rr');const n=o.cloneNode(true);o.replaceWith(n)})()")
    r = bt.run({"action": "click", "ref": ref})
    assert r.ok, r
    assert "rerender" in hits(page)


def test_old_number_is_refused_with_the_current_list(page):
    r = bt.run({"action": "click", "ref": 99999})
    assert not r.ok
    assert "no element has number" in r and 'button "Plain"' in r


def test_no_match_gives_close_names(page):
    r = bt.run({"action": "click", "text": "Plian"})
    assert not r.ok
    assert "no element matched" in r and 'button "Plain"' in r


def test_typing(page):
    assert bt.run({"action": "type", "field": "Editor", "text": "hello"}).ok
    assert "contenteditable" in hits(page)
    assert bt.run({"action": "type", "field": "ShadowField", "text": "hello"}).ok
    assert "shadowtype" in hits(page)
    assert bt.run({"action": "type", "field": "Email", "text": "me@x.jp"}).ok
    assert "email:me@x.jp" in hits(page)
    assert bt.run({"action": "type", "field": "Date", "text": "2026-11-03"}).ok
    assert "date:2026-11-03" in hits(page)
    assert bt.run({"action": "type", "field": "Country", "text": "USA"}).ok
    assert "select:us" in hits(page)
    r = bt.run({"action": "type", "field": "Country", "text": "Mars"})
    assert not r.ok and "Japan" in r


def test_fill_and_submit_tells_new_text(page):
    r = bt.run({"action": "fill", "fields": [{"field": "Query", "text": "abc"}], "submit": True})
    assert r.ok, r
    assert "submit" in hits(page)
    assert "Saved OK" in r


def test_dialogs_do_not_freeze(page):
    r = bt.run({"action": "click", "text": "ShowsAlert"})
    assert r.ok and 'alert: "hello"' in r
    assert "alert" in hits(page)
    r = bt.run({"action": "click", "text": "ShowsConfirm"})
    assert r.ok and "confirm dialog is open" in r
    r = bt.run({"action": "read"})
    assert not r.ok and "dialog is open" in r
    assert bt.run({"action": "dialog", "accept": False}).ok
    assert "confirm-no" in hits(page)


def test_upload(page, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    r = bt.run({"action": "upload", "text": "Upload", "path": str(f)})
    assert r.ok, r
    assert "upload:1" in hits(page)
    assert not bt.run({"action": "upload", "text": "Upload", "path": str(tmp_path / "none.txt")}).ok


def test_covered_is_not_clicked_silently(page):
    r = bt.run({"action": "click", "text": "CoveredBtn"})
    assert not r.ok and "is covered by" in r and "Overlay" in r


def test_says_when_nothing_changed(page):
    r = bt.run({"action": "click", "text": "DoesNothing"})
    assert r.ok and "Nothing on the page changed" in r


def test_console_errors_are_reported(page):
    r = bt.run({"action": "click", "text": "LogsError"})
    assert "boom in console" in r


def test_hover_shows_what_appeared(page):
    r = bt.run({"action": "hover", "text": "HoverMe"})
    assert r.ok and "MenuItem" in r
    assert bt.run({"action": "click", "text": "MenuItem"}).ok
    assert "menuitem" in hits(page)


def test_look_gives_a_picture_and_zoom(page):
    r = bt.run({"action": "look"})
    assert r.ok and r.data["image"]["b64"]
    assert '] button "Plain"' in r
    assert page.ev("!!document.getElementById('__bw_marks')") is False     # 札は残らない
    r2 = bt.run({"action": "look", "region": [0, 0, 500, 300]})
    assert r2.ok and "this part of the page" in r2


def test_click_by_position_in_the_picture(page):
    page.ev("document.getElementById('cv').scrollIntoView({block:'center'})")
    b = page.ev("(()=>{const r=document.getElementById('cv').getBoundingClientRect();return [(r.left+r.width/2)*1000/innerWidth,(r.top+r.height/2)*1000/innerHeight]})()")
    assert bt.run({"action": "click", "x": b[0], "y": b[1]}).ok
    assert "canvas" in hits(page)


def test_navigation_and_back(page):
    r = bt.run({"action": "click", "text": "GoTwo"})
    assert r.ok and "Now at: Two" in r
    r = bt.run({"action": "back"})
    assert r.ok and "Now at: fixture" in r


def test_steps_stop_at_the_first_failure(page):
    r = bt.run({"action": "steps", "steps": [{"action": "click", "text": "Plain"}, {"action": "click", "text": "Nope!"}, {"action": "click", "text": "InShadow"}]})
    assert not r.ok and "Stopped at step 2" in r
    assert "plain" in hits(page) and "shadow" not in hits(page)


def test_names_other_tools_use():
    assert bt.normalize({"action": "browser_click", "element": "OK"})["action"] == "click"
    assert bt.normalize({"action": "fill", "selector": "#a", "value": "x"})["action"] == "type"
    assert bt.normalize({"action": "fill", "fields": [{"ref": 1, "text": "x"}]})["action"] == "fill"
    assert bt.normalize({"action": "dismiss_dialog"})["accept"] is False


def test_no_vision_text_has_no_pictures():
    d = bt.description(False)
    assert "look" not in d and "[3] button" in d
    assert "look" not in bt.schema(False)["properties"]["action"]["enum"]


def test_only_http_pages():
    with pytest.raises(bc.CdpError):
        bc.to_url("file:///etc/passwd")
    assert bc.to_url("localhost:5173/a") == "http://localhost:5173/a"
    assert bc.to_url("example.com").startswith("https://")


def test_desktop_text_click_prefers_the_tree(monkeypatch):
    """文字で押すとき、部品の木に同じ名前が 1 つあれば番号で押す（OCR の座標で押すと Chrome で効かなかった）。"""
    from laps.oc import desktop

    class H:
        def call(self, q, timeout):
            return {"ok": True, "text": 'Window "x - Google Chrome" - chrome on s1\n[3] Button "閉じる"\n[38] Document "x"\n[43] Button "Add Element"\n[44] Text "Add Element"'}
    monkeypatch.setattr(desktop, "_get_helper", lambda: H())
    monkeypatch.setattr(desktop, "_guard_pids", lambda: [])
    r = {"action": "click", "text": "add element", "window": "x"}
    assert desktop._click_target_from_tree(r) and r["id"] == 43 and "text" not in r
    r = {"action": "click", "text": "Nope", "window": "x"}
    assert not desktop._click_target_from_tree(r) and r["text"] == "Nope"


def test_foreign_resource_errors_are_noise():
    assert bc._foreign("https://x.optimizely.com/a", "https://the-internet.herokuapp.com/")
    assert not bc._foreign("https://the-internet.herokuapp.com/a.js", "https://the-internet.herokuapp.com/")
    assert not bc._foreign("https://cdn.example.com/a.js", "https://example.com/")


def test_image_window_moves_in_steps_not_every_picture():
    """古い画像を外すと読み込みの控えが切れる。1 枚ごとではなく keep 枚ごとにまとめて外す。"""
    from laps.engine.loop import _first_kept_image as f
    firsts = [f(t, 3) for t in range(0, 13)]
    assert firsts == [0, 0, 0, 0, 0, 0, 3, 3, 3, 6, 6, 6, 9]
    assert sum(1 for a, b in zip(firsts, firsts[1:]) if a != b) == 3       # 12 枚で切れるのは 3 回（前は 9 回）
    assert all(t - f(t, 3) <= 5 for t in range(13))                        # 同時に送るのは多くて 5 枚


# ★2026-11-19 記録で eval／js を何度も呼んでいた → action=eval（式でも関数でもよい・Promise は待つ）。
def test_eval_returns_values_and_errors(page):
    assert str(bt.run({"action": "eval", "expression": "document.title"})) == "fixture"
    assert str(bt.run({"action": "js", "js": "() => [1, 2].length"})) == "2"
    assert str(bt.run({"action": "evaluate", "script": "async () => { await new Promise(r => setTimeout(r, 50)); return {a: 1}; }"})) == '{"a": 1}'
    bad = bt.run({"action": "eval", "expression": "nope.x"})
    assert not bad.ok and "nope" in str(bad)
    assert not bt.run({"action": "eval"}).ok


def test_file_url_points_to_serve(page):
    r = bt.run({"action": "open", "url": "file:///tmp/x.html"})
    assert not r.ok and "Serve" in str(r)
