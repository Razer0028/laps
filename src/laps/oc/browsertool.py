"""Browser 道具: Blackwall のアプリの中のブラウザを、エージェントが操作する（2026-10-18）。

オーナー「Blackwall 用ブラウザとかもつけたら幅が広がっていいかも」→ 順番は Files → 見るためのブラウザ → これ。
ページは右の欄の Browser（または切り離した窓）に出る＝利用者はエージェントが何を見て何を押したかをその場で見られる。
中身はアプリ（Rust の browser.rs）が WebView2 で JS を動かして返す。★待ち受けの口（CDP のポート）は作らない
（アプリの決まり）。頼みはサイドカーの標準入出力（app_call → app_reply・tools/appcall）で渡す。
出すのはアプリが app_caps に "browser" を名乗ったときだけ（端末・作業用のコンテナ・古いアプリでは出さない）。

★2026-11-03: ページの読み書きを oc/browsercdp.py（DevTools の命令）へ移した。アプリが "browser_cdp" を名乗るとき・アプリの外で
  Chrome があるときはそちらを使う。この下の app の JS を呼ぶ形（_call 以降）は、古いアプリに繋いだときのためだけに残してある。
"""
from __future__ import annotations

from ..tools import ToolResult, appcall
from . import browsercdp

NAME = "Browser"
APP_ACTIONS = ("open", "read", "look", "click", "type", "scroll", "key", "back", "forward", "reload")
# ★wait・find はアプリに頭まず、read を使ってこちらで組む（Rust を変えないので古いアプリでも動く・2026-10-19）。
ACTIONS = APP_ACTIONS + ("wait", "find", "hover", "drag", "fill", "upload", "dialog", "console", "steps", "tabs", "eval")
# 小さいモデルが他の道具（Playwright・Computer Use・Puppeteer）の名前で呼んだときに読み替える。
_ACTION_ALIASES = {
    "navigate": "open", "goto": "open", "go": "open", "visit": "open", "browser_navigate": "open",
    "snapshot": "read", "text": "read", "get_text": "read", "content": "read", "browser_snapshot": "read",
    "screenshot": "look", "see": "look", "view": "look", "browser_take_screenshot": "look",
    "left_click": "click", "press": "click", "tap": "click", "browser_click": "click",
    "input": "type", "enter_text": "type", "browser_type": "type",
    "press_key": "key", "keypress": "key", "browser_press_key": "key",
    "go_back": "back", "go_forward": "forward", "refresh": "reload",
    "sleep": "wait", "wait_for": "wait", "browser_wait_for": "wait", "search": "find", "find_text": "find",
    "mouse_move": "hover", "browser_hover": "hover", "browser_drag": "drag", "drag_and_drop": "drag",
    "select": "type", "select_option": "type", "browser_select_option": "type", "browser_fill_form": "fill", "fill_form": "fill",
    "upload_file": "upload", "file_upload": "upload", "browser_file_upload": "upload", "set_input_files": "upload",
    "browser_handle_dialog": "dialog", "handle_dialog": "dialog", "accept_dialog": "dialog", "dismiss_dialog": "dialog",
    "browser_console_messages": "console", "console_messages": "console", "browser_navigate_back": "back", "double_click": "click",
    "batch": "steps", "browser_evaluate": "eval", "evaluate": "eval", "js": "eval", "script": "eval", "run_js": "eval",
    "execute_script": "eval", "javascript": "eval",
}
_KEY_ALIASES = {"query": "text", "value": "text", "label": "text", "element": "text", "css": "selector",
                "href": "url", "link": "url", "id": "ref", "number": "ref", "press": "key",
                "duration": "seconds", "time": "seconds", "after": "then", "file": "path", "files": "path", "paths": "path",
                "form": "fields", "actions": "steps", "commands": "steps", "zoom": "region", "clip": "region",
                # ★2026-11-19 記録の失敗から: target_name（押す物の名前）・eval の式の名前
                "target_name": "text", "name_text": "text", "js": "expression", "script": "expression", "code": "expression",
                "function": "expression", "expr": "expression"}

DESCRIPTION = (
    "Use a real web browser. In the L.A.P.S app the user sees the page in the side panel while you work. Use it when a "
    "page needs clicking, typing or logging in, or to check a web page you built (open http://localhost:PORT after Serve). "
    "To just get the text of a page, WebFetch is faster.\n"
    "open and read return the page in reading order. Everything you can use has a number, including inside iframes:\n"
    "  [3] button \"Sign in\"\n  [4] textbox \"Email\"\n"
    "Use those numbers. A number stays the same while the element exists. After each action you are told what changed "
    "(new page, new items, new text, or that nothing changed), so you rarely need to read again.\n"
    "Example: Browser(action=\"open\", url=\"https://example.com\")\n"
    "Example: Browser(action=\"click\", ref=3)   or by label: Browser(action=\"click\", text=\"Sign in\")\n"
    "Example: Browser(action=\"type\", ref=4, text=\"me@example.com\", submit=true)   (also picks an option of a select)\n"
    "Example: Browser(action=\"fill\", fields=[{\"ref\": 4, \"text\": \"me\"}, {\"field\": \"Password\", \"text\": \"x\"}], submit=true)\n"
    "Example: Browser(action=\"find\", text=\"price\")   Browser(action=\"wait\", text=\"Done\", seconds=10)\n"
    "Example: Browser(action=\"key\", key=\"Escape\")   (or Control+A)   Browser(action=\"scroll\", direction=\"down\")\n"
    "Example: Browser(action=\"hover\", ref=5)   Browser(action=\"drag\", ref=5, to_ref=9)\n"
    "Example: Browser(action=\"upload\", ref=7, path=\"report.pdf\")   Browser(action=\"dialog\", accept=false)\n"
    "Example: Browser(action=\"console\")   (errors in the page console; they are also added to each result)\n"
    "Example: Browser(action=\"eval\", expression=\"document.querySelectorAll('.card').length\")   (run JavaScript in the page; returns the value)\n"
    "Example: Browser(action=\"steps\", steps=[{\"action\": \"click\", \"ref\": 3}, {\"action\": \"read\"}])   (several at once)\n"
    "Seeing the page: Browser(action=\"look\") takes a picture of the page and shows it to you, with the same red numbers. "
    "Use it to check how a page looks, or for what has no number (canvas, games). "
    "Browser(action=\"look\", region=[0, 0, 500, 300]) shows a part larger (left, top, right, bottom, 0 to 1000). "
    "Browser(action=\"click\", x=512, y=300) clicks a spot in the last picture (0 to 1000 across it).\n"
    "Add then=\"read\" or then=\"look\" to an action to get the page in the same call.\n"
    "The user's own Chrome: add browser=\"chrome\" to work in the user's everyday Chrome through the L.A.P.S Chrome extension "
    "(their logins and cookies; the tab runs in the background in a \"L.A.P.S\" tab group, so the user can keep working). "
    "Use it when the task needs a site the user is logged into, or the user asks for their Chrome. "
    "Browser(action=\"tabs\", browser=\"chrome\") lists their open tabs; Browser(action=\"open\", browser=\"chrome\", tab=12) "
    "works in an open tab instead of a new one. The first time on each site the user is asked to allow it. "
    "Never submit, buy, send or delete in the user's Chrome without asking the user first. "
    "If the extension is not connected, Desktop(action=\"web_snapshot\") can still read and click a page in the user's "
    "Chrome or Edge window (it moves the real mouse and cannot run JavaScript)."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS), "description": "what to do"},
        "url": {"type": "string", "description": "page to open (action=open)"},
        "ref": {"type": "integer", "description": "number of the element, from open, read or look"},
        "text": {"type": "string", "description": "visible label to click, or the text to type, or the words for find and wait"},
        "selector": {"type": "string", "description": "CSS selector of the element"},
        "field": {"type": "string", "description": "visible label of the field to type into"},
        "submit": {"type": "boolean", "description": "after typing, press Enter"},
        "fields": {"type": "array", "items": {"type": "object"}, "description": "fill: list of {ref or field or selector, text}"},
        "max_chars": {"type": "integer", "description": "how much page text to return (default 12000)"},
        "x": {"type": "number", "description": "click position across the last picture, 0 (left) to 1000 (right)"},
        "y": {"type": "number", "description": "click position down the last picture, 0 (top) to 1000 (bottom)"},
        "double": {"type": "boolean", "description": "click: double click"},
        "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "click: mouse button"},
        "force": {"type": "boolean", "description": "click even though something covers the element"},
        "direction": {"type": "string", "enum": ["down", "up"], "description": "scroll one screen down or up"},
        "key": {"type": "string", "description": "key to press: Enter, Tab, Escape, ArrowDown, Control+A"},
        "then": {"type": "string", "enum": ["read", "look"], "description": "after the action, also read or look at the page"},
        "seconds": {"type": "number", "description": "wait: longest time to wait (default 10, at most 30)"},
        "gone": {"type": "boolean", "description": "wait: wait until the text is gone"},
        "region": {"type": "array", "items": {"type": "number"}, "description": "look: part of the page to show larger: left, top, right, bottom, 0 to 1000"},
        "to_ref": {"type": "integer", "description": "drag: number of the element to drop on"},
        "to_text": {"type": "string", "description": "drag: label of the element to drop on"},
        "to_x": {"type": "number", "description": "drag: drop position, 0 to 1000"},
        "to_y": {"type": "number", "description": "drag: drop position, 0 to 1000"},
        "path": {"type": "string", "description": "upload: file to give to the file input"},
        "accept": {"type": "boolean", "description": "dialog: true to accept, false to dismiss"},
        "steps": {"type": "array", "items": {"type": "object"}, "description": "steps: Browser actions to run in order; stops at the first failure"},
        "browser": {"type": "string", "enum": ["panel", "chrome"], "description": "panel (default: the app's own browser) or chrome (the user's everyday Chrome through the L.A.P.S extension)"},
        "tab": {"type": "integer", "description": "open with browser=chrome: number of an open tab (from action=tabs) to work in"},
    },
    "required": ["action"],
}


def description(vision: bool = True) -> str:
    """画像を見られないモデルには look（写真）の段を外す（desktop.description と対・2026-10-19）。番号は read で得られる。"""
    if vision:
        return DESCRIPTION
    head, _, rest = DESCRIPTION.partition("Seeing the page:")
    tail = rest[rest.find("Add then="):].replace('then=\"read\" or then=\"look\"', 'then=\"read\"')
    return head + "This model cannot view pictures: read gives everything as text. Canvas and games cannot be read.\n" + tail


def schema(vision: bool = True) -> dict:
    if vision:
        return SCHEMA
    props = {k: dict(v) for k, v in SCHEMA["properties"].items() if k not in ("x", "y", "region", "to_x", "to_y")}
    props["action"]["enum"] = [a for a in ACTIONS if a != "look"]
    props["then"]["enum"] = ["read"]
    return {**SCHEMA, "properties": props}


def available() -> bool:
    return appcall.can("browser") or browsercdp.usable()


def _page(r: dict, max_links: int = 40, max_fields: int = 30) -> str:
    out = [f"Page: {r.get('title') or '(no title)'}", f"URL: {r.get('url') or ''}"]
    text = str(r.get("text") or "").strip()
    if text:
        out += ["", text + ("\n[... text cut; ask with a larger max_chars to see more]" if r.get("truncated") else "")]
    links = [l for l in (r.get("links") or []) if isinstance(l, dict) and l.get("href")]
    if links:
        out += ["", f"Links ({min(len(links), max_links)} of {len(links)}):"]
        out += [f"- {str(l.get('text') or '').strip()[:80] or '(no text)'} -> {l.get('href')}" for l in links[:max_links]]
    fields = [f for f in (r.get("fields") or []) if isinstance(f, dict)]
    if fields:
        out += ["", "Fields and buttons:"]
        for f in fields[:max_fields]:
            sel = f"#{f['id']}" if f.get("id") else (f"{f.get('tag')}[name={f['name']}]" if f.get("name") else f.get("tag", ""))
            desc = " ".join(x for x in (f.get("type"), f.get("label"), f.get("placeholder") and f"placeholder={f['placeholder']}") if x)
            out.append(f"- {sel}  {desc}".rstrip())
    return "\n".join(out)


def _look(r: dict, max_marks: int = 80) -> ToolResult:
    """撮った画面（番号の札つき）を画像としてモデルへ渡し、札の一覧を文字で添える。"""
    marks = [m for m in (r.get("marks") or []) if isinstance(m, dict)]
    lines = [f"Page: {r.get('title') or '(no title)'}", f"URL: {r.get('url') or ''}",
             "The picture of the visible page is attached. Red numbers mark what you can click or type into:"]
    for m in marks[:max_marks]:
        label = str(m.get("label") or "").strip()[:60]
        lines.append(f"[{m.get('ref')}] {m.get('kind') or ''} {label}".rstrip())
    if len(marks) > max_marks:
        lines.append(f"... and {len(marks) - max_marks} more (scroll or read to see the rest)")
    if r.get("more_below"):
        lines.append("There is more page below; Browser(action=\"scroll\", direction=\"down\") then look again.")
    b64 = str(r.get("png") or "")
    data = {"image": {"path": "browser-look.png", "mime": "image/png", "b64": b64}} if b64 else None
    return ToolResult("\n".join(lines), data=data)


def normalize(inp: dict) -> dict:
    """他の道具の名前の操作と引数をこの道具の形に直す（小さいモデルの言い間違いで 1 手無駄にしない）。"""
    r = dict(inp)
    a = str(r.get("action") or "").strip().lower().replace("-", "_")
    a = _ACTION_ALIASES.get(a, a)
    if a == "fill" and not isinstance(r.get("fields") or r.get("form"), list):      # Playwright の fill は 1 欄
        a = "type"
    if not a and r.get("url"):
        a = "open"
    r["action"] = a
    for k, v in _KEY_ALIASES.items():
        if k in r and v not in r:
            r[v] = r.pop(k)
    if a == "key" and not r.get("key") and r.get("text"):
        r["key"] = r.pop("text")
    if str(inp.get("action") or "").strip().lower() == "dismiss_dialog" and "accept" not in r:
        r["accept"] = False
    if str(inp.get("action") or "").strip().lower() == "double_click":
        r["double"] = True
    # 打つ先を label や element で書いた（文字は text か value）
    if a in ("type", "fill") and "text" in inp and ("label" in inp or "element" in inp) and "field" not in r:
        r["field"] = inp.get("label") or inp.get("element")
        r["text"] = inp["text"]
    if a == "type" and "text" not in r and inp.get("value") is not None:
        r["text"] = inp["value"]
    if a == "scroll":
        d = str(r.get("direction") or r.pop("scroll_direction", "") or "down").lower()
        r["direction"] = "up" if d in ("up", "top") else "down"
    coord = r.pop("coordinate", None)
    if isinstance(coord, (list, tuple)) and len(coord) == 2 and "x" not in r:
        r["x"], r["y"] = coord
    if isinstance(r.get("ref"), str) and r["ref"].strip().lstrip("[#").rstrip("]").isdigit():
        r["ref"] = int(r["ref"].strip().lstrip("[#").rstrip("]"))
    if isinstance(r.get("then"), str):
        r["then"] = r["then"].strip().lower()
    return r


def _find(r: dict, words: str, max_hits: int = 20) -> str:
    """ページの文字・リンク・欄から語を含む行だけを返す（ページ全体を読まずに済ませる）。"""
    w = words.strip().lower()
    hits = []
    for line in str(r.get("text") or "").splitlines():
        if w in line.lower():
            hits.append("text: " + line.strip()[:200])
    for l in r.get("links") or []:
        if isinstance(l, dict) and w in (str(l.get("text") or "") + " " + str(l.get("href") or "")).lower():
            hits.append(f"link: {str(l.get('text') or '').strip()[:80]} -> {l.get('href')}")
    for f in r.get("fields") or []:
        if isinstance(f, dict) and w in " ".join(str(f.get(k) or "") for k in ("label", "placeholder", "name", "id")).lower():
            hits.append("field: " + " ".join(str(f.get(k)) for k in ("tag", "name", "id", "label", "placeholder") if f.get(k)))
    head = f"Page: {r.get('title') or '(no title)'}\nURL: {r.get('url') or ''}\n"
    if not hits:
        return head + f"\"{words}\" is not on this page (text, links and fields were searched)."
    more = f"\n... {len(hits) - max_hits} more" if len(hits) > max_hits else ""
    return head + f"{len(hits)} place(s) with \"{words}\":\n" + "\n".join(hits[:max_hits]) + more


def _call(args: dict, timeout: float = 60.0):
    try:
        r = appcall.call("browser", args, timeout=timeout)
    except Exception as e:
        return ToolResult(f"Error: the app's browser could not be used: {e}", ok=False, kind="error")
    if not isinstance(r, dict):
        return ToolResult("Error: the app sent an answer that could not be read", ok=False, kind="error")
    if not r.get("ok"):
        return ToolResult(f"Error: {r.get('error') or 'the browser action failed'}"
                          + (f"\nURL: {r.get('url')}" if r.get("url") else ""), ok=False, kind="error")
    return r


def _wait(inp: dict) -> ToolResult:
    import time
    try:
        limit = max(0.0, min(30.0, float(inp.get("seconds") or 10)))
    except (TypeError, ValueError):
        limit = 10.0
    words = str(inp.get("text") or "").strip()
    if not words:
        time.sleep(limit)
        return ToolResult(f"Waited {limit:g} s.")
    end = time.monotonic() + limit
    while True:
        r = _call({"action": "read", "max_chars": 60000}, timeout=30.0)
        if isinstance(r, ToolResult):
            return r
        if words.lower() in str(r.get("text") or "").lower():
            return ToolResult(f"\"{words}\" is on the page now.\n" + _find(r, words, max_hits=5))
        if time.monotonic() >= end:
            return ToolResult(f"\"{words}\" did not appear within {limit:g} s.\nPage: {r.get('title') or '(no title)'}\n"
                              f"URL: {r.get('url') or ''}", ok=False, kind="error")
        time.sleep(1.0)


def run(inp: dict) -> ToolResult:
    if not isinstance(inp, dict):
        return ToolResult("Error: the arguments must be an object", ok=False, kind="error")
    inp = normalize(inp)
    action = str(inp.get("action") or "").strip().lower()
    if action not in ACTIONS:
        return ToolResult(f"Error: action must be one of {', '.join(ACTIONS)}", ok=False, kind="error")
    if action == "open" and not str(inp.get("url") or "").strip() and inp.get("tab") in (None, ""):
        return ToolResult("Error: url is required for action=open", ok=False, kind="error")
    has_xy = inp.get("x") is not None and inp.get("y") is not None
    if action == "click" and not (inp.get("text") or inp.get("selector") or inp.get("ref") is not None or has_xy):
        return ToolResult("Error: give ref (the number from read or look), x and y, text (the visible label) or selector "
                          "for action=click", ok=False, kind="error")
    if action == "type" and inp.get("text") is None:
        return ToolResult("Error: text is required for action=type", ok=False, kind="error")
    if action == "key" and not str(inp.get("key") or "").strip():
        return ToolResult("Error: key is required for action=key (for example Enter or Escape)", ok=False, kind="error")
    if action == "find" and not str(inp.get("text") or "").strip():
        return ToolResult("Error: text is required for action=find (the words to look for)", ok=False, kind="error")
    if str(inp.get("browser") or "").lower() == "chrome" or action == "tabs":
        return browsercdp.run_chrome(inp)          # ふだんの Chrome（拡張機能の線・2026-11-18）
    if browsercdp.usable():
        return browsercdp.run(inp)
    if action == "eval" and not str(inp.get("expression") or "").strip():
        return ToolResult("Error: expression is required for action=eval (JavaScript to run in the page, e.g. document.title)", ok=False, kind="error")
    if action in ("hover", "drag", "fill", "upload", "dialog", "console", "steps", "eval"):
        return ToolResult(f"Error: action={action} needs a newer L.A.P.S app. Update the app.", ok=False, kind="error")
    if action == "wait":
        return _wait(inp)
    if action == "find":
        r = _call({"action": "read", "max_chars": 60000})
        return r if isinstance(r, ToolResult) else ToolResult(_find(r, str(inp["text"])))
    args = {k: inp[k] for k in ("url", "text", "selector", "submit", "max_chars", "ref", "x", "y", "direction", "key")
            if k in inp}
    args["action"] = action
    r = _call(args)
    if isinstance(r, ToolResult):
        return r
    if action == "read":
        return ToolResult(_page(r))
    if action == "look":
        return _look(r)
    head = {"open": "Opened", "click": f"Clicked «{r.get('clicked') or inp.get('text') or inp.get('selector') or inp.get('ref')}»",
            "scroll": f"Scrolled {inp.get('direction') or 'down'}", "key": f"Pressed {inp.get('key')}",
            "type": "Typed" + (" and submitted" if inp.get("submit") else ""), "back": "Went back",
            "forward": "Went forward", "reload": "Reloaded"}[action]
    then = inp.get("then")
    if then in ("read", "look"):
        # ★操作と確かめを 1 手に。小さいモデルは「押す→読む」の 2 手目で前置きを読み直すのが高い（2026-10-19）。
        r2 = _call({"action": then})
        if isinstance(r2, ToolResult):
            return ToolResult(f"{head}, but the page could not be {then}: {r2}", ok=False, kind="error")
        if then == "look":
            res = _look(r2)
            return ToolResult(f"{head}.\n" + str(res), data=res.data)
        return ToolResult(f"{head}.\n" + _page(r2))
    return ToolResult(f"{head}. Now at: {r.get('title') or '(no title)'}\nURL: {r.get('url') or ''}\n"
                      "Add then=\"read\" (or look) to get the page in the same call next time.")
