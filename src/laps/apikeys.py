"""API キーの置き場（2026-10-08 オーナー「他のプラットフォームの API を Blackwall から扱えるようにしたい。
API キーを Blackwall のモデルに伝えることで使えるようにしたり」）。

★モデルには**名前だけ**を見せる。値は Api 道具が送る瞬間に `{{NAME}}` を差し替えて使う。
  道具の結果に値が混ざっていたら伏せる（redact）ので、保存した後の値は会話にも記録にも入らない。
★キーごとに**送ってよいドメイン**を持つ。違うドメインへは送らない（ウェブや文書に紛れた指示で、
  キーを別の宛先へ送らせる手口を道具の側で止める）。
★保存: Windows は DPAPI（利用者のアカウントでだけ復号できる）・それ以外は所有者だけが読める権限（0600）。
  置き場は LAPS_HOME/apikeys.json（アプリは <データ根>\\agent\\apikeys.json）。

命令行（アプリの設定画面から使う）:
  python -m laps.apikeys list                 → JSON（名前・ドメイン・メモ・作った日時・値の頭 4 字）
  python -m laps.apikeys set NAME host1,host2 [メモ]   値は環境変数 LAPS_APIKEY_VALUE か標準入力
  python -m laps.apikeys delete NAME
"""
from __future__ import annotations

from . import paths as _lp
import base64
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,47}$")
PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_]{1,47})\s*\}\}")
_HOST_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)*(:\d+)?$")
_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float | None = None
_plain: dict[str, str] = {}      # 復号した値（ファイルが変わったら捨てる）。伏せる処理が道具の結果ごとに走るので、毎回 DPAPI を呼ばない


class KeyError_(Exception):
    pass


def _path() -> Path:
    d = Path(str(_lp.home_dir()))
    d.mkdir(parents=True, exist_ok=True)
    return d / "apikeys.json"


# ── 暗号化（Windows は DPAPI）───────────────────────────────────────────

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst))   # CRYPTPROTECT_UI_FORBIDDEN
    if not ok:
        raise KeyError_(f"DPAPI failed ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)


def _seal(value: str) -> str:
    raw = value.encode("utf-8")
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
    return "plain:" + base64.b64encode(raw).decode("ascii")


def _open(sealed: str) -> str:
    kind, _, b64 = sealed.partition(":")
    raw = base64.b64decode(b64)
    if kind == "dpapi":
        return _dpapi(raw, False).decode("utf-8")
    return raw.decode("utf-8")


# ── 読み書き ───────────────────────────────────────────────────────────

def _load() -> dict:
    global _cache, _cache_mtime
    p = _path()
    try:
        m = p.stat().st_mtime
    except OSError:
        _cache, _cache_mtime = {}, None
        _plain.clear()
        return {}
    if _cache is not None and _cache_mtime == m:
        return _cache
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _cache, _cache_mtime = data, m
    _plain.clear()
    return data


def _save(data: dict) -> None:
    global _cache, _cache_mtime
    p = _path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    _cache, _cache_mtime = None, None
    _plain.clear()


def norm_hosts(hosts) -> list[str]:
    if isinstance(hosts, str):
        hosts = re.split(r"[,\s]+", hosts)
    out = []
    for h in hosts or []:
        h = str(h).strip().lower()
        h = re.sub(r"^https?://", "", h).split("/")[0]
        if h and _HOST_RE.match(h) and h not in out:
            out.append(h)
    return out


def set_key(name: str, value: str, hosts, note: str = "") -> dict:
    name = str(name or "").strip().upper()
    if not NAME_RE.match(name):
        raise KeyError_("name: use 2-48 letters A-Z, digits or _ (starting with a letter), e.g. OPENWEATHER_KEY")
    value = str(value or "").strip()
    if len(value) < 4:
        raise KeyError_("value is empty or too short")
    hs = norm_hosts(hosts)
    if not hs:
        raise KeyError_("hosts: give at least one domain this key may be sent to (e.g. api.openweathermap.org)")
    with _lock:
        data = dict(_load())
        data[name] = {"value": _seal(value), "hosts": hs, "note": " ".join(str(note or "").split())[:200],
                      "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save(data)
    return {"name": name, "hosts": hs}


def delete_key(name: str) -> bool:
    name = str(name or "").strip().upper()
    with _lock:
        data = dict(_load())
        if name not in data:
            return False
        data.pop(name)
        _save(data)
    return True


def list_keys() -> list[dict]:
    out = []
    for name, rec in sorted(_load().items()):
        try:
            v = _open(rec.get("value", ""))
            head = v[:4] + "…" if len(v) > 8 else "…"
        except Exception:
            head = "（読めません）"
        out.append({"name": name, "hosts": rec.get("hosts") or [], "note": rec.get("note") or "",
                    "created": rec.get("created") or "", "head": head})
    return out


def value(name: str) -> str | None:
    name = str(name or "").strip().upper()
    rec = _load().get(name)
    if not rec:
        return None
    if name in _plain:
        return _plain[name]
    try:
        v = _open(rec.get("value", ""))
    except Exception:
        return None
    _plain[name] = v
    return v


def hosts_of(name: str) -> list[str]:
    rec = _load().get(str(name or "").strip().upper())
    return list(rec.get("hosts") or []) if rec else []


def host_allowed(host: str, port: int | None, patterns: list[str]) -> bool:
    """[host]（と [port]）が、登録したドメインのどれかに当たるか。`example.com` は下の名前（api.example.com）も含む。
    口を書いたドメイン（`localhost:8080`）は口も一致したときだけ。"""
    host = (host or "").lower().rstrip(".")
    for pat in patterns:
        p = str(pat).lower()
        pport = None
        if ":" in p:
            p, pp = p.rsplit(":", 1)
            pport = int(pp) if pp.isdigit() else None
        if pport is not None and port != pport:
            continue
        base = p[2:] if p.startswith("*.") else p
        if host == base or host.endswith("." + base):
            return True
    return False


def _variants(v: str) -> set[str]:
    from urllib.parse import quote
    vs = {v, quote(v, safe=""), quote(v)}
    try:
        vs.add(base64.b64encode(v.encode()).decode())
    except Exception:
        pass
    return {x for x in vs if len(x) >= 6}


def redact(text: str) -> tuple[str, int]:
    """保存してあるキーの値（と URL 符号化・base64 の形）を伏せる。伏せた数を返す。"""
    data = _load()
    if not data or not text:
        return text, 0
    n = 0
    for name in data:
        v = value(name)
        if not v or len(v) < 6:
            continue
        for form in sorted(_variants(v), key=len, reverse=True):
            if form in text:
                n += text.count(form)
                text = text.replace(form, f"{{{{{name}}}}}")
    return text, n


def has_keys() -> bool:
    return bool(_load())


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    try:
        if not args or args[0] == "list":
            print(json.dumps(list_keys(), ensure_ascii=False))
            return 0
        if args[0] == "set" and len(args) >= 3:
            v = os.environ.get("LAPS_APIKEY_VALUE")
            if v is None:
                v = sys.stdin.read()
            print(json.dumps(set_key(args[1], v, args[2], " ".join(args[3:])), ensure_ascii=False))
            return 0
        if args[0] == "delete" and len(args) >= 2:
            print(json.dumps({"deleted": delete_key(args[1])}))
            return 0
        print("usage: list | set NAME hosts [note] | delete NAME", file=sys.stderr)
        return 2
    except KeyError_ as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
