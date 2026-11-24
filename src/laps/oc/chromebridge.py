"""ふだんの Chrome をつなぐ線（2026-11-18 オーナー「L.A.P.S の Chrome 連携を強化したい。もっと Chrome を操作しやすく」）。

形（Claude in Chrome と同じ）:
  L.A.P.S の Chrome 拡張機能（app/chrome-ext）が、この PC の 127.0.0.1:<port>/laps-bridge へ WebSocket でつなぐ。
  本体は拡張機能に「タブを開く・付ける・DevTools の命令を送る」を頼み、拡張機能は chrome.debugger で中継して知らせを返す。
  → browsercdp の Page はこの線の上でもそのまま動く（線の形 = call・drain・navigate・show・close・alive は PipeLine と同じ）。
良い点: ログイン状態のまま・マウスを取らない（後ろのタブでも動く）・速い。Chrome 136 以降はふだんのプロファイルを外から
  --remote-debugging-port でつなげないので、ふだんの Chrome は拡張機能でつなぐしかない。
守り:
  ・待ち受けは 127.0.0.1 だけ（外へは開けない）。つないだ相手は最初に合言葉（token）を送る。違えば切る。Origin は chrome-extension:// だけ。
  ・合言葉は home_dir()/chrome-bridge.json（0600）。利用者が拡張機能のポップアップに入れる。
  ・初めてのサイトを操作する前に利用者に聞く（site_allowed）。「この会話の間ずっと」「いつも」で覚える。
"""
from __future__ import annotations

import collections
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PORT = 18765
EVENTS_KEEP = 500


def _cfg_path() -> Path:
    try:
        from ..paths import home_dir
        return Path(home_dir()) / "chrome-bridge.json"
    except Exception:  # noqa: BLE001 — 置き場が引けない環境（試験など）
        return Path(os.path.expanduser("~/.laps/chrome-bridge.json"))


def config() -> dict:
    """{"port", "token", "sites": {origin: "always"}}。無ければ作る（合言葉は新しく作る）。"""
    p = _cfg_path()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("token"):
            d.setdefault("port", DEFAULT_PORT)
            d.setdefault("sites", {})
            return d
    except (OSError, ValueError):
        pass
    d = {"port": int(os.environ.get("LAPS_CHROME_BRIDGE_PORT") or DEFAULT_PORT), "token": secrets.token_urlsafe(18), "sites": {}}
    _save(d)
    return d


def _save(d: dict) -> None:
    p = _cfg_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, p)
    except OSError:
        pass


class BridgeError(RuntimeError):
    pass


class Bridge:
    """拡張機能 1 つとの線（最後につないだ物を使う）。待ち受けは別の糸。"""

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token
        self.conn = None                   # websockets.sync.server.ServerConnection
        self.info: dict = {}
        self._lock = threading.Lock()
        self._seq = 0
        self._wait: dict[int, dict] = {}
        self._events: dict[int, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=EVENTS_KEEP))
        self._gone: dict[int, str] = {}    # 外れたタブ → 理由
        self.server = None
        self.error = ""

    # -- 待ち受け ----------------------------------------------------------------------------------------------------
    def start(self) -> bool:
        try:
            from websockets.sync.server import serve
        except ImportError as e:
            self.error = f"websockets is not installed: {e}"
            return False

        def handler(conn) -> None:
            origin = ""
            try:
                origin = str(conn.request.headers.get("Origin") or "")
            except Exception:  # noqa: BLE001
                pass
            if not origin.startswith("chrome-extension://"):
                conn.close(code=4003, reason="not an extension")
                return
            try:
                hello = json.loads(conn.recv(timeout=10))
            except Exception:  # noqa: BLE001
                return
            if not isinstance(hello, dict) or hello.get("type") != "hello" or \
                    not secrets.compare_digest(str(hello.get("token") or ""), self.token):
                try:
                    conn.send(json.dumps({"type": "denied", "reason": "合言葉が違います"}))
                finally:
                    conn.close(code=4001, reason="bad token")
                return
            with self._lock:
                old = self.conn
                self.conn = conn
                self.info = {"version": hello.get("version"), "ua": hello.get("ua"), "origin": origin, "since": time.time()}
            if old is not None:
                try:
                    old.close(code=4000, reason="replaced")
                except Exception:  # noqa: BLE001
                    pass
            conn.send(json.dumps({"type": "welcome"}))
            try:
                for raw in conn:
                    self._on_message(raw)
            except Exception:  # noqa: BLE001 — 切れた
                pass
            finally:
                with self._lock:
                    if self.conn is conn:
                        self.conn = None
                for slot in list(self._wait.values()):
                    slot["ev"].set()

        try:
            self.server = serve(handler, "127.0.0.1", self.port, max_size=64 * 1024 * 1024)
        except OSError as e:
            self.error = f"port {self.port} is in use or not allowed: {e}"
            return False
        threading.Thread(target=self.server.serve_forever, daemon=True, name="laps-chrome-bridge").start()
        return True

    def _on_message(self, raw) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if not isinstance(m, dict):
            return
        if "re" in m:
            slot = self._wait.get(m["re"])
            if slot:
                slot["r"] = m
                slot["ev"].set()
            return
        t = m.get("type")
        if t == "event":
            from .browsercdp import EVENTS
            if m.get("method") in EVENTS:
                self._events[int(m.get("tabId") or 0)].append(
                    {"method": m["method"], "params": m.get("params") or {}, "session": str(m.get("sessionId") or "")})
        elif t in ("detached", "closed"):
            self._gone[int(m.get("tabId") or 0)] = str(m.get("reason") or t)

    # -- 頼む --------------------------------------------------------------------------------------------------------
    def connected(self) -> bool:
        return self.conn is not None

    def ask(self, op: str, timeout: float = 15.0, **kw) -> dict | list:
        conn = self.conn
        if conn is None:
            raise BridgeError("the L.A.P.S Chrome extension is not connected")
        with self._lock:
            self._seq += 1
            i = self._seq
            slot = {"ev": threading.Event(), "r": None}
            self._wait[i] = slot
        try:
            conn.send(json.dumps({"id": i, "op": op, **kw}))
            if not slot["ev"].wait(timeout):
                from .browsercdp import CdpTimeout
                raise CdpTimeout(f"{kw.get('method') or op} timed out after {timeout:g} s")
        finally:
            self._wait.pop(i, None)
        r = slot["r"]
        if r is None:
            raise BridgeError("the Chrome extension disconnected")
        if r.get("error"):
            raise BridgeError(str(r["error"]))
        return r.get("result") if r.get("result") is not None else {}

    def drain(self, tab: int) -> list[dict]:
        q = self._events.get(tab)
        out = []
        while q:
            try:
                out.append(q.popleft())
            except IndexError:
                break
        return out


class AppBridge:
    """アプリの中（Windows・Linux の GUI）: 口はアプリ（chromebridge.rs）が 1 つだけ持つ＝本体は app_call op="chrome" で頼む。
    ★本体は会話ごとに最大 3 つ動くので、本体に口を持たせると口を持たない本体から使えない。"""
    server = True
    error = ""

    def _call(self, args: dict, timeout: float) -> dict:
        from ..tools import appcall
        try:
            r = appcall.call("chrome", args, timeout=timeout + 5.0)
        except Exception as e:  # noqa: BLE001
            raise BridgeError(f"could not ask the app: {e}") from e
        if not isinstance(r, dict):
            raise BridgeError("the app gave no answer")
        if not r.get("ok"):
            if r.get("timeout"):
                from .browsercdp import CdpTimeout
                raise CdpTimeout(str(r.get("error") or "timed out"))
            raise BridgeError(str(r.get("error") or "the Chrome extension failed"))
        return r

    @property
    def info(self) -> dict:
        try:
            return dict(self._call({"op": "status"}, 5.0).get("extension") or {})
        except Exception:  # noqa: BLE001
            return {}

    def connected(self) -> bool:
        try:
            return bool(self._call({"op": "status"}, 5.0).get("connected"))
        except Exception:  # noqa: BLE001
            return False

    def ask(self, op: str, timeout: float = 15.0, **kw):
        r = self._call({"op": op, "timeout": timeout, **kw}, timeout)
        return r.get("result") if r.get("result") is not None else {}

    def drain(self, tab: int) -> list[dict]:
        try:
            r = self._call({"op": "events", "tabId": tab}, 5.0)
        except Exception:  # noqa: BLE001
            return []
        if r.get("gone"):
            self._gone[tab] = str(r["gone"])
        from .browsercdp import EVENTS
        return [e for e in (r.get("events") or []) if isinstance(e, dict) and e.get("method") in EVENTS]

    def __init__(self) -> None:
        self._gone: dict[int, str] = {}


_BRIDGE: Bridge | None = None
_APP_BRIDGE: AppBridge | None = None
_BLOCK = threading.Lock()


def bridge(start: bool = True):
    """アプリの中ならアプリの口（AppBridge）。外（端末・開発機・試験）なら自分で待ち受ける（1 回だけ）。LAPS_CHROME_BRIDGE=0 で使わない。"""
    global _BRIDGE, _APP_BRIDGE
    if os.environ.get("LAPS_CHROME_BRIDGE", "1") == "0":
        return None
    from ..tools import appcall
    if appcall.can("chrome"):
        with _BLOCK:
            if _APP_BRIDGE is None:
                _APP_BRIDGE = AppBridge()
            return _APP_BRIDGE
    with _BLOCK:
        if _BRIDGE is None and start:
            c = config()
            b = Bridge(int(c.get("port") or DEFAULT_PORT), str(c["token"]))
            b.start()
            _BRIDGE = b
        return _BRIDGE


def status() -> dict:
    b = bridge(start=False)
    c = config()
    return {"listening": bool(b and b.server), "connected": bool(b and b.connected()), "port": c.get("port"),
            "error": (b.error if b else ""), "extension": (b.info if b and b.connected() else {})}


# -- サイトごとの許可 ----------------------------------------------------------------------------------------------------
_SESSION_SITES: set[str] = set()


def origin_of(url: str) -> str:
    try:
        u = urlsplit(url)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https"):
        return ""
    return f"{u.scheme}://{u.hostname or ''}" + (f":{u.port}" if u.port else "")


def site_allowed(url: str) -> tuple[bool, str]:
    """ふだんの Chrome で、このサイトを操作してよいか。初めてなら利用者に聞く。(よいか, 理由)。"""
    o = origin_of(url)
    if not o:
        return True, ""                    # about:blank など
    if o in _SESSION_SITES or config().get("sites", {}).get(o) == "always":
        return True, ""
    if os.environ.get("LAPS_CHROME_ASK_SITES", "1") == "0":
        return True, ""
    from ..tools import ask_user
    asker = ask_user._ASKER
    if asker is None:
        return False, (f"{o} has not been allowed for the user's Chrome yet, and the user cannot be asked here. "
                       "Ask the user in your reply whether L.A.P.S may operate this site in their Chrome.")
    opts = [{"label": "この会話の間は許す", "description": f"{o} をこの会話の間だけ操作してよい"},
            {"label": "いつも許す", "description": f"{o} をこれからも聞かずに操作してよい"},
            {"label": "許さない", "description": "このサイトは操作しない"}]
    try:
        ans = str(asker(f"L.A.P.S があなたの Chrome で {o} を操作しようとしています。許しますか？", opts) or "")
    except Exception as e:  # noqa: BLE001
        return False, f"could not ask the user about {o}: {e}"
    if "いつも" in ans:
        c = config()
        c.setdefault("sites", {})[o] = "always"
        _save(c)
        _SESSION_SITES.add(o)
        return True, ""
    if "この会話" in ans or ans.strip().lower() in ("yes", "y", "1"):
        _SESSION_SITES.add(o)
        return True, ""
    return False, f"The user did not allow L.A.P.S to operate {o} in their Chrome. Do not open or operate it."


# -- 線（browsercdp.Page から使う）--------------------------------------------------------------------------------------
class ExtLine:
    """ふだんの Chrome の 1 つのタブ。タブを開くか、指定のタブに付ける。"""
    kind = "chrome"

    def __init__(self, tab: int | None = None, url: str = "about:blank") -> None:
        b = bridge()
        if b is None or not b.connected():
            from .browsercdp import CdpError
            why = (b.error if b and b.error else "") if b else "the bridge is turned off (LAPS_CHROME_BRIDGE=0)"
            raise CdpError("the user's Chrome is not connected: install and pair the L.A.P.S Chrome extension "
                           "(app/chrome-ext; the passphrase is in chrome-bridge.json)" + (f" — {why}" if why else ""))
        self.b = b
        if tab is None:
            r = b.ask("open", url=url, active=True, timeout=20.0)
            tab = int(r["tabId"])
        else:
            b.ask("attach", tabId=int(tab), timeout=15.0)
        self.tab = int(tab)
        self.view = f"chrome:{self.tab}"
        b._gone.pop(self.tab, None)

    def alive(self) -> bool:
        return self.b.connected() and self.tab not in self.b._gone

    def gone_reason(self) -> str:
        return self.b._gone.get(self.tab, "")

    def call(self, method: str, params: dict | None = None, session: str = "", timeout: float = 10.0) -> dict:
        from .browsercdp import CdpError
        if self.tab in self.b._gone:
            why = self.b._gone[self.tab]
            raise CdpError("the browser is gone: the user's Chrome tab was " +
                           ("closed" if why == "closed" else f"released ({why}; the user may have pressed Cancel on the debugging bar)"))
        kw = {"tabId": self.tab, "method": method, "params": params or {}}
        if session:
            kw["sessionId"] = session
        try:
            r = self.b.ask("cdp", timeout=timeout, **kw)
        except BridgeError as e:
            msg = str(e)
            if "not attached" in msg or "No tab with" in msg:
                # 利用者が Chrome の「デバッグ中」の帯で切った・タブを閉じた（onDetach より先にこちらが届くことがある）
                self.b._gone[self.tab] = "closed" if "No tab with" in msg else "canceled_by_user"
                raise CdpError("the browser is gone: the user's Chrome tab was " +
                               ("closed" if "No tab with" in msg else "released by the user (Cancel on the debugging bar)")) from e
            raise CdpError(msg) from e
        return r if isinstance(r, dict) else {}

    def drain(self) -> list[dict]:
        return self.b.drain(self.tab)

    def show(self) -> None:
        pass                               # 後ろのタブのまま動く（利用者の作業を止めない）。見せたいときは activate

    def activate(self) -> None:
        try:
            self.b.ask("activate", tabId=self.tab)
        except Exception:  # noqa: BLE001
            pass

    def navigate(self, url: str) -> str:
        from .browsercdp import to_url
        r = self.call("Page.navigate", {"url": to_url(url)}, timeout=30.0)
        return str(r.get("errorText") or "")

    def close(self) -> None:
        try:
            self.b.ask("detach", tabId=self.tab, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass

    def tabs(self) -> list:
        r = self.b.ask("tabs")
        return r if isinstance(r, list) else []
