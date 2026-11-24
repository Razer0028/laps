"""ウェブの道具。WebFetch は opencode の tool/webfetch.ts を手本に、標準ライブラリで HTML を文字にする。
WebSearch は検索先の扱いが込み入っているので laps/tools/websearch.py をそのまま呼ぶ。"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import httpx

from . import Ctx, Result, Tool, clip, fail

MAX_BYTES = 5 * 1024 * 1024
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_SKIP = {"script", "style", "noscript", "svg", "head", "template"}
_BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "table", "ul", "ol"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag == "body":                  # ★</head> を書かないページで本文が丸ごと消えていた（2026-11-16 全体のレビュー）
            self.skip = 0
        elif tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith("http"):
                self.out.append("[")
                self._href = href

    def handle_endtag(self, tag):
        if tag in _SKIP and self.skip:
            self.skip -= 1
        elif tag in _BLOCK:
            self.out.append("\n")
        elif tag == "a" and getattr(self, "_href", None):
            self.out.append(f"]({self._href})")
            self._href = None

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html)
    except Exception:
        return html
    lines = [" ".join(x.split()) for x in "".join(p.out).split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)


def decode_page(data: bytes, ctype: str) -> str:
    """ページの文字コードを決めて文字にする。見出し（Content-Type）の charset → 本文の <meta charset> → UTF-8 → cp932 の順。
    ★2026-11-16 全体のレビュー: 見出しに charset の無い Shift_JIS のページが UTF-8 として読まれ、文字化けしていた。"""
    m = re.search(r"charset\s*=\s*[\"']?([A-Za-z0-9_.:-]+)", ctype or "", re.I)
    names = [m.group(1)] if m else []
    mm = _META_CHARSET.search(data[:4096])
    if mm:
        names.append(mm.group(1).decode("ascii", "replace"))
    for n in names:
        n = {"shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932", "windows-31j": "cp932"}.get(n.lower(), n)
        try:
            return data.decode(n)
        except LookupError:
            continue
        except UnicodeDecodeError:
            return data.decode(n, "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp932")
    except UnicodeDecodeError:
        return data.decode("utf-8", "replace")


def _blocked(url: str) -> str:
    """内部の住所（ループバック・プライベート・リンクローカル・クラウドのメタデータ）なら理由を返す。"""
    try:
        from ...tools.webfetch import _check_hop
        return _check_hop(url)
    except Exception:
        return ""


def webfetch(inp: dict, ctx: Ctx) -> Result:
    url = str(inp.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return fail("url must start with http:// or https://", head=url[:200])
    try:
        limit = min(60.0, max(1.0, float(inp.get("timeout") or 30)))
    except (TypeError, ValueError):
        limit = 30.0
    why = _blocked(url)
    if why:
        return fail(why, kind="denied", head=url[:200])

    def check_hop(request: httpx.Request) -> None:     # 転送先も同じ検査を通す（公開の URL から内部へ飛ばす形を塞ぐ）
        reason = _blocked(str(request.url))
        if reason:
            raise httpx.UnsupportedProtocol(reason)
    try:
        with httpx.Client(follow_redirects=True, timeout=limit, headers={"User-Agent": _UA}, event_hooks={"request": [check_hop]}) as c:
            with c.stream("GET", url) as r:
                data = bytearray()
                for chunk in r.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES or ctx.stopped():
                        break
                status, ctype = r.status_code, r.headers.get("content-type", "")
    except httpx.HTTPError as e:
        return fail(f"could not fetch {url}: {e}", head=url[:200])
    text = decode_page(bytes(data), ctype)
    raw_html = str(inp.get("format") or "").lower() == "html"
    if not raw_html and ("html" in ctype.lower() or text.lstrip()[:15].lower().startswith(("<!doctype", "<html"))):
        text = html_to_text(text)
    body, cut = clip(ctx, text)
    if status >= 400:
        return Result(f"Error: HTTP {status} from {url}\n{body[:2000]}", ok=False, kind="error", head=url[:200], tail=f"HTTP {status}")
    if body:                               # ★AI を操ろうとする文が載ったページは頭に一言（2026-11-15・本文は渡す＝データとして読む）
        from .. import skillguard
        warn = skillguard.web_warning(text)
        if warn:
            body = warn + body
    return Result(body or "(empty page)", kind="ok" if body else "empty", head=url[:200], tail=f"{len(text):,} 字", truncated=cut)


def websearch(inp: dict, ctx: Ctx) -> Result:
    from ...tools import websearch as old
    q = str(inp.get("query") or "").strip()
    if not q:
        return fail("query is required")
    out = str(old.SPEC.handler({"query": q, "n": inp.get("n") or 8}))
    failed = out.lstrip().startswith("Error:")
    body, cut = clip(ctx, out)
    return Result(body, ok=not failed, kind="error" if failed else "ok", head=q[:120], truncated=cut)


WEBFETCH = Tool(
    "WebFetch",
    "Fetch a URL and return it as text. Use it to open a page you found with WebSearch or that the user gave you.",
    {"url": {"type": "string", "description": "The full URL"},
     "format": {"type": "string", "enum": ["markdown", "text", "html"], "description": "markdown and text: readable text (default); html: the raw HTML"},
     "timeout": {"type": "number", "description": "Timeout in seconds (default 30, max 60)"}},
    webfetch, required=("url",), parallel=True)

WEBSEARCH = Tool(
    "WebSearch",
    "Search the web for current information. Returns titles, URLs and snippets; open the useful ones with WebFetch.",
    {"query": {"type": "string", "description": "Search words"},
     "n": {"type": "integer", "description": "Number of results (default 8)"}},
    websearch, required=("query",), parallel=True)
