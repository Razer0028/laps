"""WebSearch tool: DuckDuckGo HTML scrape, no API key needed.

Returns a list of {title, url, snippet} results. Capped at `n` (default 10).
Falls back to bare-text if BS4 is missing.

Note: DDG html endpoint may rate-limit. For heavy use, swap in a paid API
(Brave / Serper / Tavily) by setting LAPS_SEARCH_BACKEND env var.
"""
from __future__ import annotations
import os
import re
import urllib.parse
from html.parser import HTMLParser

import httpx

from . import limits
from . import ToolSpec, ToolResult
from .webfetch import num_or_default

_DDG_URL = "https://html.duckduckgo.com/html/"

#: DuckDuckGo が「本当に 0 件」のときだけ返す印（実測 2026-08-31）。
#:   結果あり(27.8KB): no-results 0 / result__a 10
#:   0 件   ( 9.4KB): no-results 4 / result__a 0 / "No results" 1
#: これが無いのに 0 件なら、**0 件ではなく読めなかった**と考える。
_DDG_ZERO_MARKERS = ("no-results", "No results")
_UA = "Mozilla/5.0 (X11; Linux x86_64) Blackwall/0.1"


def _parse_ddg(html_text: str, n: int) -> list[dict]:
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html_text, "html.parser")
        results = []
        for r in soup.select(".result")[:n]:
            a = r.select_one(".result__a")
            snip = r.select_one(".result__snippet")
            if not a:
                continue
            url = a.get("href", "")
            # DDG wraps redirects: /l/?uddg=...
            if "uddg=" in url:
                m = re.search(r"uddg=([^&]+)", url)
                if m:
                    url = urllib.parse.unquote(m.group(1))
            results.append({
                "title": a.get_text(strip=True),
                "url": url,
                "snippet": snip.get_text(strip=True) if snip else "",
            })
        return results
    except ImportError:
        # ★2026-10-12: 同梱の Python には bs4 が無く、旧い予備（正規表現）は題と URL しか拾わず、
        #   抜粋は常に空だった（作業係の WebSearch も声の係も、題名だけで答えていた）。
        #   標準の html.parser で題・URL・抜粋をそろえて読む（<b> などの中の印も外す）。
        return _parse_ddg_stdlib(html_text, n)


class _DDGParser(HTMLParser):
    """DuckDuckGo の html 版の結果を読む（.result__a＝題と URL・.result__snippet＝抜粋）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._cur: dict | None = None
        self._grab: str | None = None   # "title" / "snippet"
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if self._grab:
            self._depth += 1
            return
        if tag == "a" and "result__a" in cls:
            self._cur = {"title": "", "url": _ddg_url(a.get("href") or ""), "snippet": ""}
            self.results.append(self._cur)
            self._grab, self._depth, self._buf = "title", 0, []
        elif "result__snippet" in cls and self._cur is not None:
            self._grab, self._depth, self._buf = "snippet", 0, []

    def handle_endtag(self, tag):
        if not self._grab:
            return
        if self._depth > 0:
            self._depth -= 1
            return
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if self._cur is not None:
            self._cur[self._grab] = text
        self._grab = None

    def handle_data(self, data):
        if self._grab:
            self._buf.append(data)


def _ddg_url(url: str) -> str:
    # DDG wraps redirects: //duckduckgo.com/l/?uddg=...
    if "uddg=" in url:
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            return urllib.parse.unquote(m.group(1))
    return url


def _parse_ddg_stdlib(html_text: str, n: int) -> list[dict]:
    p = _DDGParser()
    try:
        p.feed(html_text)
        p.close()
    except Exception:  # noqa: BLE001 — 壊れた頁でも、読めたぶんは返す
        pass
    return [r for r in p.results if r.get("title")][:n]


_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_KEY = "BRAVE_SEARCH_API_KEY"


def _brave_search(q: str, n: int) -> list[dict] | None:
    """Brave Search API（公式）で引く。鍵が無い・宛先が許されていない・失敗したら None（DuckDuckGo に回る）。"""
    try:
        from .. import apikeys
        key = apikeys.value(_BRAVE_KEY)
        if not key:
            return None
        hosts = apikeys.hosts_of(_BRAVE_KEY)
        if hosts and not apikeys.host_allowed("api.search.brave.com", 443, hosts):
            return None
        with httpx.Client(timeout=20, headers={"Accept": "application/json", "X-Subscription-Token": key,
                                                "User-Agent": _UA}) as c:
            r = c.get(_BRAVE_URL, params={"q": q, "count": min(n, 20)})
        if r.status_code != 200:
            return None
        items = ((r.json() or {}).get("web") or {}).get("results") or []
        return [{"title": str(x.get("title") or ""), "url": str(x.get("url") or ""),
                 "snippet": re.sub(r"<[^>]+>", "", str(x.get("description") or ""))} for x in items[:n]]
    except Exception:
        return None


def _run(inp: dict) -> str:
    q = inp.get("query") or inp.get("q")
    if not q:
        return "Error: missing 'query'"
    # ★数として読めない n で落ちない（2026-09-02）。旧: 素の `int(...)`。
    #   モデルは "5件" / "all" / null を渡す。そのとき利用者に届くのは
    #   "Error in WebSearch: invalid literal for int()" だけで、
    #   **何を直せばよいか分からない**（検索そのものは 1 回も試していない）。
    #   ★判断は WebFetch の timeout と**同じ 1 本**を使う（別々に書くと、
    #     同じ問いに別の答えを持つことになる——この木で何度も踏んだ形）。
    _n, _nnote = num_or_default(inp.get("n"), 10.0, "n")
    n = max(1, min(int(_n), 25))

    backend = os.environ.get("LAPS_SEARCH_BACKEND", "ddg")
    # ★公式の検索 API（Brave Search API）の鍵が保存されていれば、そちらで引く（2026-10-23 オーナー「Blackwallの改善を進めてください」）。
    #   Terminal-Bench の記録で WebSearch の 30%（27 回中 8 回）が、DuckDuckGo の HTML 版が機械からの検索を断る HTTP 202 だった。
    #   その判定はくぐらない（ブラウザの名乗りや入口を替えない）。正式な道として、鍵がある時だけ公式の API を使う。
    if backend == "ddg":
        _brave = _brave_search(q, n)
        if _brave is not None:
            results, backend = _brave, "brave"
            backend_done = True
        else:
            backend_done = False
    else:
        backend_done = False
    if backend == "ddg" and not backend_done:
        try:
            with httpx.Client(timeout=20, headers={"User-Agent": _UA}, follow_redirects=True) as c:
                r = c.post(_DDG_URL, data={"q": q})
        except httpx.HTTPError as e:
            return f"Error: search fetch failed: {e}"
        if r.status_code >= 400:
            return f"Error: HTTP {r.status_code} from DuckDuckGo"
        results = _parse_ddg(r.text, n)
    elif backend != "brave":
        return f"Error: unsupported LAPS_SEARCH_BACKEND={backend}"

    if not results:
        # ★2026-08-31（レビュー E12・再現済み）: ここは「0 件」と「頁を読めなかった」を
        #   **同じ文字列**にしていた。実測:
        #     ・本当に 0 件の問い          → HTTP 200 / 9.4KB / 解析 0 件 → 0 件と同じ文言
        #     ・体裁の違う頁(lite 版)      → HTTP 200 / 22.1KB / 解析 0 件 → 0 件と同じ文言
        #   後者は**結果がぎっしり載っている頁**で、こちらの読み取りが外れただけ。
        #   区別できないので、モデルは「その話題は世の中に無い」と結論して先へ進む。
        #   Grep の終了コードを見ていなかった不具合とまったく同じ形。
        if backend == "brave":
            return ToolResult(f"(no results for {q!r})", ok=True, kind="empty")
        if any(m in r.text for m in _DDG_ZERO_MARKERS):
            return ToolResult(f"(no results for {q!r})", ok=True, kind="empty")
        # ★小さいモデルが読むので英語で・次の手を具体的に（2026-10-23）。202 は DuckDuckGo が機械からの検索を断った印。
        why = ("the search site refused automated searching for now (HTTP 202)" if r.status_code == 202 else
               f"the results page could not be read (HTTP {r.status_code}, {len(r.text):,} bytes, no 'no results' marker)")
        return (f"Error: web search did not work: {why}. This is NOT zero results. Do not repeat the same search right away. "
                "Instead: read the official documentation directly with WebFetch (for example the project's docs site or its "
                "GitHub README). "
                "A Brave Search API key saved in Settings → API keys as BRAVE_SEARCH_API_KEY makes web search use that "
                "official API instead.")
    lines = [f"Query: {q}", f"Backend: {backend}", f"Hits: {len(results)}"]
    if _nnote:
        lines.append(_nnote)
    lines.append("---")
    for i, item in enumerate(results, 1):
        lines.append(f"{i}. {item['title']}")
        lines.append(f"   {item['url']}")
        if item.get("snippet"):
            lines.append(f"   {item['snippet']}")
    # ★上限を持たせる（2026-08-19）。検索結果は外から来る文字列で、
    #   長さを決めているのは相手側。件数（n≤25）で抑えているだけでは、
    #   1件が異常に長いときに窓を潰せる。上限の考え方は limits に1本化。
    out = "\n".join(lines)
    cap = limits.out_cap(20000)
    if len(out) > cap:
        out = out[:cap] + limits.note(len(out), cap, "n を小さくして引き直してください。")
    return out


SPEC = ToolSpec(
    name="WebSearch",
    description=(
        "Search the web via DuckDuckGo HTML (no API key needed). "
        "Returns title/url/snippet for up to n=25 hits. "
        "Use it whenever you need information you are not sure of: documentation, how to use a program, "
        "official download pages and current versions, the meaning of an error message, recent facts. "
        "Searching is cheaper than guessing; then open the best results with WebFetch."
        # ★2026-10-16 監査: 「LAPS_SEARCH_BACKEND で検索先を替えられる」は開発者向けの話で、モデルには替えられない。外した。
    ),
    schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "n": {"type": "integer", "description": "Max results (default 10, max 25)"},
        },
        "required": ["query"],
    },
    readonly=True, parallel_safe=True,
    handler=_run,
)
