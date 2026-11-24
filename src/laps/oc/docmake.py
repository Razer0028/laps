"""資料作成（Word / PowerPoint / Excel / PDF を作る・読む）。2026-10-08 オーナー「資料作成のツールや Skills も欲しい」。

★blackwall を import しない（Windows は同梱の Python で動くが、Linux では専用の環境で別の process として走らせる）。
★中身は Markdown で受ける（見出し・段落・箇条書き・番号つき・表・コード・引用・画像・区切り・改ページ・グラフ）。
  グラフは ```chart の中に JSON（{"type": "column", "categories": [...], "series": [{"name": ..., "values": [...]}]}）。
  Word と PDF は matplotlib の画像、PowerPoint と Excel はネイティブのグラフ（開いた人が編集できる）。
★日本語: Word / PowerPoint は東アジアの字体も指定する。PDF は Windows の游ゴシック・メイリオ、Linux の IPA などを埋め込み、
  無ければ reportlab の CID 字体（HeiseiKakuGo-W5・埋め込みなし）に落とす。行の折り返しは CJK の規則。

命令行: python docmake.py <依頼.json> <結果.json>   依頼 = {"action": "create" | "read", ...}（doctool.py が作る）
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

PALETTE = ["#3A6EA5", "#E07B39", "#4C9F70", "#C44E52", "#8172B2", "#937860", "#D17DB8", "#7F7F7F"]
CJK_FONTS_WIN = ["Yu Gothic", "Meiryo", "MS Gothic"]
CJK_FONTS_OTHER = ["IPAexGothic", "IPAGothic", "Noto Sans CJK JP", "TakaoGothic", "VL Gothic"]


def default_font() -> str:
    return "Yu Gothic" if sys.platform == "win32" else "Noto Sans CJK JP"


# ── Markdown → 塊 ──────────────────────────────────────────────────────

def _md():
    from markdown_it import MarkdownIt
    return MarkdownIt("commonmark", {"breaks": False, "html": True}).enable("table").enable("strikethrough")


def _inline(children) -> list[tuple[str, dict]]:
    runs: list[tuple[str, dict]] = []
    st = {"b": False, "i": False, "s": False, "link": None}
    for t in children or []:
        ty = t.type
        if ty == "text":
            runs.append((t.content, dict(st)))
        elif ty == "code_inline":
            runs.append((t.content, dict(st, code=True)))
        elif ty in ("softbreak",):
            runs.append((" ", dict(st)))
        elif ty == "hardbreak":
            runs.append(("\n", dict(st)))
        elif ty == "strong_open":
            st["b"] = True
        elif ty == "strong_close":
            st["b"] = False
        elif ty == "em_open":
            st["i"] = True
        elif ty == "em_close":
            st["i"] = False
        elif ty == "s_open":
            st["s"] = True
        elif ty == "s_close":
            st["s"] = False
        elif ty == "link_open":
            st["link"] = t.attrGet("href")
        elif ty == "link_close":
            st["link"] = None
        elif ty == "image":
            runs.append((f"[{t.content or 'image'}]", dict(st)))
        elif ty == "html_inline":
            if re.fullmatch(r"<br\s*/?>", t.content.strip(), re.I):
                runs.append(("\n", dict(st)))
    return runs


def _only_image(tok) -> dict | None:
    kids = [c for c in (tok.children or []) if not (c.type == "text" and not c.content.strip()) and c.type != "softbreak"]
    if len(kids) == 1 and kids[0].type == "image":
        return {"t": "img", "src": kids[0].attrGet("src") or "", "alt": kids[0].content or ""}
    return None


def parse_markdown(text: str) -> list[dict]:
    toks = _md().parse(text or "")
    pos = 0

    def blocks(until: str | None) -> list[dict]:
        nonlocal pos
        out: list[dict] = []
        while pos < len(toks):
            t = toks[pos]
            if until and t.type == until:
                pos += 1
                return out
            pos += 1
            if t.type == "heading_open":
                inl = toks[pos]
                pos += 2
                out.append({"t": "h", "level": int(t.tag[1]), "inl": _inline(inl.children)})
            elif t.type == "paragraph_open":
                inl = toks[pos]
                pos += 2
                img = _only_image(inl)
                out.append(img or {"t": "p", "inl": _inline(inl.children)})
            elif t.type in ("bullet_list_open", "ordered_list_open"):
                start = int(t.attrGet("start") or 1) if t.type == "ordered_list_open" else 1
                close = t.type.replace("_open", "_close")
                items = []
                while pos < len(toks) and toks[pos].type != close:
                    if toks[pos].type == "list_item_open":
                        pos += 1
                        items.append(blocks("list_item_close"))
                    else:
                        pos += 1
                pos += 1
                out.append({"t": "ol" if t.type.startswith("ordered") else "ul", "items": items, "start": start})
            elif t.type in ("fence", "code_block"):
                info = (t.info or "").strip().lower()
                if info.startswith("chart"):
                    try:
                        out.append({"t": "chart", "spec": json.loads(t.content)})
                    except ValueError as e:
                        out.append({"t": "p", "inl": [(f"(chart JSON could not be read: {e})", {})]})
                else:
                    out.append({"t": "code", "lang": info, "text": t.content.rstrip("\n")})
            elif t.type == "blockquote_open":
                out.append({"t": "quote", "blocks": blocks("blockquote_close")})
            elif t.type == "hr":
                out.append({"t": "hr"})
            elif t.type == "html_block":
                if re.search(r"<!--\s*page\s*break\s*-->|<div[^>]*page-break", t.content, re.I):
                    out.append({"t": "pagebreak"})
            elif t.type == "table_open":
                all_rows: list[list] = []
                aligns: list[str] = []
                cur: list | None = None
                while pos < len(toks) and toks[pos].type != "table_close":
                    x = toks[pos]
                    if x.type == "tr_open":
                        cur = []
                    elif x.type == "th_open":
                        st = x.attrGet("style") or ""
                        aligns.append("right" if "right" in st else "center" if "center" in st else "left")
                    elif x.type == "inline" and cur is not None:
                        cur.append(_inline(x.children))
                    elif x.type == "tr_close" and cur is not None:
                        all_rows.append(cur)
                        cur = None
                    pos += 1
                pos += 1
                if all_rows:
                    out.append({"t": "table", "head": all_rows[0], "rows": all_rows[1:], "align": aligns})
        return out

    return blocks(None)


def plain(runs: list[tuple[str, dict]]) -> str:
    return "".join(r[0] for r in runs)


# ── グラフ（matplotlib の画像）─────────────────────────────────────────

def _mpl_font() -> str | None:
    from matplotlib import font_manager
    names = {f.name for f in font_manager.fontManager.ttflist}
    for n in (CJK_FONTS_WIN if sys.platform == "win32" else CJK_FONTS_OTHER) + CJK_FONTS_WIN + CJK_FONTS_OTHER:
        if n in names:
            return n
    return None


def chart_png(spec: dict, width_in: float = 6.4, height_in: float = 3.6, dpi: int = 200) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    f = _mpl_font()
    rc = {"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True,
          "grid.color": "#DDDDDD", "grid.linewidth": 0.6, "axes.axisbelow": True, "axes.edgecolor": "#888888"}
    if f:
        rc["font.family"] = f
    typ = str(spec.get("type") or "column").lower()
    cats = [str(c) for c in spec.get("categories") or []]
    series = [s for s in spec.get("series") or [] if isinstance(s, dict)]
    with plt.rc_context(rc):
        fig, ax = plt.subplots(figsize=(width_in, height_in), dpi=dpi)
        n = max(1, len(series))
        if typ == "pie":
            vals = [float(v) for v in (series[0].get("values") if series else [])]
            ax.pie(vals, labels=cats, colors=PALETTE[:len(vals)], autopct="%1.0f%%", startangle=90, counterclock=False,
                   wedgeprops={"linewidth": 1.5, "edgecolor": "white"})
            ax.axis("equal")
            ax.grid(False)
        elif typ in ("bar", "column"):
            import numpy as np
            x = np.arange(len(cats))
            w = 0.8 / n
            for i, s in enumerate(series):
                vals = [float(v or 0) for v in s.get("values") or []]
                pos = x - 0.4 + w * (i + 0.5)
                if typ == "column":
                    ax.bar(pos[:len(vals)], vals, w * 0.92, label=s.get("name"), color=PALETTE[i % len(PALETTE)])
                else:
                    ax.barh(pos[:len(vals)], vals, w * 0.92, label=s.get("name"), color=PALETTE[i % len(PALETTE)])
            if typ == "column":
                ax.set_xticks(x, cats)
                ax.grid(axis="x", visible=False)
            else:
                ax.set_yticks(x, cats)
                ax.invert_yaxis()
                ax.grid(axis="y", visible=False)
        elif typ == "scatter":
            for i, s in enumerate(series):
                pts = s.get("points") or list(zip(s.get("x") or [], s.get("values") or []))
                ax.scatter([float(p[0]) for p in pts], [float(p[1]) for p in pts], s=24, label=s.get("name"),
                           color=PALETTE[i % len(PALETTE)])
        else:   # line / area
            for i, s in enumerate(series):
                vals = [float(v) if v is not None else float("nan") for v in s.get("values") or []]
                xs = list(range(len(vals)))
                ax.plot(xs, vals, label=s.get("name"), color=PALETTE[i % len(PALETTE)], linewidth=2,
                        marker="o" if len(vals) <= 24 else None, markersize=4)
                if typ == "area":
                    ax.fill_between(xs, vals, alpha=0.18, color=PALETTE[i % len(PALETTE)])
            ax.set_xticks(range(len(cats)), cats)
            ax.grid(axis="x", visible=False)
        if spec.get("title"):
            ax.set_title(str(spec["title"]), loc="left", fontsize=12, fontweight="bold")
        if spec.get("x_label"):
            ax.set_xlabel(str(spec["x_label"]))
        if spec.get("y_label"):
            ax.set_ylabel(str(spec["y_label"]))
        if typ != "pie" and len(series) > 1:
            ax.legend(frameon=False, loc="best")
        if len(cats) > 6 and typ in ("column", "line", "area"):
            plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi)
        plt.close(fig)
    return buf.getvalue()


def _img_path(src: str, base: str) -> Path | None:
    if not src or re.match(r"^[a-z]+://", src, re.I):
        return None
    p = Path(src)
    if not p.is_absolute():
        p = Path(base) / p
    return p if p.is_file() else None


# ── Word ───────────────────────────────────────────────────────────────

def _docx_ea(run_or_style, font: str) -> None:
    from docx.oxml.ns import qn
    el = run_or_style.element if hasattr(run_or_style, "element") else run_or_style._element
    rpr = el.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.insert(0, rfonts)
    for k in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        rfonts.set(qn(k), font)


def _docx_link(par, url: str, text: str, fmt: dict, font: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    rid = par.part.relate_to(url, RT.HYPERLINK, is_external=True)
    h = OxmlElement("w:hyperlink")
    h.set(qn("r:id"), rid)
    r = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    c = OxmlElement("w:color")
    c.set(qn("w:val"), "1F5FAF")
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rpr.append(c)
    rpr.append(u)
    if fmt.get("b"):
        rpr.append(OxmlElement("w:b"))
    r.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    h.append(r)
    par._p.append(h)


def _docx_runs(par, runs, font: str, mono: str = "Consolas") -> None:
    from docx.shared import RGBColor
    for text, f in runs:
        if f.get("link"):
            _docx_link(par, f["link"], text, f, font)
            continue
        parts = text.split("\n")
        for i, part in enumerate(parts):
            r = par.add_run(part)
            r.bold = f.get("b") or None
            r.italic = f.get("i") or None
            if f.get("s"):
                r.font.strike = True
            if f.get("code"):
                r.font.name = mono
                r.font.color.rgb = RGBColor(0x8A, 0x2B, 0x2B)
            if i < len(parts) - 1:
                r.add_break()


def _docx_shade(par, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    ppr = par._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    ppr.append(shd)


def _docx_border_bottom(par) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    ppr = par._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "BFBFBF")):
        b.set(qn(k), v)
    bdr.append(b)
    ppr.append(bdr)


def _first_heading(md: Any) -> str:
    """Markdown の最初の行が見出しならその文言。"""
    for ln in str(md or "").splitlines():
        if ln.strip():
            m = re.match(r"#{1,6}\s+(.+?)(?:\s+#+)?$", ln.strip())
            return m.group(1).strip() if m else ""
    return ""


def build_docx(req: dict) -> dict:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Mm, Pt, RGBColor
    warnings: list[str] = []
    tpl = req.get("template")
    doc = Document(tpl) if tpl else Document()
    if tpl and not req.get("keep_template_content"):
        # 雛形の本文（見本の文）は消し、書式・ヘッダー・フッター・用紙（sectPr）だけ残す
        body = doc.element.body
        for child in list(body):
            if not child.tag.endswith("}sectPr"):
                body.remove(child)
    font = (req.get("theme") or {}).get("font") or default_font()
    base = req.get("base_dir") or os.getcwd()
    if not tpl:
        sec = doc.sections[0]
        page = str(req.get("page") or "A4").lower()
        w, h = (Mm(216), Mm(279)) if page == "letter" else (Mm(210), Mm(297))
        if req.get("landscape"):
            sec.orientation = WD_ORIENT.LANDSCAPE
            w, h = h, w
        sec.page_width, sec.page_height = w, h
        for side in ("left_margin", "right_margin"):
            setattr(sec, side, Mm(22))
        sec.top_margin, sec.bottom_margin = Mm(20), Mm(20)
        st = doc.styles["Normal"]
        st.font.name = font
        st.font.size = Pt(10.5)
        _docx_ea(st, font)
        st.paragraph_format.space_after = Pt(6)
        st.paragraph_format.line_spacing = 1.35
        for lvl, size in ((1, 16), (2, 13.5), (3, 12), (4, 11)):
            try:
                hs = doc.styles[f"Heading {lvl}"]
                hs.font.name = font
                hs.font.size = Pt(size)
                hs.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
                _docx_ea(hs, font)
            except KeyError:
                pass
        try:
            ts = doc.styles["Title"]
            ts.font.name = font
            _docx_ea(ts, font)
        except KeyError:
            pass
    doc.core_properties.title = str(req.get("title") or "")
    if req.get("author"):
        doc.core_properties.author = str(req["author"])
    # 本文の先頭が同じ見出しなら二重にしない。目次があるときは表紙に題名が要るので残す
    if req.get("title") and (req.get("toc") or str(req["title"]).strip() != _first_heading(req.get("content"))):
        doc.add_heading(str(req["title"]), 0)
    if req.get("toc"):
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        p = doc.add_paragraph()
        r = p.add_run()
        for tag, attrs, text in (("w:fldChar", {"w:fldCharType": "begin"}, None),
                                 ("w:instrText", {"xml:space": "preserve"}, 'TOC \\o "1-3" \\h \\z \\u'),
                                 ("w:fldChar", {"w:fldCharType": "separate"}, None)):
            el = OxmlElement(tag)
            for k, v in attrs.items():
                el.set(qn(k), v)
            if text:
                el.text = text
            r._r.append(el)
        r2 = p.add_run("（目次: Word で開いて F9 で更新）")
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        r2._r.append(end)
        doc.add_page_break()
    usable = doc.sections[-1].page_width - doc.sections[-1].left_margin - doc.sections[-1].right_margin

    def style_or(name: str, fallback: str = "Normal") -> str:
        try:
            doc.styles[name]
            return name
        except KeyError:
            return fallback

    def emit(blocks: list[dict], depth: int = 0) -> None:
        for b in blocks:
            t = b["t"]
            if t == "h":
                par = doc.add_heading("", min(9, max(1, b["level"])))
                _docx_runs(par, b["inl"], font)
            elif t == "p":
                par = doc.add_paragraph(style=style_or("List Paragraph") if depth else None)
                _docx_runs(par, b["inl"], font)
            elif t in ("ul", "ol"):
                for n, item in enumerate(b["items"]):
                    first, rest = (item[0], item[1:]) if item and item[0]["t"] == "p" else (None, item)
                    if t == "ul":
                        name = "List Bullet" + (f" {min(depth + 1, 3)}" if depth else "")
                        par = doc.add_paragraph(style=style_or(name, style_or("List Bullet")))
                    else:
                        par = doc.add_paragraph(style=style_or("List Paragraph"))
                        par.paragraph_format.left_indent = Mm(7 + 6 * depth)
                        par.paragraph_format.first_line_indent = Mm(-6)
                        par.add_run(f"{b['start'] + n}. ")
                    if first:
                        _docx_runs(par, first["inl"], font)
                    emit(rest, depth + 1)
            elif t == "code":
                par = doc.add_paragraph()
                _docx_shade(par, "F2F2F2")
                par.paragraph_format.left_indent = Mm(3)
                lines = b["text"].split("\n")
                for i, line in enumerate(lines):
                    r = par.add_run(line)
                    r.font.name = "Consolas"
                    r.font.size = Pt(9)
                    if i < len(lines) - 1:
                        r.add_break()
            elif t == "quote":
                for qb in b["blocks"]:
                    if qb["t"] == "p":
                        par = doc.add_paragraph(style=style_or("Quote"))
                        _docx_runs(par, qb["inl"], font)
                    else:
                        emit([qb], depth)
            elif t == "table":
                cols = max([len(b["head"])] + [len(r) for r in b["rows"]] or [1])
                tbl = doc.add_table(rows=1 + len(b["rows"]), cols=cols)
                tbl.style = style_or("Table Grid", tbl.style.name if tbl.style else "Normal Table")
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                for ri, row in enumerate([b["head"]] + b["rows"]):
                    for ci in range(cols):
                        cell = tbl.cell(ri, ci)
                        par = cell.paragraphs[0]
                        runs = row[ci] if ci < len(row) else []
                        _docx_runs(par, [(x, dict(f, b=True) if ri == 0 else f) for x, f in runs], font)
                        al = b["align"][ci] if ci < len(b["align"]) else "left"
                        par.alignment = {"right": WD_ALIGN_PARAGRAPH.RIGHT, "center": WD_ALIGN_PARAGRAPH.CENTER}.get(al)
                        if ri == 0:
                            from docx.oxml import OxmlElement
                            from docx.oxml.ns import qn
                            tcpr = cell._tc.get_or_add_tcPr()
                            shd = OxmlElement("w:shd")
                            shd.set(qn("w:val"), "clear")
                            shd.set(qn("w:fill"), "E8EEF5")
                            tcpr.append(shd)
                doc.add_paragraph()
            elif t == "hr":
                _docx_border_bottom(doc.add_paragraph())
            elif t == "pagebreak":
                doc.add_page_break()
            elif t == "img":
                p = _img_path(b["src"], base)
                if p is None:
                    warnings.append(f"image not found: {b['src']}")
                    continue
                try:
                    from PIL import Image
                    with Image.open(p) as im:
                        wpx, hpx = im.size
                    width = min(usable, int(usable * min(1.0, wpx / 1400)) or usable)
                    doc.add_picture(str(p), width=width)
                    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                    if b.get("alt"):
                        cap = doc.add_paragraph(b["alt"], style=style_or("Caption"))
                        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"image {b['src']}: {e}")
            elif t == "chart":
                try:
                    png = chart_png(b["spec"])
                    doc.add_picture(io.BytesIO(png), width=usable)
                    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"chart: {type(e).__name__}: {e}")

    emit(parse_markdown(req.get("content") or ""))
    doc.save(req["path"])
    return {"warnings": warnings, "paragraphs": len(doc.paragraphs), "tables": len(doc.tables)}


# ── PowerPoint ─────────────────────────────────────────────────────────

def _hex(c: str):
    from pptx.dml.color import RGBColor
    c = str(c or "").lstrip("#")
    return RGBColor.from_string(c.upper()) if re.fullmatch(r"[0-9a-fA-F]{6}", c) else None


def _pptx_font(run, font: str) -> None:
    from pptx.oxml.ns import qn
    run.font.name = font
    rpr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rpr.find(qn(tag))
        if el is None:
            el = rpr.makeelement(qn(tag), {})
            rpr.append(el)
        el.set("typeface", font)


def _pptx_runs(par, runs, font: str, size=None, color=None) -> None:
    from pptx.util import Pt
    for text, f in runs:
        r = par.add_run()
        r.text = text.replace("\n", "\v")
        _pptx_font(r, "Consolas" if f.get("code") else font)
        if f.get("code"):
            from pptx.oxml.ns import qn
            r._r.get_or_add_rPr().find(qn("a:ea")).set("typeface", font)
        if f.get("b"):
            r.font.bold = True
        if f.get("i"):
            r.font.italic = True
        if size:
            r.font.size = Pt(size)
        if color is not None:
            r.font.color.rgb = color
        if f.get("link"):
            r.hyperlink.address = f["link"]


def slides_from_markdown(text: str) -> list[dict]:
    """`---` の行で区切る（Marp と同じ）。区切りが無ければ # / ## の見出しで区切る。各枚の最初の見出しが題。"""
    text = text or ""
    if re.search(r"(?m)^---\s*$", text):
        chunks = re.split(r"(?m)^---\s*$", text)
    else:
        chunks = re.split(r"(?m)^(?=#{1,2} )", text)
    out = []
    for ch in chunks:
        if not ch.strip():
            continue
        notes = ""
        m = re.search(r"(?s)<!--\s*notes?:?(.*?)-->", ch)
        if m:
            notes, ch = m.group(1).strip(), ch[:m.start()] + ch[m.end():]
        blocks = parse_markdown(ch)
        title = ""
        if blocks and blocks[0]["t"] == "h":
            title = plain(blocks[0]["inl"])
            lvl = blocks[0]["level"]
            blocks = blocks[1:]
        else:
            lvl = 2
        out.append({"title": title, "blocks": blocks, "notes": notes, "level": lvl})
    return out


def build_pptx(req: dict) -> dict:
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData, XyChartData
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    from pptx.util import Emu, Inches, Pt
    warnings: list[str] = []
    theme = req.get("theme") or {}
    font = theme.get("font") or default_font()
    accent = _hex(theme.get("accent") or "#3A6EA5")
    dark = bool(theme.get("dark"))
    fg = _hex("#F2F2F2" if dark else "#1F1F1F")
    sub = _hex("#BFBFBF" if dark else "#595959")
    base = req.get("base_dir") or os.getcwd()
    tpl = req.get("template")
    prs = Presentation(tpl) if tpl else Presentation()
    if not tpl:
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    elif not req.get("keep_template_content"):
        # 雛形の見本のスライドは消し、マスターとレイアウトだけ使う
        lst = prs.slides._sldIdLst
        for sid in list(lst):
            prs.part.drop_rel(sid.rId)
            lst.remove(sid)
    W, H = prs.slide_width, prs.slide_height
    layouts = prs.slide_layouts

    def layout(i: int):
        """0 = 表紙（中央の題）・5 = 題だけ。雛形ではレイアウトの番号が違うので、枠の種類で探す。"""
        from pptx.enum.shapes import PP_PLACEHOLDER
        if tpl:
            for lo in layouts:
                kinds = [ph.placeholder_format.type for ph in lo.placeholders]
                if i == 0 and PP_PLACEHOLDER.CENTER_TITLE in kinds:
                    return lo
                if i == 5 and PP_PLACEHOLDER.TITLE in kinds and not any(
                        k in kinds for k in (PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.SUBTITLE)):
                    return lo
        return layouts[min(i, len(layouts) - 1)]

    slides = req.get("slides")
    if not slides:
        slides = slides_from_markdown(req.get("content") or "")
    else:
        norm = []
        for s in slides:
            s = dict(s)
            blocks = parse_markdown(s.get("body") or s.get("content") or "")
            if s.get("bullets"):
                blocks.insert(0, {"t": "ul", "start": 1, "items": [[{"t": "p", "inl": [(str(x), {})]}]
                                                                    for x in s["bullets"]]})
            if s.get("table"):
                tb = s["table"]
                blocks.append({"t": "table", "head": [[(str(c), {})] for c in tb.get("header") or []],
                               "rows": [[[(str(c), {})] for c in r] for r in tb.get("rows") or []], "align": []})
            if s.get("chart"):
                blocks.append({"t": "chart", "spec": s["chart"]})
            if s.get("image"):
                blocks.append({"t": "img", "src": s["image"], "alt": ""})
            norm.append({"title": s.get("title") or "", "subtitle": s.get("subtitle") or "", "blocks": blocks,
                         "notes": s.get("notes") or "", "kind": s.get("kind") or s.get("layout") or "",
                         "level": 2})
        slides = norm
    title_txt = req.get("title")
    if title_txt and not (slides and slides[0].get("kind") == "title"):
        slides.insert(0, {"title": title_txt, "subtitle": req.get("subtitle") or req.get("author") or "",
                          "blocks": [], "notes": "", "kind": "title"})

    def background(slide) -> None:
        if dark and not tpl:
            fill = slide.background.fill
            fill.solid()
            fill.fore_color.rgb = _hex("#16181D")

    def bar(slide) -> None:
        if tpl:
            return
        from pptx.enum.shapes import MSO_SHAPE
        shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.6), Inches(0.42), Inches(0.9), Emu(54000))
        shp.fill.solid()
        shp.fill.fore_color.rgb = accent
        shp.line.fill.background()

    def set_title(slide, text: str, size: int = 30) -> None:
        if slide.shapes.title is None:
            return
        tf = slide.shapes.title.text_frame
        tf.clear()
        p = tf.paragraphs[0]
        _pptx_runs(p, [(text, {"b": True})], font, size, fg)
        if not tpl:
            p.alignment = PP_ALIGN.LEFT       # 左の色の帯と揃える（既定の雛形は中央寄せ）
            sh = slide.shapes.title
            sh.left, sh.top, sh.width, sh.height = Inches(0.6), Inches(0.55), W - Inches(1.2), Inches(1.0)
            tf.vertical_anchor = MSO_ANCHOR.TOP

    def body_box(slide, left, top, width, height, items: list[dict]) -> None:
        tb = slide.shapes.add_textbox(left, top, width, height)
        tf = tb.text_frame
        tf.word_wrap = True
        lines = sum(1 for _ in items)
        size = 20 if lines <= 6 else 18 if lines <= 9 else 16 if lines <= 12 else 14
        first = True

        def para(runs, lvl: int, bullet: str | None) -> None:
            nonlocal first
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            p.level = min(lvl, 4)
            p.space_after = Pt(6)
            pre = [(("　" * lvl) + bullet + " ", {})] if bullet else []
            _pptx_runs(p, pre + runs, font, size - 2 * min(lvl, 2), fg if lvl == 0 else sub)

        def walk(blocks, lvl: int) -> None:
            for b in blocks:
                if b["t"] == "p":
                    para(b["inl"], lvl, None)
                elif b["t"] == "h":
                    para([(plain(b["inl"]), {"b": True})], lvl, None)
                elif b["t"] in ("ul", "ol"):
                    for n, item in enumerate(b["items"]):
                        mark = "•" if b["t"] == "ul" else f"{b['start'] + n}."
                        if item and item[0]["t"] == "p":
                            para(item[0]["inl"], lvl, mark)
                            walk(item[1:], lvl + 1)
                        else:
                            walk(item, lvl + 1)
                elif b["t"] == "code":
                    for line in b["text"].split("\n"):
                        para([(line, {"code": True})], lvl, None)
                elif b["t"] == "quote":
                    walk(b["blocks"], lvl)
        walk(items, 0)

    def visual(slide, b: dict, left, top, width, height) -> None:
        t = b["t"]
        if t == "table":
            rows = [b["head"]] + b["rows"]
            cols = max(len(r) for r in rows)
            rh = min(Inches(0.5), int(height / max(1, len(rows))))
            gt = slide.shapes.add_table(len(rows), cols, left, top, width, rh * len(rows)).table
            size = 14 if len(rows) <= 8 else 12 if len(rows) <= 14 else 10
            for ri, row in enumerate(rows):
                for ci in range(cols):
                    cell = gt.cell(ri, ci)
                    cell.text_frame.clear()
                    p = cell.text_frame.paragraphs[0]
                    runs = row[ci] if ci < len(row) else []
                    _pptx_runs(p, [(x, dict(f, b=True) if ri == 0 else f) for x, f in runs], font, size)
                    if ri == 0 and not tpl:
                        cell.fill.solid()
                        cell.fill.fore_color.rgb = accent
                        for r in p.runs:
                            r.font.color.rgb = _hex("#FFFFFF")
        elif t == "chart":
            spec = b["spec"]
            typ = str(spec.get("type") or "column").lower()
            cats = [str(c) for c in spec.get("categories") or []]
            if typ == "scatter":
                cd = XyChartData()
                for s in spec.get("series") or []:
                    ser = cd.add_series(str(s.get("name") or ""))
                    for x, y in (s.get("points") or list(zip(s.get("x") or [], s.get("values") or []))):
                        ser.add_data_point(float(x), float(y))
                ct = XL_CHART_TYPE.XY_SCATTER
            else:
                cd = CategoryChartData()
                cd.categories = cats
                for s in spec.get("series") or []:
                    cd.add_series(str(s.get("name") or ""), [float(v) if v is not None else None
                                                             for v in s.get("values") or []])
                ct = {"bar": XL_CHART_TYPE.BAR_CLUSTERED, "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
                      "line": XL_CHART_TYPE.LINE_MARKERS, "pie": XL_CHART_TYPE.PIE,
                      "area": XL_CHART_TYPE.AREA, "doughnut": XL_CHART_TYPE.DOUGHNUT}.get(typ, XL_CHART_TYPE.COLUMN_CLUSTERED)
            ch = slide.shapes.add_chart(ct, left, top, width, height, cd).chart
            ch.font.size = Pt(12)
            ch.font.name = font
            if dark:
                ch.font.color.rgb = fg            # 暗い背景で軸と凡例の文字が沈まないように
            n_series = len(spec.get("series") or [])
            ch.has_legend = n_series > 1 or typ in ("pie", "doughnut")
            if ch.has_legend:
                ch.legend.position = XL_LEGEND_POSITION.BOTTOM
                ch.legend.include_in_layout = False
            if spec.get("title"):
                ch.has_title = True
                ch.chart_title.text_frame.text = str(spec["title"])
            if typ in ("pie", "doughnut"):
                plot = ch.plots[0]
                plot.has_data_labels = True
                plot.data_labels.number_format = "0%"
                plot.data_labels.show_percentage = True
                plot.data_labels.show_value = False
            for i, s in enumerate(ch.series):
                if typ not in ("pie", "doughnut"):
                    try:
                        s.format.fill.solid()
                        s.format.fill.fore_color.rgb = _hex(PALETTE[i % len(PALETTE)])
                    except Exception:
                        pass
                    try:
                        s.format.line.color.rgb = _hex(PALETTE[i % len(PALETTE)])
                    except Exception:
                        pass
                else:
                    for j, pt in enumerate(s.points):
                        pt.format.fill.solid()
                        pt.format.fill.fore_color.rgb = _hex(PALETTE[j % len(PALETTE)])
        elif t == "img":
            p = _img_path(b["src"], base)
            if p is None:
                warnings.append(f"image not found: {b['src']}")
                return
            from PIL import Image
            with Image.open(p) as im:
                iw, ih = im.size
            scale = min(width / iw, height / ih)
            w, h = int(iw * scale), int(ih * scale)
            slide.shapes.add_picture(str(p), left + (width - w) // 2, top + (height - h) // 2, w, h)

    for s in slides:
        kind = s.get("kind") or ""
        blocks = s.get("blocks") or []
        if kind == "title" or (s.get("level") == 1 and not blocks):
            sl = prs.slides.add_slide(layout(0))
            background(sl)
            if sl.shapes.title is not None:
                tf = sl.shapes.title.text_frame
                tf.clear()
                _pptx_runs(tf.paragraphs[0], [(s.get("title") or "", {"b": True})], font, 40, fg)
            subt = s.get("subtitle") or ""
            if not subt and blocks:
                subt = " ".join(plain(b.get("inl") or []) for b in blocks if b["t"] == "p")
            if len(sl.placeholders) > 1:
                ph = sl.placeholders[1]
                ph.text_frame.clear()
                _pptx_runs(ph.text_frame.paragraphs[0], [(subt, {})], font, 20, sub)
            if not tpl:
                # 既定の雛形は 4:3 の位置のまま（16:9 にしても枠は動かない）。左寄せで置き直し、上に色の帯。
                from pptx.enum.shapes import MSO_SHAPE
                if sl.shapes.title is not None:
                    t = sl.shapes.title
                    t.left, t.top, t.width, t.height = Inches(0.9), Inches(2.5), W - Inches(1.8), Inches(1.5)
                    t.text_frame.vertical_anchor = MSO_ANCHOR.BOTTOM
                    for p in t.text_frame.paragraphs:
                        p.alignment = PP_ALIGN.LEFT
                if len(sl.placeholders) > 1:
                    ph = sl.placeholders[1]
                    ph.left, ph.top, ph.width, ph.height = Inches(0.9), Inches(4.25), W - Inches(1.8), Inches(1.2)
                    ph.text_frame.vertical_anchor = MSO_ANCHOR.TOP
                    for p in ph.text_frame.paragraphs:
                        p.alignment = PP_ALIGN.LEFT
                shp = sl.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.9), Inches(4.1), Inches(1.4), Emu(72000))
                shp.fill.solid()
                shp.fill.fore_color.rgb = accent
                shp.line.fill.background()
        else:
            sl = prs.slides.add_slide(layout(5))
            background(sl)
            bar(sl)
            set_title(sl, s.get("title") or "")
            texts = [b for b in blocks if b["t"] in ("p", "h", "ul", "ol", "code", "quote")]
            visuals = [b for b in blocks if b["t"] in ("table", "chart", "img")]
            L, T = Inches(0.6), Inches(1.65)
            CW, CH = W - Inches(1.2), H - Inches(2.2)
            if texts and visuals:
                half = int(CW * 0.46)
                body_box(sl, L, T, half, CH, texts)
                vis_l, vis_w = L + half + Inches(0.3), CW - half - Inches(0.3)
                each = int(CH / len(visuals))
                for i, v in enumerate(visuals):
                    visual(sl, v, vis_l, T + each * i, vis_w, each - Inches(0.1))
            elif visuals:
                each = int(CH / len(visuals))
                for i, v in enumerate(visuals):
                    visual(sl, v, L, T + each * i, CW, each - Inches(0.1))
            elif texts:
                body_box(sl, L, T, CW, CH, texts)
        if s.get("notes"):
            sl.notes_slide.notes_text_frame.text = str(s["notes"])
    prs.core_properties.title = str(req.get("title") or "")
    prs.save(req["path"])
    return {"warnings": warnings, "slides": len(prs.slides)}


# ── Excel ──────────────────────────────────────────────────────────────

def _cell_width(v) -> int:
    s = str(v if v is not None else "")
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def build_xlsx(req: dict) -> dict:
    from openpyxl import Workbook, load_workbook
    from openpyxl.chart import AreaChart, BarChart, LineChart, PieChart, Reference, ScatterChart, Series
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import column_index_from_string, get_column_letter
    warnings: list[str] = []
    theme = req.get("theme") or {}
    font = theme.get("font") or default_font()
    accent = str(theme.get("accent") or "#3A6EA5").lstrip("#").upper()
    sheets = req.get("sheets") or []
    if not sheets and req.get("content"):
        for i, b in enumerate(b for b in parse_markdown(req["content"]) if b["t"] == "table"):
            sheets.append({"name": f"Sheet{i + 1}", "rows": [[plain(c) for c in b["head"]]] +
                           [[plain(c) for c in r] for r in b["rows"]]})
    if not sheets:
        raise ValueError("give sheets=[{name, rows: [[...], ...]}] (or Markdown tables in content)")
    tpl = req.get("template")
    wb = load_workbook(tpl) if tpl else Workbook()
    if not tpl:
        wb.remove(wb.active)
    thin = Side(style="thin", color="D0D0D0")
    for spec in sheets:
        name = re.sub(r"[\[\]:*?/\\]", "_", str(spec.get("name") or f"Sheet{len(wb.sheetnames) + 1}"))[:31]
        existing = name in wb.sheetnames
        ws = wb[name] if existing else wb.create_sheet(name)
        rows = spec.get("rows") or []
        header = spec.get("header", True)
        if existing:
            # 雛形のシートには 1 行目から値だけを書く（書式・列幅・色は雛形のまま）
            r0 = int(spec.get("start_row") or 1)
            for ri, r in enumerate(rows):
                for ci, v in enumerate(r if isinstance(r, list) else [r]):
                    ws.cell(row=r0 + ri, column=ci + 1, value=_xl_value(v))
            continue
        for r in rows:
            ws.append([_xl_value(v) for v in (r if isinstance(r, list) else [r])])
        ncols = max((len(r) for r in rows if isinstance(r, list)), default=0)
        body_font = Font(name=font, size=10)
        for row in ws.iter_rows(min_row=1, max_row=len(rows), max_col=ncols):
            for c in row:
                c.font = body_font
                c.border = Border(top=thin, bottom=thin, left=thin, right=thin)
        if header and rows:
            for c in ws[1][:ncols]:
                c.font = Font(name=font, size=10, bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor=accent)
                c.alignment = Alignment(vertical="center", wrap_text=True)
            ws.freeze_panes = spec.get("freeze") or "A2"
            if spec.get("filter", True) and len(rows) > 1 and ncols:
                ws.auto_filter.ref = f"A1:{get_column_letter(ncols)}{len(rows)}"
        elif spec.get("freeze"):
            ws.freeze_panes = spec["freeze"]
        widths = spec.get("widths") or {}
        for ci in range(1, ncols + 1):
            letter = get_column_letter(ci)
            w = widths.get(letter) if isinstance(widths, dict) else (widths[ci - 1] if ci - 1 < len(widths) else None)
            if not w:
                w = min(60, max(8, max((_cell_width(r[ci - 1]) for r in rows if isinstance(r, list) and ci - 1 < len(r)
                                        and not str(r[ci - 1]).startswith("=")), default=8) + 2))
            ws.column_dimensions[letter].width = w
        for col, fmt in (spec.get("number_formats") or {}).items():
            ci = column_index_from_string(col)
            for row in ws.iter_rows(min_row=2 if header else 1, max_row=len(rows), min_col=ci, max_col=ci):
                for c in row:
                    c.number_format = fmt
        for ch_spec in ([spec["chart"]] if isinstance(spec.get("chart"), dict) else spec.get("charts") or []):
            try:
                typ = str(ch_spec.get("type") or "column").lower()
                cat_col = column_index_from_string(str(ch_spec.get("categories") or "A"))
                ser_cols = [column_index_from_string(str(c)) for c in (ch_spec.get("series") or ["B"])]
                last = len(rows)
                if typ == "scatter":
                    ch = ScatterChart()
                    xs = Reference(ws, min_col=cat_col, min_row=2, max_row=last)
                    for sc in ser_cols:
                        ys = Reference(ws, min_col=sc, min_row=1, max_row=last)
                        se = Series(ys, xs, title_from_data=True)
                        se.marker.symbol = "circle"
                        se.graphicalProperties.line.noFill = True
                        ch.series.append(se)
                else:
                    ch = {"bar": BarChart, "column": BarChart, "line": LineChart, "pie": PieChart,
                          "area": AreaChart}.get(typ, BarChart)()
                    if typ == "bar":
                        ch.type = "bar"
                    elif typ == "column":
                        ch.type = "col"
                    for sc in ser_cols:
                        ch.add_data(Reference(ws, min_col=sc, min_row=1, max_row=last), titles_from_data=True)
                    ch.set_categories(Reference(ws, min_col=cat_col, min_row=2, max_row=last))
                if ch_spec.get("title"):
                    ch.title = str(ch_spec["title"])
                if ch_spec.get("x_label") and hasattr(ch, "x_axis"):
                    ch.x_axis.title = str(ch_spec["x_label"])
                if ch_spec.get("y_label") and hasattr(ch, "y_axis"):
                    ch.y_axis.title = str(ch_spec["y_label"])
                ch.width, ch.height = float(ch_spec.get("width") or 18), float(ch_spec.get("height") or 9)
                ws.add_chart(ch, str(ch_spec.get("anchor") or f"{get_column_letter(ncols + 2)}2"))
            except Exception as e:  # noqa: BLE001
                warnings.append(f"chart on {name}: {type(e).__name__}: {e}")
    wb.save(req["path"])
    return {"warnings": warnings, "sheets": wb.sheetnames}


def _xl_value(v):
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+", s) and len(s) < 16 and not (len(s) > 1 and s.startswith("0")):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
    return v


# ── PDF ────────────────────────────────────────────────────────────────

def _pdf_fonts() -> tuple[str, str, list[str]]:
    """(本文の字体, 太字の字体, 注記)。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    notes: list[str] = []
    windir = os.environ.get("WINDIR", r"C:\Windows")
    cands = [
        (Path(windir) / "Fonts" / "YuGothM.ttc", Path(windir) / "Fonts" / "YuGothB.ttc"),
        (Path(windir) / "Fonts" / "meiryo.ttc", Path(windir) / "Fonts" / "meiryob.ttc"),
        (Path(windir) / "Fonts" / "msgothic.ttc", None),
        (Path("/usr/share/fonts/opentype/ipaexfont-gothic/ipaexg.ttf"), None),
        (Path("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"), None),
        (Path("/usr/share/fonts/truetype/takao-gothic/TakaoPGothic.ttf"), None),
        (Path("/usr/share/fonts/opentype/ipafont-gothic/ipagp.ttf"), None),
    ]
    for reg, bold in cands:
        if not reg.is_file():
            continue
        try:
            kw = {"subfontIndex": 0} if reg.suffix.lower() == ".ttc" else {}
            pdfmetrics.registerFont(TTFont("BWBody", str(reg), **kw))
            bname = "BWBody"
            if bold and bold.is_file():
                try:
                    pdfmetrics.registerFont(TTFont("BWBold", str(bold), **kw))
                    bname = "BWBold"
                except Exception:
                    pass
            from reportlab.lib.fonts import addMapping
            addMapping("BWBody", 0, 0, "BWBody")
            addMapping("BWBody", 1, 0, bname)
            addMapping("BWBody", 0, 1, "BWBody")
            addMapping("BWBody", 1, 1, bname)
            return "BWBody", bname, notes
        except Exception as e:  # noqa: BLE001
            notes.append(f"{reg.name}: {e}")
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    notes.append("no Japanese TrueType font was found; used the CID font HeiseiKakuGo-W5 (not embedded)")
    return "HeiseiKakuGo-W5", "HeiseiKakuGo-W5", notes


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pdf_inline(runs, mono: str = "Courier") -> str:
    out = []
    for text, f in runs:
        t = _esc(text).replace("\n", "<br/>")
        if f.get("code"):
            t = (f'<font face="{mono}" color="#8A2B2B">{t}</font>' if all(ord(ch) < 128 for ch in text)
                 else f'<font color="#8A2B2B">{t}</font>')
        if f.get("b"):
            t = f"<b>{t}</b>"
        if f.get("i"):
            t = f"<i>{t}</i>"
        if f.get("s"):
            t = f"<strike>{t}</strike>"
        if f.get("link"):
            t = f'<a href="{_esc(f["link"])}" color="#1F5FAF"><u>{t}</u></a>'
        out.append(t)
    return "".join(out)


def build_pdf(req: dict) -> dict:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (HRFlowable, Image, KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph,
                                    Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle)
    body, bold, notes = _pdf_fonts()
    warnings = list(notes)
    base = req.get("base_dir") or os.getcwd()
    size = letter if str(req.get("page") or "").lower() == "letter" else A4
    if req.get("landscape"):
        size = landscape(size)
    accent = colors.HexColor((req.get("theme") or {}).get("accent") or "#1F3A5F")
    S = {
        "title": ParagraphStyle("t", fontName=bold, fontSize=20, leading=28, spaceAfter=10, textColor=accent, wordWrap="CJK"),
        "h1": ParagraphStyle("h1", fontName=bold, fontSize=15, leading=22, spaceBefore=12, spaceAfter=6, textColor=accent, wordWrap="CJK"),
        "h2": ParagraphStyle("h2", fontName=bold, fontSize=12.5, leading=19, spaceBefore=10, spaceAfter=4, textColor=accent, wordWrap="CJK"),
        "h3": ParagraphStyle("h3", fontName=bold, fontSize=11, leading=17, spaceBefore=8, spaceAfter=3, wordWrap="CJK"),
        "p": ParagraphStyle("p", fontName=body, fontSize=10, leading=16.5, spaceAfter=5, wordWrap="CJK"),
        "q": ParagraphStyle("q", fontName=body, fontSize=10, leading=16, leftIndent=10, textColor=colors.HexColor("#555555"),
                            borderPadding=(2, 0, 2, 6), wordWrap="CJK"),
        "code": ParagraphStyle("c", fontName="Courier", fontSize=8.5, leading=11.5, backColor=colors.HexColor("#F2F2F2"),
                               borderPadding=5, spaceBefore=4, spaceAfter=8),
        "cell": ParagraphStyle("cell", fontName=body, fontSize=9, leading=13, wordWrap="CJK"),
        "cap": ParagraphStyle("cap", fontName=body, fontSize=8.5, leading=12, alignment=TA_CENTER, textColor=colors.HexColor("#666666")),
    }
    frame_w = size[0] - 40 * mm
    story: list = []
    if req.get("title"):
        story.append(Paragraph(_esc(str(req["title"])), S["title"]))
        story.append(HRFlowable(width="100%", color=accent, thickness=1.2, spaceAfter=10))

    def flow(blocks, depth: int = 0) -> list:
        out: list = []
        for b in blocks:
            t = b["t"]
            if t == "h":
                out.append(Paragraph(_pdf_inline(b["inl"]), S[f"h{min(3, b['level'])}"]))
            elif t == "p":
                out.append(Paragraph(_pdf_inline(b["inl"]), S["p"]))
            elif t in ("ul", "ol"):
                items = []
                for item in b["items"]:
                    inner = flow(item, depth + 1)
                    items.append(ListItem(inner or [Paragraph("", S["p"])], leftIndent=14))
                kw = {"bulletType": "1", "start": b["start"]} if t == "ol" else {"bulletType": "bullet", "start": "•"}
                out.append(ListFlowable(items, bulletFontName=body, bulletFontSize=9, leftIndent=14, **kw))
            elif t == "code":
                # Courier に日本語の字形は無い（■ になる）。英数字だけのときだけ Courier にする。
                st = S["code"] if all(ord(ch) < 128 for ch in b["text"]) else ParagraphStyle(
                    "cj", parent=S["code"], fontName=body)
                out.append(Preformatted(b["text"], st))
            elif t == "quote":
                for q in flow(b["blocks"], depth):
                    if isinstance(q, Paragraph):
                        q = Paragraph(q.text, S["q"])
                    out.append(q)
            elif t == "table":
                rows = [b["head"]] + b["rows"]
                cols = max(len(r) for r in rows)
                al = [{"right": TA_RIGHT, "center": TA_CENTER}.get(b["align"][c] if c < len(b["align"]) else "", 0)
                      for c in range(cols)]
                cst = [ParagraphStyle(f"cell{c}", parent=S["cell"], alignment=al[c]) for c in range(cols)]
                data = [[Paragraph(_pdf_inline(r[c]) if c < len(r) else "", cst[c]) for c in range(cols)] for r in rows]
                tbl = Table(data, colWidths=[frame_w / cols] * cols, repeatRows=1)
                st = [("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#BFBFBF")),
                      ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF5")),
                      ("VALIGN", (0, 0), (-1, -1), "TOP"),
                      ("FONTNAME", (0, 0), (-1, 0), bold)]
                for ri in range(2, len(rows), 2):
                    st.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#FAFAFA")))
                tbl.setStyle(TableStyle(st))
                out += [tbl, Spacer(1, 8)]
            elif t == "hr":
                out.append(HRFlowable(width="100%", color=colors.HexColor("#BFBFBF"), thickness=0.6, spaceBefore=4, spaceAfter=8))
            elif t == "pagebreak":
                out.append(PageBreak())
            elif t == "img":
                p = _img_path(b["src"], base)
                if p is None:
                    warnings.append(f"image not found: {b['src']}")
                    continue
                from PIL import Image as PImage
                with PImage.open(p) as im:
                    iw, ih = im.size
                w = min(frame_w, iw * 0.75)
                img = Image(str(p), width=w, height=ih * w / iw)
                grp = [img] + ([Paragraph(_esc(b["alt"]), S["cap"])] if b.get("alt") else [])
                out.append(KeepTogether(grp))
            elif t == "chart":
                try:
                    png = chart_png(b["spec"])
                    img = Image(io.BytesIO(png), width=frame_w, height=frame_w * 3.6 / 6.4)
                    out += [img, Spacer(1, 6)]
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"chart: {type(e).__name__}: {e}")
        return out

    story += flow(parse_markdown(req.get("content") or ""))

    def page(c, d):
        c.saveState()
        c.setFont(body, 8)
        c.setFillColor(colors.HexColor("#888888"))
        c.drawRightString(size[0] - 20 * mm, 12 * mm, f"{d.page}")
        if req.get("title"):
            c.drawString(20 * mm, 12 * mm, str(req["title"])[:80])
        c.restoreState()

    doc = SimpleDocTemplate(req["path"], pagesize=size, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm,
                            bottomMargin=20 * mm, title=str(req.get("title") or ""), author=str(req.get("author") or ""))
    doc.build(story, onFirstPage=page, onLaterPages=page)
    return {"warnings": warnings, "pages": doc.page}


# ── 読む ───────────────────────────────────────────────────────────────

def read_doc(path: str, max_chars: int = 60000) -> str:
    ext = Path(path).suffix.lower()
    out: list[str] = []
    if ext == ".docx":
        from docx import Document
        d = Document(path)
        body = d.element.body
        from docx.table import Table as DTable
        from docx.text.paragraph import Paragraph as DPar
        for child in body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                p = DPar(child, d)
                txt = p.text.strip()
                if not txt:
                    continue
                sn = (p.style.name if p.style is not None else "") or ""
                m = re.match(r"Heading (\d)", sn)
                if sn == "Title":
                    out.append(f"# {txt}")
                elif m:
                    out.append("#" * (int(m.group(1)) + 1) + " " + txt)
                elif "List" in sn:
                    out.append(f"- {txt}")
                else:
                    out.append(txt)
            elif tag == "tbl":
                t = DTable(child, d)
                rows = [[c.text.strip().replace("\n", " ").replace("|", "\\|") for c in r.cells] for r in t.rows]
                if rows:
                    out.append("| " + " | ".join(rows[0]) + " |")
                    out.append("|" + "---|" * len(rows[0]))
                    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        out.insert(0, f"(Word document: {len(d.paragraphs)} paragraphs, {len(d.tables)} tables)")
    elif ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(path)
        out.append(f"(PowerPoint: {len(prs.slides)} slides)")
        for i, s in enumerate(prs.slides, 1):
            title = s.shapes.title.text_frame.text.strip() if s.shapes.title is not None and s.shapes.title.has_text_frame else ""
            out.append(f"\n## Slide {i}: {title}")
            for sh in s.shapes:
                if sh == s.shapes.title:
                    continue
                if sh.has_text_frame and sh.text_frame.text.strip():
                    for p in sh.text_frame.paragraphs:
                        if p.text.strip():
                            out.append("  " * p.level + "- " + p.text.strip())
                if getattr(sh, "has_table", False) and sh.has_table:
                    for r in sh.table.rows:
                        out.append("| " + " | ".join(c.text.strip() for c in r.cells) + " |")
                if getattr(sh, "has_chart", False) and sh.has_chart:
                    ch = sh.chart
                    try:
                        cats = list(ch.plots[0].categories)
                        out.append(f"[chart {ch.chart_type}: categories {cats}; " + "; ".join(
                            f"{se.name}={list(se.values)}" for se in ch.series) + "]")
                    except Exception:
                        out.append("[chart]")
                if sh.shape_type == 13:
                    out.append(f"[picture {sh.name}]")
            if s.has_notes_slide and s.notes_slide.notes_text_frame.text.strip():
                out.append("Notes: " + s.notes_slide.notes_text_frame.text.strip())
    elif ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=False, read_only=True)
        try:
            wbv = load_workbook(path, data_only=True, read_only=True)
        except Exception:
            wbv = None
        out.append(f"(Excel: sheets {wb.sheetnames})")
        for ws in wb.worksheets:
            vs = wbv[ws.title] if wbv is not None else None
            out.append(f"\n## Sheet {ws.title} ({ws.max_row} rows × {ws.max_column} columns)")
            vrows = vs.iter_rows(values_only=True) if vs is not None else None
            for ri, row in enumerate(ws.iter_rows(values_only=True), 1):
                vrow = next(vrows, None) if vrows is not None else None
                cells = []
                for ci, v in enumerate(row):
                    if isinstance(v, str) and v.startswith("="):
                        cached = vrow[ci] if vrow is not None and ci < len(vrow) else None
                        cells.append(f"{v} → {cached}" if cached is not None else v)
                    else:
                        cells.append("" if v is None else str(v))
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    out.append(f"{ri}\t" + "\t".join(cells))
                if sum(len(x) for x in out) > max_chars:
                    out.append(f"… (stopped at row {ri}; read a range with Bash/Python for more)")
                    break
    elif ext == ".pdf":
        from pypdf import PdfReader
        r = PdfReader(path)
        out.append(f"(PDF: {len(r.pages)} pages)")
        for i, pg in enumerate(r.pages, 1):
            out.append(f"\n--- page {i} ---\n" + (pg.extract_text() or "").strip())
            if sum(len(x) for x in out) > max_chars:
                out.append(f"… (stopped at page {i})")
                break
    else:
        raise ValueError(f"reading {ext} is not supported (docx, pptx, xlsx, pdf)")
    text = "\n".join(out)
    return text if len(text) <= max_chars else text[:max_chars] + f"\n… ({len(text) - max_chars} more characters)"


BUILDERS = {".docx": build_docx, ".pptx": build_pptx, ".xlsx": build_xlsx, ".pdf": build_pdf}


def run(req: dict) -> dict:
    act = req.get("action") or "create"
    if act == "read":
        return {"ok": True, "text": read_doc(req["path"], int(req.get("max_chars") or 60000))}
    ext = Path(req["path"]).suffix.lower()
    fn = BUILDERS.get(ext)
    if fn is None:
        raise ValueError(f"cannot create {ext} (use .docx, .pptx, .xlsx or .pdf)")
    Path(req["path"]).parent.mkdir(parents=True, exist_ok=True)
    tmp_final = req["path"]
    fd, tmp = tempfile.mkstemp(suffix=ext, dir=str(Path(tmp_final).parent))
    os.close(fd)
    try:
        info = fn(dict(req, path=tmp))
        os.replace(tmp, tmp_final)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    info.update(ok=True, path=tmp_final, bytes=os.path.getsize(tmp_final))
    return info


def main() -> int:
    req = json.load(open(sys.argv[1], encoding="utf-8"))
    try:
        res = run(req)
    except Exception as e:  # noqa: BLE001
        import traceback
        res = {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}
    json.dump(res, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
