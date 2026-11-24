"""機械の操作の線（`/ws?ctl=1`・2026-10-10）。

オーナー（2026-10-10）: 「モバイル版はリモートセッション管理用に機能を絞ります。その代わりリモート環境でできる操作は
すべてできるように」→ 範囲は「会話の操作＋機械の管理まで全部」。

Android → 中継 → この機械のゲートウェイ → ここ。1 フレーム＝1 要求:

    {"type": "ctl", "id": "c-1", "op": "fs.list", "args": {"path": "C:\\\\work"}}

返り（要求ごとに 1 つ・順不同＝長い操作が他を待たせない）:

    {"type": "ctl_result", "id": "c-1", "ok": true, "result": {...}}
    {"type": "ctl_result", "id": "c-1", "ok": false, "error": "unknown_op", "message": "…"}

★ゲートウェイの HTTP は GET しか受けない（websockets の握手の口）ので、書き込みを伴う操作はこの線で運ぶ。

操作の置き場は 3 つ。**同じ操作を 2 つの言語で書かない**（片方だけ直す型を作らない）:
  native … ここ（Python）で行う: 機械の様子・フォルダの一覧・添付・会話の削除
  module … 本体の命令行をそのまま呼ぶ（デスクトップ版の画面が呼ぶのと同じ口・同じ渡し方）: API キー・MCP
  app    … Windows のアプリ（Rust）にしか無い物（ローカル推論・モデルの取り寄せ・整合性・推論の場所・作業フォルダ・記憶）。
           受け箱 `<LAPS_HOME>/remote/ctl/` に要求を置き、アプリ（remotectl.rs）が画面と同じ関数を呼んで答えを置く。
           ★アプリに待ち受けの口を作らない約束（sidecar.rs の冒頭）を守るため、ファイルで渡す。
"""
from __future__ import annotations

from . import paths as _lp
import asyncio
import base64
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

#: 添付 1 つの上限（デスクトップ版の貼り付けの上限 64MB より大きく、写真・動画の短い物は通る程度）
ATTACH_MAX = 512 * 1024 * 1024
#: 1 フレームに載せる添付の欠片の上限（ゲートウェイの WS の上限 8MB に base64 の膨らみを見込む）
ATTACH_CHUNK_MAX = 4 * 1024 * 1024
#: フォルダの一覧に返す数の上限
FS_LIST_MAX = 2000
#: 差分の上限（字）
GIT_DIFF_MAX = 256 * 1024
#: アプリの心拍がこれより古ければ「アプリは動いていない」
APP_STALE_SECS = 10.0

#: アプリ側の操作で、既定（120 秒）より長く待つ物
_APP_TIMEOUTS = {
    "hf.download": 6 * 3600, "gpu.local.prepare": 3600, "integrity.fix": 7200,
    "llama.start": 900, "llama.load": 900, "selftest": 900, "hf.search": 120, "hf.files": 120,
}


def _log(msg: str) -> None:
    print(f"[remotectl] {time.strftime('%H:%M:%S')} {msg}", file=sys.stderr, flush=True)


class CtlError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


# ── アプリ（Rust）への橋 ─────────────────────────────────────────────

class AppBridge:
    """受け箱 `<home>/remote/ctl/` を介して、Windows のアプリの操作を呼ぶ。

    置く物（どちらも一時名で書いてから名前を替える＝読みかけを掴ませない）:
      req-<id>.json … {"id", "op", "args", "ts"}（こちらが置く・アプリが読んで消す）
      res-<id>.json … {"id", "ok", "result" | "error"}（アプリが置く・こちらが読んで消す）
      app.json      … アプリの心拍 {"pid", "version", "ts", "ops": [...]}（2 秒ごと）
    """

    def __init__(self, home: Path) -> None:
        self.dir = Path(home) / "remote" / "ctl"

    def status(self) -> dict:
        try:
            o = json.loads((self.dir / "app.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"alive": False}
        age = time.time() - float(o.get("ts") or 0)
        return {"alive": age < APP_STALE_SECS, "age": round(age, 1), "version": str(o.get("version") or ""),
                "ops": [str(x) for x in (o.get("ops") or []) if isinstance(x, str)]}

    async def call(self, op: str, args: dict, timeout: float) -> Any:
        st = self.status()
        if not st["alive"]:
            raise CtlError("app_not_running", "この機械の L.A.P.S のアプリが起動していません（この操作はアプリが行います）")
        if op not in st["ops"]:
            raise CtlError("unknown_op", f"この機械のアプリ（{st.get('version') or '版不明'}）は「{op}」を持っていません")
        rid = uuid.uuid4().hex[:12]
        self.dir.mkdir(parents=True, exist_ok=True)
        req, res = self.dir / f"req-{rid}.json", self.dir / f"res-{rid}.json"
        tmp = self.dir / f".req-{rid}.tmp"
        tmp.write_text(json.dumps({"id": rid, "op": op, "args": args, "ts": time.time()}, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, req)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                try:
                    o = json.loads(res.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    # アプリが途中で落ちたら待ち続けない（要求が残ったまま心拍が止まった）
                    if req.exists() and not self.status()["alive"]:
                        raise CtlError("app_not_running", "アプリが止まりました（要求は届いていません）")
                    continue
                except (OSError, ValueError):
                    continue                        # 書きかけ（名前を替える前の一瞬など）
                try:
                    res.unlink()
                except OSError:
                    pass
                if o.get("ok"):
                    return o.get("result")
                raise CtlError("app_error", str(o.get("error") or "アプリが失敗しました"))
            raise CtlError("timeout", f"アプリが {int(timeout)} 秒で答えませんでした（{op}）")
        finally:
            for p in (req, tmp):
                try:
                    p.unlink()
                except OSError:
                    pass


# ── 本体の命令行（デスクトップ版の画面が呼ぶのと同じ口）─────────────────────

async def _run_module(module: str, args: list[str], env: dict[str, str] | None, timeout: float) -> tuple[int, str, str]:
    """`python -m <module> <args>` を走らせる。★秘密は引数でなく環境変数で渡す（プロセスの一覧に出さない）。"""
    e = dict(os.environ)
    e.update(env or {})
    e.setdefault("PYTHONIOENCODING", "utf-8")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", module, *args, env=e,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **({"creationflags": 0x08000000} if os.name == "nt" else {}),   # CREATE_NO_WINDOW
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise CtlError("timeout", f"{module} が {int(timeout)} 秒で終わりませんでした")
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _last_json(out: str, err: str, code: int, first: str = "") -> Any:
    """最後の空でない行（[first] を渡したら、その字で始まる最後の行）を JSON として読む（アプリの読み方と同じ）。"""
    line = next((l for l in reversed(out.splitlines()) if l.strip() and l.strip().startswith(first)), "")
    try:
        v = json.loads(line)
    except ValueError:
        tail = " / ".join(err.strip().splitlines()[-3:])
        raise CtlError("module_error", f"本体の返事を読めません（終了 {code}）{tail}")
    if isinstance(v, dict) and isinstance(v.get("error"), str):
        raise CtlError("module_error", v["error"])
    return v


async def _apikeys(op: str, a: dict) -> Any:
    if op == "apikeys.list":
        c, o, e = await _run_module("laps.apikeys", ["list"], None, 60)
    elif op == "apikeys.set":
        name, hosts, note = str(a.get("name") or ""), str(a.get("hosts") or ""), str(a.get("note") or "")
        argv = ["set", name, hosts] + ([note] if note.strip() else [])
        c, o, e = await _run_module("laps.apikeys", argv, {"LAPS_APIKEY_VALUE": str(a.get("value") or "")}, 60)
    else:
        c, o, e = await _run_module("laps.apikeys", ["delete", str(a.get("name") or "")], None, 60)
    return _last_json(o, e, c)


async def _mcp(op: str, a: dict) -> Any:
    if op == "mcp.get":
        c, o, e = await _run_module("laps.oc.mcp", ["get"], None, 60)
        if c != 0:
            raise CtlError("module_error", "mcp.json を読めません: " + " / ".join(e.strip().splitlines()[-3:]))
        return {"text": o}
    if op == "mcp.set":
        c, o, e = await _run_module("laps.oc.mcp", ["set"], {"LAPS_MCP_JSON": str(a.get("text") or "")}, 60)
        return _last_json(o, e, c)
    c, o, e = await _run_module("laps.oc.mcp", ["status"], None, 420)
    return _last_json(o, e, c, first="[")


# ── ここで行う操作 ───────────────────────────────────────────────

def _workspace(gw, a: dict) -> Path:
    """この機械のいまの作業フォルダ（電話から依頼を出すと、ここで走る）。"""
    w = str(a.get("workspace") or "").strip()
    if w and os.path.isdir(w):
        return Path(w)
    try:
        from .remotehost import current_workspace
        cur = current_workspace()
        if os.environ.get("LAPS_REMOTE_WSFILE") or os.environ.get("LAPS_REMOTE_WORKSPACE"):
            return Path(cur)
    except Exception:  # noqa: BLE001
        pass
    return Path(getattr(gw, "workspace", None) or Path.home())


def _version() -> str:
    try:
        from .gateway import _version as v
        return v()
    except Exception:  # noqa: BLE001
        return ""


def machine_info(gw, bridge: AppBridge) -> dict:
    try:
        import getpass
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = ""
    native = sorted(NATIVE) + sorted(MODULE)
    app = bridge.status()
    return {"name": socket.gethostname(), "os": f"{platform.system()} {platform.release()}".strip(),
            "version": _version(), "user": user, "python": sys.version.split()[0],
            "home": str(getattr(gw, "home", "")), "workspace": str(_workspace(gw, {})),
            "ops": native + [o for o in app.get("ops", []) if o not in native], "app": app}


def _roots(gw) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def add(label: str, p: str) -> None:
        if p and p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append({"label": label, "path": p})
    add("作業フォルダ", str(_workspace(gw, {})))
    add("ホーム", str(Path.home()))
    if os.name == "nt":
        import string
        for d in string.ascii_uppercase:
            add(f"{d}:", f"{d}:\\")
    else:
        add("/", "/")
    return out


def fs_list(gw, a: dict) -> dict:
    """フォルダの中身（フォルダが先・名前の順）。path が空なら入口の一覧。読むだけ。"""
    raw = str(a.get("path") or "").strip()
    if not raw:
        return {"path": "", "parent": None, "roots": _roots(gw), "entries": []}
    p = Path(os.path.expanduser(raw))
    try:
        p = p.resolve()
    except OSError:
        pass
    if not p.is_dir():
        raise CtlError("not_a_dir", f"フォルダではありません: {p}")
    rows = []
    try:
        it = list(os.scandir(p))
    except OSError as e:
        raise CtlError("unreadable", f"読めません: {e.strerror or e}")
    for d in it:
        try:
            isdir = d.is_dir()
            st = d.stat()
        except OSError:
            continue
        rows.append({"name": d.name, "dir": isdir, "size": 0 if isdir else st.st_size, "mtime": int(st.st_mtime),
                     "hidden": d.name.startswith(".")})
    rows.sort(key=lambda r: (not r["dir"], r["name"].lower()))
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "entries": rows[:FS_LIST_MAX], "truncated": len(rows) > FS_LIST_MAX,
            "roots": _roots(gw)}


def _safe_name(name: str) -> str:
    n = name.replace("\\", "/").split("/")[-1].strip().strip(".")
    n = "".join(ch for ch in n if ch not in '<>:"|?*' and ord(ch) >= 32)
    return n[:120] or "file"


def _unique(d: Path, name: str) -> Path:
    p = d / name
    if not p.exists():
        return p
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    for i in range(2, 1000):
        q = d / (f"{stem} ({i}).{ext}" if ext else f"{stem} ({i})")
        if not q.exists():
            return q
    return d / f"{uuid.uuid4().hex[:8]}-{name}"


def attach_put(gw, a: dict) -> dict:
    """添付を欠片で受け取り、揃ったら `<作業フォルダ>/.blackwall/attachments/<YYYYMMDD-HHMMSS>/<名前>` に置く
    （デスクトップ版 attach.rs と同じ置き場と名前の規則）。返す rel を依頼の文に書けば、エージェントがそのまま読める。"""
    up = str(a.get("upload") or "")
    if not up or not up.replace("-", "").isalnum() or len(up) > 64:
        raise CtlError("bad_args", "upload が不正です")
    total = int(a.get("total") or 0)
    offset = int(a.get("offset") or 0)
    if total <= 0 or total > ATTACH_MAX:
        raise CtlError("too_large", f"添付は 1 つ {ATTACH_MAX // (1024 * 1024)}MB までです")
    data = base64.b64decode(str(a.get("data") or ""), validate=False)
    if len(data) > ATTACH_CHUNK_MAX:
        raise CtlError("bad_args", "欠片が大きすぎます")
    tmpdir = Path(gw.home) / "remote" / "uploads"
    tmpdir.mkdir(parents=True, exist_ok=True)
    part = tmpdir / f"{up}.part"
    have = part.stat().st_size if part.exists() else 0
    if offset != have:
        if offset == 0:
            part.unlink(missing_ok=True)
            have = 0
        else:
            return {"done": False, "received": have}      # 送り手は received から送り直す
    if have + len(data) > total:
        part.unlink(missing_ok=True)
        raise CtlError("bad_args", "大きさが合いません")
    with open(part, "ab") as fh:
        fh.write(data)
    got = have + len(data)
    if got < total:
        return {"done": False, "received": got}
    ws = _workspace(gw, a)
    d = _lp.workspace_dir(ws) / "attachments" / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    dst = _unique(d, _safe_name(str(a.get("name") or "file")))
    shutil.move(str(part), str(dst))
    rel = dst.relative_to(ws).as_posix()
    _log(f"添付を置いた: {rel}（{got} バイト）")
    return {"done": True, "received": got, "name": dst.name, "rel": rel, "path": str(dst), "size": got,
            "workspace": str(ws)}


def _dir_bytes(p: Path) -> int:
    n = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


#: 写真 1 枚の上限（デスクトップの shot_data・道具 Image の MAX_SHOW と揃える）
SHOT_MAX = 12 * 1024 * 1024
#: 1 回に返す写真の欠片（base64 で約 2.7MB＝ゲートウェイの WS の上限 8MB に余裕を残す）
SHOT_CHUNK = 2 * 1024 * 1024
_SHOT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def _shot_kind(head: bytes) -> str:
    """中身の先頭から画像の型を当てる（拡張子だけを信じない）。分からなければ空。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


def shot_get(gw, a: dict) -> dict:
    """会話の写真を返す（2026-11-24 オーナー「Android の方も…モデルがチャット欄に画像を表示する機能をより優れたものに」）。

    道具 Image・Browser・Desktop が撮った写真は `<home>/sessions/<会話>/shots/` にあり、道具の要約（summary.shot）と答えの
    `![説明](パス)` にこの機械の上の絶対パスで載る。デスクトップは Rust の shot_data で読み、Android はこの操作で取りに来る。
    ★読めるのは `<home>/sessions/<会話>/shots/<名前>` の形だけ（任意のパスを読ませない・デスクトップと同じ決まりを 1 段固く）。
    ★拡張子だけでなく中身の先頭でも画像かを確かめる。★開いた後に、確かめた時と同じファイルかを st_ino で見る（差し替えの隙）。
    大きな写真は欠片（SHOT_CHUNK）で返す: offset から読み、done が真になるまで offset を進めて呼ぶ。
    """
    raw = str(a.get("path") or "").strip()
    if not raw or "\x00" in raw:
        raise CtlError("bad_args", "path（写真の場所）が要ります")
    try:
        offset = max(0, int(a.get("offset") or 0))
    except (TypeError, ValueError):
        raise CtlError("bad_args", "offset は整数で") from None
    base = (Path(gw.home) / "sessions").resolve()
    try:
        p = Path(raw).resolve(strict=True)
    except (OSError, RuntimeError):
        raise CtlError("not_found", "写真が見つかりません（消されたか、別の機械の会話です）") from None
    if p.parent.name != "shots" or p.parent.parent.parent != base:
        raise CtlError("denied", "読めるのは会話の写真（shots）だけです")
    mime = _SHOT_MIME.get(p.suffix.lower())
    if not mime:
        raise CtlError("denied", "写真ではありません")
    st0 = p.stat()
    if st0.st_size > SHOT_MAX:
        raise CtlError("too_large", f"写真が大きすぎます（{st0.st_size // (1024 * 1024)}MB・上限 {SHOT_MAX // (1024 * 1024)}MB）")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(p, flags)
    except OSError:
        raise CtlError("not_found", "写真を開けません") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if (st.st_ino, st.st_dev) != (st0.st_ino, st0.st_dev) or st.st_size > SHOT_MAX:
            raise CtlError("denied", "確かめている間に写真が入れ替わりました")
        kind = _shot_kind(fh.read(16))
        if not kind:
            raise CtlError("denied", "中身が画像ではありません")
        if offset > st.st_size:
            raise CtlError("bad_args", "offset が大きさを超えています")
        fh.seek(offset)
        data = fh.read(SHOT_CHUNK)
    end = offset + len(data)
    return {"mime": kind, "size": st.st_size, "mtime": int(st.st_mtime), "offset": offset,
            "data": base64.b64encode(data).decode("ascii"), "done": end >= st.st_size}


def sessions_delete(gw, a: dict) -> dict:
    """名指した会話の記録を消す。★走っている会話（写しが閉じていない）は消さない。"""
    from .session import is_valid_session_id
    base = Path(gw.home) / "sessions"
    running = {r["id"] for r in (gw.live_sessions(limit=50) if hasattr(gw, "live_sessions") else []) if r.get("running")}
    deleted, freed, failed, ids = 0, 0, [], []
    for sid in [str(x) for x in (a.get("ids") or [])][:500]:
        if not is_valid_session_id(sid):
            failed.append(f"{sid}: 名前が不正")
            continue
        if sid in running:
            failed.append(f"{sid}: 走っているので消しません")
            continue
        d = base / sid
        if not d.is_dir():
            continue
        b = _dir_bytes(d)
        try:
            shutil.rmtree(d)
            deleted += 1
            freed += b
            ids.append(sid)
        except OSError as e:
            failed.append(f"{sid}: {e.strerror or e}")
    return {"deleted": deleted, "bytes": freed, "failed": failed, "ids": ids}


def git_changes(gw, a: dict) -> dict:
    """この会話の作業フォルダの変更（読むだけ・Claude の Remote Control の差分の欄と同じ・2026-10-31）。
    枝が既定の枝より進んでいれば分かれた所から（未コミットを含む）、既定の枝の上なら未コミットだけ。"""
    from .session import is_valid_session_id
    sid = str(a.get("session") or "")
    ws = ""
    if sid and is_valid_session_id(sid):
        try:
            ws = str(json.loads((Path(gw.home) / "sessions" / sid / "meta.json").read_text(encoding="utf-8")).get("workspace") or "")
        except (OSError, ValueError):
            pass
    root = Path(ws) if ws else Path(gw.workspace)
    out: dict = {"workspace": str(root), "git": False, "branch": "", "base": "", "status": [], "diff": "", "truncated": False}
    if not root.is_dir():
        return out

    def git(*args: str) -> tuple[int, str]:
        try:
            r = subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=false", *args], capture_output=True, timeout=20,
                               **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return r.returncode, r.stdout.decode("utf-8", "replace")

    if git("rev-parse", "--is-inside-work-tree")[0] != 0:
        return out
    out["git"] = True
    out["branch"] = git("rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    head_default = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")[1].strip()
    candidates = [head_default] if head_default else ["origin/main", "origin/master", "main", "master"]
    base = "HEAD"
    for c in candidates:
        if c.split("/")[-1] == out["branch"]:
            break
        code, mb = git("merge-base", "HEAD", c)
        if code == 0 and mb.strip():
            base = mb.strip()
            break
    out["base"] = base if base == "HEAD" else base[:10]
    out["status"] = git("status", "--porcelain")[1].splitlines()[:500]
    d = git("diff", base)[1]
    if len(d) > GIT_DIFF_MAX:
        d, out["truncated"] = d[:GIT_DIFF_MAX], True
    out["diff"] = d
    return out


def desktop(gw, a: dict) -> dict:
    """この機械の画面の部品を読み・押す（Desktop 道具と同じ関数 oc/desktop.desktop_tool・2026-10-14）。

    オーナー（2026-10-14）「B で進めて」＝Claude Code の MCP（blackwall-mcp の desktop・blackwall/ctlclient）から、
    中継を通してこの機械の画面を操作する。ゲートウェイはアプリが起こす＝利用者がログインしている画面のセッションで
    動くので窓に届く（SSH の命令は画面の無い Session 0 で動き、窓の一覧もクリックも空振りする・2026-10-05 実測）。
    前面の窓が Blackwall 自身なら宛先なしの打鍵は断る（desktop._guard_pids＝このゲートウェイと親のアプリ）。
    この機械で切るには LAPS_REMOTE_DESKTOP=0。"""
    if (os.environ.get("LAPS_REMOTE_DESKTOP") or "1").strip().lower() in ("0", "off", "false", "no"):
        raise CtlError("disabled", "この機械では遠隔の画面操作を切っています（LAPS_REMOTE_DESKTOP=0）")
    from .oc import desktop as _dk
    if not _dk.available():
        raise CtlError("unavailable", "この機械では画面操作を使えません（Windows と .NET Framework 4 の csc.exe が要る）")
    r = _dk.desktop_tool(dict(a or {}))
    _log(f"desktop {str((a or {}).get('action') or '')[:20]} → {'ok' if getattr(r, 'ok', True) else 'error'}")
    out = {"ok": bool(getattr(r, "ok", True)), "text": str(r)}
    img = _desktop_image(getattr(r, "data", None))
    if img:
        out["image"] = img
    return out


#: look の写真を中継に載せる上限（base64 の文字数）。中継の 1 通は 8 MB まで（remotehost の max_size）。
DESKTOP_IMAGE_MAX = 3 * 1024 * 1024


def _desktop_image(data) -> dict | None:
    """look の写真（data.image）を中継に載る大きさで返す。大きい PNG は JPEG に直す。載らなければ None。"""
    img = (data or {}).get("image") if isinstance(data, dict) else None
    if not isinstance(img, dict) or not img.get("b64"):
        return None
    b64, mime = str(img["b64"]), str(img.get("mime") or "image/png")
    if len(b64) > DESKTOP_IMAGE_MAX:
        try:
            import base64
            import io
            from PIL import Image
            im = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            b64, mime = base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
        except Exception:
            return None
    if len(b64) > DESKTOP_IMAGE_MAX:
        return None
    return {"mime": mime, "b64": b64}


def _modelctl_bases(gw) -> list[str]:
    """推論機のモデルの切り替え口の候補（2026-10-19 オーナー「Android 版を Claude のスマホ版に寄せて」の上のモデル選び）。
    Windows のアプリの modelctl.rs と同じ決め方: LAPS_MODELCTL_URL があればそれ。無ければ、この機械の宛先そのものの
    /modelctl/（Claude の中継・scripts/claude_relay.py）→ 宛先と同じ機械の :8743（推論機の モデル切り替えの口）の順に試す。"""
    forced = (os.environ.get("LAPS_MODELCTL_URL") or "").strip()
    if forced:
        return [forced.rstrip("/") + "/"]
    from urllib.parse import urlsplit
    u = urlsplit(str(getattr(gw, "backend", "") or ""))
    if not u.scheme or not u.hostname:
        raise CtlError("no_server", "この機械は手元で推論しているので、サーバーのモデルはありません")
    host = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
    root = f"{u.scheme}://{host}{f':{u.port}' if u.port else ''}"
    return [root + "/modelctl/", f"{u.scheme}://{host}:8743/"]


def _modelctl_get(gw) -> tuple[str, dict]:
    import httpx
    from .backend import _trust_env
    errs = []
    for base in _modelctl_bases(gw):
        try:
            r = httpx.get(base + "v1/models", timeout=8.0, trust_env=_trust_env())
        except Exception as e:  # noqa: BLE001
            errs.append(f"{base}: {type(e).__name__}")
            continue
        if r.status_code == 200:
            try:
                d = r.json()
            except ValueError:
                d = None
            if isinstance(d, dict) and isinstance(d.get("units"), list):
                return base, d
        errs.append(f"{base}: HTTP {r.status_code}")
    raise CtlError("unreachable", "モデルの切り替え口に届きません（" + "・".join(errs)[:300] + "）")


def server_models(gw, a: dict) -> dict:
    """推論機（または Claude の中継）のモデルの一覧・いまの物・切り替えの進み具合。"""
    base, d = _modelctl_get(gw)
    return {**d, "base": base}


def server_switch(gw, a: dict) -> dict:
    """モデルを切り替える。推論機は裏で入れ替える（1〜3 分・進み具合は server.models の switch）。中継はすぐ終わる。"""
    import httpx
    from .backend import _trust_env
    mid = str((a or {}).get("id") or "").strip()
    if not mid:
        raise CtlError("bad_args", "id（切り替えるモデル）が要ります")
    base, _ = _modelctl_get(gw)
    try:
        r = httpx.post(base + "v1/switch", json={"id": mid, "force": bool((a or {}).get("force"))}, timeout=15.0,
                       trust_env=_trust_env())
    except Exception as e:  # noqa: BLE001
        raise CtlError("unreachable", f"切り替え口に届きません（{type(e).__name__}）") from None
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code >= 400:
        raise CtlError("refused", str(d.get("error") or f"切り替え口が {r.status_code} を返しました")[:300])
    _log(f"server.switch {mid} → {r.status_code}")
    return {"ok": True, "message": str(d.get("message") or "切り替えを始めました")}


def server_stop(gw, a: dict) -> dict:
    """推論機を止めて GPU を空ける（2026-11-03 オーナー「アプリや CLI 版から推論機を空けたり起動したりできるようにしたい」）。
    モデル切り替えの口 の POST /v1/stop に頼む（裏で止める・進み具合は server.models の switch・action="stop"）。起動は server.switch。
    ★Claude の中継（一覧に stoppable が無い）には頼まない。推論中（busy）・GPU をほかの仕事が使っている（lease）は
      向こうの言い分を code で分けて返す（busy / lease）。"""
    import httpx
    from .backend import _trust_env
    base, d = _modelctl_get(gw)
    if not d.get("stoppable"):
        raise CtlError("unsupported", "この宛先には推論機を止める口がありません（Claude の中継・または古い モデル切り替えの口）")
    try:
        r = httpx.post(base + "v1/stop", json={"force": bool((a or {}).get("force"))}, timeout=15.0, trust_env=_trust_env())
    except Exception as e:  # noqa: BLE001
        raise CtlError("unreachable", f"切り替え口に届きません（{type(e).__name__}）") from None
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if r.status_code >= 400:
        code = "lease" if body.get("lease") else "busy" if body.get("busy") else "refused"
        raise CtlError(code, str(body.get("error") or f"切り替え口が {r.status_code} を返しました")[:300])
    _log(f"server.stop → {r.status_code}")
    return {"ok": True, "already": bool(body.get("already")), "message": str(body.get("message") or "推論機を止めます")}


#: ここで行う操作（名前 → 関数(gw, args)）
NATIVE = {"fs.list": fs_list, "attach.put": attach_put, "sessions.delete": sessions_delete, "git.changes": git_changes, "desktop": desktop,
          "shot.get": shot_get,
          "server.models": server_models, "server.switch": server_switch,
          "server.stop": server_stop}
#: 本体の命令行を呼ぶ操作
MODULE = {"apikeys.list": _apikeys, "apikeys.set": _apikeys, "apikeys.delete": _apikeys,
          "mcp.get": _mcp, "mcp.set": _mcp, "mcp.status": _mcp}


async def dispatch(gw, bridge: AppBridge, op: str, args: dict) -> Any:
    if op == "machine.info":
        return machine_info(gw, bridge)
    if op == "sessions.delete":
        r = await asyncio.to_thread(sessions_delete, gw, args)
        # ★アプリが動いていれば、その画面の一覧からも消す（記録だけ消すと、向こうの一覧に抜け殻が残る・2026-10-31）。
        st = bridge.status()
        if r.get("ids") and st.get("alive") and "chat.delete" in (st.get("ops") or []):
            try:
                await bridge.call("chat.delete", {"ids": r["ids"]}, 10.0)
            except Exception as e:  # noqa: BLE001 — 記録は消えている。画面の一覧は次の起動で直せる
                _log(f"chat.delete: {e!r}")
        return r
    if op in NATIVE:
        return await asyncio.to_thread(NATIVE[op], gw, args)
    if op in MODULE:
        return await MODULE[op](op, args)
    return await bridge.call(op, args, float(_APP_TIMEOUTS.get(op, 120)))


async def serve(gw, connection) -> None:
    """1 本の操作の線。要求ごとに別の仕事にする（長い取り寄せが短い問い合わせを待たせない）。"""
    bridge = AppBridge(Path(gw.home))
    tasks: set[asyncio.Task] = set()
    send_lock = asyncio.Lock()

    async def reply(obj: dict) -> None:
        async with send_lock:
            await connection.send(json.dumps(obj, ensure_ascii=False, default=str))

    async def one(rid: Any, op: str, args: dict) -> None:
        t0 = time.monotonic()
        try:
            result = await dispatch(gw, bridge, op, args)
            await reply({"type": "ctl_result", "id": rid, "ok": True, "result": result})
            _log(f"{op} ok（{time.monotonic() - t0:.1f}s）")
        except CtlError as e:
            await reply({"type": "ctl_result", "id": rid, "ok": False, "error": e.code, "message": e.message})
            _log(f"{op} {e.code}: {e.message[:120]}")
        except Exception as e:  # noqa: BLE001 — 線を落とさない
            await reply({"type": "ctl_result", "id": rid, "ok": False, "error": "internal",
                         "message": f"{type(e).__name__}: {e}"[:400]})
            _log(f"{op} 内部の失敗: {e!r}")

    try:
        async for msg in connection:
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", "replace")
            try:
                obj = json.loads(msg)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "ping":
                await reply({"type": "pong", "id": obj.get("id")})
                continue
            if obj.get("type") != "ctl" or not isinstance(obj.get("op"), str):
                await reply({"type": "ctl_result", "id": obj.get("id"), "ok": False, "error": "bad_request",
                             "message": "この線で受けるのは操作（type=ctl）だけです"})
                continue
            args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
            t = asyncio.create_task(one(obj.get("id"), obj["op"], args))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    except Exception as e:  # noqa: BLE001 — 切断など
        _log(f"線が閉じた: {type(e).__name__}")
    finally:
        for t in tasks:
            t.cancel()
