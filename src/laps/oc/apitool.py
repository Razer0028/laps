"""Api — 他のプラットフォームの API を呼ぶ道具（2026-10-08 オーナー「他のプラットフォームの API を Blackwall から
扱えるようにしたい。API キーを Blackwall のモデルに伝えることで使えるようにしたり」）。

★キーは名前で書く: url・headers・query・json・body の中の `{{OPENWEATHER_KEY}}` を、送る瞬間に値へ差し替える
  （値は laps/apikeys.py が暗号化して持つ）。モデルの文脈にも記録にも値は入らない。
★キーごとに登録したドメインにしか送らない（違えば送らずに断る）。リダイレクトは追わない（Location を返す）。
★応答に値が混ざっていたら伏せる。読み取り専用の方針（LAPS_POLICY=readonly）では GET/HEAD/OPTIONS だけ。
★LAN の中の宛先（127.0.0.1・192.168.x.x 等）は WebFetch と同じく断る（LAPS_ALLOW_PRIVATE_FETCH=1 で許す）。
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from ..tools import ToolResult
from .. import apikeys

_MAX_OUT = 20000
_MAX_DOWNLOAD = 50 * 1024 * 1024
_SHOW_HEADERS = ("content-type", "content-length", "location", "retry-after", "link", "x-request-id",
                 "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "ratelimit-remaining",
                 "ratelimit-reset", "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens")


def _names_in(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, str):
        out |= {m.group(1).upper() for m in apikeys.PLACEHOLDER_RE.finditer(obj)}
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out |= _names_in(k) | _names_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _names_in(v)
    return out


def _fill(obj, values: dict[str, str]):
    if isinstance(obj, str):
        return apikeys.PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1).upper(), m.group(0)), obj)
    if isinstance(obj, dict):
        return {_fill(k, values): _fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, values) for v in obj]
    return obj


def _err(text: str, kind: str = "error") -> ToolResult:
    return ToolResult(f"Error: {text}", ok=False, kind=kind)


def _save_key(inp: dict) -> ToolResult:
    sk = inp.get("save_key")
    if not isinstance(sk, dict):
        return _err('save_key must be {"name": ..., "value": ..., "hosts": [...]}.')
    try:
        r = apikeys.set_key(sk.get("name"), sk.get("value"), sk.get("hosts"), sk.get("note") or "")
    except apikeys.KeyError_ as e:
        return _err(str(e))
    return ToolResult(f"Saved API key {r['name']} for {', '.join(r['hosts'])}. Use it as {{{{{r['name']}}}}} in Api "
                      "calls (url, headers, query, json or body). Do not repeat the key itself in replies.", ok=True)


def _list() -> ToolResult:
    ks = apikeys.list_keys()
    if not ks:
        return ToolResult("No API keys are saved. Ask the user to add one in Settings → API keys (safest), or save one "
                          'they gave you with Api(save_key={"name": "SERVICE_KEY", "value": "...", "hosts": ["api.example.com"]}).',
                          ok=True, kind="empty")
    rows = [f"- {{{{{k['name']}}}}} → {', '.join(k['hosts'])}" + (f" · {k['note']}" if k["note"] else "") for k in ks]
    return ToolResult("Saved API keys (use the {{NAME}} form; the values are filled in when the request is sent):\n"
                      + "\n".join(rows), ok=True)


def api_tool(inp: dict) -> ToolResult:
    inp = inp if isinstance(inp, dict) else {}
    if inp.get("save_key"):
        return _save_key(inp)
    if inp.get("list_keys") or not inp.get("url"):
        if not inp.get("url") and not inp.get("list_keys"):
            return _err("give url (and method), or list_keys=true, or save_key={...}.")
        return _list()
    method = str(inp.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        return _err(f"method {method} is not supported")
    try:
        from .. import policy
        if policy._mode() == "readonly" and method not in ("GET", "HEAD", "OPTIONS"):
            return _err(f"the read-only policy allows only GET/HEAD/OPTIONS (got {method})", kind="denied")
    except Exception:
        pass
    url = str(inp.get("url") or "").strip()
    headers = inp.get("headers") if isinstance(inp.get("headers"), dict) else {}
    query = inp.get("query") if isinstance(inp.get("query"), dict) else {}
    body_json = inp.get("json")
    # ★json を文字列で渡されたら、JSON として読めれば中身を送る（2026-10-16 の点検: 文字列のまま二重に符号化して送っていた）。
    if isinstance(body_json, str):
        try:
            body_json = json.loads(body_json)
        except ValueError:
            return _err("json must be a JSON object or array (it was a string that is not valid JSON); "
                        "send plain text with body instead")
    body_text = inp.get("body") if isinstance(inp.get("body"), str) else None

    names = _names_in([url, headers, query, body_json, body_text or ""])
    missing = sorted(n for n in names if apikeys.value(n) is None)
    if missing:
        have = ", ".join(k["name"] for k in apikeys.list_keys()) or "none"
        return _err(f"unknown API key name(s): {', '.join(missing)} (saved: {have}). Ask the user to add it in "
                    "Settings → API keys, or save it with save_key.")
    filled_url = _fill(url, {n: apikeys.value(n) or "" for n in names})
    u = urlparse(filled_url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return _err("url must be an absolute http(s) URL")
    if u.scheme == "http" and names:
        return _err("API keys are only sent over https (the url is http://)")
    for n in sorted(names):
        hs = apikeys.hosts_of(n)
        if not apikeys.host_allowed(u.hostname, u.port, hs):
            return _err(f"{{{{{n}}}}} may only be sent to {', '.join(hs)}, not to {u.hostname}. If this host is "
                        "right, ask the user to add it to that key's domains.", kind="denied")
    try:
        from ..tools import webfetch as _wf
        msg = _wf._check_hop(filled_url)
        if msg:
            return _err(msg, kind="error" if msg.startswith("名前を解決できません") else "denied")
    except Exception:
        pass
    values = {n: apikeys.value(n) or "" for n in names}
    req_headers = {str(k): str(v) for k, v in _fill(headers, values).items()}
    req_query = {str(k): str(v) for k, v in _fill(query, values).items()}
    import httpx
    try:
        with httpx.Client(timeout=float(inp.get("timeout") or 60), follow_redirects=False) as c:
            kw: dict = {"headers": req_headers, "params": req_query or None}
            if body_json is not None:
                kw["json"] = _fill(body_json, values)
            elif body_text is not None:
                kw["content"] = _fill(body_text, values).encode("utf-8")
            with c.stream(method, filled_url, **kw) as r:
                chunks, size = [], 0
                for ch in r.iter_bytes():
                    size += len(ch)
                    if size > _MAX_DOWNLOAD:
                        return _err(f"the response is larger than {_MAX_DOWNLOAD // (1024 * 1024)} MB; stopped")
                    chunks.append(ch)
                raw = b"".join(chunks)
                status, rheaders = r.status_code, r.headers
    except httpx.HTTPError as e:
        text, _ = apikeys.redact(f"{type(e).__name__}: {e}")
        return _err(f"the request failed: {text}")
    ctype = rheaders.get("content-type", "")
    head = [f"HTTP {status} {method} {apikeys.redact(url)[0]}"]
    for h in _SHOW_HEADERS:
        if h in rheaders and h != "content-length":
            head.append(f"{h}: {apikeys.redact(rheaders[h])[0]}")
    head.append(f"bytes: {len(raw)}")
    if 300 <= status < 400 and rheaders.get("location"):
        head.append("(redirect not followed — if the location is the right place, call it again with that URL)")
    save_to = str(inp.get("save_to") or "").strip()
    texty = any(t in ctype for t in ("json", "text", "xml", "javascript", "yaml", "x-www-form-urlencoded")) or not ctype
    if save_to:
        from . import tools as _oc
        dest = Path(_oc._abs(save_to))
        denied = _oc._check_write(str(dest))          # ★Write と同じ制限（2026-10-16 の点検: workspace でも外へ書けた）
        if denied:
            return denied
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        except OSError as e:
            return _err(f"could not save to {dest}: {e}")
        body = f"(saved {len(raw)} bytes to {dest})"
    elif not texty:
        body = (f"(binary content {ctype or 'unknown'}, {len(raw)} bytes — not shown. Call again with "
                "save_to=<file path> to keep it.)")
    else:
        text = raw.decode(r.encoding or "utf-8", errors="replace") if raw else ""
        if "json" in ctype:
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=1)
            except Exception:
                pass
        text, _n = apikeys.redact(text)
        if len(text) > _MAX_OUT:
            text = text[:_MAX_OUT] + f"\n… ({len(text) - _MAX_OUT} more characters not shown; use query/fields/pagination to narrow it, or save_to)"
        body = text or "(empty body)"
    try:
        from .. import policy
        policy.audit("api", {"method": method, "host": u.hostname, "path": u.path[:200], "keys": sorted(names),
                             "status": status}, "allow")
    except Exception:
        pass
    ok = status < 400
    return ToolResult("\n".join(head) + "\n\n" + body, ok=ok, kind="ok" if ok else "error")


DESCRIPTION = """Call another platform's web API (REST/JSON) with saved API keys.

- method, url, headers, query, json (an object) or body (a string), timeout (seconds), save_to (a file path for binary responses such as images or PDFs).
- API keys: write {{NAME}} where the key goes, e.g. headers={"Authorization": "Bearer {{OPENAI_KEY}}"} or query={"appid": "{{OPENWEATHER_KEY}}"}. The value is filled in when the request is sent and is never shown to you. Each key can only be sent to the domains it was saved for.
- list_keys: true shows the saved key names and their domains.
- save_key: {"name": "SERVICE_KEY", "value": "<the key the user gave>", "hosts": ["api.example.com"], "note": "..."} saves a key the user pasted. After saving, never repeat the key itself; refer to it as {{SERVICE_KEY}}.
Do not put API keys in Bash or curl commands, files, or replies. Redirects are not followed (the Location header is returned). Read the platform's API documentation (WebFetch) before guessing endpoints or parameters."""

SCHEMA_PROPS = {
    "method": {"type": "string", "description": "GET, POST, PUT, PATCH, DELETE, HEAD or OPTIONS (default GET)"},
    "url": {"type": "string", "description": "Absolute https URL; may contain {{KEY_NAME}}"},
    "headers": {"type": "object", "description": "Request headers; values may contain {{KEY_NAME}}"},
    "query": {"type": "object", "description": "Query parameters; values may contain {{KEY_NAME}}"},
    "json": {"description": "JSON request body (object or array)"},
    "body": {"type": "string", "description": "Raw request body (text) instead of json"},
    "timeout": {"type": "number", "description": "Seconds (default 60)"},
    "save_to": {"type": "string", "description": "Save the response body to this file (for binary content)"},
    "list_keys": {"type": "boolean", "description": "List saved API key names and domains"},
    "save_key": {"type": "object", "description": "Save a key: {name, value, hosts, note}"},
}
