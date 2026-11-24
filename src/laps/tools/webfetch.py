"""WebFetch tool: GET a URL and return readable text (markdown-ish).

Strategy:
- httpx for the request (30s default, max 60s)
- BeautifulSoup if installed to strip nav/script/style; otherwise raw HTML
- Optional 'prompt' parameter is recorded but not used here — the agent
  re-reads the content with whatever instructions it needs.
- SSRF guard: rejects loopback / private RFC1918 / link-local / cloud metadata
  unless LAPS_ALLOW_PRIVATE_FETCH=1 is set.
"""
from __future__ import annotations
import ipaddress
import os
import re
import html
import socket
from urllib.parse import urlparse

import httpx

from . import limits
from . import ToolSpec

_MAX_BYTES = 4 * 1024 * 1024  # 4 MiB hard cap on download
_MAX_OUT_CHARS = 50_000       # 50K char cap returned to model

#: 文章として読んでよい Content-Type。**allowlist にする**——
#: 「バイナリを弾く」形（denylist）は必ず漏れる。実機で漏れたのは
#: application/octet-stream だが、次に来るのが何かは分からない。
_TEXT_TYPES = (
    "text/", "application/json", "application/xml", "application/xhtml",
    "application/javascript", "application/ecmascript", "application/rss",
    "application/atom", "application/x-yaml", "application/yaml",
    "application/ld+json", "application/graphql", "application/x-ndjson",
    "application/sql", "image/svg+xml",
)


def _refusal(url: str, status: int, ctype: str, nbytes: str) -> str:
    """文章でないものを断るときの返事。

    ★**何であるかを名指しする。** 「読めません」だけだと、モデルは
      同じ URL を別の道具で取りに行く。実機で踏んだのは 29,900,480 バイトの
      実行ファイルで、断らなかったために PE ヘッダ以降のバイナリが
      50,175 文字ぶん文脈へ入り、走行が context_overflow で終わった。
    """
    kind = (ctype or "").split(";")[0].strip() or "不明"
    return (f"URL: {url}\nStatus: {status}\nContent-Type: {ctype}\n"
            f"Bytes: {nbytes}\n---\n"
            f"Error: 文章ではない内容なので読み込みませんでした（{kind}）。\n"
            "この道具は文章を読むためのものです。実行ファイル・書庫・画像・"
            "音声などは、中身を文脈に入れても意味がなく窓を潰します。\n"
            "ファイルとして保存したいなら、その旨を利用者に確認してください。")


def _out_cap() -> int:
    """1回の取得が返してよい文字数。上限の考え方は `limits` に1本化してある。"""
    return limits.out_cap(_MAX_OUT_CHARS)


def _looks_like_text(ctype: str, raw: bytes) -> bool:
    """文章として読めるか。**種類の申告と中身の両方**を見る。

    種類だけでは足りない: `text/plain` を名乗って中身がバイナリのことがある。
    中身だけでも足りない: 短い JSON などは判定材料が少ない。
    """
    ct = (ctype or "").split(";")[0].strip().lower()
    if ct and not any(ct.startswith(t) for t in _TEXT_TYPES):
        return False
    if not raw:
        return True
    head = raw[:4096]
    if b"\x00" in head:               # NUL を含む＝ほぼ確実にバイナリ
        return False
    # 復号して置換文字（U+FFFD）が多いなら中身がバイナリ。
    # ★UTF-8 で読めなくても、Shift_JIS・EUC-JP として読めるなら文章（2026-10-16 の点検: 日本語の多い Shift_JIS のページを
    #   「文章ではない」と断っていた）。終わりの 1 字が途中で切れている分は数えない。
    for enc in ("utf-8", "cp932", "euc-jp"):
        dec = head.decode(enc, errors="replace")
        if not dec or dec[:-1].count("\ufffd") / len(dec) <= 0.10:
            return True
    return False


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?\s*([A-Za-z0-9_\-]+)""", re.I)


def _page_encoding(header_enc: str | None, ctype: str, raw: bytes) -> str | None:
    """本文の符号化。見出しの charset → <meta charset> → 中身から推す（UTF-8・cp932・EUC-JP）。
    ★2026-10-16 の点検: 見出しに charset の無い Shift_JIS のページを UTF-8 で読み、日本語が `���{��` に化けていた。"""
    if "charset=" in (ctype or "").lower() and header_enc:
        return header_enc
    m = _META_CHARSET.search(raw[:4096])
    if m:
        name = m.group(1).decode("ascii", "replace").lower()
        name = {"shift_jis": "cp932", "shift-jis": "cp932", "sjis": "cp932", "x-sjis": "cp932", "windows-31j": "cp932"}.get(name, name)
        try:
            "".encode(name)
            return name
        except LookupError:
            pass
    for enc in ("utf-8", "cp932", "euc-jp"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return header_enc


_DEFAULT_UA = "Blackwall/0.1 (+https://github.com/blackwall) httpx"

# Cloud metadata + special-use hosts always denied
_METADATA_HOSTS = {
    "169.254.169.254",   # AWS / Azure / GCP / OpenStack
    "metadata.google.internal",
    "metadata.aws",
    "metadata",
}


def _is_private_url(url: str) -> tuple[bool, str]:
    """Return (is_private, reason). Resolves DNS so a name pointing at an
    internal address is caught, not just a literal IP.

    ★2026-09-02: 旧 docstring は「dynamic redirects to internal IPs を捉える」と
      書いていたが、**それは嘘だった**——この関数は与えられた 1 本の URL しか見ず、
      呼び出し側は `follow_redirects=True` で httpx に転送を任せていたので、
      外向きの URL が 302 で `169.254.169.254`（クラウドのメタデータ＝資格情報）へ
      飛ぶ経路は**素通り**する。事実に反する註釈は、無い註釈より悪い
      （読んだ人が守られていると信じて、そこを検査しなくなる）。
      → 転送は下の `_fetch_following_redirects` で 1 段ずつ辿り、**毎回ここを通す**。
    """
    try:
        u = urlparse(url)
    except Exception as e:
        return True, f"unparseable URL: {e}"
    host = (u.hostname or "").lower()
    if not host:
        return True, "missing host"
    if host in _METADATA_HOSTS:
        return True, f"cloud metadata host: {host}"
    # Resolve DNS
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        return True, f"DNS lookup failed: {e}"
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback:
            return True, f"loopback: {addr}"
        if ip.is_private:
            return True, f"private (RFC1918): {addr}"
        if ip.is_link_local:
            return True, f"link-local: {addr}"
        if ip.is_reserved:
            return True, f"reserved: {addr}"
    return False, ""


#: 転送を辿る上限。無限転送で回り続けないための数。
_MAX_REDIRECTS = 5


def num_or_default(value, default: float, name: str) -> tuple[float, str]:
    """調整用の数値引数を読む。読めなければ**既定に落として、そう言う**。

    ★1 か所に置く理由（2026-09-02）: `webfetch` の timeout と `websearch` の n は
      同じ問いで、危うく**別々の答え**を実装しかけた（片方は Error で断り、
      片方は黙って既定に落とす）。この木で何度も踏んでいる「片方で直して
      片方に残す」を、書いている最中に自分でやりかけた。

    ★どちらに倒すか: **仕事は続ける。** 「30s」と書かれたら望みは 30 秒であって、
      書式の綴りを理由に取得そのものを断るのは、利用者の目的から遠い。
      ただし**黙って変えない**（黙ると、設定したつもりで別の値で走る）。
    """
    if value is None or value == "" or value == [] or value == {}:
        return default, ""
    try:
        return float(value), ""
    except (TypeError, ValueError):
        return default, f"（{name}={value!r} は数として読めないので既定 {default:g} を使いました）"


def _check_hop(url: str) -> str:
    """1 ホップ分の可否。通ってよければ ""、駄目なら理由。"""
    if os.environ.get("LAPS_ALLOW_PRIVATE_FETCH") == "1":
        return ""
    priv, reason = _is_private_url(url)
    if not priv:
        return ""
    # ★「名前が引けない」を「内部アドレスだから塞いだ」と言わない（原因が消える）。
    if reason.startswith("DNS lookup failed"):
        return f"名前を解決できません: {reason}"
    return (f"blocked private/internal URL ({reason}). "
            "Set LAPS_ALLOW_PRIVATE_FETCH=1 to allow.")


def _html_to_text(body: str) -> str:
    try:
        from bs4 import BeautifulSoup  # type: ignore
    except ImportError:
        # Lightweight fallback
        body = re.sub(r"<script[\s\S]*?</script>", "", body, flags=re.I)
        body = re.sub(r"<style[\s\S]*?</style>", "", body, flags=re.I)
        body = re.sub(r"<[^>]+>", "", body)
        return html.unescape(body)
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse runs of blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


#: Stack Exchange の質問の URL（★2026-10-23 Terminal-Bench の記録: stackoverflow.com の頁は 403 で断られた・WebFetch の失敗 11 回のうち 7 回）。
#: 頁を無理に取りに行かず、サイトが機械からの読み取りのために出している公式の API（api.stackexchange.com・鍵なしで 1 日 300 回）から
#: 質問と答えの本文を取る。使うのは頁が 403 を返したときだけ。
_SE_QUESTION = re.compile(r"^https?://(?:www\.)?((?:[a-z0-9-]+\.)?(?:stackoverflow|superuser|serverfault|askubuntu|stackexchange)\.com"
                          r"|mathoverflow\.net)/questions/(\d+)", re.I)


def _se_site(host: str) -> str:
    """API の site 引数。★ドメインのまま渡す（API はドメインでも受ける）。2026-10-25 敵対レビュー: 旧は stackexchange.com 以外を
    先頭の部分だけにしていて、ja.stackoverflow.com が "ja"・meta.stackoverflow.com が "meta" になり、API は
    「No site found for name `ja`」で断っていた（日本語版・各国語版・meta は読めなかった）。"""
    h = host.lower()
    return h[4:] if h.startswith("www.") else h


def _stackexchange(url: str, timeout: float) -> str | None:
    """Stack Exchange の公式 API から質問と答え（票の多い順に 5 つまで）を読む。読めなければ None。"""
    m = _SE_QUESTION.match(url or "")
    if not m:
        return None
    site, qid = _se_site(m.group(1)), m.group(2)
    base = "https://api.stackexchange.com/2.3"
    try:
        with httpx.Client(timeout=timeout, headers={"User-Agent": _DEFAULT_UA}) as c:
            q = c.get(f"{base}/questions/{qid}", params={"site": site, "filter": "withbody"})
            a = c.get(f"{base}/questions/{qid}/answers",
                      params={"site": site, "filter": "withbody", "sort": "votes", "order": "desc", "pagesize": 5})
        if q.status_code != 200:
            return None
        items = (q.json() or {}).get("items") or []
        if not items:
            return None
        qi = items[0]
        out = [f"Question: {html.unescape(str(qi.get('title') or ''))}  (score {qi.get('score')}, "
               f"{qi.get('answer_count', 0)} answers)", _html_to_text(str(qi.get("body") or ""))]
        ans = ((a.json() or {}).get("items") or []) if a.status_code == 200 else []
        for i, x in enumerate(ans, 1):
            mark = " ✓ accepted" if x.get("is_accepted") else ""
            out.append(f"\n--- Answer {i} (score {x.get('score')}{mark})\n" + _html_to_text(str(x.get("body") or "")))
        return "\n".join(out)
    except (httpx.HTTPError, ValueError):
        return None


def _run(inp: dict) -> str:
    url = inp.get("url")
    # ★数として読めない timeout で落ちない（モデルは "30s" や null を渡す）
    timeout, _tnote = num_or_default(inp.get("timeout"), 30.0, "timeout")
    timeout = min(max(timeout, 1.0), 60.0)
    if not url:
        return "Error: missing 'url'"
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"Error: url must start with http(s)://: {url}"
    # SSRF guard（最初の 1 本）。転送先は下のループで**毎ホップ**同じ検査を通す。
    _bad = _check_hop(url)
    if _bad:
        return f"Error: {_bad}"
    # ★**読む前に断る**（2026-08-19）。素の `c.get(url)` は本文を全部
    #   ダウンロードしてから種類を見ることになる。実機が踏んだ相手は
    #   29,900,480 バイトの実行ファイルで、断ると決めている物を丸ごと
    #   取りに行くのは時間も帯域も無駄。ヘッダを見て、必要な分だけ読む。
    try:
        with httpx.Client(
            # ★**転送を httpx に任せない**（2026-09-02）。旧: `follow_redirects=True`。
            #   SSRF の検査は最初の URL にしか掛かっていなかったので、
            #   外向きの URL が 302 で `169.254.169.254`（クラウドのメタデータ＝
            #   一時資格情報が読める）や社内の管理画面へ飛ぶと**そのまま取りに行った**。
            #   `_is_private_url` の docstring は「dynamic redirects を捉える」と
            #   書いてあり、**申告と実体が食い違っていた**（この木で何度も踏んだ形）。
            #   → 自分で 1 段ずつ辿り、行き先を毎回 `_check_hop` に通す。
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": _DEFAULT_UA},
        ) as c:
            cur, hops = url, 0
            while True:
                with c.stream("GET", cur) as r:
                    if r.is_redirect:
                        loc = r.headers.get("location", "")
                        if not loc:
                            return (f"Error: HTTP {r.status_code} for {cur} "
                                    "（転送と言っているのに転送先が無い）")
                        nxt = str(httpx.URL(cur).join(loc))
                        hops += 1
                        if hops > _MAX_REDIRECTS:
                            return (f"Error: 転送が {_MAX_REDIRECTS} 回を超えました"
                                    f"（最後の行き先: {nxt}）")
                        if not nxt.lower().startswith(("http://", "https://")):
                            return f"Error: http(s) 以外への転送は辿りません: {nxt}"
                        _bad = _check_hop(nxt)
                        if _bad:
                            # ★どこから飛ばされたかを必ず言う（原因が消えると
                            #   「この URL は読めない」としか分からない）。
                            return (f"Error: {_bad}\n"
                                    f"　→ {cur} から {nxt} へ転送されました。")
                        cur = nxt
                        continue
                    if r.status_code >= 400:
                        if r.status_code == 403 and _SE_QUESTION.match(cur):
                            se = _stackexchange(cur, timeout)
                            if se:
                                from . import ToolResult
                                cap = _out_cap()
                                if len(se) > cap:
                                    se = se[:cap] + limits.note(len(se), cap, "必要な箇所を Grep で探してください。")
                                return ToolResult(f"URL: {cur}\nStatus: 403 (the page refused; read through the official "
                                                  "Stack Exchange API instead)\nSource: api.stackexchange.com\n---\n" + se,
                                                  ok=True, kind="ok")
                        return f"Error: HTTP {r.status_code} for {cur}"
                    ctype = r.headers.get("content-type", "")
                    declared = r.headers.get("content-length")
                    # 種類の申告だけで断れるなら、本文は1バイトも読まない。
                    if not _looks_like_text(ctype, b""):
                        return _refusal(cur, r.status_code, ctype, declared or "?")
                    chunks, total = [], 0
                    for chunk in r.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= _MAX_BYTES:
                            break
                    raw = b"".join(chunks)[:_MAX_BYTES]
                    encoding = r.encoding
                    status = r.status_code
                    nbytes = declared or str(total)
                    url = cur          # 見出しには**実際に読んだ**URL を出す
                    break
    except httpx.HTTPError as e:
        return f"Error: fetch failed: {e}"

    # ★申告が文章でも、中身がバイナリのことがある（`text/plain` を名乗る実行ファイル）。
    #   読んだあとに改めて見る。ここで断ると、モデルは「これは実行ファイルだ」と
    #   分かって別の手を選べる——黙って化けた文字を渡すと、それを内容だと思う。
    if not _looks_like_text(ctype, raw):
        return _refusal(url, status, ctype, nbytes)

    # ★format="html" なら印を残したまま返す（2026-10-15・oc の WebFetch の format が渡っていなかった）。
    encoding = _page_encoding(encoding, ctype, raw)
    if ("html" in ctype.lower() or raw.lstrip().startswith(b"<")) and str(inp.get("format") or "").lower() != "html":
        text = _html_to_text(raw.decode(encoding or "utf-8", errors="replace"))
    else:
        text = raw.decode(encoding or "utf-8", errors="replace")

    cap = _out_cap()
    if len(text) > cap:
        text = text[:cap] + limits.note(
            len(text), cap, "URL を絞るか、必要な箇所を Grep で探してください。")
    head = (f"URL: {url}\nStatus: {status}\nContent-Type: {ctype}\n"
            f"Bytes: {nbytes}\n{_tnote + chr(10) if _tnote else ''}---\n")
    # ★成功を型で名乗る（2026-09-21）。素の str で返すと `as_result` が
    #   **取ってきたページの中身**で成否を当てにいく。★この道具も確実に当たる:
    #   開発者が読むのは「`command not found` の直し方」のような**エラーについての
    #   ページ**なので、役に立つ取得ほど失敗に見える。
    from . import ToolResult
    return ToolResult(head + text, ok=True, kind="ok")


SPEC = ToolSpec(
    name="WebFetch",
    description=(
        "Fetch a URL via HTTP GET and return its readable text content. "
        "Follows redirects, strips scripts/styles/nav from HTML. "
        "文章でない内容（実行ファイル・書庫・画像など）は読み込まない。"
        "返す量は推論器の窓に合わせて自動で絞る。"
    ),
    schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full http(s):// URL"},
            "timeout": {"type": "number", "description": "Seconds, default 30, max 60"},
            "prompt": {"type": "string", "description": "Optional context (not used by fetch itself)"},
        },
        "required": ["url"],
    },
    readonly=True, parallel_safe=True,
    handler=_run,
)
