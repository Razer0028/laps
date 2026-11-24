"""Blackwall の道具を MCP サーバとして渡す（stdio）。Claude Code・Claude デスクトップから使う。

2026-10-10 オーナー「Blackwall と Claude サブスクリプションの連携」→ サブスクの枠を Blackwall の推論機に使うのは
規約上やらない（Anthropic は第三者のアプリでサブスクの枠を使わないよう案内している）。向きを逆にして、
**Claude（公式アプリ）が Blackwall の道具を呼ぶ**形にした。「全セッションに共有して使えるようにする」。

渡す道具は 4 つ（道具は少ないほど選び間違いが減る・ai-agent-research-notes）。上の 3 つは読むだけで、何も書き換えない:
  inspect_file  ファイルの構造（Unity・動画・音声・書庫・実行ファイル…）・16 進・中身の地図・2 つのファイルの比較
  find_bytes    バイナリの中を文字列か 16 進で探す
  desktop       Blackwall のアプリが動いている Windows の画面を読み・押す・打つ（2026-10-14 オーナー「B で進めて」）。
                中継を通してその機械のゲートウェイ（ログイン中の画面のセッションで動く・remotectl.desktop）が行う
                （SSH の命令は Session 0 で動き窓に届かない）。★相手は毎回 machine で名指しする（既定の相手は置かない＝
                MCP を共有する別のセッションが、うっかり人の画面を触らない）。Windows の上で動かしたときだけ、名指しが
                無ければその機械の画面を直接触る。

使い方（Claude Code）:
  claude mcp add --scope user blackwall -- <python> -m laps.mcpserver
  （開発機 は /opt/blackwall-mcp/bin/blackwall-mcp。別の機械からは ssh 越しでもよい: -- ssh bw /opt/blackwall-mcp/bin/blackwall-mcp）

★標準出力は JSON-RPC 専用。道具の中の print は標準エラーへ回す（混ざるとクライアントが接続を切る）。
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import traceback

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("laps")
    except Exception:  # noqa: BLE001
        return "dev"


TOOLS = [
    {
        "name": "inspect_file",
        "description": (
            "Inspect a binary file on the machine this server runs on. mode='structure' (default) returns the "
            "internal structure for many formats: Unity bundles (UnityFS, per-block compression, contained files) "
            "and .assets (object counts per type, whether a type tree is present, MonoBehaviours per script), "
            "ZIP/7z/RAR/CAB/MSI/ISO/tar/gzip, Windows/Linux executables, MP4/MOV, MP3, FLAC, Ogg, WAV, fonts, PSD, "
            "PNG chunks with CRC, DDS/KTX, glTF/FBX, Live2D .moc3, Wwise/CRI/FMOD audio banks, Ren'Py/Kirikiri/"
            "RPG Maker/Godot/Unreal archives, .pyc, Java class, WebAssembly; otherwise size, magic, strings and "
            "entropy. mode='hex': hex dump at byte_offset (byte_length <= 4096) with little/big-endian integer and "
            "float values computed. mode='map': what each region of a big file looks like (text, fill, structured, "
            "compressed/encrypted) and which embedded formats start where. mode='compare': where this file and "
            "compare_with differ. mode='find' (with find=<text>): Unity bundle/.assets/_Data folder — GameObjects "
            "with components, MonoBehaviours by script class, MonoScripts and named assets matching the text, with "
            "path_ids; .NET DLL — types with fields in declaration order (and whether Unity serializes them) and "
            "methods. mode='object' (with path_id): one Unity object in detail (components, parent/children, script "
            "class, strings and references in its data, hex). Read-only."),
        "inputSchema": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Absolute path of the file"},
            "mode": {"type": "string", "enum": ["structure", "hex", "map", "compare", "find", "object"]},
            "find": {"type": "string", "description": "find: text to look for (a|b for several)"},
            "path_id": {"type": "integer", "description": "object: the Unity object's path_id"},
            "byte_offset": {"type": "integer", "description": "hex: start offset (negative counts from the end)"},
            "byte_length": {"type": "integer", "description": "hex: number of bytes (default 256, max 4096)"},
            "compare_with": {"type": "string", "description": "compare: absolute path of the second file"},
        }},
    },
    {
        "name": "find_bytes",
        "description": (
            "Search raw bytes inside files, including binaries, on the machine this server runs on. pattern is "
            "literal text (matched as UTF-8 and UTF-16LE), hex written as hex:4d4f4333, or \\x escapes. Returns "
            "byte offsets with a hex dump around the first matches (the rest one line each) and the values right after them. Do not convert text "
            "to hex by hand. Read-only."),
        "inputSchema": {"type": "object", "required": ["pattern", "path"], "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or folder to search"},
            "glob": {"type": "string", "description": "Only files matching this name pattern (e.g. '*.assets')"},
            "ignore_case": {"type": "boolean", "description": "ASCII case-insensitive (text patterns)"},
        }},
    },
]


def _desktop_tool() -> dict:
    """desktop の定義（Desktop 道具の説明と引数をそのまま使う・写さない）＋ machine と machines。"""
    from .oc import desktop as dk
    schema = json.loads(json.dumps(dk.schema()))
    schema["properties"]["action"]["enum"] = ["machines"] + list(schema["properties"]["action"]["enum"])
    schema["properties"]["machine"] = {
        "type": "string", "description": "The machine to operate: its name (e.g. MY-PC) or relay id. "
        "Required for every action except machines (list them with action='machines')."}
    desc = ("Operate the Windows desktop of a machine that runs the Blackwall app, through the Blackwall relay. "
            "This is a real person's screen: read before acting, and only click or type what the user asked for. "
            "Name the machine in every call (machine=...); action='machines' lists the machines on the relay.\n\n"
            + dk.DESKTOP_DESCRIPTION.split("\n", 1)[1].lstrip()
            .replace("Use Bash for files and command-line programs; use this tool only for things that need the "
                     "graphical interface.", "Use this tool only for things that need the graphical interface."))
    return {"name": "desktop", "description": desc, "inputSchema": schema}


def _desktop(args: dict) -> tuple:
    from . import ctlclient as cc
    a = dict(args or {})
    action = str(a.get("action") or "").strip().lower()
    machine = str(a.pop("machine", "") or "").strip()
    if action == "machines":
        try:
            rows = cc.devices()
        except cc.CtlClientError as e:
            return str(e), True
        lines = [f"- {r.get('name')} (id {r.get('id')}, {r.get('os') or '?'}, Blackwall {r.get('version') or '?'}, "
                 f"{'online' if r.get('online') else 'offline'})" for r in rows]
        return "Machines on the relay " + cc.relay_url() + ":\n" + ("\n".join(lines) or "(none)"), False
    if not machine:
        from .oc import desktop as dk
        if sys.platform == "win32" and dk.available():
            r = dk.desktop_tool(a)
            img = ((getattr(r, "data", None) or {}).get("image") or {}) if isinstance(getattr(r, "data", None), dict) else {}
            extra = [{"type": "image", "data": img["b64"], "mimeType": img.get("mime") or "image/png"}] \
                if img.get("b64") else []
            return str(r), not getattr(r, "ok", True), extra
        return "Name the machine to operate with machine=... (action='machines' lists them).", True
    try:
        dev = cc.resolve(machine)
        # 初回は相手の機械で補助を組み立てる（最大 180 秒・oc/desktop._BUILD_TIMEOUT）。
        ans = cc.call(str(dev["id"]), "desktop", a, timeout=240.0)
    except cc.CtlClientError as e:
        return f"{machine}: {e}", True
    if not ans.get("ok"):
        err = str(ans.get("error") or "error")
        # 0.1.497 からゲートウェイが自分で受ける（remotectl.desktop）。それより古いゲートウェイは知らない操作をアプリへ回し、
        # unknown_op・app_not_running のような、画面操作とは関係の無い誤りになる。
        if err not in ("disabled", "unavailable", "internal", "timeout"):
            return (f"{dev.get('name')}: the Blackwall on that machine (version {dev.get('version') or '?'}) cannot "
                    f"operate the desktop yet; it needs 0.1.497 or later. ({err})"), True
        return f"{dev.get('name')}: {err}: {ans.get('message') or ''}", True
    res = ans.get("result") or {}
    img = res.get("image") if isinstance(res.get("image"), dict) else {}
    # ★look の写真（0.1.547〜のゲートウェイが添える）は画像として返す＝Claude Code からも窓が見える。
    extra = [{"type": "image", "data": img["b64"], "mimeType": img.get("mime") or "image/png"}] \
        if img.get("b64") else []
    return f"[{dev.get('name')}] " + str(res.get("text") or ""), not res.get("ok", True), extra


def _structure(path: str) -> tuple[str, bool]:
    if os.path.isdir(path):
        return f"{path} is a folder. Give a file path.", True
    if not os.path.exists(path):
        return f"Not found: {path}", True
    with open(path, "rb") as fh:
        head = fh.read(4096)
    from .oc import fileformats
    rows = None
    try:
        rows = fileformats.describe(path, head)
        if rows is None and head.startswith(b"\x89PNG"):
            rows = fileformats.png(path, os.path.getsize(path))
    except Exception as e:  # noqa: BLE001
        rows = [f"構造を読む途中で止まりました: {type(e).__name__}: {e}"]
    size = os.path.getsize(path)
    if rows:
        return (f"{path}\n大きさ: {size:,} バイト\n" + "\n".join(rows)
                + f"\n先頭 32 バイト: {head[:32].hex(' ')}"), False
    from pathlib import Path
    from .tools import read as _old
    if b"\x00" not in head[:4096] and head:
        try:
            head.decode("utf-8")
            return f"{path} はテキストのファイルです（{size:,} バイト）。Claude の Read で読んでください。", False
        except UnicodeDecodeError:
            pass
    return f"{path}\n" + _old._binary_facts(Path(path), head), False


def _call(name: str, args: dict) -> tuple:
    if name == "inspect_file":
        path = str(args.get("path") or "")
        mode = str(args.get("mode") or "structure").lower()
        from .oc import bytesview as bv
        if mode == "hex":
            r = bv.read_bytes(path, args.get("byte_offset"), args.get("byte_length"))
        elif mode == "map":
            r = bv.map_file(path)
        elif mode == "compare":
            other = str(args.get("compare_with") or "")
            if not other:
                return "compare_with is required for mode='compare'", True
            r = bv.compare_files(path, other)
        elif mode in ("find", "object"):                # ★Unity のオブジェクト・.NET の型（oc/unityobjects.py・2026-10-10）
            if not os.path.exists(path):
                return f"Not found: {path}", True
            from .oc import unityobjects as uo
            want = {"find": args.get("find")} if mode == "find" else {
                "path_id": args.get("path_id"), "byte_offset": args.get("byte_offset"),
                "byte_length": args.get("byte_length")}
            try:
                return "\n".join(uo.lookup(path, want)), False
            except (ValueError, OSError) as e:
                return f"{path}: {e}", True
        else:
            return _structure(path)
        return str(r), not getattr(r, "ok", True)
    if name == "find_bytes":
        from .oc import bytesview as bv
        path = str(args.get("path") or "")
        if not os.path.exists(path):
            return f"Not found: {path}", True
        r = bv.find_bytes(path, str(args.get("pattern") or ""), args.get("glob"), bool(args.get("ignore_case")))
        return str(r), not getattr(r, "ok", True)
    if name == "desktop":
        return _desktop(args)
    return f"Unknown tool: {name}", True


def _handle(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:                      # 通知（notifications/initialized など）には返さない
        return None
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "blackwall", "version": _version()},
            "instructions": ("L.A.P.S tools: inspect_file and find_bytes (binary structure, hex, map, compare, "
                             "byte search) are read-only. desktop operates the Windows screen of a machine that "
                             "runs the L.A.P.S app (a real person's screen; name the machine every time).")}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        tools = list(TOOLS)
        try:
            tools.append(_desktop_tool())
        except Exception:  # noqa: BLE001 — 定義が組めなくても他の道具は渡す
            traceback.print_exc(file=sys.stderr)
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tools}}
    if method == "tools/call":
        p = msg.get("params") or {}
        if not isinstance(p, dict):                # 形の違う要求で、下の except の中でもう一度落ちて接続ごと終わらない
            p = {}
        try:
            with contextlib.redirect_stdout(sys.stderr):
                res = _call(str(p.get("name") or ""), p.get("arguments") or {})
            text, is_err = res[0], res[1]
            extra = list(res[2]) if len(res) > 2 else []      # desktop の look の写真など
        except Exception as e:  # noqa: BLE001 — 道具の不具合で接続を落とさない
            traceback.print_exc(file=sys.stderr)
            text, is_err, extra = f"Internal error in {p.get('name')}: {type(e).__name__}: {e}", True, []
        return {"jsonrpc": "2.0", "id": mid,
                "result": {"content": [{"type": "text", "text": text}] + extra, "isError": bool(is_err)}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}


#: 出荷で venv の中身が入れ替わったら、新しい版で起動し直す。読みかけの要求はこの環境変数で引き継ぐ。
#: ★2026-11-16: 11-14 の改名の後、それより前から動いていた MCP サーバ（セッション 7 本分）は desktop を呼ぶと
#:   「cannot import name 'ctlclient' from 'blackwall'」で落ちた。古いコードが、もう無い場所から部品を後で読もうとしたため。
_RESUME_ENV = "LAPS_MCP_RESUME"
_RESUME_MAX = 100_000


def _restart_if_replaced(start: str, pending: bytes) -> None:
    now = _version()
    if start == "dev" or now in ("dev", start):
        return
    import base64
    import tempfile
    import time
    time.sleep(1.0)                      # 入れ替えの途中で起動しない
    data = base64.b64encode(pending).decode()
    if len(data) > _RESUME_MAX:          # 環境変数に入らない大きさはファイルで渡す
        fd, path = tempfile.mkstemp(prefix="laps-mcp-")
        with os.fdopen(fd, "wb") as f:
            f.write(pending)
        data = "@" + path
    os.environ[_RESUME_ENV] = data
    print(f"[mcp] the installed version changed ({start} -> {now}); restarting", file=sys.stderr, flush=True)
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, *sys.argv])


def _resumed() -> bytes:
    raw = os.environ.pop(_RESUME_ENV, "")
    if not raw:
        return b""
    if raw.startswith("@"):
        try:
            with open(raw[1:], "rb") as f:
                data = f.read()
            os.unlink(raw[1:])
            return data
        except OSError:
            return b""
    import base64
    try:
        return base64.b64decode(raw)
    except ValueError:
        return b""


def _respond(out, line: bytes) -> None:
    try:
        msg = json.loads(line)
    except ValueError:
        resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    else:
        resp = _handle(msg) if isinstance(msg, dict) else {
            "jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    if resp is not None:
        out.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
        out.flush()


def main() -> int:
    out = sys.stdout.buffer
    start = _version()
    # ★stdin は自分で行に分ける（Python のバッファに先読みされた分は、起動し直すと失われるため）
    fd = sys.stdin.fileno()
    buf = _resumed()
    while True:
        while b"\n" not in buf:
            chunk = os.read(fd, 65536)
            if not chunk:
                if buf.strip():
                    _respond(out, buf.strip())
                return 0
            buf += chunk
        line, buf = buf.split(b"\n", 1)
        line = line.strip()
        if not line:
            continue
        _restart_if_replaced(start, line + b"\n" + buf)
        _respond(out, line)


if __name__ == "__main__":
    sys.exit(main())
