"""Doc — 資料（Word / PowerPoint / Excel / PDF）を作る・読む道具（2026-10-08 オーナー「資料作成のツールや Skills も欲しい」）。

中身は oc/docmake.py（blackwall を import しない）。
★Windows: 部品（python-docx・python-pptx・openpyxl・reportlab・matplotlib・pypdf）はインストーラに同梱
  （オーナー「インストーラーに同梱していいです」・scripts/build_pyembed.sh の DOC_WHEELS）。同じ process で動かす。
★それ以外（Linux の .deb は system の Python）: 初回に専用の環境を作る（Voice と同じ uv・版は DOC_WHEELS と同じ）。
  置き場は LAPS_DOCS_DIR（既定 ~/.blackwall/docs）。LAPS_DOCS=0 で道具を外す。
"""
from __future__ import annotations

from .. import paths as _lp
import importlib.util
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from ..tools import ToolResult

#: 同梱の版と同じ（scripts/build_pyembed.sh の DOC_WHEELS の直接の依存）。専用の環境にもこの版を入れる。
PINS = ["python-docx==1.2.0", "python-pptx==1.0.2", "openpyxl==3.1.5", "reportlab==5.0.1", "matplotlib==3.11.2",
        "pypdf==6.19.0", "markdown-it-py==4.2.0"]
_MODS = ("docx", "pptx", "openpyxl", "reportlab", "matplotlib", "pypdf", "markdown_it")
EXTS = (".docx", ".pptx", ".xlsx", ".pdf")
_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("LAPS_DOCS", "1").strip() != "0"


def in_process() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in _MODS)


def _root() -> Path:
    d = os.environ.get("LAPS_DOCS_DIR")
    return Path(d) if d else _lp.dot_dir() / "docs"


def _env_python() -> Path:
    e = _root() / "env"
    return e / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def missing() -> list[str]:
    return [m for m in _MODS if importlib.util.find_spec(m) is None]


def prepare() -> str:
    """同じ process で使えなければ、専用の環境（uv・Python 3.12）を作って部品を入れる。そろっていれば何もしない。"""
    if in_process():
        return "docs: bundled"
    with _lock:
        py = _env_python()
        ok = _root() / "env" / ".ok"
        if py.exists() and ok.exists() and ok.read_text(encoding="utf-8").strip() == " ".join(PINS):
            return f"docs: {py}"
        from . import voice as _v
        r = _root()
        r.mkdir(parents=True, exist_ok=True)
        log = r / "install.log"
        uv = _v._ensure_uv()
        env = _v._uv_env()
        if _v._run([str(uv), "venv", "--python", "3.12", str(r / "env")], r, 900, env, log) != 0:
            raise RuntimeError("could not create the document environment:\n" + _v._log_tail(log))
        if _v._run([str(uv), "pip", "install", "--python", str(py), *PINS], r, 1800, env, log) != 0:
            raise RuntimeError("could not install the document libraries:\n" + _v._log_tail(log))
        ok.write_text(" ".join(PINS), encoding="utf-8")
        return f"docs: {py}"


def _run(req: dict) -> dict:
    if in_process():
        from . import docmake
        return docmake.run(req)
    prepare()
    from . import voice as _v
    d = Path(tempfile.mkdtemp(prefix="bwdoc-"))
    try:
        (d / "req.json").write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH")}
        env["PYTHONIOENCODING"] = "utf-8"
        log = d / "worker.log"
        _v._run([str(_env_python()), "-I", str(Path(__file__).with_name("docmake.py")), str(d / "req.json"),
                 str(d / "res.json")], d, 600, env, log)
        try:
            return json.loads((d / "res.json").read_text(encoding="utf-8"))
        except Exception:
            return {"ok": False, "error": "the document worker did not answer:\n" + _v._log_tail(log)}
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def _err(text: str, kind: str = "error") -> ToolResult:
    return ToolResult(f"Error: {text}", ok=False, kind=kind)


def _size(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


def doc_tool(inp: dict) -> ToolResult:
    from . import tools as _oc
    inp = inp if isinstance(inp, dict) else {}
    act = str(inp.get("action") or "create").lower()
    raw = str(inp.get("path") or inp.get("file_path") or "").strip()
    if not raw:
        return _err("give path (…/report.docx, deck.pptx, data.xlsx or report.pdf)")
    path = _oc._abs(raw)
    ext = Path(path).suffix.lower()
    if act == "read":
        if not os.path.isfile(path):
            return _err(f"{path} does not exist", kind="empty")
        if ext not in EXTS + (".xlsm",):
            return _err(f"Doc reads .docx, .pptx, .xlsx and .pdf (for {ext or 'this file'} use Read)")
        try:
            res = _run({"action": "read", "path": path, "max_chars": int(inp.get("max_chars") or 60000)})
        except Exception as e:  # noqa: BLE001
            return _err(f"{type(e).__name__}: {e}")
        if not res.get("ok"):
            return _err(str(res.get("error") or "could not read"))
        return ToolResult(str(res.get("text") or ""), ok=True)
    if act != "create":
        return _err('action must be "create" or "read"')
    if ext not in EXTS:
        return _err("path must end in .docx, .pptx, .xlsx or .pdf")
    existed = os.path.exists(path)
    if existed and not inp.get("overwrite"):
        return _err(f"{path} already exists. Pass overwrite=true to replace it (read it first with action=read "
                    "if it is the user's own file), or choose a new name.")
    req = {k: inp.get(k) for k in ("title", "subtitle", "author", "content", "slides", "sheets", "theme", "toc",
                                    "page", "landscape", "keep_template_content")
           if inp.get(k) not in (None, "", [], {})}
    req.update(action="create", path=path, base_dir=_oc._cwd())     # 画像の相対パスは作業場から
    if inp.get("template"):
        tpl = _oc._abs(str(inp["template"]))
        if not os.path.isfile(tpl):
            return _err(f"template {tpl} does not exist")
        req["template"] = tpl
    if not any(req.get(k) for k in ("content", "slides", "sheets")):
        return _err("give content (Markdown), slides (for .pptx) or sheets (for .xlsx)")
    try:
        res = _run(req)
    except Exception as e:  # noqa: BLE001
        return _err(f"{type(e).__name__}: {e}")
    if not res.get("ok"):
        return _err(str(res.get("error") or "could not create the document"))
    bits = []
    for k, label in (("slides", "slides"), ("pages", "pages"), ("paragraphs", "paragraphs"), ("tables", "tables")):
        if isinstance(res.get(k), int):
            bits.append(f"{res[k]} {label}")
    if isinstance(res.get("sheets"), list):
        bits.append("sheets " + ", ".join(res["sheets"]))
    msg = f"{'Replaced' if existed else 'Created'} {path} ({_size(int(res.get('bytes') or 0))}" + \
          (f", {', '.join(bits)}" if bits else "") + ")."
    if res.get("warnings"):
        msg += "\nWarnings:\n" + "\n".join(f"- {w}" for w in res["warnings"][:12])
    msg += "\nCheck it with Doc(action=\"read\") before telling the user it is done."
    return ToolResult(msg, ok=True)


DESCRIPTION = """Create or read documents: Word (.docx), PowerPoint (.pptx), Excel (.xlsx), PDF (.pdf).

create (the default): path, plus
- content: Markdown. Headings, paragraphs, **bold**, *italic*, `code`, links, bullet and numbered lists (nested), tables, code blocks, quotes, images ![caption](file.png), `---` rule, `<!-- pagebreak -->`. Charts: a ```chart block containing JSON {"type": "column|bar|line|area|pie|scatter", "title": ..., "categories": [...], "series": [{"name": ..., "values": [...]}]}.
  For .pptx, `---` lines separate slides (Marp style); without them each # / ## heading starts a slide; the first heading of a slide is its title; `<!-- notes: ... -->` adds speaker notes. A slide with text and a table/chart/image puts them side by side.
- slides (.pptx, instead of content): [{"title", "bullets": [...], "body": Markdown, "table": {"header": [...], "rows": [[...]]}, "chart": {...}, "image": path, "notes", "kind": "title"}].
- sheets (.xlsx): [{"name", "rows": [[header...], [values...]], "number_formats": {"C": "#,##0"}, "widths": {"A": 20}, "chart": {"type", "title", "categories": "A", "series": ["B", "C"], "anchor": "F2"}}]. Strings starting with = are formulas. Without sheets, Markdown tables in content become sheets.
- title, subtitle, author, theme {"font", "accent": "#hex", "dark": true (pptx)}, template (an existing .docx/.pptx/.xlsx whose styles, layouts, headers and footers to use; its sample text and slides are dropped; an .xlsx template's sheets keep their formatting and get the values), toc (docx), page ("A4" or "Letter"), landscape.
- overwrite: true is required to replace an existing file.
read: action="read", path → the text, headings, tables, slide contents, sheet cells (formulas with values) or PDF pages.
Write in the user's language. After creating, read the file back to check it."""

SCHEMA_PROPS = {
    "action": {"type": "string", "enum": ["create", "read"], "description": "create (default) or read"},
    "path": {"type": "string", "description": "File path ending in .docx, .pptx, .xlsx or .pdf"},
    "content": {"type": "string", "description": "Markdown content (see the description)"},
    "title": {"type": "string"},
    "subtitle": {"type": "string"},
    "author": {"type": "string"},
    "slides": {"type": "array", "items": {"type": "object"}, "description": "Slides for .pptx (instead of content)"},
    "sheets": {"type": "array", "items": {"type": "object"}, "description": "Sheets for .xlsx"},
    "theme": {"type": "object", "description": '{"font": "Yu Gothic", "accent": "#3A6EA5", "dark": false}'},
    "template": {"type": "string", "description": "Existing .docx/.pptx/.xlsx to start from"},
    "toc": {"type": "boolean", "description": "Word: add a table of contents"},
    "page": {"type": "string", "description": "A4 (default) or Letter"},
    "landscape": {"type": "boolean"},
    "overwrite": {"type": "boolean", "description": "Replace an existing file"},
    "keep_template_content": {"type": "boolean", "description": "Keep the template's own text/slides (default: only its styles)"},
    "max_chars": {"type": "integer", "description": "read: maximum characters to return"},
}
