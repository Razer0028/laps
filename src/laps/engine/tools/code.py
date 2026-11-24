"""コードの地図（Code）とエディターで見せる（Editor）— 2026-11-17 オーナー「ツールとハーネス強化」の案 3・4。

Code: 言語サーバーの軽い版。重い言語サーバーは入れず、ファイルの中の定義の形（Python は ast・ほかは行の形）で引く。
  outline     ファイルの定義の一覧（行番号つき）
  definition  名前の定義の場所（作業場の中・前後 2 行つき）
  references  名前が出てくる場所（単語の境目で・定義の行は印つき）
  check       書いた後の検査（構文・未定義の名前・プロジェクトの linter＝Write/Edit の後と同じ物）
★記録（11-14〜17）で ApplyPatch は 207 回中 37 回失敗し、ほとんどが「その行がファイルに無い」（記憶から書く）。
  当てる工夫では直らない（knowledge「ローカルLLMエージェントの編集失敗は照合では直らない」）ので、
  正しい場所と今の中身を安く引けるようにする。手引き: skills.sh の axiomantic/spellbook@using-lsp-tools（記号は意味で・文字列は Grep）。

Editor: アプリのエディター（Monaco）で利用者にファイルを開いて見せる。差分（この依頼の前 ↔ 今）でも見せられる。
  アプリが app_caps に editor を名乗っているときだけ（端末・中継の走行では使えないと返す）。
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from collections.abc import Iterator

from . import Ctx, Result, clip, fail

SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build", "target",
             ".laps", ".blackwall", ".idea", ".vs", "bin", "obj", ".next", ".cache", "Library", "Temp"}
CODE_EXT = {".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".cs", ".java", ".kt", ".kts", ".go", ".rs",
            ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".swift", ".rb", ".php", ".lua", ".sh", ".bash", ".ps1", ".gd",
            ".scala", ".dart", ".m", ".mm", ".vue", ".svelte", ".zig", ".nim", ".ex", ".exs", ".erl", ".hs", ".ml", ".r"}
MAX_FILES = 20000
MAX_BYTES = 2 * 1024 * 1024
MAX_HITS = 80

# 行の形で定義を見分ける（名前は 1 つ目の組）。言語ごとに完全ではない＝見つからなければ references と Grep で補う、と返す。
_DEF_PATTERNS: list[tuple[set[str], re.Pattern]] = [
    ({".py", ".pyi"}, re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")),
    ({".py", ".pyi"}, re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")),
    ({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"},
     re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\*?|class|interface|type|enum|namespace)\s+([A-Za-z_$][\w$]*)")),
    ({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte"},
     re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=")),
    ({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"},
     re.compile(r"^\s+(?:static\s+|async\s+|get\s+|set\s+|public\s+|private\s+|protected\s+)*([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?::[^{]+)?\{\s*$")),
    ({".cs", ".java", ".kt", ".kts", ".scala", ".swift", ".dart"},
     re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal|static|abstract|sealed|partial|final|open|data|"
                r"override|virtual|readonly|async|unsafe|extern|new)\s+)*(?:class|struct|interface|enum|record|object|"
                r"protocol|extension|trait)\s+([A-Za-z_]\w*)")),
    ({".cs", ".java", ".kt", ".scala", ".swift", ".dart"},
     re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:(?:public|private|protected|internal|static|abstract|final|override|virtual|"
                r"async|unsafe|extern|new|synchronized|fun|func)\s+)+[\w<>\[\],.?\s]*?\b([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\(")),
    ({".go"}, re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")),
    ({".go"}, re.compile(r"^\s*type\s+([A-Za-z_]\w*)")),
    ({".rs"}, re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+|const\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*"
                         r"(?:fn|struct|enum|trait|type|mod|const|static|macro_rules!)\s+([A-Za-z_]\w*)")),
    ({".c", ".h", ".cc", ".cpp", ".hpp", ".cxx", ".m", ".mm"},
     re.compile(r"^(?:[A-Za-z_][\w:<>*&\s,]*?\s+[*&]*)?([A-Za-z_][\w:]*)\s*\([^;]*\)\s*(?:const\s*)?(?:\{|$)")),
    ({".c", ".h", ".cc", ".cpp", ".hpp", ".cxx"}, re.compile(r"^\s*(?:typedef\s+)?(?:struct|class|enum|union)\s+([A-Za-z_]\w*)")),
    ({".rb"}, re.compile(r"^\s*(?:def|class|module)\s+(?:self\.)?([A-Za-z_]\w*[?!]?)")),
    ({".php"}, re.compile(r"^\s*(?:abstract\s+|final\s+|public\s+|private\s+|protected\s+|static\s+)*(?:function|class|interface|trait)\s+([A-Za-z_]\w*)")),
    ({".lua"}, re.compile(r"^\s*(?:local\s+)?function\s+(?:[\w.:]+[.:])?([A-Za-z_]\w*)")),
    ({".sh", ".bash"}, re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{?")),
    ({".ps1"}, re.compile(r"^\s*function\s+([A-Za-z_][\w-]*)", re.IGNORECASE)),
    ({".gd"}, re.compile(r"^\s*(?:static\s+)?(?:func|class|class_name|signal|enum|const|var)\s+([A-Za-z_]\w*)")),
]
_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "else", "do", "new", "sizeof", "using", "lock", "foreach",
             "fixed", "when", "try", "await", "yield", "throw", "elif", "with", "match", "typeof", "nameof", "default"}


def _patterns(ext: str) -> list[re.Pattern]:
    return [p for exts, p in _DEF_PATTERNS if ext in exts]


def _read(path: str) -> str | None:
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    for enc in ("utf-8-sig", "utf-16", "cp932"):
        try:
            if enc == "utf-16" and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def outline_text(path: str, text: str) -> list[tuple[int, str, str]]:
    """(行, 種類, 名前) の並び。Python は ast（入れ子は「Class.method」）、ほかは行の形。"""
    ext = os.path.splitext(path)[1].lower()
    out: list[tuple[int, str, str]] = []
    if ext in (".py", ".pyi"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            def walk(node, prefix: str) -> None:
                for ch in ast.iter_child_nodes(node):
                    if isinstance(ch, ast.ClassDef):
                        out.append((ch.lineno, "class", prefix + ch.name))
                        walk(ch, prefix + ch.name + ".")
                    elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append((ch.lineno, "def", prefix + ch.name))
                    elif isinstance(ch, (ast.Assign, ast.AnnAssign)) and not prefix:
                        tg = ch.targets if isinstance(ch, ast.Assign) else [ch.target]
                        for t in tg:
                            if isinstance(t, ast.Name) and (t.id.isupper() or t.id[:1].isupper()):
                                out.append((ch.lineno, "var", t.id))
            walk(tree, "")
            return sorted(out)
    pats = _patterns(ext)
    if not pats:
        return out
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:
            continue
        for p in pats:
            m = p.match(line)
            if m and m.group(1) not in _KEYWORDS:
                kind = re.match(r"\s*(?:export\s+)?(?:default\s+)?(?:pub\s+)?(\w+)", line)
                out.append((i, (kind.group(1) if kind else "def")[:10], m.group(1)))
                break
    return out


def _files(root: str) -> Iterator[str]:
    n = 0
    for d, dirs, names in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        for nm in sorted(names):
            if os.path.splitext(nm)[1].lower() in CODE_EXT:
                n += 1
                if n > MAX_FILES:
                    return
                yield os.path.join(d, nm)


def _candidates(root: str, name: str) -> list[str]:
    """その名前を含むファイル。rg があれば使う（大きな作業場で速い）。"""
    rg = shutil.which("rg")
    if rg:
        try:
            globs: list[str] = []
            for e in sorted(CODE_EXT):
                globs += ["-g", "*" + e]
            for d in sorted(SKIP_DIRS):
                globs += ["-g", "!" + d]
            p = subprocess.run([rg, "-l", "-F", "-w", "--max-filesize", "2M", *globs, "--", name, root],
                               capture_output=True, text=True, timeout=30, check=False, encoding="utf-8", errors="replace")
            if p.returncode in (0, 1):
                return sorted(p.stdout.splitlines())[:MAX_FILES]
        except (OSError, subprocess.SubprocessError):
            pass
    out = []
    for f in _files(root):
        t = _read(f)
        if t is not None and name in t:
            out.append(f)
    return out


def _rel(ctx: Ctx, p: str) -> str:
    try:
        r = os.path.relpath(p, ctx.workspace)
        return p if r.startswith("..") else r.replace("\\", "/")
    except ValueError:
        return p


def _ctx_lines(lines: list[str], i: int, n: int = 2) -> str:
    a, b = max(0, i - 1 - n), min(len(lines), i + n)
    return "\n".join(f"{'>' if k + 1 == i else ' '}{k + 1:>6}  {lines[k][:200]}" for k in range(a, b))


def code(inp: dict, ctx: Ctx) -> Result:
    from . import files
    action = str(inp.get("action") or "").strip().lower()
    name = str(inp.get("name") or inp.get("symbol") or "").strip()
    raw = str(inp.get("path") or "").strip()
    if action not in ("outline", "definition", "references", "check"):
        return fail("Code needs action: outline (path), definition (name), references (name) or check (path).", head="Code")
    if action in ("outline", "check"):
        if not raw:
            return fail(f"Code {action} needs path.", head="Code")
        path = files.resolve(ctx, raw)
        if not os.path.isfile(path):
            return fail(f"{raw} does not exist.", head="Code")
        text = _read(path)
        if text is None:
            return fail(f"{raw} is binary or larger than 2 MB.", head="Code")
        if action == "check":
            note = files._diagnostics(path, text).strip()
            return Result(note or f"No problems found in {_rel(ctx, path)} (syntax and the project's quick checks). "
                          "For the project's full linter, type checker and tests use Tools(name=\"Check\").",
                          head=_rel(ctx, path), tail="検査", effect=False)
        rows = outline_text(path, text)
        if not rows:
            return Result(f"No definitions recognised in {_rel(ctx, path)} ({len(text.splitlines())} lines). Read it instead.",
                          kind="empty", head=_rel(ctx, path), tail="0 件", effect=False)
        body = "\n".join(f"{ln:>6}  {kind:<9} {nm}" for ln, kind, nm in rows)
        out, cut = clip(ctx, f"{_rel(ctx, path)} — {len(rows)} definitions (line, kind, name):\n{body}")
        return Result(out, head=_rel(ctx, path), tail=f"{len(rows)} 件", truncated=cut, effect=False)
    if not name or not re.fullmatch(r"[\w$.:#-]{1,120}", name):
        return fail(f"Code {action} needs name: one identifier (a function, class, variable…). Search plain text with Grep.",
                    head="Code")
    base = name.replace("::", ".").replace("#", ".").split(".")[-1]
    root = files.resolve(ctx, raw) if raw else ctx.workspace
    if not os.path.isdir(root):
        root = os.path.dirname(root) if os.path.isfile(root) else ctx.workspace
    word = re.compile(r"(?<![\w$])" + re.escape(base) + r"(?![\w$])")
    defs: list[str] = []
    refs: list[str] = []
    nref = 0
    for f in _candidates(root, base):
        text = _read(f)
        if text is None:
            continue
        lines = text.splitlines()
        dlines = {ln for ln, _k, nm in outline_text(f, text) if nm == base or nm.endswith("." + base)}
        for ln in sorted(dlines):
            if len(defs) < MAX_HITS:
                defs.append(f"{_rel(ctx, f)}:{ln}\n{_ctx_lines(lines, ln)}")
        if action == "references":
            for i, line in enumerate(lines, 1):
                if word.search(line):
                    nref += 1
                    if len(refs) < MAX_HITS * 2:
                        refs.append(f"{_rel(ctx, f)}:{i}{' (definition)' if i in dlines else ''}  {line.strip()[:200]}")
    if action == "definition":
        if not defs:
            return Result(f"No definition of {base!r} found under {_rel(ctx, root) or '.'} (looked for definition lines in "
                          "code files). It may come from a library, or be defined in a way this tool does not recognise; "
                          f"try Code references name={base!r} or Grep.", kind="empty", head=base, tail="0 件", effect=False)
        out, cut = clip(ctx, f"Definitions of {base} ({len(defs)}):\n\n" + "\n\n".join(defs))
        return Result(out, head=base, tail=f"{len(defs)} 件", truncated=cut, effect=False)
    if not refs:
        return Result(f"{base!r} does not appear as a whole word in code files under {_rel(ctx, root) or '.'}.",
                      kind="empty", head=base, tail="0 件", effect=False)
    more = f"\n… {nref - len(refs)} more not shown; narrow with path=<folder or file>." if nref > len(refs) else ""
    out, cut = clip(ctx, f"{nref} places use {base}:\n" + "\n".join(refs) + more)
    return Result(out, head=base, tail=f"{nref} 件", truncated=cut, effect=False)


CODE_DESC = ("Code map without a language server: outline a file, find where a name is defined, or every place it is used, "
             "and check a file after editing.\n"
             "- {\"action\":\"outline\",\"path\":\"src/app.py\"} → definitions with line numbers (read only those lines next)\n"
             "- {\"action\":\"definition\",\"name\":\"load_config\"} → file:line and the lines around each definition\n"
             "- {\"action\":\"references\",\"name\":\"load_config\",\"path\":\"src\"} → every whole-word use (path narrows)\n"
             "- {\"action\":\"check\",\"path\":\"src/app.py\"} → syntax, undefined names and the project's quick linter\n"
             "Use it before editing code you have not read in this conversation, and when a patch or Edit failed because "
             "the lines were not found: look the place up instead of writing it from memory. Symbols → Code; plain text, "
             "comments and strings → Grep.")
CODE_SCHEMA = {"type": "object", "properties": {
    "action": {"type": "string", "enum": ["outline", "definition", "references", "check"]},
    "name": {"type": "string", "description": "identifier for definition / references"},
    "path": {"type": "string", "description": "file for outline / check; folder or file to narrow definition / references"}},
    "required": ["action"]}


def editor(inp: dict, ctx: Ctx) -> Result:
    """アプリのエディターでファイルを開いて利用者に見せる。"""
    from ...tools import appcall
    from . import files
    raw = str(inp.get("path") or "").strip()
    if not raw:
        return fail("Editor needs path.", head="Editor")
    path = files.resolve(ctx, raw)
    if not os.path.isfile(path):
        return fail(f"{raw} does not exist.", head="Editor")
    if not appcall.can("editor"):
        return fail("The app's editor is not available in this run (it works only in the L.A.P.S desktop app). "
                    "Tell the user the path and line instead.", head="Editor")
    try:
        line = max(1, int(inp.get("line") or 1))
    except (TypeError, ValueError):
        line = 1
    diff = bool(inp.get("diff"))
    try:
        r = appcall.call("editor", {"action": "open", "path": path, "line": line, "diff": diff,
                                    "session": ctx.root.name}, timeout=20.0)
    except Exception as e:  # noqa: BLE001 — アプリの窓口の失敗は何であれ道具の失敗として返す
        return fail(f"Could not ask the app: {e}", head="Editor")
    if not isinstance(r, dict) or not r.get("ok"):
        return fail(f"The app did not open it: {(r or {}).get('error') if isinstance(r, dict) else r}", head="Editor")
    what = "the changes of this conversation (before ↔ now)" if diff else f"line {line}"
    return Result(f"Opened {_rel(ctx, path)} in the user's editor at {what}. The user can see and edit it there.",
                  head=_rel(ctx, path), tail="エディター", effect=False)


EDITOR_DESC = ("Show a file to the user in the app's editor (the side panel), at a line, or as a diff of what this "
               "conversation changed (before ↔ now; the user can revert single changes there).\n"
               "- {\"path\":\"src/app.py\",\"line\":120} → opens it and scrolls to the line\n"
               "- {\"path\":\"src/app.py\",\"diff\":true} → opens the before/after diff of your changes\n"
               "Use it when you want the user to look at a specific place or review a change, instead of pasting long code.")
EDITOR_SCHEMA = {"type": "object", "properties": {
    "path": {"type": "string"}, "line": {"type": "integer", "minimum": 1},
    "diff": {"type": "boolean", "description": "show this conversation's changes to the file as a diff"}},
    "required": ["path"]}
