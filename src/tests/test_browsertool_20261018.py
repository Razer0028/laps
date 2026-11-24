"""★2026-10-18 Browser 道具（アプリの中のブラウザをエージェントが操作する）。アプリへの頼みは tools/appcall の窓口を通る。
アプリ（Rust の browser.rs）は使わない: 窓口に偽の相手を差し込んで、道具の形（名乗り・引数の確かめ・答えの書き方）を見る。
"""
from __future__ import annotations

from laps.oc import browsertool
from laps.tools import appcall


def teardown_function(_f):
    appcall.set_caller(None)


def test_not_offered_unless_the_app_says_so(monkeypatch):
    # ★2026-11-03: アプリの外でも Chrome があれば出す（browsercdp.PipeLine）。ここは Chrome の無い機械の話
    monkeypatch.setattr(browsertool.browsercdp, "pipe_ok", lambda: False)
    appcall.set_caller(None)
    assert not browsertool.available()
    appcall.set_caller(lambda *a: {"ok": True}, caps=["files"])
    assert not browsertool.available()                     # browser を名乗っていない
    appcall.set_caller(lambda *a: {"ok": True}, caps=["browser"])
    assert browsertool.available()


def test_read_formats_page_links_and_fields():
    seen = []

    def fake(op, args, timeout):
        seen.append((op, args))
        return {"ok": True, "url": "https://example.com/", "title": "Example", "text": "Hello page", "truncated": False,
                "links": [{"text": "More", "href": "https://example.com/more"}],
                "fields": [{"tag": "input", "type": "text", "name": "q", "id": "", "placeholder": "Search", "label": ""}]}
    appcall.set_caller(fake, caps=["browser"])
    r = browsertool.run({"action": "read"})
    assert r.ok
    s = str(r)
    assert "Page: Example" in s and "URL: https://example.com/" in s and "Hello page" in s
    assert "More -> https://example.com/more" in s
    assert "input[name=q]" in s and "placeholder=Search" in s
    assert seen == [("browser", {"action": "read"})]


def test_bad_arguments_are_refused_without_calling_the_app():
    calls = []
    appcall.set_caller(lambda *a: calls.append(a) or {"ok": True}, caps=["browser"])
    assert not browsertool.run({"action": "open"}).ok             # url が無い
    assert not browsertool.run({"action": "click"}).ok            # text も selector も無い
    assert not browsertool.run({"action": "jump"}).ok             # 知らない action
    assert calls == []


def test_app_failure_is_named():
    appcall.set_caller(lambda *a: {"ok": False, "error": "no element matched text \"Sign in\"", "url": "https://x/"},
                       caps=["browser"])
    r = browsertool.run({"action": "click", "text": "Sign in"})
    assert not r.ok and "no element matched" in str(r) and "URL: https://x/" in str(r)


def test_click_reports_where_it_landed():
    appcall.set_caller(lambda *a: {"ok": True, "url": "https://x/done", "title": "Done", "clicked": "Sign in"},
                       caps=["browser"])
    r = browsertool.run({"action": "click", "text": "Sign in"})
    assert r.ok and "Clicked «Sign in»" in str(r) and "https://x/done" in str(r)




def _fake_page(log):
    def fake(op, args, timeout):
        log.append(args)
        return {"ok": True, "url": "https://x/", "title": "X", "text": "Hello\nPrice: 300 yen\nBye",
                "links": [{"text": "Price list", "href": "https://x/p"}], "fields": [], "marks": [], "png": ""}
    return fake


def test_aliases_from_other_browser_tools():
    log = []
    appcall.set_caller(_fake_page(log), caps=["browser"])
    assert browsertool.run({"action": "navigate", "url": "https://x/"}).ok
    assert browsertool.run({"action": "left_click", "coordinate": [10, 20]}).ok
    assert browsertool.run({"action": "press_key", "text": "Enter"}).ok
    assert [a["action"] for a in log] == ["open", "click", "key"]
    assert log[1]["x"] == 10 and log[2]["key"] == "Enter"


def test_find_lists_only_matching_places():
    log = []
    appcall.set_caller(_fake_page(log), caps=["browser"])
    s = str(browsertool.run({"action": "find", "text": "price"}))
    assert "Price: 300 yen" in s and "Price list -> https://x/p" in s and "Hello" not in s
    assert "not on this page" in str(browsertool.run({"action": "find", "text": "zzz"}))


def test_then_read_returns_the_page_in_one_call():
    log = []
    appcall.set_caller(_fake_page(log), caps=["browser"])
    s = str(browsertool.run({"action": "click", "text": "Price list", "then": "read"}))
    assert "Clicked" in s and "Price: 300 yen" in s
    assert [a["action"] for a in log] == ["click", "read"]


def test_wait_for_text_present():
    log = []
    appcall.set_caller(_fake_page(log), caps=["browser"])
    r = browsertool.run({"action": "wait", "text": "300 yen", "seconds": 2})
    assert r.ok and "is on the page now" in str(r)
