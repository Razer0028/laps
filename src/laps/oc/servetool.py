"""Serve 道具（2026-10-10・オーナー「開発系を強化してください」「フルコード対応」「Gemma にしたことで追加できそうな機能も」）。

手元のフォルダを静的サーバで配り、URL を返す。`screenshot=true` で、そのページを headless の Edge/Chrome で撮って
モデルに見せる（Gemma は画像を読める）＋ページの JS の誤り（コンソール）と、ページが求めたのに無かったファイル（404）を返す。

★なぜ要るか（2026-10-10 オーナー機の走行）: エージェントが `python -m http.server` を何度も立て直した。`python` は PATH に無く
  （Windows は py）、8000 番は別のもの（IP ヘルパーのポート中継の輪）が受けていて、ブラウザ操作が 60 秒切れで 4 回失敗し、
  curl も固まった。ここでは:
  ・Blackwall の中の Python で配る（PATH に依らない）。走行が終わっても、アプリが動いている間は同じフォルダに同じ URL。
  ・ポートは接続して確かめてから使う（何かが答えるポートは避ける）。Windows では同じポートの二重の待ち受けを許さない。
  ・.js/.mjs を text/javascript で返す（Windows のレジストリが text/plain にしているとモジュールが読めない）。キャッシュはさせない。
★待ち受けは既定で 127.0.0.1 だけ（lan=true のときだけ LAN へ）。
"""
from __future__ import annotations

import base64
import collections
import http.server
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request

from ..tools import ToolResult

DEFAULT_PORT = 8765
_lock = threading.Lock()
_servers: dict[str, dict] = {}        # 配っているフォルダ（実パス）→ 状態


class _Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript", ".json": "application/json",
        ".css": "text/css", ".html": "text/html", ".htm": "text/html", ".svg": "image/svg+xml", ".wasm": "application/wasm",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
        ".avif": "image/avif", ".ico": "image/x-icon", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav",
        ".m4a": "audio/mp4", ".mp4": "video/mp4", ".webm": "video/webm", ".glb": "model/gltf-binary",
        ".gltf": "model/gltf+json", ".txt": "text/plain; charset=utf-8", ".map": "application/json",
    }

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_request(self, code="-", size="-") -> None:
        try:
            c = int(code)
        except (TypeError, ValueError):
            c = 0
        st = getattr(self.server, "bw_state", None)
        if st is not None:
            st["requests"] += 1
            if c >= 400 and not self.path.split("?")[0].endswith("/favicon.ico"):   # ブラウザが勝手に求める物は数えない
                st["errors"].append((time.strftime("%H:%M:%S"), c, self.path))

    def log_message(self, fmt: str, *args) -> None:    # 画面にも記録にも流さない
        return


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"     # Windows の SO_REUSEADDR は使用中のポートにも二重に待ち受けてしまう


def _answers(port: int) -> bool:
    """そのポートで何かが接続を受けるか（受ければ使用中）。IPv4 と IPv6 の両方を見る。

    ★2026-10-11 の見回り（0.1.394）: vite は Windows では localhost＝[::1]:5173 だけで待ち受ける。127.0.0.1 しか見ないと空いていると
      判断して 127.0.0.1:5173 に重ねて配り、ブラウザが vite ではなくこの静的な配信を開いて 404 を出した（モデルは開発サーバーが
      壊れたと思い、5173 の PID＝Blackwall を taskkill した）。"""
    for fam, addr in ((socket.AF_INET, "127.0.0.1"), (getattr(socket, "AF_INET6", None), "::1")):
        if fam is None:
            continue
        try:
            with socket.socket(fam, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((addr, port)) == 0:
                    return True
        except OSError:
            continue
    return False


def _start(directory: str, port: int | None, lan: bool) -> tuple[dict | None, str]:
    host = "0.0.0.0" if lan else "127.0.0.1"
    tried: list[str] = []
    candidates = ([port] if port else []) + [p for p in range(DEFAULT_PORT, DEFAULT_PORT + 40) if p != port]
    for p in candidates:
        if _answers(p):
            tried.append(f"{p} (already answered by another program)")
            continue
        try:
            handler = lambda *a, **k: _Handler(*a, directory=directory, **k)     # noqa: E731
            httpd = _Server((host, p), handler)
        except OSError as e:
            tried.append(f"{p} ({e.strerror or e})")
            continue
        st = {"dir": directory, "port": p, "host": host, "httpd": httpd, "started": time.time(), "requests": 0,
              "errors": collections.deque(maxlen=50)}
        httpd.bw_state = st
        th = threading.Thread(target=httpd.serve_forever, name=f"blackwall-serve-{p}", daemon=True)
        th.start()
        st["thread"] = th
        note = ""
        if port and p != port:
            note = f"Port {port} was not usable ({tried[0] if tried else 'in use'}), so port {p} is used instead. "
        return st, note
    return None, "No free port found: " + "; ".join(tried[:6])


def _url(st: dict, path: str = "/") -> str:
    path = path if path.startswith("/") else "/" + path
    return f"http://127.0.0.1:{st['port']}{path}"


def _probe(url: str) -> str:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as r:
            return f"HTTP {r.status} ({r.headers.get('Content-Type', '?')})"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:     # noqa: BLE001
        return f"no answer ({type(e).__name__})"


def _browser() -> str | None:
    env = os.environ.get("LAPS_BROWSER")
    if env and os.path.exists(env):
        return env
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    for c in cands:
        if os.path.exists(c):
            return c
    for name in ("msedge", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        w = shutil.which(name)
        if w:
            return w
    return None


def _screenshot(url: str, width: int, height: int, wait_ms: int) -> tuple[dict | None, list[str], str]:
    """(画像, コンソールの誤り, 一言)。"""
    exe = _browser()
    if not exe:
        return None, [], "No Edge/Chrome found for a screenshot (set LAPS_BROWSER to its path)."
    tmpdir = tempfile.mkdtemp(prefix="bw-shot-")
    out = os.path.join(tmpdir, "shot.png")
    args = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
            "--mute-audio", f"--user-data-dir={os.path.join(tmpdir, 'profile')}", f"--window-size={width},{height}",
            f"--virtual-time-budget={wait_ms}", "--enable-logging=stderr", "--v=0", f"--screenshot={out}", url]
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        r = subprocess.run(args, capture_output=True, timeout=60, **kw)
        log = r.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, [], "The browser did not finish within 60 s."
    except OSError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, [], f"Could not start the browser ({e})."
    # ★Windows の Edge/Chrome は標準エラーへ書かないことがある＝プロファイルの chrome_debug.log も読む（2026-10-10 の点検）。
    try:
        with open(os.path.join(tmpdir, "profile", "chrome_debug.log"), encoding="utf-8", errors="replace") as f:
            log += "\n" + f.read()
    except OSError:
        pass
    errs = []
    for ln in log.splitlines():
        if "CONSOLE" in ln or "Uncaught" in ln:
            m = ln.split("] ", 1)
            msg = m[1] if len(m) == 2 else ln
            if msg not in errs:
                errs.append(msg[:300])
    img = None
    try:
        with open(out, "rb") as f:
            raw = f.read()
        img = {"path": url, "mime": "image/png", "b64": base64.b64encode(raw).decode("ascii")}
    except OSError:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)
    return img, errs[:15], ("" if img else "The browser did not write a screenshot.")


def _status_lines(st: dict) -> list[str]:
    errs = list(st["errors"])
    lines = [f"Serving {st['dir']} at {_url(st)} (requests so far: {st['requests']})"]
    if errs:
        lines.append("Requests that failed (the page asked for these but they are missing or broken):")
        lines += [f"  {t} HTTP {c} {p}" for t, c, p in list(errs)[-15:]]
    return lines


def serve_tool(inp: dict) -> ToolResult:
    inp = inp if isinstance(inp, dict) else {}
    stop = inp.get("stop")
    if stop not in (None, "", False):
        with _lock:
            keys = list(_servers) if str(stop).lower() in ("all", "true", "1") else \
                [k for k, s in _servers.items() if str(s["port"]) == str(stop) or k == os.path.realpath(str(stop))]
            done = []
            for k in keys:
                s = _servers.pop(k)
                try:
                    s["httpd"].shutdown()
                    s["httpd"].server_close()
                except Exception:
                    pass
                done.append(f"{s['dir']} (port {s['port']})")
        return ToolResult("Stopped: " + ", ".join(done) if done else "No matching server.", ok=bool(done),
                          kind="ok" if done else "empty")
    d = str(inp.get("directory") or inp.get("dir") or os.getcwd())
    # ★2026-11-19 記録の失敗から: Windows で Git Bash の書き方（/d/Blackwall/_scratch）を渡すと、D:\d\Blackwall\_scratch と読んで
    #   5 回断っていた。/<1 文字>/… は <その文字>:\… として読む。
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])(/.*)?$", d.replace("\\", "/"))
        if m:
            d = f"{m.group(1).upper()}:" + (m.group(2) or "/").replace("/", "\\")
    d = os.path.realpath(os.path.expanduser(d))
    if not os.path.isdir(d):
        return ToolResult(f"Error: not a directory: {d}", ok=False, kind="error")
    try:
        port = int(inp["port"]) if inp.get("port") not in (None, "") else None
    except (TypeError, ValueError):
        port = None
    lan = inp.get("lan") in (True, "true", "True", 1, "1")
    with _lock:
        st = _servers.get(d)
        note = ""
        if st is not None and port and st["port"] != port:
            note = f"(Already serving this folder on port {st['port']}; reusing it.) "
        if st is None:
            st, note = _start(d, port, lan)
            if st is None:
                return ToolResult(f"Error: {note}", ok=False, kind="error")
            _servers[d] = st
    page = str(inp.get("path") or "/")
    url = _url(st, page)
    lines = [note + f"Serving {d} at {_url(st)} — open {url}",
             f"Check: GET {page} → {_probe(url)}"]
    if not os.path.exists(os.path.join(d, "index.html")) and page in ("/", ""):
        lines.append("There is no index.html in this folder, so / shows a file listing.")
    data: dict = {}
    if inp.get("screenshot") in (True, "true", "True", 1, "1"):
        n_err = len(st["errors"])
        w = int(inp.get("width") or 1280)
        h = int(inp.get("height") or 800)
        wait_ms = int(inp.get("wait_ms") or 3000)
        img, errs, why = _screenshot(url, max(320, min(w, 2560)), max(240, min(h, 2560)), max(500, min(wait_ms, 20000)))
        if img:
            data["image"] = img
            lines.append(f"Screenshot of {url} ({w}x{h}) is attached in the next message for you to look at.")
        elif why:
            lines.append(why)
        if errs:
            lines.append("Browser console (errors and messages):")
            lines += [f"  {e}" for e in errs]
        new_errs = list(st["errors"])[n_err:]
        if new_errs:
            lines.append("Files the page requested that failed:")
            lines += [f"  HTTP {c} {p}" for _, c, p in new_errs[-15:]]
        elif img:
            lines.append("All files the page requested were served.")
    else:
        if st["errors"]:
            lines += _status_lines(st)[1:]
    # ★2026-10-11 の見回り（0.1.394）: netstat で 5173 を聞いている PID を見て `taskkill /F /PID` した＝その PID は Blackwall の
    #   本体（Serve のサーバーはこのプロセスの中で動く）で、走行が「Python の出力が閉じました」で失敗した。
    lines.append(f"Stop it with Serve(stop=<port>) when you no longer need it. This server runs inside L.A.P.S's own process "
                 f"(PID {os.getpid()}, the one netstat shows for this port): never taskkill that PID — it stops L.A.P.S itself.")
    return ToolResult("\n".join(lines), ok=True, kind="ok", data=data or None)


def running() -> list[str]:
    with _lock:
        return [f"{s['dir']} → {_url(s)}" for s in _servers.values()]


def ports() -> list[int]:
    """Serve が配っている口（Bash で自分の PID を止めようとしたときの案内に使う）。"""
    with _lock:
        return sorted(int(s["port"]) for s in _servers.values() if s.get("port"))


DESCRIPTION = """Serve a local folder over HTTP to preview a web page or web game, and optionally look at it.

- directory: the folder to serve (default: the current directory). Serving the same folder again reuses its server and URL.
- port: preferred port (default: a free port from 8765). A port where another program already answers is skipped.
- path: the page to open, e.g. "/index.html" (default "/").
- screenshot: true to open the page in a headless browser (Edge/Chrome) and get a screenshot to look at, the browser console errors, and the files the page requested that were missing (HTTP 404). Use it after building or changing a page to check what it really shows. width/height (default 1280x800) and wait_ms (default 3000, time for scripts to run) adjust it.
- lan: true to make it reachable from other devices on the network (default: this computer only).
- stop: a port, a folder, or "all" to stop servers.
Use this instead of starting `python -m http.server` or other ad-hoc servers with Bash. Opening files directly (file://) breaks module scripts and fetch(); serve the folder instead."""

SCHEMA_PROPS = {
    "directory": {"type": "string", "description": "Folder to serve (default: current directory)"},
    "port": {"type": "number", "description": "Preferred port (default: a free one from 8765)"},
    "path": {"type": "string", "description": "Page to open, e.g. /index.html"},
    "screenshot": {"type": "boolean", "description": "Open the page in a headless browser: screenshot, console errors, missing files"},
    "width": {"type": "number", "description": "Screenshot width in pixels (default 1280)"},
    "height": {"type": "number", "description": "Screenshot height in pixels (default 800)"},
    "wait_ms": {"type": "number", "description": "Milliseconds to let scripts run before the screenshot (default 3000)"},
    "lan": {"type": "boolean", "description": "Reachable from other devices on the network (default false)"},
    "stop": {"type": "string", "description": "Port, folder, or \"all\" to stop"},
}
