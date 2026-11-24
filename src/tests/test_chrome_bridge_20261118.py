"""ふだんの Chrome の線（L.A.P.S の Chrome 拡張機能・2026-11-18）を本物の Chrome で端から端まで確かめる。

本物の拡張機能（app/chrome-ext）を Chrome に読み込み（Extensions.loadUnpacked・--enable-unsafe-extension-debugging）、
本体側の待ち受け（chromebridge.Bridge・アプリの外の形）につながせて、Browser(browser="chrome") を流す。
アプリの中の形（Rust の chromebridge.rs が口を持つ）は Windows の実機で確かめる（ここでは偽の app_call で答え方だけ）。
Chrome が無い・拡張機能を読み込めない機械では飛ばす。
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import threading
import time
from pathlib import Path

import pytest

from laps.oc import browsercdp as bc
from laps.oc import browsertool as bt
from laps.oc import chromebridge as cb

pytestmark = pytest.mark.skipif(not bc.pipe_ok(), reason="no Chrome")
EXT = Path(__file__).resolve().parents[2] / "app" / "chrome-ext"
PAGE = """<!doctype html><meta charset=utf-8><title>bridge</title><label>名前 <input id=n></label>
<button id=go onclick="document.getElementById('out').textContent='送信: '+document.getElementById('n').value">送信する</button>
<p id=out>まだ</p>"""


class _ExtChrome(bc.PipeLine):
    FLAGS = tuple(f for f in bc.PipeLine.FLAGS if f != "--disable-extensions") + ("--enable-unsafe-extension-debugging",)


@pytest.fixture(scope="module")
def chrome(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    old = {k: os.environ.get(k) for k in ("LAPS_HOME", "LAPS_CHROME_ASK_SITES", "LAPS_CHROME_BRIDGE_PORT")}
    os.environ["LAPS_HOME"] = str(home)
    os.environ["LAPS_CHROME_ASK_SITES"] = "0"
    os.environ["LAPS_CHROME_BRIDGE_PORT"] = "18799"            # 本物の 18765 と重ねない
    site = tmp_path_factory.mktemp("site")
    (site / "page.html").write_text(PAGE, encoding="utf-8")

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(site)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cb._BRIDGE = None
    br = cb.bridge()
    assert br is not None and br.server is not None, br and br.error
    ch = _ExtChrome()
    try:
        ext = ch._raw("Extensions.loadUnpacked", {"path": str(EXT)})
    except bc.CdpError as e:
        ch.close()
        pytest.skip(f"this Chrome cannot load an unpacked extension here: {e}")
    sw = None
    for _ in range(40):
        sw = next((t for t in ch._raw("Target.getTargets")["targetInfos"]
                   if t["type"] == "service_worker" and ext["id"] in t["url"]), None)
        if sw:
            break
        time.sleep(0.2)
    s = ch._raw("Target.attachToTarget", {"targetId": sw["targetId"], "flatten": True})["sessionId"]
    cfg = cb.config()
    ch._raw("Runtime.evaluate", {"expression": f"chrome.storage.local.set({{token: {json.dumps(cfg['token'])}, port: {cfg['port']}}})",
                                 "awaitPromise": True}, s)
    for _ in range(60):
        if br.connected():
            break
        time.sleep(0.25)
    assert br.connected(), "the extension did not connect"
    yield {"url": f"http://127.0.0.1:{srv.server_address[1]}/page.html", "chrome": ch, "sw": s, "bridge": br}
    bc._CPAGE = None
    ch.close()
    srv.shutdown()
    br.server.shutdown()
    cb._BRIDGE = None
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_open_type_click_in_the_users_chrome(chrome):
    r = bt.run({"action": "open", "browser": "chrome", "url": chrome["url"]})
    assert r.ok and "送信する" in str(r), str(r)
    assert bt.run({"action": "type", "browser": "chrome", "selector": "#n", "text": "花子"}).ok
    r = bt.run({"action": "click", "browser": "chrome", "text": "送信する"})
    assert r.ok and "送信: 花子" in str(r), str(r)
    assert bc._CPAGE.ev("!!document.querySelector('[data-laps-agent]')")       # 操作中の表示もこの線で出る
    groups = chrome["chrome"]._raw("Runtime.evaluate", {"expression": "chrome.tabGroups.query({}).then(g=>JSON.stringify(g.map(x=>x.title)))",
                                                        "awaitPromise": True}, chrome["sw"])["result"]["value"]
    assert "L.A.P.S" in groups


def test_tabs_and_attach_to_an_open_tab(chrome):
    r = bt.run({"action": "tabs"})
    assert r.ok and "bridge" in str(r), str(r)
    tab = bc._CPAGE.line.tab
    r = bt.run({"action": "open", "browser": "chrome", "tab": tab})          # url なし＝そのタブにつないで読むだけ
    assert r.ok and "送信する" in str(r), str(r)


def test_user_releasing_the_tab_stops_without_reopening(chrome):
    assert bt.run({"action": "open", "browser": "chrome", "url": chrome["url"]}).ok
    tab = bc._CPAGE.line.tab
    chrome["chrome"]._raw("Runtime.evaluate", {"expression": f"chrome.debugger.detach({{tabId:{tab}}})", "awaitPromise": True}, chrome["sw"])
    time.sleep(0.4)
    r = bt.run({"action": "read", "browser": "chrome"})
    assert not r.ok and r.kind == "denied" and "released" in str(r), str(r)


def test_site_permission_is_asked_and_remembered(chrome, monkeypatch):
    from laps.tools import ask_user
    monkeypatch.setenv("LAPS_CHROME_ASK_SITES", "1")
    cb._SESSION_SITES.clear()
    asked = []
    monkeypatch.setattr(ask_user, "_ASKER", lambda q, opts: (asked.append(q), "許さない")[1])
    r = bt.run({"action": "open", "browser": "chrome", "url": chrome["url"]})
    assert not r.ok and r.kind == "denied" and asked, str(r)
    monkeypatch.setattr(ask_user, "_ASKER", lambda q, opts: (asked.append(q), "この会話の間は許す")[1])
    assert bt.run({"action": "open", "browser": "chrome", "url": chrome["url"]}).ok
    n = len(asked)
    assert bt.run({"action": "read", "browser": "chrome"}).ok and len(asked) == n      # 2 回目は聞かない


def test_inside_the_app_requests_go_through_app_call(monkeypatch):
    from laps.tools import appcall
    calls = []

    def fake(op, args, timeout):
        calls.append((op, dict(args)))
        if args.get("op") == "status":
            return {"ok": True, "connected": False}
        return {"ok": False, "error": "the L.A.P.S Chrome extension is not connected"}
    monkeypatch.setattr(cb, "_APP_BRIDGE", None)
    appcall.set_caller(fake, caps=("chrome",))
    try:
        r = bt.run({"action": "tabs"})
    finally:
        appcall.set_caller(None)
    assert not r.ok and "not connected" in str(r) and calls and calls[0][0] == "chrome"
