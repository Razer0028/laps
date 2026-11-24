"""MCP クライアント（2026-10-08 オーナー「API や MCP、コネクタ対応させたい」）。

設定は Claude Desktop / Claude Code と同じ形の `mcp.json`（`{"mcpServers": {名前: {...}}}`・VS Code の `servers` も読む）:
  置き場  LAPS_HOME/mcp.json（アプリは <データ根>\\agent\\mcp.json）。作業場の `.mcp.json` は
          LAPS_MCP_PROJECT=1 のときだけ読む（取ってきたリポジトリに仕込まれた命令を黙って走らせない）。
  stdio   {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "{{GITHUB_TOKEN}}"}, "cwd": "..."}
  HTTP    {"type": "http", "url": "https://.../mcp", "headers": {"Authorization": "Bearer {{NAME}}"}}
  無効    {"disabled": true}
★鍵は API キーの置き場（laps/apikeys.py）の `{{NAME}}` で書ける（値をファイルに書かない）。HTTP は鍵の送り先ドメインを守る。
  `${VAR}` / `${VAR:-既定}` は環境変数。
★道具の数が多いと選び方が崩れる（knowledge/ai-agent-research-notes.md）。合わせて LAPS_MCP_MAX_DIRECT（既定 12）本までは
  `mcp__<server>__<tool>` として直接載せ、超えたら道具 1 本（Mcp: list / describe / call）にまとめる。
★接続は 1 度張ったら走行をまたいで使い回す（serve の process が生きている間）。道具の一覧は mcp-cache.json に控え、
  次の起動では控えから載せて裏で繋ぎ直す（npx の初回取得で走行の開始を待たせない）。
★まだ無いもの: OAuth のサインイン（トークンを headers に書く形だけ）・旧 SSE 方式・resources/prompts の閲覧。
"""
from __future__ import annotations

from .. import paths as _lp
import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..tools import ToolResult, ToolSpec

PROTOCOL = "2025-06-18"
_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def enabled() -> bool:
    return os.environ.get("LAPS_MCP", "1").strip() != "0"


def _home() -> Path:
    d = Path(str(_lp.home_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return _home() / "mcp.json"


def _cache_path() -> Path:
    return _home() / "mcp-cache.json"


def _workspace() -> str:
    return os.environ.get("LAPS_WORKSPACE") or os.getcwd()


def _num(env: str, default: float) -> float:
    try:
        return float(os.environ.get(env, "") or default)
    except ValueError:
        return default


class McpError(Exception):
    pass


def _safe(name: str) -> str:
    return _SAFE.sub("_", str(name)).strip("_")[:40] or "server"


# ── 設定 ───────────────────────────────────────────────────────────────

def load_config() -> dict[str, dict]:
    """{安全な名前: 設定}。読めないファイルは `!<file>` の項目に理由を入れて返す（黙って空にしない）。"""
    out: dict[str, dict] = {}
    seen: set[str] = set()
    srcs = [config_path()]
    if os.environ.get("LAPS_MCP_PROJECT") == "1":
        srcs.append(Path(_workspace()) / ".mcp.json")
    for p in srcs:
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            out[f"!{p.name}"] = {"_error": f"{p} を読めません: {e}"}
            continue
        servers = (data.get("mcpServers") or data.get("servers") or {}) if isinstance(data, dict) else {}
        for name, cfg in servers.items():
            seen.add(_safe(name))
            if isinstance(cfg, dict) and not cfg.get("disabled"):
                out[_safe(name)] = dict(cfg, _name=str(name), _source=str(p))
    # ★同梱の REA（2026-11-03 オーナー「同梱してください」）。利用者の設定に "rea" があれば（disabled でも）そちらが勝つ。
    if "rea" not in seen:
        b = _builtin_rea()
        if b is not None:
            out["rea"] = b
    return out


# ── 同梱のサーバー（REA）──────────────────────────────────────────────
# ★REA（npm の rea-agents・MIT・逆解析の MCP サーバー）を Windows のアプリに同梱する（2026-11-03）。
#   置き場は組み込みの Python の木の中: pyruntime/rea/node/node.exe と pyruntime/rea/pkg/node_modules/rea-agents/
#   scripts/rea.mjs（python.exe が pyruntime/ に居るので sys.executable の隣の rea/）。LAPS_REA_DIR で別の場所を指せる。
#   どちらかが欠けていれば何も足さない（誤りの項目も作らない＝同梱していない Linux の .deb などで黙って無い）。
#   Ghidra と Java は同梱しない。GHIDRA_INSTALL_DIR（または LAPS_REA_GHIDRA）があるときだけ Ghidra を選ばせる。
#   無くても REA の静的な読み手（JS/Electron・.NET・PE・IL2CPP・Godot PCK・GGUF・SQLite など）は動く。
#   ★"rea setup" / "rea update" は走らせない（引数は "mcp" だけ）。
#   ★_lazy: 走行の開始では起動しない（下の _tool_index）。REA を使わない機械で毎回 node を立ち上げないため。

def _rea_dir() -> Path:
    d = os.environ.get("LAPS_REA_DIR", "").strip()
    if d:
        return Path(d)
    # ★利用者名が非 ASCII の機械では、アプリが pyruntime を app_root\pyruntime へ複製して、そこの python.exe を
    #   起動する。REA は Python と違って ASCII のパスを要らないので、インストール先の rea/ が分かるならそちらを使う
    #   （複製の側を読まない＝将来アプリが rea/ を複製から外しても動く・2026-11-03 レビュー）。
    inst = os.environ.get("LAPS_INSTALL_PYRUNTIME", "").strip()
    if inst and (Path(inst) / "rea").is_dir():
        return Path(inst) / "rea"
    # ★Linux の .deb は /opt/blackwall/venv で動き、REA は隣の /opt/blackwall/rea（2026-11-03）。
    #   venv の python は系の python への symlink なので、sys.executable を resolve すると /usr/bin を指す＝
    #   venv の場所（sys.prefix）から辿る。
    deb = Path(sys.prefix).parent / "rea"
    if sys.platform != "win32" and deb.is_dir():
        return deb
    return Path(sys.executable).resolve().parent / "rea"


def _builtin_rea() -> dict | None:
    if os.environ.get("LAPS_REA", "1").strip() == "0":
        return None
    root = _rea_dir()
    node = root / "node" / ("node.exe" if sys.platform == "win32" else "node")
    entry = root / "pkg" / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"
    if not node.is_file() or not entry.is_file():
        return None
    env = {"REA_MCP_INPUT_SCHEMA_PROFILE": "compact", "REA_MCP_OUTPUT_SCHEMA_PROFILE": "off"}
    ghidra = (os.environ.get("LAPS_REA_GHIDRA") or os.environ.get("GHIDRA_INSTALL_DIR") or "").strip() or _find_tool("ghidra")
    prepend = [str(node.parent)]
    if ghidra:
        env["GHIDRA_INSTALL_DIR"] = ghidra
        env["REA_ANALYSIS_PROVIDER"] = "ghidra"
        # ★Ghidra は JDK 21 が要る。JAVA_HOME が無いと provider_unavailable（作業用のコンテナ の podman exec で実際に踏んだ・11-03）。
        java = (os.environ.get("LAPS_REA_JAVA") or "").strip() or _find_tool("jdk")
        if java:
            env["JAVA_HOME"] = java
            prepend.insert(0, str(Path(java) / "bin"))
    # ★PATH は env に入れない（_key に入ると PATH が変わるたびに控えが外れる）。_path_prepend で起動のときに前へ足す。
    return {"command": str(node), "args": [str(entry), "mcp"], "env": env,
            "_name": "rea", "_source": "built-in", "_lazy": True, "_path_prepend": os.pathsep.join(prepend)}


def _find_tool(kind: str) -> str:
    """<LAPS_HOME>/tools/ に置いた Ghidra と JDK を見つける（2026-11-04 オーナー「Ghidra 入れてください」）。
    環境変数を設定しなくても、決まった場所に展開すれば使える。Windows では %LOCALAPPDATA%\\Blackwall\\agent\\tools
    （scripts/install_ghidra.ps1 が置く）。複数あれば名前の新しい物。見つからなければ ""。★ここで作らない（mkdir しない）。"""
    home = Path(_lp.home_dir())
    if kind == "ghidra":
        hits = [p for p in (home / "tools" / "ghidra").glob("ghidra_*_PUBLIC") if (p / "Ghidra" / "application.properties").is_file()]
    else:
        exe = "java.exe" if sys.platform == "win32" else "java"
        hits = [p for p in (home / "tools" / "jdk").glob("jdk-21*") if (p / "bin" / exe).is_file()]
    return str(sorted(hits)[-1]) if hits else ""



def rea_info() -> dict | None:
    """モデルに REA の使いどころを伝えるための事実（起動はしない・2026-11-04 オーナー「REA とかをモデルが自律的に使うか判断できるように」）。
    rea が設定に無ければ None。native＝実行ファイルの関数の一覧・逆コンパイルが使えるか。
    ★同梱の REA は Ghidra の設定があるときだけ native（無いと binary_overview・list_procedures・procedure_pseudo_code が
    provider_unavailable を返す・開発機 の Linux 版で実測）。利用者が自分で設定した rea は中が分からないので使えるとみなす。
    環境変数と設定ファイルだけで決まる＝この process の間は変わらない（道具の説明に入れても控えを崩さない）。"""
    try:
        if not enabled():
            return None
        cfg = load_config().get("rea")
    except Exception:
        return None
    if not cfg:
        return None
    if cfg.get("_source") == "built-in":
        return {"built_in": True, "native": bool((cfg.get("env") or {}).get("REA_ANALYSIS_PROVIDER"))}
    return {"built_in": False, "native": True}

def _transport(cfg: dict) -> str:
    t = str(cfg.get("type") or cfg.get("transport") or "").lower()
    if t in ("sse",):
        return "sse"
    if cfg.get("url") or t in ("http", "streamable-http", "streamable_http"):
        return "http"
    return "stdio"


def _key(cfg: dict) -> str:
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    return hashlib.sha256(json.dumps(clean, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def _fill(obj: Any, host: str | None = None, port: int | None = None) -> Any:
    """`{{NAME}}`（API キー）と `${VAR}`（環境変数）を埋める。HTTP では鍵の送り先ドメインを確かめる。"""
    from .. import apikeys
    if isinstance(obj, str):
        def key(m: re.Match) -> str:
            n = m.group(1).upper()
            v = apikeys.value(n)
            if v is None:
                raise McpError(f"API key {{{{{n}}}}} is not saved (add it in Settings → API keys)")
            if host is not None and not apikeys.host_allowed(host, port, apikeys.hosts_of(n)):
                raise McpError(f"{{{{{n}}}}} may only be sent to {', '.join(apikeys.hosts_of(n))}, not {host}")
            return v
        s = apikeys.PLACEHOLDER_RE.sub(key, obj)
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), s)
    if isinstance(obj, dict):
        return {k: _fill(v, host, port) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, host, port) for v in obj]
    return obj


# ── 接続（共通）────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, name: str, cfg: dict) -> None:
        self.name, self.cfg, self.key = name, cfg, _key(cfg)
        self.alive = False
        self.tools: list[dict] | None = None
        self.server_info: dict = {}
        self.instructions = ""
        self.protocol = PROTOCOL
        self.log: deque = deque(maxlen=40)
        self._id = 0
        self._id_lock = threading.Lock()
        self.stale = False
        self.workdir: str | None = None     # stdio のサーバーを起動した場所（結果に出る相対パスの基準）

    def _next_id(self) -> int:
        with self._id_lock:
            self._id += 1
            return self._id

    # 下の 2 つは方式ごと
    def request(self, method: str, params: dict | None, timeout: float) -> dict:
        raise NotImplementedError

    def notify(self, method: str, params: dict | None = None) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self.alive = False

    def handshake(self, timeout: float) -> None:
        from .. import __version__
        # ★roots は既定で名乗らない（LAPS_MCP_ROOTS=1 で名乗る）。名乗ると、ファイル用のサーバーは設定の args に
        #   書いたフォルダを捨てて作業場に置き換える（server-filesystem で実測・2026-10-08）＝利用者の設定が黙って変わる。
        caps = {"roots": {"listChanged": False}} if os.environ.get("LAPS_MCP_ROOTS") == "1" else {}
        r = self.request("initialize", {"protocolVersion": PROTOCOL,
                                        "capabilities": caps,
                                        "clientInfo": {"name": "blackwall", "version": str(__version__)}}, timeout)
        self.protocol = str(r.get("protocolVersion") or PROTOCOL)
        self.server_info = r.get("serverInfo") or {}
        self.instructions = str(r.get("instructions") or "")[:2000]
        self.notify("notifications/initialized", {})
        self.list_tools(timeout)

    def list_tools(self, timeout: float) -> None:
        tools: list[dict] = []
        cursor = None
        for _ in range(20):
            r = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            tools += [t for t in (r.get("tools") or []) if isinstance(t, dict) and t.get("name")]
            cursor = r.get("nextCursor")
            if not cursor:
                break
        self.tools = tools
        self.stale = False

    def answer_server(self, msg: dict) -> dict | None:
        """サーバーからの依頼に返す中身（ping・roots/list）。知らない依頼は誤りで返す。"""
        m = msg.get("method")
        if m == "ping":
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {}}
        if m == "roots/list":
            ws = Path(_workspace()).resolve()
            return {"jsonrpc": "2.0", "id": msg.get("id"),
                    "result": {"roots": [{"uri": ws.as_uri(), "name": ws.name or "workspace"}]}}
        if m == "elicitation/create":
            return {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"action": "decline"}}
        return {"jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32601, "message": f"blackwall does not support {m}"}}

    def on_notification(self, msg: dict) -> None:
        m = str(msg.get("method") or "")
        if m == "notifications/tools/list_changed":
            self.stale = True
        elif m == "notifications/message":
            p = msg.get("params") or {}
            self.log.append(f"[{p.get('level', '')}] {str(p.get('data'))[:300]}")


def _rpc_error(err: dict) -> McpError:
    return McpError(f"{err.get('message') or 'error'} (code {err.get('code')})"
                    + (f": {json.dumps(err.get('data'), ensure_ascii=False)[:300]}" if err.get("data") else ""))


# ── stdio ──────────────────────────────────────────────────────────────

class _Stdio(_Conn):
    def __init__(self, name: str, cfg: dict) -> None:
        super().__init__(name, cfg)
        self.proc: subprocess.Popen | None = None
        self._pending: dict[int, dict] = {}
        self._plock = threading.Lock()
        self._wlock = threading.Lock()

    def start(self) -> None:
        cfg = self.cfg
        cmd = str(cfg.get("command") or "").strip()
        if not cmd:
            raise McpError("command is empty")
        args = [str(a) for a in _fill(cfg.get("args") or [])]
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in _fill(cfg.get("env") or {}).items()})
        if cfg.get("_path_prepend"):
            env["PATH"] = str(cfg["_path_prepend"]) + os.pathsep + env.get("PATH", "")
        cwd = str(_fill(cfg.get("cwd") or "")) or _workspace()
        exe = shutil.which(cmd, path=env.get("PATH")) or (cmd if os.path.isfile(cmd) else None)
        stem = Path(cmd).stem.lower()
        if not exe and stem in ("uvx", "uv"):
            # ★uv が無い機械（利用者の PC など）では、Voice が取り寄せる uv（sha256 で固定・約 20MB）を使う。
            try:
                from . import voice as _v
                uv = _v._ensure_uv()
                cand = uv.with_name(("uvx.exe" if sys.platform == "win32" else "uvx") if stem == "uvx" else uv.name)
                exe = str(cand if cand.exists() else uv)
                if stem == "uvx" and not cand.exists():
                    args = ["tool", "run"] + args
                for k, v in _v._uv_env().items():
                    if k.startswith("UV_"):
                        env.setdefault(k, v)
            except Exception as e:  # noqa: BLE001
                raise McpError(f"'{cmd}' is not installed and uv could not be prepared: {e}") from e
        if not exe:
            hint = {"npx": "Node.js", "node": "Node.js", "uvx": "uv", "uv": "uv", "docker": "Docker",
                    "python": "Python", "deno": "Deno", "bunx": "Bun"}.get(Path(cmd).stem.lower(), "")
            raise McpError(f"command '{cmd}' was not found on this machine"
                           + (f" (install {hint}, then restart L.A.P.S)" if hint else " (give the full path)"))
        kw: dict = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
            if exe.lower().endswith((".cmd", ".bat")):
                line = subprocess.list2cmdline([exe] + args)
                argv: Any = f'cmd.exe /d /s /c "{line}"'
            else:
                argv = [exe] + args
        else:
            kw["start_new_session"] = True
            argv = [exe] + args
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         cwd=cwd if os.path.isdir(cwd) else None, env=env, **kw)
        except OSError as e:
            raise McpError(f"could not start '{cmd}': {e}") from e
        self.workdir = cwd if os.path.isdir(cwd) else os.getcwd()
        self.alive = True
        threading.Thread(target=self._read_out, name=f"mcp-{self.name}-out", daemon=True).start()
        threading.Thread(target=self._read_err, name=f"mcp-{self.name}-err", daemon=True).start()

    def _read_err(self) -> None:
        try:
            for raw in self.proc.stderr:      # type: ignore[union-attr]
                self.log.append(raw.decode("utf-8", errors="replace").rstrip()[:400])
        except Exception:
            pass

    def _read_out(self) -> None:
        try:
            for raw in self.proc.stdout:      # type: ignore[union-attr]
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self.log.append(line[:400])          # 標準出力にログを書くサーバーがある
                    continue
                for m in (msg if isinstance(msg, list) else [msg]):
                    self._dispatch(m)
        except Exception:
            pass
        self.alive = False
        tail = " / ".join(list(self.log)[-4:])
        with self._plock:
            for slot in self._pending.values():
                slot["error"] = McpError(f"the server exited{f' ({tail})' if tail else ''}")
                slot["ev"].set()

    def _dispatch(self, m: dict) -> None:
        if not isinstance(m, dict):
            return
        if "method" in m and "id" in m:
            reply = self.answer_server(m)
            if reply:
                self._write(reply)
        elif "method" in m:
            self.on_notification(m)
        elif "id" in m:
            with self._plock:
                slot = self._pending.get(m["id"])
            if slot is not None:
                slot["msg"] = m
                slot["ev"].set()

    def _write(self, obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self.proc.stdin.write(data)   # type: ignore[union-attr]
                self.proc.stdin.flush()       # type: ignore[union-attr]
            except (OSError, ValueError) as e:
                self.alive = False
                raise McpError(f"the server is not running ({e})") from e

    def request(self, method: str, params: dict | None, timeout: float) -> dict:
        if not self.alive:
            raise McpError("the server is not running")
        rid = self._next_id()
        slot = {"ev": threading.Event(), "msg": None, "error": None}
        with self._plock:
            self._pending[rid] = slot
        try:
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})})
            if not slot["ev"].wait(timeout):
                try:
                    self.notify("notifications/cancelled", {"requestId": rid, "reason": "timeout"})
                except Exception:
                    pass
                raise McpError(f"{method} did not answer within {timeout:g} s")
        finally:
            with self._plock:
                self._pending.pop(rid, None)
        if slot["error"]:
            raise slot["error"]
        msg = slot["msg"] or {}
        if msg.get("error"):
            raise _rpc_error(msg["error"])
        return msg.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})})

    def close(self) -> None:
        self.alive = False
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            p.stdin.close()               # type: ignore[union-attr]  # 行儀のよいサーバーはこれで終わる
            p.wait(timeout=2)
            return
        except Exception:
            pass
        from ..procrun import _kill_tree
        _kill_tree(p)


# ── Streamable HTTP ────────────────────────────────────────────────────

class _Http(_Conn):
    def __init__(self, name: str, cfg: dict) -> None:
        super().__init__(name, cfg)
        self.session_id = ""
        self.url = ""
        self.headers: dict[str, str] = {}

    def start(self) -> None:
        from urllib.parse import urlparse
        raw = str(self.cfg.get("url") or "")
        url = str(_fill(raw))       # URL の中の鍵は下で送り先を確かめる（ここでは埋めるだけ）
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise McpError("url must be an absolute http(s) URL")
        url = str(_fill(raw, u.hostname, u.port))
        hdrs = _fill(self.cfg.get("headers") or {}, u.hostname, u.port)
        from .. import apikeys
        if u.scheme == "http" and (apikeys.PLACEHOLDER_RE.search(raw) or
                                   apikeys.PLACEHOLDER_RE.search(json.dumps(self.cfg.get("headers") or {}))):
            raise McpError("API keys are only sent over https")
        if os.environ.get("LAPS_ALLOW_PRIVATE_FETCH") != "1" and not self.cfg.get("allow_private"):
            from ..tools import webfetch as _wf
            msg = _wf._check_hop(url)
            if msg:
                raise McpError(msg + ' (or add "allow_private": true to this server)')
        self.url, self.headers = url, {str(k): str(v) for k, v in hdrs.items()}
        self.alive = True

    def _post(self, body: dict, timeout: float, want_id: int | None) -> dict | None:
        import httpx
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if body.get("method") != "initialize":
            h["MCP-Protocol-Version"] = self.protocol
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=min(timeout, 20)), follow_redirects=False) as c:
            with c.stream("POST", self.url, json=body, headers=h) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                if r.status_code == 401 or r.status_code == 403:
                    raise McpError(f"HTTP {r.status_code}: the server refused the credentials. Put a token in this "
                                   'server\'s "headers" (e.g. "Authorization": "Bearer {{NAME}}"); OAuth sign-in is '
                                   "not supported yet")
                if r.status_code == 404 and self.session_id and body.get("method") != "initialize":
                    self.session_id = ""
                    raise _SessionGone()
                if r.status_code >= 400:
                    raise McpError(f"HTTP {r.status_code}: {r.read()[:300].decode('utf-8', 'replace')}")
                if want_id is None:
                    return None
                ctype = r.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    data: list[str] = []
                    for line in r.iter_lines():
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line.strip() and data:
                            got = self._sse_event("\n".join(data), want_id)
                            data = []
                            if got is not None:
                                return got
                    if data:
                        got = self._sse_event("\n".join(data), want_id)
                        if got is not None:
                            return got
                    raise McpError("the server closed the stream without an answer")
                payload = json.loads(r.read().decode("utf-8") or "{}")
                for m in (payload if isinstance(payload, list) else [payload]):
                    if isinstance(m, dict) and m.get("id") == want_id:
                        return m
                raise McpError("the server's answer did not match the request")

    def _sse_event(self, text: str, want_id: int) -> dict | None:
        try:
            m = json.loads(text)
        except ValueError:
            return None
        for one in (m if isinstance(m, list) else [m]):
            if not isinstance(one, dict):
                continue
            if "method" in one and "id" in one:
                reply = self.answer_server(one)
                if reply:
                    try:
                        self._post(reply, 30, None)
                    except Exception:
                        pass
            elif "method" in one:
                self.on_notification(one)
            elif one.get("id") == want_id:
                return one
        return None

    def request(self, method: str, params: dict | None, timeout: float) -> dict:
        import httpx
        for attempt in (1, 2):
            rid = self._next_id()
            body = {"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})}
            try:
                msg = self._post(body, timeout, rid) or {}
            except _SessionGone:
                if attempt == 2 or method == "initialize":
                    raise McpError("the server ended the session")
                self.handshake(timeout)            # セッションが切れた → 張り直して 1 度だけやり直す
                continue
            except httpx.TimeoutException as e:
                raise McpError(f"{method} did not answer within {timeout:g} s") from e
            except httpx.HTTPError as e:
                raise McpError(f"{type(e).__name__}: {e}") from e
            if msg.get("error"):
                raise _rpc_error(msg["error"])
            return msg.get("result") or {}
        raise McpError("unreachable")

    def notify(self, method: str, params: dict | None = None) -> None:
        try:
            self._post({"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})},
                       30, None)
        except McpError:
            pass

    def close(self) -> None:
        self.alive = False
        if self.session_id:
            try:
                import httpx
                httpx.delete(self.url, headers={**self.headers, "Mcp-Session-Id": self.session_id}, timeout=5)
            except Exception:
                pass


class _SessionGone(Exception):
    pass


# ── 接続の置き場（走行をまたいで使い回す）──────────────────────────────

_pool: dict[str, _Conn] = {}
_errors: dict[str, str] = {}
_failed: dict[str, tuple[str, float]] = {}     # {server: (設定の鍵, 時刻)}。失敗したばかりのサーバーで走行の開始を待たせない
_locks: dict[str, threading.Lock] = {}
_pool_lock = threading.Lock()


def _lock_for(name: str) -> threading.Lock:
    with _pool_lock:
        return _locks.setdefault(name, threading.Lock())


def connect(name: str, cfg: dict, timeout: float | None = None) -> _Conn:
    timeout = timeout or _num("LAPS_MCP_START_TIMEOUT", 90)
    with _lock_for(name):
        c = _pool.get(name)
        if c is not None and c.alive and c.key == _key(cfg):
            if c.stale:
                # ★「道具の一覧が変わった」は一覧を取り直すだけ。サーバーを起動し直さない（2026-10-08 オーナーの実機:
                #   Playwright はブラウザにつながると list_changed を送る。起動し直していたので、呼ぶたびに新しい接続が
                #   でき、拡張機能の接続のページが開いて、それが「今のタブ」になっていた）。
                try:
                    c.list_tools(timeout)
                    _save_cache(name, c)
                except Exception:
                    c.stale = False      # 取り直せなくても、前の一覧のまま使い続ける
            return c
        if c is not None:
            c.close()
        t = _transport(cfg)
        if t == "sse":
            raise McpError('the old SSE transport is not supported; use the server\'s Streamable HTTP endpoint '
                           '(usually …/mcp) with "type": "http"')
        c = _Http(name, cfg) if t == "http" else _Stdio(name, cfg)
        try:
            c.start()
            c.handshake(timeout)
        except Exception as e:
            tail = " / ".join(list(c.log)[-3:])
            c.close()
            msg = f"{e}" + (f" — server log: {tail}" if tail and tail not in str(e) else "")
            _errors[name] = msg
            _failed[name] = (c.key, time.time())
            raise McpError(msg) from e
        _pool[name] = c
        _errors.pop(name, None)
        _failed.pop(name, None)
        _save_cache(name, c)
        return c


def close_all() -> None:
    with _pool_lock:
        conns = list(_pool.values())
        _pool.clear()
    for c in conns:
        try:
            c.close()
        except Exception:
            pass


atexit.register(close_all)


def _load_cache() -> dict:
    try:
        d = json.loads(_cache_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_cache_lock = threading.Lock()


def _save_cache(name: str, c: _Conn) -> None:
    with _cache_lock:
        d = _load_cache()
        d[name] = {"key": c.key, "tools": c.tools or [], "server": c.server_info, "at": int(time.time())}
        try:
            p = _cache_path()
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass


def _warm(name: str, cfg: dict) -> None:
    def go() -> None:
        try:
            connect(name, cfg)
        except Exception:
            pass
    threading.Thread(target=go, name=f"mcp-warm-{name}", daemon=True).start()


# ── 道具にする ─────────────────────────────────────────────────────────

def _tool_index() -> tuple[dict[str, dict], dict[str, list[dict]], dict[str, str]]:
    """(設定, {server: tools}, {server: 理由})。控えがあれば控えを使い、無いものだけ今つなぐ（並列・期限つき）。"""
    cfgs = load_config()
    cache = _load_cache()
    tools: dict[str, list[dict]] = {}
    errs: dict[str, str] = {}
    need: list[str] = []
    for name, cfg in cfgs.items():
        if cfg.get("_error"):
            errs[name] = cfg["_error"]
            continue
        c = _pool.get(name)
        k = _key(cfg)
        if c is not None and c.alive and c.key == k and c.tools is not None:
            if c.stale:
                try:
                    c = connect(name, cfg)       # 一覧を取り直す（起動し直さない）
                except Exception:
                    pass
            tools[name] = c.tools or []
        elif isinstance(cache.get(name), dict) and cache[name].get("key") == k:
            tools[name] = cache[name].get("tools") or []
            if not cfg.get("_lazy"):
                _warm(name, cfg)
        elif name in _failed and _failed[name][0] == k and time.time() - _failed[name][1] < 600:
            errs[name] = _errors.get(name, "failed to start")      # 10 分は待たずに報告だけ（裏で 1 度やり直す）
            if time.time() - _failed[name][1] > 60 and not cfg.get("_lazy"):
                _warm(name, cfg)
        elif cfg.get("_lazy"):
            # ★同梱の REA など: 走行の開始では起動しない（控えも無い初回は tools にも errs にも入れない＝_waiting）。
            #   最初の Mcp の list(server=…) / describe / call で起動し、そのとき道具の一覧が控えに入る。
            continue
        else:
            need.append(name)
    if need:
        # ★走行の開始で待つのは LAPS_MCP_START_WAIT 秒まで（既定 30）。間に合わないものは裏で起動を続け、
        #   終われば控えに入る（次の走行から載る）。
        deadline = _num("LAPS_MCP_START_WAIT", 30)
        ths = []
        for name in need:
            def go(n: str = name) -> None:
                try:
                    tools[n] = connect(n, cfgs[n]).tools or []
                except Exception as e:  # noqa: BLE001
                    errs[n] = str(e)
            th = threading.Thread(target=go, name=f"mcp-start-{name}", daemon=True)
            th.start()
            ths.append(th)
        end = time.time() + deadline + 5
        for th in ths:
            th.join(max(0.1, end - time.time()))
        for n in need:
            if n not in tools and n not in errs:
                errs[n] = (f"still starting after {deadline:g} s (first runs of npx/uvx download packages); it keeps "
                           "starting in the background and its tools appear on the next request")
    return cfgs, tools, errs


def _waiting(cfgs: dict, tools: dict, errs: dict) -> list[str]:
    """起動を最初の呼び出しまで待っているサーバー（_lazy で、まだ道具の一覧を知らないもの）。"""
    return sorted(n for n, c in cfgs.items() if c.get("_lazy") and n not in tools and n not in errs)


def _ensure_started(server: str) -> None:
    """_lazy のサーバーをいま起動する（Mcp の list(server=…) / describe から）。失敗は _failed に残り、_tool_index が報告する。"""
    cfg = load_config().get(server)
    if not cfg or cfg.get("_error") or not cfg.get("_lazy"):
        return
    c = _pool.get(server)
    k = _key(cfg)
    if c is not None and c.alive and c.key == k:
        return
    # ★失敗したばかりなら起動し直さない（2026-11-03 レビュー）。node が固まる・初回に Defender が数千ファイルを
    #   調べるなどで起動に失敗した後、list / describe のたびに node を立て直して手番を最長 90 秒止めていた。
    #   60 秒は _tool_index が _failed の理由をそのまま報告する（_lazy でない側が裏でやり直すまでの間と同じ）。
    f = _failed.get(server)
    if f is not None and f[0] == k and time.time() - f[1] < 60:
        return
    try:
        connect(server, cfg)
    except Exception:
        pass


def _readonly_tool(t: dict) -> bool:
    return bool((t.get("annotations") or {}).get("readOnlyHint"))


# ★ページの内容を結果に入れる（2026-10-09）。Playwright MCP（0.0.83）は、開く・押すなどの操作の結果にページの
#   内容を入れず、作業場の `.playwright-mcp\page-*.yml` に書いて `### Snapshot` の下に `- [Snapshot](相対パス)`
#   だけを返す（内容が結果に入るのは browser_snapshot を明示したときだけ・playwright-core の response.ts）。
#   10/8 の実機（0.1.336・7 ページの巡回・827 秒）では 7 回とも次の手番で Read していた＝1 ページに 2 手番。
#   ここで中身を結果の末尾に足す（末尾なのは、道具の出力の上限で切れても `### Events` などを残すため。
#   上限を超えた分は今までどおり全文がファイルに残る）。
#   ★古いスナップショットを後から消すことはしない: 前の会話を書き換えると推論機のキャッシュが外れ、
#     そこから先を読み直す（0.1.318 の思考の切り詰めと同じ形）。
#   ★読むのは `### Snapshot` の直後の行が指す、サーバーの作業場の中の .yml だけ。ページの文面（`### Result`）に
#     偽のリンクがあっても、手元の別のファイルを黙って文脈に入れない。LAPS_MCP_INLINE_SNAPSHOT_KB=0 で止める。
_SNAPSHOT_LINK = re.compile(r"^(### Snapshot[ \t]*\r?\n)([ \t]*-[ \t]*)\[Snapshot\]\(([^)\r\n]+)\)[ \t]*$", re.M)


def _inline_snapshots(text: str, workdir: str | None) -> tuple[str, int]:
    """(結果の本文, 足したスナップショットの数)。"""
    try:
        limit_kb = int(os.environ.get("LAPS_MCP_INLINE_SNAPSHOT_KB") or 256)
    except ValueError:
        limit_kb = 256
    if limit_kb <= 0 or not workdir or "[Snapshot](" not in text:
        return text, 0
    try:
        base = Path(workdir).resolve()
    except OSError:
        return text, 0
    bodies: list[str] = []

    def sub(m: re.Match) -> str:
        raw = m.group(3).strip().replace("\\", "/")
        try:
            p = Path(raw)
            full = (p if p.is_absolute() else base / p).resolve()
            full.relative_to(base)
            if full.suffix.lower() not in (".yml", ".yaml") or not full.is_file():
                return m.group(0)
            size = full.stat().st_size
            if size > limit_kb * 1024:
                return (f"{m.group(1)}{m.group(2)}[Snapshot]({full}) — {size:,} bytes, too large to show here; "
                        "read parts of it with Read (offset/limit) or search it with Grep")
            body = full.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return m.group(0)
        bodies.append(body.rstrip())
        return f"{m.group(1)}{m.group(2)}The page contents are at the end of this result (also saved to {full})"

    out = _SNAPSHOT_LINK.sub(sub, text)
    for body in bodies:
        out += "\n\n### Snapshot contents\n```yaml\n" + body + "\n```"
    return out, len(bodies)


_ARG_WRAPPERS = ("arguments", "args", "input", "params", "parameters", "argument")


def _fill_choice_args(args: dict, missing: list[str], sch: dict) -> dict:
    """必須だが選ぶだけの引数（既定値あり・enum・真偽）が欠けていれば埋める。埋めた物を返す。

    ★2026-10-19 走行記録（10-12〜19）: browser_console_messages の level・browser_take_screenshot の scale・
      browser_tabs の action・browser_network_requests の static が空で 12 回弾かれた。小さいモデルは
      「任意に見える必須」を毎回落とすので、弾いてもう 1 往復させるより埋めて送る。
      url・function のような自由な値は埋めない（従来どおり形を返す）。"""
    props = sch.get("properties") or {}
    filled: dict = {}
    for k in missing:
        p = props.get(k)
        if not isinstance(p, dict):
            continue
        if "default" in p:
            filled[k] = p["default"]
        elif isinstance(p.get("enum"), list) and p["enum"]:
            filled[k] = p["enum"][0]
        elif p.get("type") == "boolean":
            filled[k] = False
    args.update(filled)
    return filled


def _normalize_args(arguments: Any, tool: str, sch: dict) -> tuple[dict, str]:
    """モデルが包んだ・文字列にした引数をほどく（2026-10-13 オーナーの走行: browser_navigate を
    arguments={"browser_navigate": {"url": …}} で呼び、「url が無い」で弾かれた）。(引数, ほどいた説明)。
    ほどくのは、唯一の鍵が道具の引数の名前ではなく、値が辞書で、鍵が道具の名前か包みの名前（arguments 等）のときだけ。"""
    args: Any = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    if not isinstance(args, dict):
        return {}, ""
    props = set((sch.get("properties") or {}).keys())
    short = tool.split("_", 1)[-1] if "_" in tool else tool
    how = ""
    for _ in range(2):
        if len(args) != 1:
            break
        (k, v), = args.items()
        if k in props:
            break
        if isinstance(v, str) and v.strip().startswith("{"):
            try:
                v = json.loads(v)
            except ValueError:
                break
        if not isinstance(v, dict):
            break
        if k == tool or k == short or k.lower() in _ARG_WRAPPERS:
            how = k
            args = v
            continue
        break
    return args, how


def find_server(tool: str) -> str:
    """その名前の道具を持つサーバー（1 つだけのとき）。無い・2 つ以上なら ""。"""
    try:
        _, ts, _ = _tool_index()
    except Exception:
        return ""
    hits = [srv for srv, tl in ts.items() if any(t.get("name") == tool for t in tl or [])]
    return hits[0] if len(hits) == 1 else ""


def lift_call(inp: dict) -> tuple[dict, str]:
    """Mcp(action=call) の引数の取り違えを直す（2026-10-14 オーナーの走行: 4 回続けて弾かれた）。(直した入力, 直した説明)。
    ・server と tool を arguments / input などの包みの中に書いた → 外へ持ち上げ、残りを arguments にする
    ・tool を name / tool_name に書いた・"server/tool" の形で書いた
    ・tool だけで server が無い → その道具を持つサーバーが 1 つならそれ"""
    inp = dict(inp)
    how = []
    for wrap in ("input", "arguments", "args", "params", "parameters", "call"):
        inner = inp.get(wrap)
        if isinstance(inner, str) and inner.strip().startswith("{"):
            try:
                inner = json.loads(inner)
            except ValueError:
                inner = None
        if (inp.get("server") and inp.get("tool")) or not isinstance(inner, dict):
            continue
        if not any(k in inner for k in ("server", "tool", "tool_name", "name")):
            continue
        inp.pop(wrap, None)
        for k in ("server", "tool"):
            if not inp.get(k) and inner.get(k):
                inp[k] = inner[k]
        if not inp.get("tool") and (inner.get("tool_name") or inner.get("name")):
            inp["tool"] = inner.get("tool_name") or inner.get("name")
        rest = {k: v for k, v in inner.items() if k not in ("server", "tool", "tool_name", "name", "action")}
        if isinstance(rest.get("arguments"), dict):
            args = dict(rest.pop("arguments"))
            args.update(rest)
            rest = args
        if rest and not inp.get("arguments"):
            inp["arguments"] = rest
        how.append(f"server/tool were inside '{wrap}'")
        break
    if not inp.get("tool") and (inp.get("tool_name") or inp.get("name")):
        inp["tool"] = inp.pop("tool_name", None) or inp.pop("name", None)
    tool = str(inp.get("tool") or "")
    if "/" in tool and not inp.get("server"):
        inp["server"], inp["tool"] = tool.split("/", 1)
        how.append("tool was given as server/tool")
    if inp.get("tool") and not inp.get("server"):
        srv = find_server(str(inp["tool"]))
        if srv:
            inp["server"] = srv
            how.append(f"server '{srv}' was filled in")
    return inp, "; ".join(how)


def call_tool(server: str, tool: str, arguments: Any) -> ToolResult:
    cfgs = load_config()
    cfg = cfgs.get(server)
    if cfg is None or cfg.get("_error"):
        return ToolResult(f"Error: MCP server '{server}' is not configured (known: {', '.join(sorted(cfgs)) or 'none'})",
                          ok=False, kind="error")
    try:
        conn = connect(server, cfg)
    except McpError as e:
        return ToolResult(f"Error: MCP server '{server}' could not start: {e}", ok=False, kind="error")
    tdef = next((t for t in (conn.tools or []) if t.get("name") == tool), None)
    if tdef is None:
        names = ", ".join(t.get("name", "") for t in (conn.tools or [])[:40])
        return ToolResult(f"Error: '{server}' has no tool '{tool}' (tools: {names})", ok=False, kind="error")
    try:
        from .. import policy
        if policy._mode() == "readonly" and not _readonly_tool(tdef):
            return ToolResult(f"Error: the read-only policy allows only MCP tools marked read-only ({server}/{tool} is not)",
                              ok=False, kind="denied")
    except Exception:
        pass
    # ★必須の引数が欠けていれば、サーバーへ送らずに引数の形を返す（2026-10-10: オーナーの走行で browser_click を
    #   describe せずに推測の引数 {"id": …, "window": …} で 7 回呼び、毎回「target が無い」とだけ返っていた）。
    sch = _schema_of(tdef)
    args, _unwrapped = _normalize_args(arguments, tool, sch)
    missing = [k for k in (sch.get("required") or []) if isinstance(k, str) and k not in args]
    _filled = _fill_choice_args(args, missing, sch)
    missing = [k for k in missing if k not in _filled]
    if missing:
        return ToolResult(f"Error: {server}/{tool} needs argument(s) {', '.join(missing)}; the call was not sent.\n"
                          + _args_brief(sch), ok=False, kind="error")
    _arg_note = (f"\n\n(Note: the arguments were wrapped in '{_unwrapped}' and were unwrapped. Pass the tool's parameters "
                 "directly in arguments next time, e.g. arguments={\"url\": ...}.)" if _unwrapped else "")
    if _filled:
        _arg_note += ("\n\n(Note: required argument(s) were filled in: "
                      + ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in _filled.items()) + ".)")
    t0 = time.time()
    try:
        r = conn.request("tools/call", {"name": tool, "arguments": args}, _num("LAPS_MCP_TIMEOUT", 180))
    except McpError as e:
        return ToolResult(f"Error: {server}/{tool}: {e}", ok=False, kind="error")
    parts: list[str] = []
    data: dict | None = None
    for c in r.get("content") or []:
        if not isinstance(c, dict):
            continue
        typ = c.get("type")
        if typ == "text":
            parts.append(str(c.get("text") or ""))
        elif typ == "image" and c.get("data"):
            mime = str(c.get("mimeType") or "image/png")
            if data is None:
                data = {"image": {"path": f"{server}/{tool}", "mime": mime, "b64": c["data"]}}
            parts.append(f"[image {mime}]")
        elif typ == "audio":
            parts.append(f"[audio {c.get('mimeType', '')} — not shown]")
        elif typ == "resource":
            res = c.get("resource") or {}
            parts.append(str(res.get("text")) if res.get("text") is not None
                         else f"[resource {res.get('uri', '')} {res.get('mimeType', '')} — binary, not shown]")
        elif typ == "resource_link":
            parts.append(f"[link {c.get('uri', '')} {c.get('name', '')}]".strip())
    if not any(p.strip() for p in parts) and r.get("structuredContent") is not None:
        parts.append(json.dumps(r["structuredContent"], ensure_ascii=False, indent=1))
    text = "\n".join(parts).strip() or "(the tool returned nothing)"
    is_err = bool(r.get("isError"))
    if not is_err:
        text, n_snap = _inline_snapshots(text, getattr(conn, "workdir", None))
        if n_snap:
            data = dict(data or {})
            data["snapshot_inlined"] = n_snap
    try:
        from .. import policy
        policy.audit("mcp", {"server": server, "tool": tool, "ok": not is_err, "secs": round(time.time() - t0, 1)}, "allow")
    except Exception:
        pass
    if is_err and re.search(r"invalid (arguments|input|params)", text, re.I):
        text += "\n" + _args_brief(sch)
    return ToolResult(("Error: " if is_err and not text.startswith("Error") else "") + text + _arg_note,
                      ok=not is_err, kind="error" if is_err else "ok", data=data)


def _args_brief(sch: dict) -> str:
    """引数の形を 1 行ずつ（名前・型・必須・説明の頭）。間違えた呼び出しの直後に見せる。"""
    props = sch.get("properties") if isinstance(sch.get("properties"), dict) else {}
    req = {k for k in (sch.get("required") or []) if isinstance(k, str)}
    if not props:
        return "Arguments: none."
    rows = []
    for k, v in list(props.items())[:20]:
        v = v if isinstance(v, dict) else {}
        typ = v.get("type") or ("enum" if v.get("enum") else "any")
        if isinstance(typ, list):
            typ = "|".join(map(str, typ))
        enum = f" one of {v['enum'][:8]}" if isinstance(v.get("enum"), list) else ""
        rows.append(f"- {k} ({typ}{', required' if k in req else ''}){enum}: {_one_line(v.get('description') or '', 90)}")
    return "Arguments of this tool:\n" + "\n".join(rows)


def _direct_name(server: str, tool: str, taken: set[str]) -> str:
    base = f"mcp__{server}__{_safe(tool)}"
    if len(base) > 64:
        base = base[:55] + "_" + hashlib.sha1(base.encode()).hexdigest()[:8]
    name, i = base, 2
    while name in taken:
        name = f"{base[:60]}_{i}"
        i += 1
    return name


def _schema_of(t: dict) -> dict:
    s = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {}
    s = {k: v for k, v in s.items() if k not in ("$schema", "$id")}
    s.setdefault("type", "object")
    s.setdefault("properties", {})
    return s


def _one_line(desc: str, limit: int = 110) -> str:
    d = " ".join(str(desc or "").split())
    return d if len(d) <= limit else d[:limit].rstrip() + "…"


META_DESCRIPTION = """Use tools from the MCP servers (connectors) the user configured.
- action="list": the servers and their tools (one line each). Start here.
- action="describe", server, tool: the tool's full description and argument schema. Read it before the first call.
- action="call", server, tool, arguments={...}: run the tool.
MCP tools act on real services (the user's GitHub, Notion, files, …). Before a call that changes or deletes something the user did not clearly ask for, say what you will do and ask."""


def _is_browser(ts: list[dict]) -> bool:
    return sum(1 for t in ts if str(t.get("name", "")).startswith("browser_")) >= 3


def meta_description(cfgs: dict, tools: dict, errs: dict) -> str:
    """まとめた道具（Mcp）の説明。★つながっているサーバーと道具の名前を載せる（2026-10-08 オーナーの実機:
    「Chrome で開いて読んで」に、モデルは Mcp の中に Playwright があると知らず Desktop で Edge を起動した）。"""
    lines = [META_DESCRIPTION, "", "Connected servers (server: tools):"]
    for s, ts in sorted(tools.items()):
        names = [str(t.get("name")) for t in ts]
        shown = ", ".join(names[:40]) + (f", … ({len(names)} tools)" if len(names) > 40 else "")
        lines.append(f"- {s}: {shown}")
        if _is_browser(ts):
            lines.append(f"  '{s}' controls the user's own web browser (Chrome/Edge tabs, logged-in sessions): open pages, "
                         "read them, click, type, fill forms, take screenshots. For anything the user asks to do in "
                         f"Chrome, Edge or \"the browser\", use server '{s}' here — not Desktop, and not WebFetch "
                         "(WebFetch only downloads a public page as text).")
    for s in _waiting(cfgs, tools, errs):
        lines.append(f"- {s}: {_waiting_note(s, cfgs[s])}")
    for s, e in sorted(errs.items()):
        if not s.startswith("!"):
            lines.append(f"- {s}: not available ({str(e)[:160]})")
    return "\n".join(lines)


def _waiting_note(s: str, cfg: dict) -> str:
    if s == "rea":
        # ★REA は道具が 229 本・一覧だけで 16KB。始める道具は Tools の説明に書いてある（engine/tools/ext.py _rea_hint）ので、
        #   一覧を引かずに直接呼べると伝える（2026-11-04・11-03 の試験で序盤の数手を使い方探しに使った）。
        info = rea_info() or {"native": True}
        first = ("open_binary(path=<absolute path>)" if info["native"]
                 else "inspect_sqlite_database / inspect_il2cpp_metadata / inspect_managed_artifact / inspect_apk_signature")
        return (f"not started yet (reverse-engineering server REA, 229 tools); it starts on first use. Call a tool directly, "
                f"e.g. {first} with Mcp(action=\"call\", server=\"rea\", tool=..., arguments={{...}}); "
                "Mcp(action=\"describe\", server=\"rea\", tool=...) shows one tool's arguments")
    return (f"not started yet; it starts on first use — call Mcp(action=\"list\", server=\"{s}\") "
            "to start it and see its tools")


def specs(add) -> None:
    """oc/tools.py の specs() から呼ぶ。設定が無ければ何も載せない。"""
    if not enabled():
        return
    if not config_path().exists() and os.environ.get("LAPS_MCP_PROJECT") != "1" and _builtin_rea() is None:
        return
    # ★結果は他の道具と同じ上限で切る（超えた分はファイルに残す）。Playwright のページの読み取りは数十〜数百 KB になり、
    #   切らないと 64K の文脈を 1 回で溢れさせる（0.1.332〜333 は切っていなかった）。
    from .tools import _wrap_truncating
    cfgs, tools, errs = _tool_index()
    if not cfgs:
        return
    # ★_lazy のサーバー（同梱の REA）は直接には載せず、いつも Mcp を通す。直接か束ねるかは利用者が設定した
    #   サーバーの道具の数だけで決める（2026-11-03 レビュー: REA の 229 本を数に入れると、REA を使わない人の
    #   3〜5 本の小さなサーバーまで mcp__<server>__<tool> から Mcp の中へ移ってしまっていた）。
    grouped = {n for n, c in cfgs.items() if c.get("_lazy")}
    total = sum(len(v) for n, v in tools.items() if n not in grouped)
    max_direct = int(_num("LAPS_MCP_MAX_DIRECT", 12))
    waiting = _waiting(cfgs, tools, errs)
    if 0 < total <= max_direct:
        taken: set[str] = set()
        for server, ts in sorted(tools.items()):
            if server in grouped:
                continue
            label = cfgs[server].get("_name") or server
            for t in ts:
                nm = _direct_name(server, str(t["name"]), taken)
                taken.add(nm)
                desc = f"[MCP: {label}] {str(t.get('description') or t.get('title') or t['name'])[:1200]}"
                if _is_browser(ts) and str(t["name"]) == "browser_navigate":
                    desc += (" Use these browser tools for anything the user asks to do in Chrome, Edge or the browser "
                             "(their own logged-in tabs) — not Desktop.")

                def h(inp: dict, _s: str = server, _t: str = str(t["name"])) -> ToolResult:
                    return call_tool(_s, _t, inp)
                add(ToolSpec(name=nm, description=desc, schema=_schema_of(t), handler=_wrap_truncating(h),
                             readonly=_readonly_tool(t), executes=True))
        # 束ねる側（REA）が無ければ Mcp は載せない（従来どおり）。あれば Mcp の説明はその分だけにする。
        if not any(n in grouped for n in (*tools, *errs, *waiting)):
            return
        d_cfgs = {n: c for n, c in cfgs.items() if n in grouped}
        d_tools = {n: v for n, v in tools.items() if n in grouped}
        d_errs = {n: e for n, e in errs.items() if n in grouped}
    else:
        if total == 0 and not errs and not waiting and not any(n in grouped for n in tools):
            return
        d_cfgs, d_tools, d_errs = cfgs, tools, errs

    def meta(inp: dict) -> ToolResult:
        inp = inp if isinstance(inp, dict) else {}
        _lifted = ""
        # ★2026-10-19 走行記録: action に道具の名前を書いた（action="browser_navigate", url=…）→ call として扱う
        _a0 = str(inp.get("action") or "")
        if _a0 and _a0.lower() not in ("list", "describe", "call") and not inp.get("tool"):
            if "/" in _a0 or find_server(_a0):
                inp = {**inp, "action": "call", "tool": _a0}
                _lifted = "the tool name was given as action"
        if str(inp.get("action") or "").lower() in ("call", "describe") or (not inp.get("action") and inp.get("tool")):
            inp, _how = lift_call(inp)
            _lifted = "; ".join(x for x in (_lifted, _how) if x)
            if not inp.get("action"):
                inp["action"] = "call"
        act = str(inp.get("action") or "list").lower()
        want = _safe(inp.get("server")) if inp.get("server") else ""
        if want and act in ("list", "describe"):
            _ensure_started(want)        # ★_lazy のサーバー（同梱の REA）はここで初めて起動する
        if act == "list":
            cf, ts, er = _tool_index()
            wt = set(_waiting(cf, ts, er))
            lines = []
            for s in sorted(set(cf) | set(ts)):
                if s.startswith("!") or (want and s != want and want in cf):
                    continue
                if s in wt:
                    lines.append(f"## {s}: {_waiting_note(s, cf[s])}")
                if s in ts:
                    lines.append(f"## {s} ({len(ts[s])} tools)")
                    lines += [f"- {t['name']}{' (read-only)' if _readonly_tool(t) else ''}: "
                              f"{_one_line(t.get('description') or t.get('title') or '')}" for t in ts[s]]
                if s in er:
                    lines.append(f"## {s}: not available — {er[s]}")
            for s, e in er.items():
                if s.startswith("!"):
                    lines.append(f"(config) {e}")
            lines.append(f"(settings file: {config_path()})")
            bi = [s for s, c in cf.items() if c.get("_source") == "built-in" and (not want or s == want)]
            if bi:
                lines.append(f"(built into L.A.P.S, not from the settings file: {', '.join(sorted(bi))})")
            return ToolResult("\n".join(lines), ok=True)
        server, tool = _safe(inp.get("server") or ""), str(inp.get("tool") or "")
        if act == "describe":
            _, ts, er = _tool_index()
            t = next((x for x in ts.get(server, []) if x.get("name") == tool), None)
            if t is None:
                return ToolResult(f"Error: no tool '{tool}' on '{server}'" + (f" ({er[server]})" if server in er else "")
                                  + ". Use action=list.", ok=False, kind="error")
            return ToolResult(f"{server}/{tool}{' (read-only)' if _readonly_tool(t) else ''}\n\n"
                              f"{t.get('description') or ''}\n\nArguments (JSON schema):\n"
                              + json.dumps(_schema_of(t), ensure_ascii=False, indent=1), ok=True)
        if act == "call":
            if not server or not tool:
                return ToolResult("Error: call needs server and tool (see action=list).", ok=False, kind="error")
            _a = inp.get("arguments")
            if not _a:
                # arguments を使わずに、呼び出しの一番上に引数を書いた形（action・server・tool 以外の鍵）も受ける。
                _a = {k: v for k, v in inp.items() if k not in ("action", "server", "tool", "arguments")}
            r = call_tool(server, tool, _a or {})
            if _lifted:
                r = ToolResult(str(r) + f"\n\n(Note: {_lifted}. Next time call Mcp(action=\"call\", server=\"{server}\", "
                               f"tool=\"{tool}\", arguments={{...}}).)", ok=r.ok, kind=r.kind, data=r.data)
            return r
        return ToolResult("Error: action must be list, describe or call.", ok=False, kind="error")

    add(ToolSpec(name="Mcp", description=meta_description(d_cfgs, d_tools, d_errs), handler=_wrap_truncating(meta),
                 executes=True, schema={
        "type": "object", "required": ["action"],
        "properties": {
            "action": {"type": "string", "enum": ["list", "describe", "call"]},
            "server": {"type": "string", "description": "The MCP server name from action=list"},
            "tool": {"type": "string", "description": "The tool name on that server"},
            "arguments": {"type": "object", "description": "The tool's arguments (see action=describe)"},
        }}))


# ── 命令行（アプリの設定画面から使う）──────────────────────────────────
#   python -m laps.oc.mcp status        → 各サーバーにつないで JSON（名前・方式・成否・道具・理由）
#   python -m laps.oc.mcp get           → mcp.json の中身（無ければ雛形）
#   python -m laps.oc.mcp set           → 標準入力の JSON を検めて mcp.json に書く

TEMPLATE = {"mcpServers": {}}


def _status() -> list[dict]:
    cfgs = load_config()
    out: list[dict] = []
    for name, cfg in cfgs.items():
        if cfg.get("_error"):
            out.append({"name": name, "ok": False, "error": cfg["_error"]})
            continue
        row = {"name": cfg.get("_name") or name, "transport": _transport(cfg), "ok": False}
        if cfg.get("_source") == "built-in":
            row["source"] = "built-in"
        try:
            c = connect(name, cfg)
            row.update(ok=True, tools=[t.get("name") for t in (c.tools or [])],
                       server=f"{c.server_info.get('name', '')} {c.server_info.get('version', '')}".strip())
        except Exception as e:  # noqa: BLE001
            row["error"] = str(e)
        out.append(row)
    close_all()
    return out


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "get":
        p = config_path()
        print(p.read_text(encoding="utf-8-sig") if p.exists() else json.dumps(TEMPLATE, indent=2))
        return 0
    if cmd == "set":
        text = os.environ.get("LAPS_MCP_JSON")
        if text is None:
            text = sys.stdin.read()
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or not isinstance(data.get("mcpServers", data.get("servers", {})), dict):
                raise ValueError('the top level must be {"mcpServers": {...}}')
        except ValueError as e:
            print(json.dumps({"error": f"JSON として読めません: {e}"}, ensure_ascii=False))
            return 1
        p = config_path()
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
        print(json.dumps({"saved": str(p)}, ensure_ascii=False))
        return 0
    if cmd == "status":
        print(json.dumps(_status(), ensure_ascii=False))
        return 0
    print("usage: status | get | set", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
