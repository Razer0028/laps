"""画像を利用者の画面に出す道具 Image（2026-11-20 オーナー「ネットから探してきた画像や、ローカルにある画像を画面に表示できるように」）。

- 手元のファイル（path）かウェブの画像（url）を、会話のフォルダの shots/ に写し、道具の行に見本として出す（押すと大きく）。
  画面が読めるのは shots/ の中だけ（app の shot_data の決まり）。だから必ずここへ写す。
- 答えの本文に `![説明](<写した場所>)` と書けば、本文の中にも出る（画面の renderMarkdown）。
- 見られるモデルには画像そのものも渡す（Read の画像と同じ）。
守り: 読みは policy の authorize_read を通す。ウェブは http/https だけ・画像の型だけ・20MB まで。SVG は受けない（中に命令を持てる）。
大きい画像は Pillow があれば長辺 2,560 px・JPEG に縮める（無ければ 12MB を超える物は断る）。
"""
from __future__ import annotations

import io
import os
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import Ctx, Result, Tool, fail

MAX_FETCH = 20 * 1024 * 1024        # 取ってくる・読む上限
MAX_SHOW = 12 * 1024 * 1024         # 画面へ渡す上限（app の shot_data と揃える）
MAX_SIDE = 2560
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
         "image/bmp": ".bmp"}
EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"BM", ".bmp"))

DESC = ("Show an image to the user in the app: a local file (path) or an image on the web (url). The picture is copied into "
        "this conversation and appears large under your answer (the user can open it full size). Call it once per picture you "
        "want the user to see (a chart you made, a found photo, a result to check); you do not need to paste it again.\n"
        "- {\"path\":\"C:/work/chart.png\"}  - {\"url\":\"https://example.com/photo.jpg\",\"caption\":\"...\"}\n"
        "Only png, jpg, gif, webp and bmp (not svg). Find web images first (WebSearch / WebFetch / Browser), then pass the "
        "image's own URL (the file, not the page).")
SCHEMA = {"type": "object", "properties": {
    "path": {"type": "string", "description": "local image file"},
    "url": {"type": "string", "description": "http(s) URL of the image file itself"},
    "caption": {"type": "string", "description": "short caption shown under the image"}}}


def _sniff(data: bytes) -> str:
    for sig, ext in _MAGIC:
        if data.startswith(sig):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def _shrink(data: bytes, ext: str) -> tuple[bytes, str, str]:
    """大きすぎる画像を縮める。（中身, 拡張子, 一言）。Pillow が無い・読めないときは元のまま。"""
    if len(data) <= MAX_SHOW and ext != ".bmp":
        try:
            from PIL import Image as _I
            with _I.open(io.BytesIO(data)) as im:
                if max(im.size) <= MAX_SIDE:
                    return data, ext, ""
        except Exception:  # noqa: BLE001
            return data, ext, ""
    try:
        from PIL import Image as _I
        with _I.open(io.BytesIO(data)) as im:
            w, h = im.size
            if getattr(im, "is_animated", False) and len(data) <= MAX_SHOW:
                return data, ext, ""          # 動く GIF は縮めない（縮めると 1 枚になる）
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=88)
            return out.getvalue(), ".jpg", f"shrunk from {w}x{h} to {im.size[0]}x{im.size[1]}"
    except Exception:  # noqa: BLE001
        return data, ext, ""


def _fetch(url: str) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (L.A.P.S image viewer)",
                                               "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=25) as r:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = r.read(MAX_FETCH + 1)
    if len(data) > MAX_FETCH:
        raise ValueError("the image is larger than 20 MB")
    return data, ctype


def show(inp: dict, ctx: Ctx) -> Result:
    path = str(inp.get("path") or inp.get("file") or inp.get("file_path") or "").strip().strip('"')
    url = str(inp.get("url") or inp.get("src") or "").strip()
    if not path and not url:
        return fail("give path (a local image file) or url (the image's own http(s) address).", head="Image")
    if path.lower().startswith(("http://", "https://")) and not url:
        path, url = "", path
    caption = str(inp.get("caption") or "").strip()[:200]
    if url:
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ("http", "https"):
            return fail("url must start with http:// or https://", head=url[:120])
        try:
            data, ctype = _fetch(url)
        except Exception as e:  # noqa: BLE001
            return fail(f"could not download the image: {type(e).__name__}: {str(e)[:200]}", head=url[:120])
        if "svg" in ctype:
            return fail("SVG images are not shown (they can carry scripts). Find a png/jpg version.", head=url[:120])
        ext = TYPES.get(ctype) or _sniff(data)
        if not ext or (ctype.startswith("text/") and not _sniff(data)):
            return fail(f"that address is not an image (Content-Type {ctype or 'unknown'}). It is probably a web page: "
                        "find the image's own URL in it (Browser read, or WebFetch), then pass that.", head=url[:120])
        src = url
    else:
        from ...policy import PolicyDenied, authorize_read
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(ctx.workspace, p)
        p = os.path.normpath(p)
        try:
            authorize_read(p)
        except PolicyDenied as e:
            return fail(str(e), kind="denied", head=path[:120])
        if not os.path.isfile(p):
            return fail(f"no such file: {p}", head=path[:120])
        ext = os.path.splitext(p)[1].lower()
        if ext == ".svg":
            return fail("SVG images are not shown (they can carry scripts). Export it as png first.", head=path[:120])
        if ext not in EXTS:
            return fail(f"not an image this viewer shows ({ext or 'no extension'}). Use png, jpg, gif, webp or bmp.", head=path[:120])
        if os.path.getsize(p) > MAX_FETCH:
            return fail("the image is larger than 20 MB", head=path[:120])
        data = Path(p).read_bytes()
        ext = ".jpg" if ext == ".jpeg" else ext
        src = p
    data, ext, note = _shrink(data, ext)
    if ext == ".bmp" or len(data) > MAX_SHOW:
        return fail("the image is too large (or a bmp) to show and could not be shrunk here. Convert it to png or jpg "
                    "smaller than 12 MB first.", head=(url or path)[:120])
    d = Path(ctx.root) / "shots"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / f"img-{uuid.uuid4().hex[:12]}{ext}"
    dst.write_bytes(data)
    lines = [f"Shown to the user: it appears large under your answer when you finish. Source: {src}", f"Saved copy: {dst}"]
    if note:
        lines.append(f"({note})")
    lines.append("Do not paste it again with ![...](...) unless it must sit at a specific place in the text "
                 f"(then: ![{caption or 'image'}]({dst}) on its own line).")
    head = caption or (os.path.basename(urllib.parse.urlsplit(url).path) if url else os.path.basename(path)) or "image"
    return Result("\n".join(lines), head=head[:120], tail=f"{len(data) // 1024} KB", effect=False, shot=str(dst),
                  images=[str(dst)] if getattr(ctx, "vision", False) else [],
                  show=[{"path": str(dst), "caption": caption}])


# ── チャットモード（talk）の Image（2026-11-24）────────────────────────────────────────────────
# オーナー「Android の方も…モデルがチャット欄に画像を表示してくれる機能をより優れたものに」。Chat は作業フォルダーを持たず、手元の
# ファイルに触れない（base_tools の talk）。そこで写すのはウェブの画像だけにした Image を渡す（path は断る）。写した先は会話のフォルダの
# shots/（作業場ではない）なので、Chat の「ファイルに触れない」は崩れない。
WEB_DESC = ("Show a picture from the web to the user in the chat. Pass the image's own http(s) URL (the file, not the page): "
            "find it with WebSearch / WebFetch first. The picture is copied into this conversation and appears in the chat; "
            "to show it again inside your answer, write ![caption](<the saved path>) on its own line. png, jpg, gif, webp only.")
WEB_SCHEMA = {"url": {"type": "string", "description": "http(s) URL of the image file itself"},
              "caption": {"type": "string", "description": "short caption shown under the image"}}


def web_show(inp: dict, ctx: Ctx) -> Result:
    if str(inp.get("path") or inp.get("file") or inp.get("file_path") or "").strip() and not str(inp.get("url") or "").strip():
        p = str(inp.get("path") or inp.get("file") or inp.get("file_path")).strip()
        if not p.lower().startswith(("http://", "https://")):
            return fail("Chat mode has no access to files. Pass url (an image on the web). Agent mode can show local files.",
                        kind="denied", head=p[:120])
    return show({"url": inp.get("url") or inp.get("path") or inp.get("src"), "caption": inp.get("caption")}, ctx)


WEB_IMAGE = Tool("Image", WEB_DESC, WEB_SCHEMA, web_show, required=("url",))
