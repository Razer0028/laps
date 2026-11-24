"""Web のファイルの診断（2026-10-10・オーナー「今まで解析系を強化していたので、開発系を強化してください」「フルコード対応にしましょう」）。

編集・書き込みの直後に、HTML・JS・CSS の **参照切れ** と **埋め込みの script/style の構文** を見て、道具の結果に添える。
★書き込みは止めない（参照先は後から作ることもある）。報告だけ（opencode の LSP の診断と同じ役）。
★見るのはファイルの中で完結すること: 手元の相対パスが実在するか・埋め込みの JS/CSS の構文。サーバ側の経路（`/` 始まり）・
  外の URL・テンプレートの式（`{{ }}`・`${ }`）は見ない（当てずっぽうで「無い」と言わない）。
"""
from __future__ import annotations

import os
import re
from html.parser import HTMLParser
from urllib.parse import unquote

MAX_ITEMS = 12
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_SKIP_CHARS = ("{", "$", "<", "%", "`")
# 参照を持つ属性（タグ → 属性）。a は手元のページへのリンクだけを見る。
_REF_ATTRS = {
    "script": ("src",), "img": ("src",), "audio": ("src",), "video": ("src", "poster"), "source": ("src",),
    "track": ("src",), "iframe": ("src",), "embed": ("src",), "object": ("data",), "input": ("src",),
    "link": ("href",), "a": ("href",),
}
_LINK_RELS = ("stylesheet", "icon", "preload", "modulepreload", "manifest", "apple-touch-icon")


def _local_ref(ref: str) -> str | None:
    """手元の相対パスなら正規化して返す。外の URL・`/` 始まり・テンプレートの式・空なら None。"""
    r = (ref or "").strip()
    if not r or r.startswith(("#", "//", "/", "\\")) or _SCHEME.match(r) or any(c in r for c in _SKIP_CHARS):
        return None
    r = r.split("#", 1)[0].split("?", 1)[0]
    return unquote(r) if r else None


def _missing(base_dir: str, ref: str) -> bool:
    try:
        return not os.path.exists(os.path.normpath(os.path.join(base_dir, ref)))
    except (OSError, ValueError):
        return False


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[int, str, str]] = []           # (行, タグ.属性, 値)
        self.scripts: list[tuple[int, str, bool]] = []      # (行, 中身, module か)
        self.styles: list[tuple[int, str]] = []
        self._cur: tuple[str, int, bool] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        line = self.getpos()[0]
        for attr in _REF_ATTRS.get(tag, ()):
            if attr not in a:
                continue
            if tag == "link" and not any(r in a.get("rel", "").lower() for r in _LINK_RELS):
                continue
            if tag == "a" and not re.search(r"\.(html?|htm)$", a[attr].split("?")[0].split("#")[0], re.I):
                continue
            self.refs.append((line, f"{tag}.{attr}", a[attr]))
        if tag == "img" and a.get("srcset"):
            for part in a["srcset"].split(","):
                u = part.strip().split(" ")[0]
                if u:
                    self.refs.append((line, "img.srcset", u))
        if tag == "script" and "src" not in a:
            t = a.get("type", "").lower()
            if t in ("", "text/javascript", "application/javascript", "module"):
                self._cur, self._buf = ("script", line, t == "module"), []
        elif tag == "style":
            self._cur, self._buf = ("style", line, False), []

    def handle_data(self, data: str) -> None:
        if self._cur is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._cur is not None and tag == self._cur[0]:
            kind, line, module = self._cur
            body = "".join(self._buf)
            if kind == "script":
                self.scripts.append((line, body, module))
            else:
                self.styles.append((line, body))
            self._cur, self._buf = None, []


def _shift_line(msg: str, offset: int) -> str:
    """埋め込みの中の行番号を、ファイルの行番号に直す。"""
    def rep(m: re.Match) -> str:
        return f"行 {int(m.group(1)) + offset - 1}"
    return re.sub(r"行 (\d+)", rep, msg)


def diagnose(path: str, text: str) -> list[str]:
    """`path` に書いた `text` の診断（1 件 1 行・無ければ空）。"""
    p = path.lower()
    base = os.path.dirname(os.path.abspath(path))
    out: list[str] = []
    try:
        if p.endswith((".html", ".htm")):
            h = _Html()
            h.feed(text)
            h.close()
            for line, where, val in h.refs:
                ref = _local_ref(val)
                if ref and _missing(base, ref):
                    out.append(f"line {line}: {where} points to a file that does not exist: {ref}")
            from ..tools.edit import _js_syntax_error, _css_syntax_error
            for line, body, module in h.scripts[:6]:
                if body.strip():
                    e = _js_syntax_error("inline.mjs" if module else "inline.js", body)
                    if e:
                        out.append(f"<script> starting at line {line}: {_shift_line(e, line)}")
            for line, body in h.styles[:6]:
                e = _css_syntax_error(body)
                if e:
                    out.append(f"<style> starting at line {line}: {_shift_line(e, line)}")
        elif p.endswith((".js", ".mjs")):
            pat = re.compile(r"""(?:^\s*import\s+(?:[^'";]*?\sfrom\s+)?|import\(\s*|new\s+URL\(\s*)['"](\.{1,2}/[^'"]+)['"]""", re.M)
            for m in pat.finditer(text):
                ref = _local_ref(m.group(1))
                if ref and _missing(base, ref):
                    line = text.count("\n", 0, m.start()) + 1
                    out.append(f"line {line}: import points to a file that does not exist: {ref}")
        elif p.endswith(".css"):
            for m in re.finditer(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)|@import\s+['"]([^'"]+)['"]""", text):
                ref = _local_ref(m.group(1) or m.group(2) or "")
                if ref and _missing(base, ref):
                    line = text.count("\n", 0, m.start()) + 1
                    out.append(f"line {line}: url() points to a file that does not exist: {ref}")
    except Exception:
        return out
    if len(out) > MAX_ITEMS:
        rest = len(out) - MAX_ITEMS
        out = out[:MAX_ITEMS] + [f"... and {rest} more"]
    return out
