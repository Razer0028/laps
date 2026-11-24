"""Browser の中身: DevTools の命令でページを読み、押す（2026-11-03 オーナー「できる改善をやってみてください」）。

なぜ作り直したか（実測）:
  - 走行記録: click の失敗 21/76 のうち 17 は要素を指せない（古い番号 9・文字で見つからず 8）。type は 4/4 失敗。
  - 試験ページ 13 場面で、ページへ流し込む JS（browser.rs の CLICK_JS など）は 2 場面しか成功しなかった。
    iframe と Shadow DOM を見ない・el.click() は mousedown を出さず isTrusted も偽＝★「成功」と返して何も起きない・
    alert で固まる・contenteditable に打てない・ファイル選択が無い・番号が look ごとに振り直される。
形: ページの読み書きはここ 1 か所。線（DevTools の命令を運ぶ物）だけ 2 通り。
  - AppLine … アプリの WebView2（CallDevToolsProtocolMethod・★待ち受けの口は作らない）。Rust は中継するだけ。
  - PipeLine … アプリの外（端末・Linux）。headless Chrome を --remote-debugging-pipe で起こす（これも口を作らない）。
    ★同じコードを Linux の Chrome で試験できるのが大事（WebView2 は Windows の実機でしか動かせない）。
番号: 同じ要素は同じ番号のまま（番号を使い回さない＝古い番号で別の物を押さない）。要素が描き直されて消えたら、
  役割と名前で 1 つに決まるときだけ付け替える（agent-browser と同じ）。決まらなければ押さずに今の一覧を返す。
読み方は browser-use に倣った: アクセシビリティの木（全フレーム・Shadow DOM も入る）＋ listener と cursor:pointer で役割の無い部品を補う。
"""
from __future__ import annotations

import collections
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass

from ..tools import ToolResult, appcall


class CdpError(RuntimeError):
    pass


class CdpTimeout(CdpError):
    pass


# 線で受け取る事象（これ以外は捨てる。Rust の EVENTS と揃える）
EVENTS = ("Page.javascriptDialogOpening", "Page.javascriptDialogClosed", "Page.fileChooserOpened",
          "Runtime.exceptionThrown", "Runtime.consoleAPICalled", "Runtime.executionContextCreated", "Runtime.executionContextDestroyed",
          "Runtime.executionContextsCleared", "Log.entryAdded", "Target.attachedToTarget", "Target.detachedFromTarget")


def _proto_error(r) -> str:
    """WebView2 は命令の失敗を {"code":…,"message":…} の本文で返すことがある。"""
    if isinstance(r, dict) and isinstance(r.get("code"), int) and "message" in r and r["code"] < 0:
        return str(r["message"])
    return ""


class AppLine:
    """アプリの WebView2 へ。頼みは serve の app_call → sidecar.rs → browser.rs の cdp。"""
    kind = "app"

    def __init__(self) -> None:
        self.view = ""

    def call(self, method: str, params: dict | None = None, session: str = "", timeout: float = 10.0) -> dict:
        r = appcall.call("browser", {"action": "cdp", "method": method, "params": params or {}, "session": session,
                                     "timeout": timeout}, timeout=timeout + 3.0)
        if not r.get("ok"):
            msg = str(r.get("error") or "the browser did not answer")
            if "timed out" in msg or "did not answer" in msg:
                raise CdpTimeout(msg)
            raise CdpError(_proto_error(_loads(msg)) or msg)
        self.view = str(r.get("view") or self.view)
        res = r.get("result")
        err = _proto_error(res)
        if err:
            raise CdpError(err)
        return res if isinstance(res, dict) else {}

    def drain(self) -> list[dict]:
        try:
            r = appcall.call("browser", {"action": "cdp_events"}, timeout=5.0)
        except Exception:
            return []
        return [e for e in (r.get("events") or []) if isinstance(e, dict)]

    def show(self) -> None:
        """右の欄の Browser を開かせる（利用者に見えるように）。"""
        try:
            appcall.call("browser", {"action": "show"}, timeout=5.0)
        except Exception:
            pass

    def navigate(self, url: str) -> str:
        r = appcall.call("browser", {"action": "go", "url": url}, timeout=20.0)
        if not r.get("ok"):
            raise CdpError(str(r.get("error") or "the page could not be opened"))
        return ""

    def close(self) -> None:
        pass


def _loads(s: str):
    try:
        return json.loads(s)
    except ValueError:
        return None


def find_chrome() -> str | None:
    from .servetool import _browser
    return _browser()


def pipe_ok() -> bool:
    """アプリの外でも動かせるか（Chrome があり、fd を渡せる OS）。"""
    return os.name == "posix" and bool(find_chrome())


class PipeLine:
    """アプリの外: headless Chrome を --remote-debugging-pipe で動かす（fd 3 を読み fd 4 へ書く・本文は NUL 区切り）。"""
    kind = "pipe"
    view = "pipe"
    # ★開発機 の Chrome は既定のフラグだと 127.0.0.1 の最初の読み込みが 25 秒止まった（2026-11-03 実測・この並びで 0.02 秒）。
    FLAGS = ("--headless=new", "--remote-debugging-pipe", "--no-first-run", "--disable-gpu", "--disable-sync",
             "--disable-background-networking", "--disable-component-update", "--disable-default-apps", "--mute-audio",
             "--window-size=1280,900", "--disable-extensions", "--disable-client-side-phishing-detection",
             "--safebrowsing-disable-auto-update", "--disable-hang-monitor", "--password-store=basic", "--use-mock-keychain",
             "--disable-features=LocalNetworkAccessChecks,BlockInsecurePrivateNetworkRequests,"
             "PrivateNetworkAccessSendPreflights,Translate,MediaRouter")

    def __init__(self) -> None:
        import fcntl
        exe = find_chrome()
        if not exe:
            raise CdpError("no Chrome or Edge on this machine (set LAPS_BROWSER to its path)")
        self.dir = tempfile.mkdtemp(prefix="bw-browser-")
        r_in, w_in = os.pipe()
        r_out, w_out = os.pipe()
        # 子の 3 と 4 に付け替える。★番号が 3・4 と重なると壊れるので離れた番号へ移してから（sh の 3<&N は dash だと 1 桁しか受けない）
        r_high = fcntl.fcntl(r_in, fcntl.F_DUPFD_CLOEXEC, 100)
        w_high = fcntl.fcntl(w_out, fcntl.F_DUPFD_CLOEXEC, 100)
        os.close(r_in)
        os.close(w_out)
        r_in, w_out = r_high, w_high
        flags = list(self.FLAGS) + [f"--user-data-dir={self.dir}", "about:blank"]
        if os.geteuid() == 0:
            flags.append("--no-sandbox")

        def child():
            os.dup2(r_in, 3, inheritable=True)
            os.dup2(w_out, 4, inheritable=True)
        try:
            self.proc = subprocess.Popen([exe, *flags], preexec_fn=child, close_fds=False, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:               # 起こせなかった: 管と作業場所を残さない（2026-11-16 全体のレビュー）
            for fd in (r_in, w_in, r_out, w_out):
                try:
                    os.close(fd)
                except OSError:
                    pass
            shutil.rmtree(self.dir, ignore_errors=True)
            raise CdpError(f"could not start the browser: {e}") from e
        os.close(r_in)
        os.close(w_out)
        self._w = os.fdopen(w_in, "wb", buffering=0)
        self._r = os.fdopen(r_out, "rb", buffering=0)
        self._lock = threading.Lock()
        self._seq = 0
        self._wait: dict[int, dict] = {}
        self._events: collections.deque = collections.deque(maxlen=500)
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            t = self._raw("Target.createTarget", {"url": "about:blank"})
            self.root = self._raw("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True})["sessionId"]
        except Exception:                  # 始めの命令で落ちた: Chrome を残さない（2026-11-16 全体のレビュー: 裏に Chrome が残り続けた）
            self.close()
            raise

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _reader(self) -> None:
        buf = b""
        while True:
            try:
                chunk = self._r.read(65536)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            buf += chunk
            while b"\0" in buf:
                one, buf = buf.split(b"\0", 1)
                d = _loads(one.decode("utf-8", "replace"))
                if not isinstance(d, dict):
                    continue
                if "id" in d:
                    slot = self._wait.get(d["id"])
                    if slot:
                        slot["r"] = d
                        slot["ev"].set()
                elif d.get("method") in EVENTS:
                    sid = d.get("sessionId") or ""
                    self._events.append({"method": d["method"], "params": d.get("params") or {},
                                         "session": "" if sid == getattr(self, "root", None) else sid})
        for slot in list(self._wait.values()):
            slot["ev"].set()

    def _raw(self, method: str, params: dict | None = None, session: str = "", timeout: float = 10.0) -> dict:
        with self._lock:
            self._seq += 1
            i = self._seq
            slot = {"ev": threading.Event(), "r": None}
            self._wait[i] = slot
            msg = {"id": i, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            try:
                self._w.write(json.dumps(msg).encode("utf-8") + b"\0")
            except OSError as e:
                self._wait.pop(i, None)
                raise CdpError(f"the browser is gone: {e}") from e
        try:
            if not slot["ev"].wait(timeout):
                raise CdpTimeout(f"{method} timed out after {timeout:g} s")
        finally:
            self._wait.pop(i, None)
        d = slot["r"]
        if d is None:
            raise CdpError("the browser is gone")
        if "error" in d:
            raise CdpError(str(d["error"].get("message") or d["error"]))
        return d.get("result") or {}

    def call(self, method: str, params: dict | None = None, session: str = "", timeout: float = 10.0) -> dict:
        return self._raw(method, params, session or self.root, timeout)

    def drain(self) -> list[dict]:
        out = []
        while self._events:
            try:
                out.append(self._events.popleft())
            except IndexError:
                break
        return out

    def show(self) -> None:
        pass

    def navigate(self, url: str) -> str:
        r = self.call("Page.navigate", {"url": to_url(url)}, timeout=30.0)
        return str(r.get("errorText") or "")

    def close(self) -> None:
        for step in (lambda: self._w.close(), lambda: self.proc.terminate(), lambda: self.proc.wait(timeout=5),
                     lambda: self._r.close()):
            try:                           # 1 つ失敗しても残りを行う（前は最初の失敗で Chrome を止めずに抜けた）
                step()
            except Exception:  # noqa: BLE001
                pass
        if self.alive():
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(self.dir, ignore_errors=True)


def to_url(text: str) -> str:
    """browser.rs の to_url と同じ規則（開けるのは http と https だけ）。"""
    t = text.strip()
    if not t:
        raise CdpError("url is empty")
    if "://" in t:
        if not t.lower().startswith(("http://", "https://")):
            raise CdpError(f"Only http and https pages can be opened (not {t.split(':')[0]}:). For a local .html file, serve its "
                           "folder with Tools(name=\"Serve\", input={\"directory\": \"<folder>\"}) and open the http://localhost URL it gives.")
        return t
    if t.startswith(("localhost", "127.", "[::1]")):
        return "http://" + t
    head = t.split("/")[0]
    if " " not in t and ("." in head or ":" in head):
        return "https://" + t
    from urllib.parse import quote_plus
    return "https://duckduckgo.com/?q=" + quote_plus(t)


# --------------------------------------------------------------------------------------------------------------------
# ページの読み方
# --------------------------------------------------------------------------------------------------------------------

# 押せる・打てる役割（アクセシビリティの木の role を小文字にした物）
_ACT = {"button", "link", "textbox", "searchbox", "checkbox", "radio", "switch", "combobox", "comboboxselect", "listbox",
        "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "slider", "spinbutton", "treeitem",
        "disclosuretriangle", "colorwell", "date", "datetime", "inputtime", "popupbutton", "togglebutton", "menulistoption"}
# 打つ先として探す役割
_FIELDS = {"textbox", "searchbox", "combobox", "comboboxselect", "listbox", "spinbutton", "slider", "date", "datetime",
           "inputtime", "colorwell"}
# ここで行を区切る（span はふつう ignored で木に出ないので generic は div とみてよい）
_BLOCKS = {"paragraph", "heading", "listitem", "row", "cell", "gridcell", "columnheader", "rowheader", "article", "main",
           "navigation", "banner", "contentinfo", "complementary", "region", "form", "list", "table", "blockquote",
           "figure", "section", "dialog", "alertdialog", "alert", "status", "pre", "code", "term", "definition",
           "linebreak", "separator", "group", "toolbar", "menu", "menubar", "tablist", "tabpanel", "details", "generic"}
_STATES = ("checked", "selected", "expanded", "pressed", "disabled", "readonly", "required", "invalid")
MAX_DEPTH = 300

# 役割の無い押せる部品を探す（ページの世界で動かす＝別世界からだと listener が見えない・getEventListeners は includeCommandLineAPI のときだけ在る）
_CLICKABLE_JS = r"""(() => {
  const inter = 'a[href],button,input,textarea,select,summary,[role=button],[role=link],[role=tab],[role=checkbox],[role=radio],[role=menuitem],[role=switch],[role=option],[contenteditable=""],[contenteditable=true]';
  const out = []; let n = 0;
  const total = document.getElementsByTagName('*').length;
  const listeners = typeof getEventListeners === 'function' && total < 6000;
  const walk = (root) => {
    for (const e of root.querySelectorAll('*')) {
      if (++n > 20000) return;
      if (e.shadowRoot) walk(e.shadowRoot);
      if (out.length >= 80 || e.matches(inter) || !e.getClientRects().length) continue;
      let hit = e.hasAttribute('onclick') || e.hasAttribute('tabindex') && e.tabIndex >= 0;
      if (!hit && listeners) { const l = getEventListeners(e); hit = !!(l.click || l.mousedown || l.pointerdown); }
      if (!hit && getComputedStyle(e).cursor === 'pointer') {
        const p = e.parentElement; hit = !(p && getComputedStyle(p).cursor === 'pointer');
      }
      if (hit && !e.querySelector(inter)) out.push(e);
    }
  };
  walk(document);
  return out;
})()"""
_LABELS_JS = """function() { return this.map(e => ((e.getAttribute('aria-label') || e.title || e.innerText || ('<' + e.tagName.toLowerCase() + '>')) + '').trim().replace(/\\s+/g, ' ').slice(0, 60)); }"""

# 押す前の確かめ: 中心に別の物が被っていないか（Shadow DOM は中まで辿る）
_HIT_JS = r"""function() {
  const e = this.nodeType === 3 ? this.parentElement : this;
  if (!e) return { ok: true };
  const rs = e.getClientRects();
  if (!rs.length) return { ok: false, hidden: true };
  const r = rs[0], x = r.left + r.width / 2, y = r.top + r.height / 2;
  let t = document.elementFromPoint(x, y);
  while (t && t.shadowRoot) { const i = t.shadowRoot.elementFromPoint(x, y); if (!i || i === t) break; t = i; }
  if (!t) return { ok: true };
  const inside = (a, b) => { for (let n = a; n; n = n.parentNode || n.host) if (n === b) return true; return false; };
  if (inside(t, e) || inside(e, t)) return { ok: true };
  const lab = t.closest && t.closest('label');
  if (lab && (lab.control === e || lab.contains(e))) return { ok: true };
  return { ok: false, by: ((t.getAttribute('aria-label') || t.innerText || t.tagName) + '').trim().replace(/\s+/g, ' ').slice(0, 60), tag: t.tagName.toLowerCase() };
}"""
_KIND_JS = """function() { const c = this.isContentEditable; return { tag: this.tagName ? this.tagName.toLowerCase() : '', type: this.type || '', edit: !!c, has_value: 'value' in this }; }"""
_FIND_FIELD_JS = """function() { if ('value' in this || this.isContentEditable) return this; return this.querySelector('input,textarea,select,[contenteditable=""],[contenteditable=true]') || this; }"""
_SELECT_ALL_JS = """function() { if (this.select) { try { this.select(); return true; } catch (e) {} } const s = getSelection(); const r = document.createRange(); r.selectNodeContents(this); s.removeAllRanges(); s.addRange(r); return true; }"""
_VALUE_JS = """function() { return 'value' in this ? String(this.value) : String(this.innerText || ''); }"""
# insertText が効かない入力（date・color・range など）は値を直接入れて知らせる（React も拾う setter を使う）
_SET_VALUE_JS = """function(v) { const p = this.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; const s = Object.getOwnPropertyDescriptor(p, 'value'); if (s && s.set) s.set.call(this, v); else this.value = v; this.dispatchEvent(new Event('input', { bubbles: true })); this.dispatchEvent(new Event('change', { bubbles: true })); return String(this.value); }"""
_SELECT_JS = r"""function(val) {
  const n = (s) => String(s).trim().toLowerCase();
  const os = [...this.options], w = n(val);
  const o = os.find(o => n(o.label) === w || n(o.value) === w) || os.find(o => n(o.label).includes(w));
  if (!o) return { ok: false, options: os.slice(0, 30).map(o => o.label) };
  this.value = o.value;
  this.dispatchEvent(new Event('input', { bubbles: true })); this.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true, label: o.label };
}"""
_STATE_JS = """(() => ({ url: location.href, title: document.title, ready: document.readyState, count: document.getElementsByTagName('*').length, chars: document.body ? document.body.innerText.length : 0, y: Math.round(scrollY), x: Math.round(scrollX), h: innerHeight, w: innerWidth, total: document.documentElement ? document.documentElement.scrollHeight : 0 }))()"""
_MARK_JS = r"""((boxes) => {
  const old = document.getElementById('__bw_marks'); if (old) old.remove();
  const box = document.createElement('div'); box.id = '__bw_marks';
  box.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none';
  for (const b of boxes) {
    const o = document.createElement('div');
    o.style.cssText = `position:fixed;left:${b.x}px;top:${b.y}px;width:${b.w}px;height:${b.h}px;outline:2px solid #e11d48`;
    const t = document.createElement('div'); t.textContent = String(b.n);
    t.style.cssText = `position:fixed;left:${Math.max(b.x - 2, 0)}px;top:${Math.max(b.y - 14, 0)}px;background:#e11d48;color:#fff;font:bold 12px/14px Arial,sans-serif;padding:0 3px;border-radius:3px`;
    box.appendChild(o); box.appendChild(t);
  }
  document.documentElement.appendChild(box);
  return boxes.length;
})(__BOXES__)"""
_UNMARK_JS = """(() => { const b = document.getElementById('__bw_marks'); if (b) b.remove(); return true; })()"""
# ★操作中の表示（2026-11-18 オーナー「L.A.P.S のブラウザ操作は、操作されているのか・どこを操作しているのかが分かりづらい。
#   Claude は画面の端が光ってカーソルも変わるので分かりやすい」）。ページの最前面に、クリックを通す層を重ねる:
#   ・縁の光（L.A.P.S のスペクトラムの赤→青）。★透明度だけで脈打たせる（Claude in Chrome は box-shadow のアニメで GPU・CPU を
#     食ったという報告がある＝影は止めたまま、層の opacity だけ変える）。
#   ・エージェントのカーソル（押す場所へ動き、押すときに波紋）・右下の札（何をしているか・「止める」）。
#   ・閉じた Shadow DOM の中に置く（ページの CSS と混ざらない）。見た目は CSSOM で組む（style 属性を止める CSP でも出る）。
#   ・8 秒動きが無ければ消える（走行の終わりを知らなくても残らない）。写真（look）を撮る間は隠す（モデルが光をページの一部と読み違えた報告がある）。
_AGENT_JS = r"""((op, a) => {
  const W = window;
  if (!W.__lapsAgent) {
    const host = document.createElement('div');
    host.setAttribute('data-laps-agent', '');
    host.setAttribute('aria-hidden', 'true');   // ページの部品の一覧（アクセシビリティの木）に入れない＝モデルが自分の「止める」を押さない
    host.style.cssText = 'position:fixed;inset:0;z-index:2147483647;pointer-events:none;contain:strict';
    const root = host.attachShadow({ mode: 'closed' });
    const css = `
      :host{all:initial}
      .rim{position:fixed;inset:0;border:3px solid transparent;border-image:linear-gradient(135deg,#d0342c,#7a3fd0 50%,#2f6fe0) 1;
           box-shadow:inset 0 0 22px rgba(208,52,44,.35),inset 0 0 22px rgba(47,111,224,.25);opacity:.9;animation:p 2s ease-in-out infinite;transition:opacity .4s}
      @keyframes p{0%,100%{opacity:.55}50%{opacity:1}}
      .cur{position:fixed;left:0;top:0;width:22px;height:22px;transform:translate(-100px,-100px);transition:transform .18s ease-out;filter:drop-shadow(0 1px 2px rgba(0,0,0,.45))}
      .tap{position:fixed;left:0;top:0;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;border:2px solid #d0342c;opacity:0;pointer-events:none}
      .tap.on{animation:t .45s ease-out}
      @keyframes t{0%{transform:scale(.3);opacity:.9}100%{transform:scale(1.6);opacity:0}}
      .bar{position:fixed;right:14px;bottom:14px;display:flex;align-items:center;gap:10px;max-width:min(560px,70vw);padding:7px 8px 7px 12px;
           border-radius:12px;background:rgba(24,24,27,.86);color:#f4f4f5;font:500 12.5px/1.35 system-ui,-apple-system,"Segoe UI","Hiragino Sans","Yu Gothic UI",sans-serif;
           box-shadow:0 6px 24px rgba(0,0,0,.35);pointer-events:auto;backdrop-filter:blur(8px)}
      .dot{width:8px;height:8px;border-radius:50%;background:linear-gradient(135deg,#d0342c,#2f6fe0);flex:none;animation:p 2s ease-in-out infinite}
      .txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .who{font-weight:700;margin-right:6px}
      button{all:unset;cursor:pointer;flex:none;padding:4px 10px;border-radius:8px;background:#d0342c;color:#fff;font:600 12px/1.2 inherit}
      button:hover{background:#b02a23}
      .off{opacity:0;transition:opacity .5s}
      @media (prefers-reduced-motion: reduce){.rim,.dot{animation:none}.cur{transition:none}}`;
    try { const sh = new CSSStyleSheet(); sh.replaceSync(css); root.adoptedStyleSheets = [sh]; }
    catch (e) { const st = document.createElement('style'); st.textContent = css; root.appendChild(st); }
    const wrap = document.createElement('div');
    wrap.innerHTML = '<div class="rim"></div><div class="tap"></div>'
      + '<svg class="cur" viewBox="0 0 24 24"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#d0342c"/><stop offset="1" stop-color="#2f6fe0"/></linearGradient></defs>'
      + '<path d="M3 2l7.5 19 2.6-8.1L21 10.4z" fill="url(#g)" stroke="#fff" stroke-width="1.4" stroke-linejoin="round"/></svg>'
      + '<div class="bar" role="status" aria-live="polite"><span class="dot"></span><span class="txt"><span class="who">L.A.P.S が操作中</span><span class="what"></span></span><button type="button">止める</button></div>';
    root.appendChild(wrap);
    const q = (s) => wrap.querySelector(s);
    const st = { host, wrap, stop: 0, timer: 0, hidden: false };
    const bump = () => {
      wrap.classList.remove('off'); clearTimeout(st.timer);
      st.timer = setTimeout(() => { wrap.classList.add('off'); setTimeout(() => { if (wrap.classList.contains('off')) host.remove(); W.__lapsAgent = null; }, 600); }, 8000);
      if (!host.isConnected) document.documentElement.appendChild(host);
    };
    q('button').addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); st.stop = Date.now(); q('.what').textContent = '止めています…'; });
    W.__lapsAgent = {
      say(t) { bump(); q('.what').textContent = t || ''; },
      move(x, y) { bump(); q('.cur').style.transform = `translate(${x - 3}px,${y - 2}px)`; },
      tap(x, y) { const t = q('.tap'); t.style.left = x + 'px'; t.style.top = y + 'px'; t.classList.remove('on'); void t.offsetWidth; t.classList.add('on'); },
      hide(on) { host.style.visibility = on ? 'hidden' : ''; },
      busy(on) { q('.bar').style.pointerEvents = on ? 'none' : 'auto'; },   // 操作の間は札もクリックを通す（下の部品を押せるように）
      takeStop() { const v = st.stop; st.stop = 0; return v; },
    };
    document.documentElement.appendChild(host);
    bump();
  }
  const A = W.__lapsAgent;
  if (op === 'say') A.say(a.text);
  else if (op === 'move') { A.say(a.text); A.move(a.x, a.y); if (a.tap) setTimeout(() => A.tap(a.x, a.y), 170); }
  else if (op === 'hide') A.hide(!!a.on);
  else if (op === 'busy') A.busy(!!a.on);
  else if (op === 'stop') return A.takeStop();
  return true;
})(__OP__, __ARG__)"""
_AGENT_VERBS = {"open": "開く", "click": "クリック", "type": "入力", "fill": "入力", "hover": "指す", "drag": "ドラッグ",
                "scroll": "スクロール", "key": "キー", "upload": "ファイルを添付", "look": "画面を見る", "read": "読む",
                "find": "探す", "wait": "待つ", "back": "戻る", "forward": "進む", "reload": "読み直す", "dialog": "ダイアログに答える"}


# 押すキーの表（Input.dispatchKeyEvent の形）
_KEYS = {
    "enter": ("Enter", "Enter", 13, "\r"), "return": ("Enter", "Enter", 13, "\r"), "tab": ("Tab", "Tab", 9, ""),
    "escape": ("Escape", "Escape", 27, ""), "esc": ("Escape", "Escape", 27, ""), "backspace": ("Backspace", "Backspace", 8, ""),
    "delete": ("Delete", "Delete", 46, ""), "space": (" ", "Space", 32, " "), "arrowup": ("ArrowUp", "ArrowUp", 38, ""),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, ""), "arrowleft": ("ArrowLeft", "ArrowLeft", 37, ""),
    "arrowright": ("ArrowRight", "ArrowRight", 39, ""), "up": ("ArrowUp", "ArrowUp", 38, ""), "down": ("ArrowDown", "ArrowDown", 40, ""),
    "left": ("ArrowLeft", "ArrowLeft", 37, ""), "right": ("ArrowRight", "ArrowRight", 39, ""), "home": ("Home", "Home", 36, ""),
    "end": ("End", "End", 35, ""), "pageup": ("PageUp", "PageUp", 33, ""), "pagedown": ("PageDown", "PageDown", 34, ""),
}
_MODS = {"alt": 1, "control": 2, "ctrl": 2, "meta": 4, "cmd": 4, "shift": 8}


def _key_params(spec: str) -> tuple[dict, str]:
    """"Control+A" や "Enter" を dispatchKeyEvent の引数と打つ文字に。"""
    parts = [p for p in re.split(r"[+\s]+", spec.strip()) if p]
    mods = 0
    while len(parts) > 1 and parts[0].lower() in _MODS:
        mods |= _MODS[parts.pop(0).lower()]
    name = parts[0] if parts else spec
    low = name.lower()
    if low in _KEYS:
        key, code, vk, text = _KEYS[low]
    elif re.fullmatch(r"f([1-9]|1[0-2])", low):
        key, code, vk, text = name.upper(), name.upper(), 111 + int(low[1:]), ""
    elif len(name) == 1:
        up = name.upper()
        key = name
        code = f"Key{up}" if up.isalpha() else (f"Digit{name}" if name.isdigit() else "")
        vk = ord(up) if up.isalnum() else 0
        text = name
    else:
        raise CdpError(f"unknown key {spec!r}: use names like Enter, Tab, Escape, ArrowDown, or Control+A")
    if mods & ~8:                 # Ctrl や Alt を押しながらは文字を打たない
        text = ""
    return {"key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods}, text


def _norm(s) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(s or "")).casefold().split())


@dataclass
class Item:
    ref: int
    session: str
    frame: str
    backend: int
    role: str
    name: str
    url: str = ""


@dataclass
class Snap:
    lines: list[str]            # 読む順の 1 行ずつ（番号つき）
    items: list[Item]           # 見えている押せる物（読む順）
    texts: list[tuple]          # (session, backend, 文字)＝文字で押すときの最後の手
    state: dict
    frames: int = 1


class Page:
    """1 つのページ（タブ）。番号の帳簿とダイアログの状態を持つ。"""

    def __init__(self, line) -> None:
        self.line = line
        self.ready_view: str | None = None
        self.items: dict[int, Item] = {}
        self.by_key: dict[tuple, int] = {}
        self.next_ref = 1
        self.children: dict[str, dict] = {}        # 別プロセスの iframe（session → 親と frame）
        self.dialog: dict | None = None
        self.alerts: list[str] = []
        self.errors: list[str] = []
        self.chooser: dict | None = None
        self.worlds: dict[tuple, int] = {}
        self.region: tuple | None = None           # 直前の look の範囲（x・y はその写真の上の位置）
        self._chooser_left = False
        self.snap: Snap | None = None
        self._agent_text = ""                      # 操作中の表示の札の文（act が決める）

    # -- 線 -----------------------------------------------------------------------------------------------------------
    def c(self, method: str, params: dict | None = None, session: str = "", timeout: float = 10.0) -> dict:
        return self.line.call(method, params, session, timeout)

    def ev(self, expr: str, session: str = "", timeout: float = 5.0, command_line: bool = False):
        r = self.c("Runtime.evaluate", {"expression": expr, "returnByValue": True, "includeCommandLineAPI": command_line},
                   session, timeout)
        if r.get("exceptionDetails"):
            raise CdpError(str(r["exceptionDetails"].get("text") or "the page script failed"))
        return (r.get("result") or {}).get("value")

    def on(self, it: Item | tuple, fn: str, *args, timeout: float = 5.0, by_value: bool = True):
        """要素の上で関数を動かす（it は Item か (session, backend)）。"""
        sess, backend = (it.session, it.backend) if isinstance(it, Item) else it
        oid = self.c("DOM.resolveNode", {"backendNodeId": backend, "objectGroup": "bw"}, sess)["object"]["objectId"]
        r = self.c("Runtime.callFunctionOn", {"objectId": oid, "functionDeclaration": fn, "returnByValue": by_value,
                                              "arguments": [{"value": a} for a in args]}, sess, timeout)
        if r.get("exceptionDetails"):
            raise CdpError(str(r["exceptionDetails"].get("text") or "the page script failed"))
        return (r.get("result") or {}).get("value") if by_value else r.get("result")

    def ensure(self) -> None:
        """線の向こうの準備（enable は新しいページの部品ごとに 1 回）。No page なら CdpError。"""
        # ★ページの中で動く命令はダイアログの間は返らない。ブラウザ側で答える命令で確かめる
        try:
            self.c("Target.getTargetInfo", timeout=8.0)
        except CdpTimeout:
            raise
        except CdpError as e:
            if "No page is open" in str(e):
                raise
            self.ev("1", timeout=8.0)
        if self.ready_view == self.line.view:
            return
        self.ready_view = self.line.view
        self.items.clear()
        self.by_key.clear()
        self.children.clear()
        self.worlds.clear()
        self.dialog = None
        self._enable("")

    def _enable(self, session: str) -> None:
        for m in ("Page.enable", "DOM.enable", "Runtime.enable", "Log.enable", "Accessibility.enable"):
            try:
                self.c(m, session=session)
            except CdpError:
                pass
        try:
            self.c("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}, session)
        except CdpError:
            pass

    # -- 事象 -----------------------------------------------------------------------------------------------------------
    def pump(self) -> None:
        for e in self.line.drain():
            m, p, s = e.get("method"), e.get("params") or {}, str(e.get("session") or "")
            if m == "Page.javascriptDialogOpening":
                if p.get("type") == "alert":
                    # alert は知らせるだけの物＝閉じて文を伝える（閉じないとページが止まったまま）
                    self.alerts.append(str(p.get("message") or "")[:300])
                    try:
                        self.c("Page.handleJavaScriptDialog", {"accept": True}, s, 3.0)
                    except CdpError:
                        pass
                else:
                    self.dialog = {"type": p.get("type"), "message": str(p.get("message") or "")[:300],
                                   "default": p.get("defaultPrompt") or "", "session": s}
            elif m == "Page.javascriptDialogClosed":
                self.dialog = None
            elif m == "Page.fileChooserOpened":
                self.chooser = {"backend": p.get("backendNodeId"), "session": s, "multiple": p.get("mode") == "selectMultiple"}
            elif m == "Runtime.exceptionThrown":
                d = p.get("exceptionDetails") or {}
                ex = d.get("exception") or {}
                self._err(ex.get("description") or d.get("text"))
            elif m == "Runtime.consoleAPICalled" and p.get("type") in ("error", "assert"):
                self._err(" ".join(str(a.get("value") or a.get("description") or "") for a in (p.get("args") or [])))
            elif m == "Log.entryAdded" and (p.get("entry") or {}).get("level") == "error":
                en = p["entry"]
                # 広告や計測の読み込み失敗（よそのサイト）は作業に関係しない（実サイトで毎回 300 字の雑音になった）
                if not _foreign(en.get("url"), (self.snap.state if self.snap else {}).get("url")):
                    self._err(f"{en.get('text')} {en.get('url') or ''}".strip())
            elif m == "Target.attachedToTarget":
                info = p.get("targetInfo") or {}
                if info.get("type") == "iframe":
                    sid = str(p.get("sessionId"))
                    self.children[sid] = {"parent": s, "frame": info.get("targetId"), "url": info.get("url")}
                    self._enable(sid)
            elif m == "Target.detachedFromTarget":
                self.children.pop(str(p.get("sessionId")), None)
            elif m == "Runtime.executionContextCreated":
                c = p.get("context") or {}
                aux = c.get("auxData") or {}
                if aux.get("isDefault") and aux.get("frameId"):
                    self.worlds[(s, aux["frameId"])] = c.get("id")
            elif m == "Runtime.executionContextDestroyed":
                cid = p.get("executionContextId")
                for k in [k for k, v in self.worlds.items() if v == cid]:
                    self.worlds.pop(k, None)
            elif m == "Runtime.executionContextsCleared":
                for k in [k for k in self.worlds if k[0] == s]:
                    self.worlds.pop(k, None)

    def _err(self, text) -> None:
        t = " ".join(str(text or "").split())[:300]
        if "favicon.ico" in t:
            return
        if t and t not in self.errors:
            self.errors.append(t)
            del self.errors[:-20]

    def _take_notes(self) -> list[str]:
        """操作の結果に添える: alert の文とページのエラー（Web の開発で確かめるのに要る）。"""
        out = [f'The page showed an alert: "{a}" (closed it)' for a in self.alerts]
        self.alerts.clear()
        if self.errors:
            out.append("Errors in the page console:")
            out += [f"- {e}" for e in self.errors[-5:]]
            self.errors.clear()
        return out

    def _dialog_text(self) -> str:
        d = self.dialog or {}
        return (f'A {d.get("type")} dialog is open: "{d.get("message")}". Nothing else works until it is answered: '
                'Browser(action="dialog", accept=true) or accept=false' + (', with text for a prompt.' if d.get("type") == "prompt" else "."))

    def guarded(self, fn, timeout: float = 8.0):
        """押した先で confirm などが出ると命令の答えが返らない（ページが止まる）。別のスレッドで呼んで事象を見張る。"""
        done: dict = {}

        def work():
            try:
                done["r"] = fn()
            except Exception as e:     # noqa: BLE001
                done["e"] = e
        t = threading.Thread(target=work, daemon=True)
        t.start()
        end = time.monotonic() + timeout
        while t.is_alive():
            t.join(0.08)
            self.pump()
            if self.dialog:
                return None
            if time.monotonic() > end:
                raise CdpTimeout("the page did not take the input in time")
        if "e" in done:
            raise done["e"]
        return done.get("r")

    # -- 読む -----------------------------------------------------------------------------------------------------------
    def state(self, session: str = "", timeout: float = 4.0) -> dict:
        v = self.ev(_STATE_JS, session, timeout=timeout)
        return v if isinstance(v, dict) else {}

    def _frames(self) -> list[tuple]:
        """(session, frameId, url)。本体の木のフレーム＋別プロセスの iframe。"""
        out = []

        def walk(t, sess):
            f = t.get("frame") or {}
            if f.get("id"):
                out.append((sess, f["id"], f.get("url") or ""))
            for k in t.get("childFrames") or []:
                walk(k, sess)
        for sess in [""] + list(self.children):
            try:
                walk(self.c("Page.getFrameTree", session=sess).get("frameTree") or {}, sess)
            except CdpError:
                continue
            if len(out) >= 30:
                break
        return out[:30]

    def _clickables(self, sess: str, frame: str) -> dict[int, str]:
        """役割の無い押せる部品（backend → 名前）。"""
        world = self.worlds.get((sess, frame))
        if not world:
            return {}
        try:
            r = self.c("Runtime.evaluate", {"expression": _CLICKABLE_JS, "contextId": world, "includeCommandLineAPI": True,
                                            "returnByValue": False, "objectGroup": "bw"}, sess, 6.0)
        except CdpError:
            self.worlds.pop((sess, frame), None)
            return {}
        oid = (r.get("result") or {}).get("objectId")
        if not oid:
            return {}
        out: dict[int, str] = {}
        try:
            lab = self.c("Runtime.callFunctionOn", {"objectId": oid, "functionDeclaration": _LABELS_JS, "returnByValue": True}, sess)
            labels = (lab.get("result") or {}).get("value") or []
            props = self.c("Runtime.getProperties", {"objectId": oid, "ownProperties": True}, sess).get("result") or []
            for p in props:
                if not str(p.get("name", "")).isdigit():
                    continue
                o = (p.get("value") or {}).get("objectId")
                if o:
                    b = self.c("DOM.describeNode", {"objectId": o}, sess)["node"].get("backendNodeId")
                    i = int(p["name"])
                    if b:
                        out[b] = labels[i] if i < len(labels) else ""
        except CdpError:
            pass
        finally:
            try:
                self.c("Runtime.releaseObjectGroup", {"objectGroup": "bw"}, sess)
            except CdpError:
                pass
        return out

    def _item(self, sess: str, frame: str, backend: int, role: str, name: str, url: str = "") -> Item:
        key = (sess, backend)
        ref = self.by_key.get(key)
        if ref is None:
            ref = self.next_ref
            self.next_ref += 1
            self.by_key[key] = ref
        it = Item(ref, sess, frame, backend, role, name, url)
        self.items[ref] = it
        return it

    def snapshot(self) -> Snap:
        """全フレームを読む順の行に。番号は同じ要素に同じ物。"""
        self.pump()
        lines: list[str] = []
        items: list[Item] = []
        texts: list[tuple] = []
        frames = self._frames()
        n_frames = 0
        for i, (sess, frame, url) in enumerate(frames):
            try:
                nodes = self.c("Accessibility.getFullAXTree", {"frameId": frame}, sess, 12.0).get("nodes") or []
            except CdpError:
                continue
            if not nodes:
                continue
            clicks = self._clickables(sess, frame)
            mine: list[str] = []
            self._outline(nodes, sess, frame, url, clicks, mine, items, texts)
            if mine:
                n_frames += 1
                if i:
                    lines.append(f"Frame: {url[:120]}")
                lines += mine
        st = {}
        try:
            st = self.state()
        except CdpError:
            pass
        self.snap = Snap(lines, items, texts, st, n_frames)
        return self.snap

    def _outline(self, nodes, sess, frame, frame_url, clicks, lines, items, texts) -> None:
        by = {n.get("nodeId"): n for n in nodes}
        roots = [n for n in nodes if not n.get("parentId")] or nodes[:1]
        buf: list[str] = []
        left = dict(clicks)

        def flush():
            if buf:
                s = " ".join(" ".join(buf).split())
                if s:
                    lines.append(s)
                buf.clear()

        origin = "/".join(frame_url.split("/")[:3]) if "://" in frame_url else ""

        def add(role, name, n, val="", extra=""):
            if buf and buf[-1] == name:      # 欄の見出し（label）を 2 回読ませない
                buf.pop()
            flush()
            if role == "clickable" and items and items[-1].name == name and items[-1].session == sess:
                return                       # 部品を包む label など（同じ名前が続くだけ）
            backend = n.get("backendDOMNodeId")
            props = {p.get("name"): (p.get("value") or {}).get("value") for p in n.get("properties") or []}
            url = str(props.get("url") or "")
            it = self._item(sess, frame, backend, role, name, url)
            items.append(it)
            st = [k for k in _STATES if props.get(k) not in (None, False, "false", "")]
            s = f'[{it.ref}] {role} "{name[:100]}"'
            if val and val != name:
                s += f' value="{val[:80]}"'
            if st:
                s += " (" + ", ".join(st) + ")"
            if url and role == "link":
                s += f" -> {_short_url(url, origin)[:100]}"
            if extra:
                s += " " + extra
            lines.append(s)

        def visit(n, depth):
            if depth > MAX_DEPTH or n is None:
                return
            role = str((n.get("role") or {}).get("value") or "").lower()
            name = " ".join(str((n.get("name") or {}).get("value") or "").split())
            val = (n.get("value") or {}).get("value")
            val = "" if val is None else " ".join(str(val).split())
            ignored = bool(n.get("ignored"))
            backend = n.get("backendDOMNodeId")
            kids = [by.get(k) for k in n.get("childIds") or []]
            if not ignored and backend:
                editable = any(p.get("name") == "editable" for p in n.get("properties") or [])
                if role in _ACT or editable:
                    extra = ""
                    if role in ("combobox", "comboboxselect", "listbox"):
                        opts = self._options(n, by)
                        if opts:
                            extra = "options: " + ", ".join(opts[:20])
                    add(role if role in _ACT else "textbox", name, n, val, extra)
                    left.pop(backend, None)
                    if role in ("listbox", "tree", "menu"):
                        for k in kids:
                            visit(k, depth + 1)
                    return
            if backend in left:
                add("clickable", name or left[backend], n, "")
                left.pop(backend, None)
                return
            if ignored:
                for k in kids:
                    visit(k, depth + 1)
                return
            if role in ("statictext", "text"):
                if name:
                    buf.append(name)
                    if backend:
                        texts.append((sess, backend, name))
                return
            if role == "inlinetextbox":
                return
            if role == "canvas":
                flush()
                lines.append("(canvas: drawn content that cannot be read as text)")
                return
            if role == "image" or role == "img":
                if name:
                    flush()
                    lines.append(f'image "{name[:100]}"')
                return
            block = role in _BLOCKS
            if block:
                flush()
            if role == "heading":
                buf.append("#")
            for k in kids:
                visit(k, depth + 1)
            if block:
                flush()

        for r in roots:
            visit(r, 0)
        flush()
        # 木に現れなかった押せる部品も番号を付ける（無いと押せない）
        for backend, label in left.items():
            it = self._item(sess, frame, backend, "clickable", label)
            items.append(it)
            lines.append(f'[{it.ref}] clickable "{label[:100]}"')

    @staticmethod
    def _options(n, by, limit: int = 30) -> list[str]:
        out: list[str] = []
        stack = list(reversed(n.get("childIds") or []))
        steps = 0
        while stack and len(out) < limit and steps < 500:
            steps += 1
            k = by.get(stack.pop())
            if not k:
                continue
            role = str((k.get("role") or {}).get("value") or "").lower()
            name = str((k.get("name") or {}).get("value") or "").strip()
            if role in ("option", "menulistoption") and name:
                out.append(name[:40])
            stack.extend(reversed(k.get("childIds") or []))
        return out

    # -- 指す -----------------------------------------------------------------------------------------------------------
    def _alive(self, it: Item) -> bool:
        """★外された要素も番号（backend）だけは生きているので、ページに繋がっているかまで見る。"""
        try:
            return bool(self.on(it, "function() { return this.isConnected; }", timeout=4.0))
        except CdpError:
            return False

    def _list_items(self, snap: Snap, limit: int = 15, want: str = "") -> str:
        items = snap.items
        if want:
            names = {it.ref: _norm(it.name) for it in items}
            close = difflib.get_close_matches(_norm(want), list(names.values()), n=6, cutoff=0.35)
            near = [it for it in items if names[it.ref] in close]
            if near:
                items = near
        rows = [f'[{it.ref}] {it.role} "{it.name[:60]}"' for it in items[:limit]]
        more = f"\n... {len(snap.items) - len(rows)} more (read shows them all)" if len(snap.items) > len(rows) else ""
        return "\n".join(rows) + more if rows else "(nothing on this page can be clicked or typed into)"

    def by_ref(self, ref: int, snap: Snap | None = None) -> Item:
        it = self.items.get(ref)
        if it and self._alive(it):
            return it
        snap = snap or self.snapshot()
        it2 = self.items.get(ref)
        if it2 and self._alive(it2) and it2 in snap.items:
            return it2
        if it:      # 描き直されて消えた: 役割と名前で 1 つに決まるときだけ付け替える
            same = [x for x in snap.items if x.role == it.role and _norm(x.name) == _norm(it.name)]
            if len(same) == 1:
                return same[0]
        raise CdpError(f"no element has number {ref} (the page changed). What is there now:\n" + self._list_items(snap))

    def by_text(self, text: str, snap: Snap, roles: set | None = None) -> Item | None:
        w = _norm(text)
        if not w:
            return None
        pool = [it for it in snap.items if not roles or it.role in roles]
        for test in (lambda n: n == w, lambda n: w in n, lambda n: len(n) >= 2 and n in w):
            hit = [it for it in pool if it.name and test(_norm(it.name))]
            if hit:
                return hit[0]
        return None

    def by_selector(self, sel: str) -> Item:
        try:
            r = self.c("Runtime.evaluate", {"expression": f"document.querySelector({json.dumps(sel)})", "returnByValue": False, "objectGroup": "bw"})
        except CdpError as e:
            raise CdpError(f"bad selector: {e}") from e
        if r.get("exceptionDetails"):
            raise CdpError(f"bad selector: {sel}")
        oid = (r.get("result") or {}).get("objectId")
        if not oid:
            raise CdpError(f"no element matched selector {sel}")
        b = self.c("DOM.describeNode", {"objectId": oid})["node"].get("backendNodeId")
        return self._item("", "", b, "element", sel)

    def resolve(self, inp: dict, snap: Snap, field: bool = False) -> Item:
        """ref → selector → 文字 の順に指す。見つからないときは今の一覧を添えて断る。"""
        if inp.get("ref") not in (None, ""):
            try:
                return self.by_ref(int(inp["ref"]), snap)
            except (TypeError, ValueError):
                raise CdpError(f'ref must be a number from read or look, not {inp["ref"]!r}') from None
        if inp.get("selector"):
            return self.by_selector(str(inp["selector"]))
        want = str(inp.get("field") or (inp.get("text") if not field else "") or "").strip()
        if want:
            it = self.by_text(want, snap, _FIELDS if field else None)
            if it:
                return it
            if not field:
                # 最後の手: その文字そのものを押す（委譲された listener・リンクでない見出しなど）
                w = _norm(want)
                for sess, backend, t in snap.texts:
                    if _norm(t) == w or w in _norm(t):
                        return Item(0, sess, "", backend, "text", t[:60])
            head = "no input field matched" if field else "no element matched"
            raise CdpError(f'{head} text "{want}". What is on the page:\n' + self._list_items(snap, want=want))
        if field:
            b = self.c("Runtime.evaluate", {"expression": "document.activeElement", "returnByValue": False, "objectGroup": "bw"})
            oid = (b.get("result") or {}).get("objectId")
            if oid:
                d = self.c("DOM.describeNode", {"objectId": oid})["node"]
                if d.get("backendNodeId") and str(d.get("nodeName", "")).upper() not in ("BODY", "HTML"):
                    return Item(0, "", "", d["backendNodeId"], "focused", "the focused field")
            raise CdpError("no input field matched (give ref, field or selector). The fields:\n" +
                           self._list_items(Snap([], [i for i in snap.items if i.role in _FIELDS], [], {})))
        raise CdpError("give ref (a number from read or look), text (the visible label) or selector")

    # -- 押す -----------------------------------------------------------------------------------------------------------
    def _offset(self, session: str) -> tuple[float, float]:
        """別プロセスの iframe は座標の原点が自分のフレーム。入力は本体へ送るので親までの差を足す。"""
        x = y = 0.0
        seen = 0
        while session and session in self.children and seen < 8:
            info = self.children[session]
            parent = info.get("parent") or ""
            owner = self.c("DOM.getFrameOwner", {"frameId": info.get("frame")}, parent)
            box = self.c("DOM.getBoxModel", {"backendNodeId": owner.get("backendNodeId")}, parent)["model"]["content"]
            x += box[0]
            y += box[1]
            session = parent
            seen += 1
        return x, y

    def point(self, it: Item) -> tuple[float, float]:
        try:
            self.c("DOM.scrollIntoViewIfNeeded", {"backendNodeId": it.backend}, it.session, 4.0)
        except CdpError:
            pass
        quads = []
        try:
            quads = self.c("DOM.getContentQuads", {"backendNodeId": it.backend}, it.session, 4.0).get("quads") or []
        except CdpError:
            pass
        if not quads:
            raise CdpError(f'{_show(it)} has no place on the page (it is hidden)')
        q = quads[0]
        ox, oy = self._offset(it.session)
        return sum(q[0::2]) / 4 + ox, sum(q[1::2]) / 4 + oy

    # -- 操作中の表示（_AGENT_JS）--------------------------------------------------------------------------------------
    def agent(self, op: str, arg: dict | None = None):
        """ページに操作中の表示を出す・動かす。失敗しても操作は止めない（表示は飾り）。LAPS_BROWSER_INDICATOR=0 で出さない。"""
        if os.environ.get("LAPS_BROWSER_INDICATOR", "1") == "0":
            return None
        try:
            return self.ev(_AGENT_JS.replace("__OP__", json.dumps(op)).replace("__ARG__", json.dumps(arg or {})), timeout=2.0)
        except CdpError:
            return None

    def agent_label(self, inp: dict) -> str:
        action = str(inp.get("action") or "")
        verb = _AGENT_VERBS.get(action, action)
        target = inp.get("text") if action in ("click", "find", "hover") else None
        if action in ("type", "fill"):
            target = str(inp.get("text") or inp.get("value") or "")[:40]
        if action == "open":
            target = inp.get("url")
        if action == "key":
            target = inp.get("key")
        if target is None and inp.get("ref") not in (None, ""):
            target = f"[{inp.get('ref')}]"
        if target is None and inp.get("selector"):
            target = str(inp.get("selector"))
        return f"{verb}: {str(target)[:80]}" if target not in (None, "") else verb

    def mouse(self, kind: str, x: float, y: float, **kw) -> None:
        self.c("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, **kw}, "", 6.0)

    def press(self, x: float, y: float, count: int = 1, button: str = "left") -> None:
        bit = {"left": 1, "right": 2, "middle": 4}.get(button, 1)
        if self.agent("move", {"x": x, "y": y, "tap": True, "text": self._agent_text}) is not None:
            time.sleep(0.2)                # カーソルが押す場所へ着いてから押す（人が目で追える）
        self.mouse("mouseMoved", x, y)
        self.mouse("mousePressed", x, y, button=button, buttons=bit, clickCount=count)
        self.guarded(lambda: self.mouse("mouseReleased", x, y, button=button, buttons=0, clickCount=count))

    def key(self, spec: str) -> None:
        p, text = _key_params(spec)
        self.c("Input.dispatchKeyEvent", {"type": "keyDown" if text else "rawKeyDown", **p, **({"text": text} if text else {})}, "", 5.0)
        self.guarded(lambda: self.c("Input.dispatchKeyEvent", {"type": "keyUp", **p}, "", 5.0))

    def settle(self, budget: float = 3.0) -> None:
        """動きが止まるまで待つ。読み込み中なら長めに。ダイアログが出たらすぐ戻る。"""
        start = time.monotonic()
        last = None
        same = 0
        limit = budget
        time.sleep(0.12)
        while time.monotonic() - start < limit:
            self.pump()
            if self.dialog:
                return
            try:
                s = self.state(timeout=1.0)
            except CdpError:
                time.sleep(0.1)
                continue
            cur = (s.get("url"), s.get("ready"), s.get("count"), s.get("chars"))
            if s.get("ready") != "complete":
                limit = max(limit, 20.0)
            if cur == last and s.get("ready") == "complete":
                same += 1
                if same >= 2:
                    return
            else:
                same = 0
                last = cur
            time.sleep(0.15)

    # -- 結果 -----------------------------------------------------------------------------------------------------------
    def page_text(self, snap: Snap, max_chars: int = 12000) -> str:
        st = snap.state
        out = [f"Page: {st.get('title') or '(no title)'}", f"URL: {st.get('url') or ''}"]
        total, h, y = st.get("total") or 0, st.get("h") or 0, st.get("y") or 0
        if total and h and total > h + 8:
            below = y + h < total - 8
            out.append(f"Scrolled to {y} of {total} px" + (" (more below)" if below else " (the end)"))
        body: list[str] = []
        size = 0
        cut = False
        for ln in snap.lines:
            size += len(ln) + 1
            if size > max_chars:
                cut = True
                break
            body.append(ln)
        out += [""] + body if body else ["", "(the page shows nothing that can be read)"]
        if cut:
            out.append('[... cut; Browser(action="find", text=...) searches the rest, or ask with a larger max_chars]')
        return "\n".join(out)

    def changes(self, prev: Snap | None, cur: Snap) -> list[str]:
        """操作の前後の違い。★何も変わらなかったことも伝える（旧い道具は「成功」とだけ返して何も起きていなかった）。"""
        out: list[str] = []
        a, b = (prev.state if prev else {}), cur.state
        if prev and a.get("url") != b.get("url"):
            out.append(f"Now at: {b.get('title') or '(no title)'}\nURL: {b.get('url') or ''}")
        elif prev and a.get("title") != b.get("title"):
            out.append(f"Title is now: {b.get('title') or '(no title)'}")
        if prev is None:
            return out
        before = collections.Counter((i.role, _norm(i.name)) for i in prev.items)
        new = []
        for it in cur.items:
            k = (it.role, _norm(it.name))
            if before[k] > 0:
                before[k] -= 1
            else:
                new.append(it)
        gone = sum(before.values())
        same_page = a.get("url") == b.get("url")
        if same_page:
            if new:
                rows = ", ".join(f'[{i.ref}] {i.role} "{i.name[:50]}"' for i in new[:12])
                more = f" and {len(new) - 12} more" if len(new) > 12 else ""
                out.append(f"New on the page: {rows}{more}")
            if gone:
                out.append(f"Gone from the page: {gone} item(s)")
            seen = set(prev.lines)
            fresh = [ln for ln in cur.lines if ln not in seen and not ln.startswith("[")]
            if fresh:
                out.append("New text: " + " | ".join(x[:200] for x in fresh[:6]))
            if not new and not gone and not fresh and prev.lines == cur.lines:
                out.append("Nothing on the page changed.")
        return out

    def finish(self, head: str, prev: Snap | None, inp: dict, settle: float = 3.0, data=None) -> ToolResult:
        lines = [head]
        self.pump()
        if self.dialog:
            lines.append(self._dialog_text())
            lines += self._take_notes()
            return ToolResult("\n".join(lines), data=data)
        self.settle(settle)
        if self.dialog:
            lines.append(self._dialog_text())
            lines += self._take_notes()
            return ToolResult("\n".join(lines), data=data)
        if self.chooser:
            ch = self.chooser
            self.chooser = None
            it = self._item(ch.get("session") or "", "", ch.get("backend"), "file input", "file input")
            lines.append(f'A file chooser opened for [{it.ref}]. Give it the files: Browser(action="upload", ref={it.ref}, path="...")')
        snap = self.snapshot()
        lines += self.changes(prev, snap)
        lines += self._take_notes()
        then = str(inp.get("then") or "").lower()
        if then == "read":
            lines += ["", self.page_text(snap, _max_chars(inp))]
        elif then == "look":
            res = self.look({}, snap)
            lines += ["", str(res)]
            data = res.data
        return ToolResult("\n".join(lines), data=data)

    # -- 操作 -----------------------------------------------------------------------------------------------------------
    def open(self, inp: dict) -> ToolResult:
        err = self.line.navigate(str(inp.get("url") or ""))
        if err:
            return ToolResult(f"Error: the page could not be opened: {err}\nURL: {inp.get('url')}", ok=False, kind="error")
        self.ensure()
        self.region = None
        self.boxes = None                  # 前のページの枠へ寄せない
        time.sleep(0.3)
        self.settle(4.0)
        if self.dialog:
            return ToolResult("Opened.\n" + self._dialog_text())
        snap = self.snapshot()
        url = str(snap.state.get("url") or "")
        if url.startswith(("chrome-error:", "edge-error:")):
            return ToolResult(f"Error: the page could not be opened\nURL: {inp.get('url')}", ok=False, kind="error")
        # ★開いたら中身まで返す（小さいモデルは「開く→読む」の 2 手目の読み込みが高い・Playwright MCP も同じ形）
        return ToolResult("\n".join(["Opened.", self.page_text(snap, _max_chars(inp, 6000))] + self._take_notes()))

    def nav(self, action: str, inp: dict) -> ToolResult:
        prev = self.snapshot()
        if action == "reload":
            self.c("Page.reload")
        else:
            hist = self.c("Page.getNavigationHistory")
            i = int(hist.get("currentIndex") or 0) + (-1 if action == "back" else 1)
            ent = hist.get("entries") or []
            if not 0 <= i < len(ent):
                return ToolResult(f"Error: there is no page to go {'back' if action == 'back' else 'forward'} to", ok=False, kind="error")
            self.c("Page.navigateToHistoryEntry", {"entryId": ent[i]["id"]})
        time.sleep(0.3)
        return self.finish({"back": "Went back.", "forward": "Went forward.", "reload": "Reloaded."}[action], prev, inp, 4.0)

    def _xy(self, inp: dict) -> tuple[float, float]:
        """x・y（0〜1000）を見えている画面の位置に。直前の look が範囲つきなら、その写真の上の位置。"""
        st = self.state()
        w, h = st.get("w") or 1000, st.get("h") or 1000
        x, y = float(inp["x"]), float(inp["y"])
        if self.region:
            x0, y0, x1, y1 = self.region
            x, y = x0 + x * (x1 - x0) / 1000, y0 + y * (y1 - y0) / 1000
        px, py = x * w / 1000, y * h / 1000
        # ★枠の少し外を指した点は、近い枠の中心へ寄せる（2026-11-15・labeye の snap）。写真の上で 1〜2% ずれて外す形を拾う。
        boxes = getattr(self, "boxes", None) or []
        if boxes and (os.environ.get("LAPS_LOOK_SNAP") or "1").strip() != "0":
            from . import lookaid
            sx, sy, hit = lookaid.snap(px, py, boxes)
            if hit is not None:
                self._snapped = f"moved to the center of [{hit.get('n')}], {round(((sx - px) ** 2 + (sy - py) ** 2) ** 0.5)} px away"
                return sx, sy
        return px, py

    def click(self, inp: dict) -> ToolResult:
        prev = self.snapshot()
        count = 2 if inp.get("double") else 1
        button = str(inp.get("button") or "left")
        if inp.get("x") is not None and inp.get("y") is not None and inp.get("ref") in (None, "") and not inp.get("selector") and not inp.get("text"):
            self._snapped = ""
            x, y = self._xy(inp)
            self._chooser(True)
            try:
                self.press(x, y, count, button)
                res = self.finish(f"Clicked at {round(x)},{round(y)}" + (f" ({self._snapped})" if self._snapped else "") + ".",
                                  prev, inp)
            finally:
                self._chooser(False)
            return res
        it = self.resolve(inp, prev)
        x, y = self.point(it)
        hit = None
        try:
            hit = self.on(it, _HIT_JS)
        except CdpError:
            pass
        if isinstance(hit, dict) and not hit.get("ok") and not inp.get("force"):
            if hit.get("hidden"):
                return ToolResult(f"Error: {_show(it)} is hidden, so it was not clicked.", ok=False, kind="error")
            by = f'{hit.get("tag") or "something"} "{hit.get("by") or ""}"'
            return ToolResult(f"Error: {_show(it)} is covered by {by}, so it was not clicked. Close or click what covers it "
                              "first, or add force=true to click that spot anyway.", ok=False, kind="error")
        self._chooser(True)
        try:
            try:
                self.press(x, y, count, button)
                note = ""
            except CdpTimeout:
                # 見えていない欄など、本物の入力が届かないとき（WebView2 が隠れている）の最後の手
                self.on(it, "function() { this.click(); return true; }")
                note = " (the page did not take a real mouse click, so it was clicked from a script)"
            res = self.finish(f"Clicked {_show(it)}.{note}", prev, inp)
        finally:
            self._chooser(False)
        return res

    def _chooser(self, on: bool) -> None:
        """押した先のファイル選択の窓を出さず知らせに変える（利用者が欄で選ぶときのために押す間だけ）。"""
        self._chooser_left = False
        if not on and self.dialog:       # ダイアログの間は返らない命令＝答えた後で外す
            self._chooser_left = True
            return
        try:
            self.c("Page.setInterceptFileChooserDialog", {"enabled": on}, "", 3.0)
        except CdpError:
            pass

    def _type_into(self, it: Item, text: str) -> str:
        """1 つの欄へ打つ。返すのは足りない所の説明（空なら問題なし）。"""
        try:
            oid = self.c("DOM.resolveNode", {"backendNodeId": it.backend, "objectGroup": "bw"}, it.session)["object"]["objectId"]
            inner = self.c("Runtime.callFunctionOn", {"objectId": oid, "functionDeclaration": _FIND_FIELD_JS}, it.session)
            ioid = (inner.get("result") or {}).get("objectId")
            if ioid and ioid != oid:
                b = self.c("DOM.describeNode", {"objectId": ioid}, it.session)["node"].get("backendNodeId")
                if b:
                    it = Item(it.ref, it.session, it.frame, b, it.role, it.name)
        except CdpError:
            pass
        kind = self.on(it, _KIND_JS) or {}
        if kind.get("tag") == "select":
            r = self.on(it, _SELECT_JS, text) or {}
            if not r.get("ok"):
                return f'"{text}" is not one of the options: ' + ", ".join(r.get("options") or [])
            return ""
        if kind.get("type") in ("checkbox", "radio"):
            x, y = self.point(it)
            self.press(x, y)
            return ""
        if not kind.get("edit") and not kind.get("has_value"):
            x, y = self.point(it)      # 欄でない物（canvas の上のエディタなど）: 押して、前に出た所へ打つ
            self.press(x, y)
        else:
            try:
                self.c("DOM.focus", {"backendNodeId": it.backend}, it.session)
            except CdpError:
                x, y = self.point(it)
                self.press(x, y)
            self.on(it, _SELECT_ALL_JS)
        if text:
            self.c("Input.insertText", {"text": text}, "", 6.0)
        else:
            self.key("Delete")
        if kind.get("has_value"):
            now = self.on(it, _VALUE_JS)
            if now != text:     # date・color・range など insertText が入らない物
                now = self.on(it, _SET_VALUE_JS, text)
                if now != text:
                    return f'the field now holds "{str(now)[:80]}"'
        return ""

    def type(self, inp: dict) -> ToolResult:
        prev = self.snapshot()
        it = self.resolve(inp, prev, field=True)
        text = str(inp.get("text") if inp.get("text") is not None else "")
        bad = self._type_into(it, text)
        if bad:
            return ToolResult(f"Error: could not type into {_show(it)}: {bad}", ok=False, kind="error")
        if inp.get("submit"):
            self.key("Enter")
        return self.finish(f"Typed into {_show(it)}." + (" Submitted." if inp.get("submit") else ""), prev, inp)

    def fill(self, inp: dict) -> ToolResult:
        fields = inp.get("fields")
        if not isinstance(fields, list) or not fields:
            return ToolResult('Error: fields must be a list like [{"ref": 3, "text": "a"}, {"field": "Email", "text": "b"}]', ok=False, kind="error")
        prev = self.snapshot()
        done = []
        for f in fields:
            if not isinstance(f, dict):
                return ToolResult("Error: each entry of fields must be an object with ref, field or selector, and text", ok=False, kind="error")
            try:
                it = self.resolve(f, prev, field=True)
                bad = self._type_into(it, str(f.get("text") if f.get("text") is not None else f.get("value", "")))
            except CdpError as e:
                head = f"Filled {', '.join(done)}.\n" if done else ""
                return ToolResult(f"{head}Error: {e}", ok=False, kind="error")
            if bad:
                return ToolResult(f"Error: could not fill {_show(it)}: {bad}", ok=False, kind="error")
            done.append(_show(it))
        if inp.get("submit"):
            self.key("Enter")
        return self.finish(f"Filled {', '.join(done)}." + (" Submitted." if inp.get("submit") else ""), prev, inp)

    def hover(self, inp: dict) -> ToolResult:
        prev = self.snapshot()
        if inp.get("x") is not None and inp.get("y") is not None and inp.get("ref") in (None, "") and not inp.get("text"):
            x, y = self._xy(inp)
            head = f"Moved the mouse to {round(x)},{round(y)}."
        else:
            it = self.resolve(inp, prev)
            x, y = self.point(it)
            head = f"Moved the mouse over {_show(it)}."
        self.agent("move", {"x": x, "y": y, "text": self._agent_text})
        self.mouse("mouseMoved", x, y)
        return self.finish(head, prev, inp, 1.5)

    def drag(self, inp: dict) -> ToolResult:
        prev = self.snapshot()
        if inp.get("x") is not None and inp.get("ref") in (None, "") and not inp.get("text"):
            x0, y0 = self._xy(inp)
        else:
            x0, y0 = self.point(self.resolve(inp, prev))
        to = {"ref": inp.get("to_ref"), "text": inp.get("to_text"), "x": inp.get("to_x"), "y": inp.get("to_y")}
        if to["x"] is not None and to["y"] is not None and to["ref"] in (None, "") and not to["text"]:
            x1, y1 = self._xy(to)
        elif to["ref"] not in (None, "") or to["text"]:
            x1, y1 = self.point(self.resolve(to, prev))
        else:
            return ToolResult("Error: give where to drag to: to_ref, to_text, or to_x and to_y", ok=False, kind="error")
        if self.agent("move", {"x": x0, "y": y0, "text": self._agent_text}) is not None:
            time.sleep(0.2)
        self.mouse("mouseMoved", x0, y0)
        self.mouse("mousePressed", x0, y0, button="left", buttons=1, clickCount=1)
        for i in range(1, 7):
            self.mouse("mouseMoved", x0 + (x1 - x0) * i / 6, y0 + (y1 - y0) * i / 6, button="left", buttons=1)
        self.agent("move", {"x": x1, "y": y1, "text": self._agent_text})
        self.guarded(lambda: self.mouse("mouseReleased", x1, y1, button="left", buttons=0, clickCount=1))
        return self.finish(f"Dragged from {round(x0)},{round(y0)} to {round(x1)},{round(y1)}.", prev, inp)

    def scroll(self, inp: dict) -> ToolResult:
        self.boxes = None                  # 枠の位置が動く＝寄せは次の look から
        prev = self.snapshot()
        if inp.get("ref") not in (None, "") or inp.get("text"):
            it = self.resolve(inp, prev)
            self.point(it)
            return self.finish(f"Scrolled to {_show(it)}.", prev, inp, 1.5)
        st = prev.state
        w, h = st.get("w") or 800, st.get("h") or 600
        d = -0.85 * h if str(inp.get("direction") or "down") == "up" else 0.85 * h
        # 本物のホイール＝入れ子の枠も動く（scrollBy はページ全体しか動かせなかった）
        self.mouse("mouseWheel", w / 2, h / 2, deltaX=0, deltaY=d)
        time.sleep(0.25)
        after = self.state()
        if after.get("y") == st.get("y"):
            self.ev(f"scrollBy(0, {round(d)})")
        return self.finish(f"Scrolled {'up' if d < 0 else 'down'}.", prev, inp, 1.5)

    def press_key(self, inp: dict) -> ToolResult:
        prev = self.snapshot()
        spec = str(inp.get("key") or "")
        self.key(spec)
        return self.finish(f"Pressed {spec}.", prev, inp)

    def upload(self, inp: dict) -> ToolResult:
        paths = inp.get("path") if inp.get("path") is not None else inp.get("paths")
        paths = [str(p) for p in (paths if isinstance(paths, (list, tuple)) else [paths]) if p]
        if not paths:
            return ToolResult("Error: path is required for action=upload", ok=False, kind="error")
        from .. import policy
        full = []
        for p in paths:
            a = os.path.abspath(os.path.expanduser(p))
            if not os.path.isfile(a):
                return ToolResult(f"Error: no such file: {p}", ok=False, kind="error")
            try:
                # ★秘密の鍵や資格情報を読める道具にしない（Read と同じ門）
                _check_read(policy, a)
            except Exception as e:     # noqa: BLE001
                return ToolResult(f"Error: {e}", ok=False, kind="error")
            full.append(a)
        prev = self.snapshot()
        if inp.get("ref") in (None, "") and not inp.get("selector") and not inp.get("text"):
            inputs = [i for i in prev.items if "file" in i.name.lower() or i.role == "file input"]
            if len(inputs) != 1:
                try:
                    it = self.by_selector("input[type=file]")
                except CdpError:
                    return ToolResult("Error: give ref or selector of the file input", ok=False, kind="error")
            else:
                it = inputs[0]
        else:
            it = self.resolve(inp, prev)
        self.c("DOM.setFileInputFiles", {"files": full, "backendNodeId": it.backend}, it.session)
        return self.finish(f"Gave {len(full)} file(s) to {_show(it)}.", prev, inp)

    def answer_dialog(self, inp: dict) -> ToolResult:
        self.pump()
        if not self.dialog:
            return ToolResult("Error: no dialog is open", ok=False, kind="error")
        accept = inp.get("accept")
        accept = True if accept is None else str(accept).strip().lower() not in ("false", "no", "0", "cancel", "dismiss")
        d = self.dialog
        params = {"accept": accept}
        if inp.get("text") is not None:
            params["promptText"] = str(inp["text"])
        self.c("Page.handleJavaScriptDialog", params, d.get("session") or "", 5.0)
        self.dialog = None
        if self._chooser_left:
            self._chooser(False)
        return self.finish(("Accepted" if accept else "Dismissed") + f' the dialog "{d.get("message")}".', None, inp)

    def find(self, inp: dict) -> ToolResult:
        snap = self.snapshot()
        w = _norm(inp.get("text"))
        hits = [ln for ln in snap.lines if w in _norm(ln)]
        head = f"Page: {snap.state.get('title') or '(no title)'}\nURL: {snap.state.get('url') or ''}\n"
        if not hits:
            return ToolResult(head + f'"{inp.get("text")}" is not on this page.')
        more = f"\n... {len(hits) - 30} more" if len(hits) > 30 else ""
        return ToolResult(head + f'{len(hits)} place(s) with "{inp.get("text")}":\n' + "\n".join(x[:300] for x in hits[:30]) + more)

    def wait(self, inp: dict) -> ToolResult:
        try:
            limit = max(0.0, min(30.0, float(inp.get("seconds") or 10)))
        except (TypeError, ValueError):
            limit = 10.0
        words = _norm(inp.get("text"))
        if not words:
            time.sleep(limit)
            self.pump()
            return ToolResult(f"Waited {limit:g} s." + (" " + self._dialog_text() if self.dialog else ""))
        gone = bool(inp.get("gone"))
        end = time.monotonic() + limit
        while True:
            self.pump()
            if self.dialog:
                return ToolResult(self._dialog_text())
            snap = self.snapshot()
            there = any(words in _norm(ln) for ln in snap.lines)
            if there != gone:
                return ToolResult(f'"{inp.get("text")}" is {"gone from" if gone else "on"} the page now.')
            if time.monotonic() >= end:
                return ToolResult(f'"{inp.get("text")}" {"is still there" if gone else "did not appear"} after {limit:g} s.\n'
                                  f"Page: {snap.state.get('title') or '(no title)'}\nURL: {snap.state.get('url') or ''}",
                                  ok=False, kind="error")
            time.sleep(0.5)

    def look(self, inp: dict, snap: Snap | None = None) -> ToolResult:
        snap = snap or self.snapshot()
        st = snap.state
        W, H = st.get("w") or 1280, st.get("h") or 800
        region = _region(inp.get("region"))
        if region:
            x0, y0, x1, y1 = region[0] * W / 1000, region[1] * H / 1000, region[2] * W / 1000, region[3] * H / 1000
        else:
            x0, y0, x1, y1 = 0.0, 0.0, float(W), float(H)
        boxes = []
        marks = []
        past = 0
        for it in snap.items[:400]:
            try:
                q = (self.c("DOM.getContentQuads", {"backendNodeId": it.backend}, it.session, 3.0).get("quads") or [None])[0]
            except CdpError:
                q = None
            if not q:
                continue
            ox, oy = self._offset(it.session) if it.session else (0.0, 0.0)
            xs, ys = [v + ox for v in q[0::2]], [v + oy for v in q[1::2]]
            bx, by, bw, bh = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
            if bw < 3 or bh < 3 or bx >= x1 or by >= y1 or bx + bw <= x0 or by + bh <= y0:
                past = past + 1 if by >= y1 else past
                if past > 25:      # 読む順なので、下へ出た物が続いたら後は見えない
                    break
                continue
            past = 0
            boxes.append({"x": round(bx), "y": round(by), "w": round(bw), "h": round(bh), "n": it.ref})
            marks.append(it)
            if len(boxes) >= 150:
                break
        self.ev(_MARK_JS.replace("__BOXES__", json.dumps(boxes)), timeout=5.0)
        self.agent("hide", {"on": True})          # 操作中の表示は写真に入れない（番号の枠は入れる）
        time.sleep(0.12)
        params: dict = {"format": "png"}
        if region:
            scale = min(3.0, 1400.0 / max(1.0, x1 - x0))
            # ★clip は文書の座標（画面の座標ではない）。スクロールした分を足す（2026-11-16 全体のレビュー: 下へスクロールしてから寄ると、
            #   文書の頭が写り、その後の click は見えている所へ行く＝見ていない物を押していた）。
            sx, sy = float(st.get("x") or 0), float(st.get("y") or 0)
            params["clip"] = {"x": x0 + sx, "y": y0 + sy, "width": x1 - x0, "height": y1 - y0, "scale": max(1.0, scale)}
        try:
            shot = self.c("Page.captureScreenshot", params, "", 20.0)
        finally:
            try:
                self.ev(_UNMARK_JS, timeout=3.0)
            except CdpError:
                pass
            self.agent("hide", {"on": False})
        self.region = region
        lines = [f"Page: {st.get('title') or '(no title)'}", f"URL: {st.get('url') or ''}"]
        where = "this part of the page" if region else "the visible page"
        lines.append(f"The picture of {where} is attached. Red numbers mark what you can click or type into:")
        lines += [f'[{it.ref}] {it.role} "{it.name[:60]}"' for it in marks[:80]]
        if len(marks) > 80:
            lines.append(f"... and {len(marks) - 80} more (read lists them all)")
        total, y = st.get("total") or 0, st.get("y") or 0
        if total and y + H < total - 8 and not region:
            lines.append('There is more page below; Browser(action="scroll", direction="down") then look again.')
        if region:
            lines.append("In a click, x and y are positions in this picture (0 to 1000 across it).")
        b64 = str(shot.get("data") or "")
        self.boxes = boxes                 # 寄せ（_xy）に使う。ページの画素
        if b64:                            # ★読み手の上限に合わせて縮め、0〜1000 の目盛りを重ねる（2026-11-15・oc/lookaid.py）
            import base64 as _b64
            from . import lookaid
            prep = lookaid.prepare(_b64.b64decode(b64))
            if prep:
                b64 = _b64.b64encode(prep[0]).decode("ascii")
                aid = lookaid.note(prep[1], zoom_hint=not region)   # 寄った写真にさらに寄れとは言わない（2026-11-16 全体のレビュー）
                if aid:
                    lines.insert(2, aid)
        data = {"image": {"path": "browser-look.png", "mime": "image/png", "b64": b64}} if b64 else None
        return ToolResult("\n".join(lines), data=data)

    def read(self, inp: dict) -> ToolResult:
        snap = self.snapshot()
        return ToolResult("\n".join([self.page_text(snap, _max_chars(inp))] + self._take_notes()))

    def evaluate(self, inp: dict) -> ToolResult:
        """ページの中で JavaScript を走らせて値を返す（2026-11-19・記録で eval／js を何度も呼んでいた）。
        式でも関数（() => … / async () => …）でもよい。Promise は待つ。値は JSON にして 8,000 字まで。"""
        expr = str(inp.get("expression") or "").strip()
        if not expr:
            return ToolResult("Error: expression is required for action=eval (JavaScript to run in the page, e.g. document.title)",
                              ok=False, kind="error")
        if re.match(r"^(async\s+)?(\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>|^(async\s+)?function\b", expr):
            expr = f"({expr})()"
        r = self.c("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True,
                                        "userGesture": True}, "", 30.0)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            msg = str(((d.get("exception") or {}).get("description")) or d.get("text") or "the page script failed")
            return ToolResult("Error: " + msg.split("\n")[0][:400], ok=False, kind="error")
        res = r.get("result") or {}
        if "value" in res:
            v = res["value"]
            text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
        else:
            text = str(res.get("description") or res.get("type") or "undefined")
        return ToolResult(text if len(text) <= 8000 else text[:8000] + f"\n... ({len(text) - 8000} more characters)")

    def console(self, inp: dict) -> ToolResult:
        self.pump()
        notes = self._take_notes()
        return ToolResult("\n".join(notes) if notes else "No errors in the page console since the last action.")

    def steps(self, inp: dict) -> ToolResult:
        steps = inp.get("steps")
        if not isinstance(steps, list) or not steps:
            return ToolResult('Error: steps must be a list of Browser actions, like [{"action": "click", "ref": 3}, {"action": "read"}]', ok=False, kind="error")
        out = []
        data = None
        for i, s in enumerate(steps[:20], 1):
            if not isinstance(s, dict) or str(s.get("action")) == "steps":
                return ToolResult("\n".join(out + [f"Error: step {i} must be one Browser action"]), ok=False, kind="error")
            try:
                r = self.act(browsertool_normalize(s))
            except CdpError as e:
                r = ToolResult(f"Error: {e}", ok=False, kind="error")
            out.append(f"{i}. {r}")
            data = getattr(r, "data", None) or data
            if getattr(r, "ok", True) is False:
                out.append(f"Stopped at step {i} of {len(steps)}.")
                return ToolResult("\n".join(out), ok=False, kind="error", data=data)
        return ToolResult("\n".join(out), data=data)

    # -- 入口 -----------------------------------------------------------------------------------------------------------
    def act(self, inp: dict) -> ToolResult:
        action = str(inp.get("action") or "")
        if action == "open":
            self.pump()
            self._agent_text = self.agent_label(inp)
            res = self.open(inp)
            self.agent("say", {"text": self._agent_text})     # 開いた後のページにも出す（移った先では消えている）
            return res
        self.pump()
        if self.dialog:
            return self.answer_dialog(inp) if action == "dialog" else ToolResult("Error: " + self._dialog_text(), ok=False, kind="error")
        self.ensure()
        self.pump()
        if self.dialog and action != "dialog":
            return ToolResult("Error: " + self._dialog_text(), ok=False, kind="error")
        # ★ページの「止める」が押されていたら、ここで止める（次の操作をしない）。
        if action not in ("dialog",) and self.agent("stop"):
            self.agent("say", {"text": "止めました"})
            return ToolResult("Error: the user pressed 止める (Stop) on the L.A.P.S bar in the page. Do not continue operating "
                              "the browser; tell the user where you stopped and ask what to do next.", ok=False, kind="denied")
        self._agent_text = self.agent_label(inp)
        self.agent("say", {"text": self._agent_text})
        self.agent("busy", {"on": True})
        try:
            return self._act(action, inp)
        finally:
            self.agent("busy", {"on": False})

    def _act(self, action: str, inp: dict) -> ToolResult:
        if action in ("back", "forward", "reload"):
            return self.nav(action, inp)
        fn = {"read": self.read, "look": self.look, "click": self.click, "type": self.type, "fill": self.fill,
              "hover": self.hover, "drag": self.drag, "scroll": self.scroll, "key": self.press_key, "upload": self.upload,
              "dialog": self.answer_dialog, "find": self.find, "wait": self.wait, "console": self.console,
              "steps": self.steps, "eval": self.evaluate}.get(action)
        if fn is None:
            return ToolResult(f"Error: unknown action {action!r}", ok=False, kind="error")
        res = fn(inp)
        if action in ("click", "key", "type", "fill"):
            self.agent("say", {"text": self._agent_text})     # 押してページが移ったら、移った先に出し直す
        return res


def browsertool_normalize(step: dict) -> dict:
    from .browsertool import normalize
    return normalize(step)


def _check_read(policy, path: str) -> None:
    v = policy.check_read_path(path)
    if not v.allowed:
        raise RuntimeError(getattr(v, "reason", "") or "this file may not be read")


def _foreign(url, page_url) -> bool:
    from urllib.parse import urlsplit
    try:
        a, b = urlsplit(str(url or "")).hostname, urlsplit(str(page_url or "")).hostname
    except ValueError:
        return False
    return bool(a and b and a != b and not a.endswith("." + b) and not b.endswith("." + a))


def _short_url(url: str, origin: str) -> str:
    """同じサイトのリンクは道だけ（長い URL が何百本も並ぶと読み込みが無駄）。"""
    return url[len(origin):] or "/" if origin and url.startswith(origin + "/") else url


def _show(it: Item) -> str:
    return (f'[{it.ref}] ' if it.ref else "") + (f'{it.role} "{it.name[:60]}"' if it.name else it.role)


def _max_chars(inp: dict, dflt: int = 12000) -> int:
    try:
        return max(1000, min(60000, int(inp.get("max_chars") or dflt)))
    except (TypeError, ValueError):
        return dflt


def _region(v) -> tuple | None:
    """look の範囲（[左, 上, 右, 下]・0〜1000）。"""
    if isinstance(v, str):
        v = re.findall(r"-?\d+(?:\.\d+)?", v)
    if isinstance(v, dict):
        v = [v.get("x0"), v.get("y0"), v.get("x1"), v.get("y1")]
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    try:
        x0, y0, x1, y1 = (max(0.0, min(1000.0, float(a))) for a in v)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1) if x1 - x0 >= 10 and y1 - y0 >= 10 else None


_PAGE: Page | None = None
_LOCK = threading.Lock()
_RESTARTED = False                         # 落ちた Chrome を作り直した（次の結果で一度だけ言う）


def usable() -> bool:
    return appcall.can("browser_cdp") or (not appcall.can("browser") and pipe_ok())


def page() -> Page:
    """いまの線のページ（走行をまたいで番号を保つ。線の種類が変わったら作り直す）。"""
    global _PAGE, _RESTARTED
    kind = "app" if appcall.can("browser_cdp") else "pipe"
    with _LOCK:
        # ★Chrome が落ちていたら作り直す（2026-11-16 全体のレビュー: 一度落ちると以後ずっと「the browser is gone」だった）
        dead = _PAGE is not None and not getattr(_PAGE.line, "alive", lambda: True)()
        if dead:
            _RESTARTED = True
        if _PAGE is None or _PAGE.line.kind != kind or dead:
            if _PAGE is not None:
                _PAGE.line.close()
            _PAGE = Page(AppLine() if kind == "app" else PipeLine())
    return _PAGE


def close() -> None:
    global _PAGE
    with _LOCK:
        if _PAGE is not None:
            _PAGE.line.close()
            _PAGE = None


def run(inp: dict) -> ToolResult:
    """Browser 道具の本体（inp は browsertool.normalize を通った物）。"""
    global _RESTARTED
    try:
        pg = page()
    except CdpError as e:
        return ToolResult(f"Error: {e}", ok=False, kind="error")
    if _RESTARTED:
        _RESTARTED = False
        if str(inp.get("action") or "") != "open":   # 前のページは無い＝黙って白紙で続けない
            return ToolResult("Error: the browser had closed (it crashed or was closed) and a new one was started. "
                              "Open the page again with Browser(action=\"open\", url=...); earlier refs no longer work.",
                              ok=False, kind="error")
    pg.line.show()
    return _run_on(pg, inp)


def _run_on(pg: Page, inp: dict) -> ToolResult:
    try:
        return pg.act(inp)
    except CdpTimeout as e:
        pg.pump()
        if pg.dialog:
            return ToolResult("Error: " + pg._dialog_text(), ok=False, kind="error")
        return ToolResult(f"Error: the page did not answer in time ({e}). It may be busy; try again or reload.", ok=False, kind="error")
    except CdpError as e:
        msg = str(e)
        if "No page is open" in msg:
            return ToolResult('Error: No page is open. Use Browser(action="open", url=...) first.', ok=False, kind="error")
        if "browser is gone" in msg and getattr(pg.line, "kind", "") == "chrome":
            return ToolResult(f"Error: {msg}. Do not reopen it on your own: the user may have stopped L.A.P.S on purpose. "
                              "Tell the user and ask before working in their Chrome again.", ok=False, kind="denied")
        if "browser is gone" in msg and not getattr(pg.line, "alive", lambda: True)():
            close()                        # 次の呼び出しで新しい Chrome を起こす
            return ToolResult("Error: the browser closed (it crashed or was closed). The next Browser call starts a new one; "
                              "open the page again — earlier refs no longer work.", ok=False, kind="error")
        return ToolResult(f"Error: {msg}", ok=False, kind="error")


# -- ふだんの Chrome（L.A.P.S の Chrome 拡張機能の線・2026-11-18）-------------------------------------------------------------
_CPAGE: Page | None = None


def run_chrome(inp: dict) -> ToolResult:
    """Browser(browser="chrome") と Browser(action="tabs")。線は chromebridge.ExtLine（ページの読み書きは同じ Page）。"""
    from . import chromebridge as cb
    global _CPAGE
    action = str(inp.get("action") or "")
    b = cb.bridge()
    if b is None or not b.connected():
        why = f" ({b.error})" if b is not None and b.error else ""
        return ToolResult("Error: the user's Chrome is not connected" + why + ". The L.A.P.S Chrome extension must be "
                          "installed in their Chrome and paired (passphrase from chrome-bridge.json). Tell the user, or use "
                          "the app's own browser (leave out browser=\"chrome\").", ok=False, kind="error")
    if action == "tabs":
        try:
            tabs = b.ask("tabs")
        except Exception as e:  # noqa: BLE001
            return ToolResult(f"Error: could not list the tabs: {e}", ok=False, kind="error")
        rows = [f"[{t.get('tabId')}] {(t.get('title') or '')[:70]} — {t.get('url') or ''}"
                + (" (active)" if t.get("active") else "") + (" (L.A.P.S)" if t.get("inGroup") else "")
                for t in (tabs if isinstance(tabs, list) else [])]
        return ToolResult(f"Tabs in the user's Chrome ({len(rows)}):\n" + "\n".join(rows[:80]) +
                          "\nWork in one with Browser(action=\"open\", browser=\"chrome\", tab=<number>).")
    tab = inp.get("tab")
    if action == "open":
        url = str(inp.get("url") or "").strip()
        if url:
            try:
                ok, why = cb.site_allowed(to_url(url))
            except CdpError as e:
                return ToolResult(f"Error: {e}", ok=False, kind="error")
            if not ok:
                return ToolResult("Error: " + why, ok=False, kind="denied")
        with _LOCK:
            want = int(tab) if tab not in (None, "") else None
            stale = _CPAGE is None or not _CPAGE.line.alive()
            if stale or (want is not None and want != _CPAGE.line.tab):
                if _CPAGE is not None:
                    _CPAGE.line.close()
                    _CPAGE = None
                try:
                    _CPAGE = Page(cb.ExtLine(tab=want))
                except (CdpError, cb.BridgeError) as e:
                    return ToolResult(f"Error: {e}", ok=False, kind="error")
        pg = _CPAGE
        if not url:                         # 開いているタブにつないだだけ（移らない）
            st = pg.state()
            ok, why = cb.site_allowed(str(st.get("url") or ""))
            if not ok:
                return ToolResult("Error: " + why, ok=False, kind="denied")
            return _run_on(pg, {**inp, "action": "read"})
        return _run_on(pg, inp)
    stopped = ("Do not reopen it on your own: the user may have stopped L.A.P.S on purpose. "
               "Tell the user and ask before working in their Chrome again.")
    if _CPAGE is not None and not _CPAGE.line.alive() and _CPAGE.line.gone_reason():
        why = _CPAGE.line.gone_reason()
        return ToolResult("Error: the user's Chrome tab was " + ("closed" if why == "closed" else
                          "released by the user (Cancel on the debugging bar)") + ". " + stopped, ok=False, kind="denied")
    if _CPAGE is None or not _CPAGE.line.alive():
        return ToolResult("Error: no page of the user's Chrome is open. Open one with Browser(action=\"open\", "
                          "browser=\"chrome\", url=...) or tab=<number> from action=tabs.", ok=False, kind="error")
    pg = _CPAGE
    try:
        st = pg.state()
    except CdpError as e:
        if "browser is gone" in str(e):
            return ToolResult(f"Error: {e}. " + stopped, ok=False, kind="denied")
        return ToolResult(f"Error: {e}", ok=False, kind="error")
    ok, why = cb.site_allowed(str(st.get("url") or ""))
    if not ok:
        return ToolResult("Error: " + why, ok=False, kind="denied")
    return _run_on(pg, inp)
