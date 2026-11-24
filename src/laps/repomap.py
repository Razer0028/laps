"""作業場の地図 — 本体を捨て、**シグネチャと依存だけ**を絶対予算で刷る。

★何のためか（2026-08-29・run29/31/32/34 のトレース実測）

窓は 32,768 トークン。4 走行はいずれも工程 4/7 で文脈切れで終わった。
道具結果 8,094,347 B のうち **Glob 239 回 896,469 B ＋ Grep 158 回 375,542 B
＝ 1,272,011 B（15.7%）が「どこに何があるか」を探すためだけに使われている**。
しかも探し方が反復する: `Glob site/*` 65 回・`Glob site/api/*` 31 回・
`Grep get_db_connection` 8 回・`Grep require_once` 6 回。
**同じ質問を毎回、道具で、全文で聞き直している。**

地図はその質問に**先に**答える。工程の頭で 1 回だけ、2,048 トークンを払う。

★入れないと決めたもの（理由つき）

* **道具にしない。** 棚は 22 個で、道具数は選択精度に最大級に効く
  （50 個で 84〜95% → 200 個で 41%）。23 個目の代償を払う価値は無い——
  地図は「モデルが呼ぶかどうか」に賭ける必要がなく、こちらから渡せる。
* **本体を載せない。** 載せたら Read と同じ物になる（Read だけで 394 万 B）。
* **要約しない。** `context_budget` と同じ理由。シグネチャは**取り違えても
  元と突き合わせられる**が、要約はできない。

★不完全さは黙らない。本体を落とした時点でこの地図は必ず不完全で、さらに
字数で省くことがある。**省いた事実と件数を毎回本文に書く**（黙って一部だけ
出すと全体だと誤解される——この現場で実際に起きた形）。

★予算はトークンの絶対値で決める（商用 API の 200K 窓の内訳は移せない）。
既定 2,048 tok ＝ 32,768 の 6.25%。下限 512 tok。
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------
# 予算
# --------------------------------------------------------------------------

#: 既定の絶対予算（トークン）。32,768 窓の 6.25%。
DEFAULT_BUDGET_TOKENS = 2048
#: これを下回る予算は受け付けない（下回ると見出しだけになり、地図の意味が消える）。
MIN_BUDGET_TOKENS = 512

#: 見積りに使う「1トークンあたりの文字数」。
#:
#: ★`context_budget.approx_tokens`（＝UTF-8 バイト数）は**証明できる上界**だが、
#:   地図の本文（パス・識別子・記号）では実測の 3.0 倍に出る。それで 2,048 を
#:   割ると地図が 2KB しか刷れず、機構として死ぬ。
#: ★そこで**この地図の実出力で**実測した（2026-08-29）。Qwen2/3 の実トークナイザ
#:   （151,643 語彙・vocab+merges を pure-Python で再実装し、`context_budget` の
#:   docstring にある既知の実測値 `"1234567890 "*10 → 110 tok` で校正）で数えた:
#:       VDIR run34 の作業場（64 f・PHP+Py+sh）  3,741 字 / 1,577 tok = 2.37 字/tok
#:       VDIR run9 の成果物 （26 f・Py）         1,982 字 /   778 tok = 2.55
#:       BLACKWALL src 自身（352 f・Py）         4,894 字 / 1,696 tok = 2.89
#:   実測の最悪は 2.37。**余裕を見て 2.20 を採る**＝見積りは実測より +20〜39% 多く出る。
#: ★これは**証明ではなく校正**である。`approx_tokens` の上界と違い、想定外に密な
#:   本文（記号だらけのパス等）では破れうる。破れる方向は「予算を超える」なので、
#:   ここを緩める（値を大きくする）ときは必ず測り直すこと。
#: ★「文字数を係数で割る」を一般の文には**使わない**。地図の本文という狭い形に
#:   対してだけ校正した値である。日本語の見出し行は別勘定にする（下）。
CHARS_PER_TOKEN = 2.20
#: 日本語（CJK）は 1 文字がほぼ 1 トークン。見出しと注記に効くので別に数える。
_CJK = re.compile(r"[　-ヿ㐀-鿿＀-￯]")


def est_tokens(text: str) -> int:
    """地図の本文に対する保守側のトークン見積り。**必ず実測以上**に出す。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return int(cjk + rest / CHARS_PER_TOKEN) + 1


# --------------------------------------------------------------------------
# 走査
# --------------------------------------------------------------------------

#: 中を読む拡張子 → 言語。ここに無い物は**名前だけ**載せる（載せないのではない）。
_LANG = {
    ".py": "py", ".pyw": "py",
    ".php": "php", ".phtml": "php",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js",
    ".sh": "sh", ".bash": "sh",
}
#: 走査しないディレクトリ。`.git` を読むと地図が git の内部で埋まる。
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".blackwall-phases", ".blackwall", ".laps", "vendor", "site-packages", ".tox",
    # ★2026-10-19: Rust の組み立て物（target/debug）が git に入っている作業場があり、地図が埋まった
    "target",
}
#: 走査するファイル数の上限。超えたぶんは「省いた」と書く。
MAX_FILES = 600
#: 1 ファイルで読むバイト数の上限（これを超える分は解析しない＝印をつける）。
MAX_FILE_BYTES = 400_000
#: 1 ファイルから採るシグネチャの上限（近い/遠い）。
SIG_CAP_NEAR = 24
SIG_CAP_FAR = 6


class FileInfo:
    __slots__ = ("path", "lang", "sigs", "deps", "size", "note", "hop")

    def __init__(self, path: str, lang: str, size: int):
        self.path = path
        self.lang = lang
        self.sigs: list[str] = []
        self.deps: list[str] = []      # 生の依存文字列（未解決）
        self.size = size
        self.note = ""                 # "解析できず" 等
        self.hop = 99


# ---- Python ------------------------------------------------------------------

def _py_sig(node: ast.AST) -> str:
    try:
        args = ast.unparse(node.args)          # type: ignore[attr-defined]
    except Exception:
        args = "…"
    kw = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    ret = ""
    r = getattr(node, "returns", None)
    if r is not None:
        try:
            ret = " -> " + ast.unparse(r)
        except Exception:
            ret = ""
    return f"{kw}{node.name}({args}){ret}"      # type: ignore[attr-defined]


def _parse_py(text: str, info: FileInfo) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        info.note = f"構文誤り {e.lineno} 行目"
        return
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info.sigs.append(_py_sig(node))
        elif isinstance(node, ast.ClassDef):
            bases = ""
            try:
                bs = [ast.unparse(b) for b in node.bases]
                bases = f"({', '.join(bs)})" if bs else ""
            except Exception:
                pass
            info.sigs.append(f"class {node.name}{bases}")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    info.sigs.append("  " + _py_sig(sub))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    info.sigs.append(f"{t.id} = …")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            info.deps += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                info.deps.append(("." * (node.level or 0)) + node.module)
    # ★手掛かりは**文字列定数だけ**から採る。本文全体に掛けると python の
    #   `from aiohttp import ...` が SQL の `FROM <表>` に化ける（実測で出た）。
    lits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    info.sigs += _clues("\n".join(lits)[:40_000])


# ---- PHP / JS / sh（正規表現。完璧を目指さない） ------------------------------

#: ★行頭に固定しない。`<?php function f() {}` のように 1 行に詰めて書かれると
#:   行頭固定の正規表現は**黙って 0 件**を返す（試験で踏んだ）。区切りの直後も見る。
_PHP_SIG = re.compile(
    r"(?:^|[;{}>]|<\?php)[ \t]*(?:(?:public|private|protected|static|final|abstract)[ \t]+)*"
    r"(function[ \t]+&?\w+[ \t]*\([^)]{0,300}\)|"
    r"(?:class|interface|trait)[ \t]+\w+(?:[ \t]+extends[ \t]+\w+)?"
    r"(?:[ \t]+implements[ \t]+[\w, ]+)?)", re.M)
_PHP_DEP = re.compile(r"""(?:require|include)(?:_once)?[ \t]*\(?[ \t]*['"]([^'"]+)['"]""")
#: ★語を並べない（`api|video|works|…` と書きかけて捨てた）。それは VDIR の課題に
#:   合わせただけで、白紙から始める別の作業場では 1 つも当たらない。
#:   「`/` で始まる短い文字列＝経路」で採り、**明らかな OS のパスだけ**を外す。
_ROUTE = re.compile(r"""['"](/[A-Za-z][\w/{}.\-]{0,40})['"]""")
_ROUTE_STOP = ("/tmp", "/var", "/usr", "/etc", "/home", "/root", "/dev", "/proc",
               "/opt", "/srv", "/bin", "/sbin", "/lib", "/mnt", "/media")


def _looks_like_route(r: str) -> bool:
    """`/config.php` のような**1 段の取り込み先**を経路と呼ばない。

    取り込み先は `→` の行に解決済みで出るので、ここに出すと同じ物を 2 度払う。
    残すのは「`/` で終わる」「2 段以上」「拡張子が無い」のどれか。
    """
    body = r.rstrip("/")
    return r.endswith("/") or body.count("/") >= 2 or "." not in body

_JS_SIG = re.compile(
    r"(?:^|[;{}>])[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?"
    r"((?:async[ \t]+)?function[ \t]*\*?[ \t]*\w+[ \t]*\([^)]{0,200}\)|"
    r"class[ \t]+\w+(?:[ \t]+extends[ \t]+[\w.]+)?|"
    r"(?:const|let|var)[ \t]+\w+[ \t]*=[ \t]*(?:async[ \t]*)?\([^)]{0,200}\)[ \t]*=>)", re.M)
_JS_DEP = re.compile(r"""(?:from[ \t]*['"]([^'"]+)['"]|require\([ \t]*['"]([^'"]+)['"])""")

_SH_SIG = re.compile(r"^[ \t]*(?:function[ \t]+)?(\w+)[ \t]*\([ \t]*\)[ \t]*\{", re.M)
_SH_DEP = re.compile(r"^[ \t]*(?:source|\.)[ \t]+([\w./$-]+)", re.M)

# ---- 手掛かり ----------------------------------------------------------------
# ★なぜ要るか（実測）: この作業場の PHP は**関数を1つも定義しない手続きファイル**が
#   大半で、シグネチャだけの地図では中身が空欄になる。ところがトレースで実際に
#   探されていたのは `HTTP_RANGE`(5回) `slug`(3回) `video_path`(2回) のような
#   **入力の鍵と表の名前**だった。それは 1 行で載る。
_KEY_REQ = re.compile(r"\$_(?:GET|POST|REQUEST|SERVER|FILES)\[[ \t]*['\"](\w+)['\"]")
_KEY_SQL = re.compile(r"(?is)\b(?:from|into|update|table(?:\s+if\s+not\s+exists)?|join)\s+"
                      r"[`\"']?([a-z_][a-z0-9_]{2,30})[`\"']?")
_KEY_CODE = re.compile(r"http_response_code\([ \t]*(\d{3})|HTTP/1\.[01][ \t]+(\d{3})")
#: SQL の予約語を表の名前と読み違えないための除外。
_SQL_STOP = {"select", "where", "order", "group", "limit", "values", "set", "and",
             "not", "null", "exists", "table", "index", "database", "the"}


def _clues(text: str) -> list[str]:
    """本体を載せずに「この物が何を読み、何の表を触るか」だけを拾う。"""
    out: list[str] = []
    keys = sorted({k for k in _KEY_REQ.findall(text)})[:8]
    if keys:
        out.append("入力 " + ",".join(keys))
    tabs = sorted({t.lower() for t in _KEY_SQL.findall(text)} - _SQL_STOP)[:6]
    if tabs:
        out.append("表 " + ",".join(tabs))
    codes = sorted({a or b for a, b in _KEY_CODE.findall(text)})[:6]
    if codes:
        out.append("状態 " + ",".join(codes))
    routes = sorted({r for r in _ROUTE.findall(text)
                     if not r.startswith(_ROUTE_STOP) and _looks_like_route(r)})[:5]
    if routes:
        out.append("経路 " + " ".join(routes))
    return out


def _parse_regex(text: str, info: FileInfo) -> None:
    if info.lang == "php":
        info.sigs = [re.sub(r"[ \t]+", " ", m.group(1)).strip()
                     for m in _PHP_SIG.finditer(text)]
        info.deps = _PHP_DEP.findall(text)
        info.sigs += _clues(text)
    elif info.lang == "js":
        info.sigs = [re.sub(r"[ \t]+", " ", m.group(1)).strip()
                     for m in _JS_SIG.finditer(text)]
        info.deps = [a or b for a, b in _JS_DEP.findall(text)]
    elif info.lang == "sh":
        info.sigs = [f"{m.group(1)}()" for m in _SH_SIG.finditer(text)]
        info.deps = _SH_DEP.findall(text)


def _skip_dir(d: str) -> bool:
    return d in _SKIP_DIRS or d.startswith(".blackwall") or d.startswith(".laps") or ".bak" in d


def _git_paths(rootp: Path) -> list[Path] | None:
    """git の作業木なら、追跡中と未追跡（.gitignore で外していない物）だけを返す。

    ★2026-10-19: os.walk だと .gitignore した写し（app/pyruntime・pyruntime.bak-*）で
      600 件が埋まり、本体の src/laps が地図に 1 件も載らなかった（未走査 94,870 件）。
    """
    if not (rootp / ".git").exists():
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(rootp), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    out: list[Path] = []
    for rel in sorted(set(r.stdout.decode("utf-8", "replace").split("\0"))):
        if not rel:
            continue
        parts = rel.split("/")
        if any(_skip_dir(d) for d in parts[:-1]):
            continue
        out.append(rootp / rel)
    return out


def scan(root: str, *, max_files: int = MAX_FILES) -> tuple[list[FileInfo], dict]:
    """作業場を 1 回歩いて FileInfo を作る。戻り: (files, 落としたものの記録)"""
    rootp = Path(root)
    files: list[FileInfo] = []
    dropped = {"over_max_files": 0, "too_big": 0, "unreadable": 0}
    paths = _git_paths(rootp)
    if paths is None:
        paths = []
        for dirpath, dirnames, filenames in os.walk(rootp):
            dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
            for fn in sorted(filenames):
                paths.append(Path(dirpath) / fn)
    if len(paths) > max_files:
        # 上限で切るときはコードを先に残す（字体や画像で枠を使い切らない）
        dropped["over_max_files"] = len(paths) - max_files
        code = [p for p in paths if p.suffix.lower() in _LANG]
        rest = [p for p in paths if p.suffix.lower() not in _LANG]
        keep = set(map(str, (code + rest)[:max_files]))
        paths = [p for p in paths if str(p) in keep]
    for p in paths:
        try:
            size = p.stat().st_size
        except OSError:
            dropped["unreadable"] += 1
            continue
        rel = str(p.relative_to(rootp))
        lang = _LANG.get(p.suffix.lower(), "")
        info = FileInfo(rel, lang, size)
        if lang:
            if size > MAX_FILE_BYTES:
                info.note = f"大きすぎて未解析 {size:,}B"
                dropped["too_big"] += 1
            else:
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    info.note = "読めず"
                    dropped["unreadable"] += 1
                    text = ""
                if text:
                    if lang == "py":
                        _parse_py(text, info)
                    else:
                        _parse_regex(text, info)
        files.append(info)
    return files, dropped


# --------------------------------------------------------------------------
# 依存グラフ（2 ホップ）
# --------------------------------------------------------------------------

def _resolve(dep: str, src: str, index: dict[str, str]) -> str | None:
    """依存文字列を作業場内のパスに寄せる。**当たらなければ捨てる**（外部依存）。"""
    dep = dep.strip().strip("'\"")
    if not dep:
        return None
    srcdir = os.path.dirname(src)
    cands: list[str] = []
    if dep.startswith("."):
        # python の相対 import と、js の "./x"
        if "/" in dep or dep.startswith("./") or dep.startswith("../"):
            cands.append(os.path.normpath(os.path.join(srcdir, dep)))
        else:
            mod = dep.lstrip(".").replace(".", "/")
            cands.append(os.path.normpath(os.path.join(srcdir, mod)))
    else:
        cands.append(os.path.normpath(os.path.join(srcdir, dep)))
        cands.append(os.path.normpath(dep))
        cands.append(os.path.normpath(dep.replace(".", "/")))
    for c in cands:
        if c in index:
            return index[c]
        for ext in (".py", ".php", ".js", ".ts", ".mjs", ".sh"):
            if c + ext in index:
                return index[c + ext]
        if (c + "/__init__.py") in index:
            return index[c + "/__init__.py"]
        if (c + "/index.php") in index:
            return index[c + "/index.php"]
        if (c + "/index.js") in index:
            return index[c + "/index.js"]
    return None


def build_graph(files: list[FileInfo]) -> tuple[dict[str, set], dict[str, set]]:
    """戻り: (forward: 依存先, reverse: 依存元)。**両方向を使う**——
    「これは誰から呼ばれているか」は「これは何を呼ぶか」と同じだけ要る。"""
    index = {f.path: f.path for f in files}
    fwd: dict[str, set] = {f.path: set() for f in files}
    rev: dict[str, set] = {f.path: set() for f in files}
    for f in files:
        for d in f.deps:
            t = _resolve(d, f.path, index)
            if t and t != f.path:
                fwd[f.path].add(t)
                rev[t].add(f.path)
    return fwd, rev


#: 種が見つからないときに種とみなす名前（作業場の入口になりがちな物）。
_ENTRY_NAMES = ("index.php", "main.py", "app.py", "server.py", "__main__.py",
                "start.sh", "run.py", "index.js", "app.js", "manage.py",
                "router.php", "api.php", "conftest.py", "Makefile")


def _seed_paths(files: list[FileInfo], seed_text: str) -> list[str]:
    """工程の指示と検収コマンドから、作業場に**実在するパス**を拾う。

    ★「それらしい語」ではなく実在で照合する。存在しない物を種にすると、
      地図の中心が課題の外へずれる。
    """
    if not seed_text:
        return []
    hits: list[str] = []
    lowered = seed_text
    by_base: dict[str, list[str]] = {}
    for f in files:
        by_base.setdefault(os.path.basename(f.path), []).append(f.path)
    for f in files:
        if f.path in lowered:
            hits.append(f.path)
    for base, ps in by_base.items():
        if len(base) >= 5 and base in lowered:
            for p in ps:
                if p not in hits:
                    hits.append(p)
    return hits


def assign_hops(files: list[FileInfo], fwd, rev, seeds: list[str]) -> None:
    """種から 2 ホップだけ辿って `hop` を振る。3 ホップ以上は 99 のまま。"""
    for f in files:
        f.hop = 99
    byp = {f.path: f for f in files}
    frontier = [s for s in seeds if s in byp]
    for s in frontier:
        byp[s].hop = 0
    for h in (1, 2):
        nxt: list[str] = []
        for p in frontier:
            for q in list(fwd.get(p, ())) + list(rev.get(p, ())):
                if q in byp and byp[q].hop == 99:
                    byp[q].hop = h
                    nxt.append(q)
        frontier = nxt
        if not frontier:
            break


# --------------------------------------------------------------------------
# 刷る
# --------------------------------------------------------------------------

_HEADER = "[作業場の地図（本体は省いてシグネチャだけ。自動生成・{n} ファイル）]"


#: 同じ置き場・同じ拡張子のファイルが何個以上なら 1 行にまとめるか。
_OTHER_FOLD_MIN = 4
#: 「その他のファイル」に使ってよい行数と、まとめずに並べる名前の数。
_OTHER_MAX_LINES = 10
_OTHER_MAX_NAMES = 20


def _fold_others(rest: list[FileInfo]) -> tuple[list[str], int]:
    """中を見ないファイルは**置き場と拡張子でまとめる**。戻り: (行, 省いた件数)

    ★なぜ（2026-09-02・実測）: この課題の作業場は `images/<作者>/*.png` を
      数十枚作る。1 個ずつ名前を並べると、地図 1,940 字のうち
      **約 1,500 字＝77%** が `images/作者A/0.png(204B) …` の羅列になり、
      肝心のコードの側は 450 字しか残らない。地図は「どこに何があるか」に
      先に答えるための物なので、これでは払った予算が資産の名前に消える。
      実走行の作業場にも同じ形の資産がある（run46: 画像 88・動画 258 の経路が
      出力に出る／run48: 画像 60）。
    ★これは `phases._false_green` で一度直したのと**同じ族**——
      「同じ物の変種が枠を使い切って、いちばん効く行が落ちる」。
    ★省いた件数は必ず返す（この地図の約束: 省いた事実と件数を本文に書く）。
    """
    groups: dict[tuple[str, str], list[FileInfo]] = {}
    for f in rest:
        groups.setdefault((os.path.dirname(f.path),
                           os.path.splitext(f.path)[1].lower()), []).append(f)
    folded: list[tuple[str, int]] = []          # (行, その行が代表する件数)
    singles: list[FileInfo] = []
    for (d, ext), fs in sorted(groups.items()):
        if len(fs) >= _OTHER_FOLD_MIN:
            where = f"{d}/" if d else ""
            folded.append((f"  {where}*{ext or '（拡張子なし）'} × {len(fs)} 個"
                           f"（合計 {sum(x.size for x in fs):,}B）", len(fs)))
        else:
            singles += fs
    hidden = 0
    if len(folded) > _OTHER_MAX_LINES:
        hidden += sum(n for _, n in folded[_OTHER_MAX_LINES:])
        folded = folded[:_OTHER_MAX_LINES]
    out = [ln for ln, _ in folded]
    if singles:
        keep = singles[:_OTHER_MAX_NAMES]
        hidden += len(singles) - len(keep)
        out.append("  " + " ".join(f"{x.path}({x.size}B)" for x in keep))
    return out, hidden


def _render(files: list[FileInfo], fwd, dropped: dict, *,
            sig_near: int, sig_far: int, far_sigs: bool,
            others: bool, root: str) -> str:
    shown = [f for f in files if f.lang]
    shown.sort(key=lambda f: (f.hop, f.path))
    lines: list[str] = [_HEADER.format(n=len(files)), f"根: {root}"]
    omitted_sigs = 0
    omitted_files = 0
    body: list[str] = []
    for f in shown:
        near = f.hop <= 1
        if not near and not far_sigs:
            omitted_files += 1
            continue
        cap = sig_near if near else sig_far
        sigs = f.sigs[:cap]
        omitted_sigs += max(0, len(f.sigs) - cap)
        mark = "" if f.hop <= 1 else "  (2ホップ先)" if f.hop == 2 else "  (未到達)"
        head = f"- {f.path}{mark}"
        if f.note:
            head += f"  ※{f.note}"
        # ★予算が尽きて cap=0 になったとき、「…他 96 件」だけの行を刷らない。
        #   1 行使って何も伝えないのは、その行ぶん他のファイルを削っているのと同じ。
        if cap == 0 and f.sigs:
            head += f"  [{len(f.sigs)} 定義]"
        body.append(head)
        for s in sigs:
            body.append(f"    {s}")
        if cap and len(f.sigs) > cap:
            body.append(f"    …他 {len(f.sigs) - cap} 件")
        deps = sorted(fwd.get(f.path, ()))
        if deps:
            body.append(f"    → {', '.join(deps[:6])}"
                        + (f" …他 {len(deps) - 6}" if len(deps) > 6 else ""))
    lines += body
    rest = [f for f in files if not f.lang]
    if others and rest:
        lines.append("[その他のファイル（中は見ていません）]")
        body_lines, hidden = _fold_others(rest)
        lines += body_lines
        omitted_files += hidden
    elif rest:
        omitted_files += len(rest)

    notes = ["※この地図は**不完全**です: 関数の本体は載せていません"]
    if omitted_sigs:
        notes.append(f"シグネチャ {omitted_sigs} 件を字数で省きました")
    if omitted_files:
        notes.append(f"ファイル {omitted_files} 件を字数で省きました")
    if dropped.get("over_max_files"):
        notes.append(f"走査上限を超えた {dropped['over_max_files']} 件は見ていません")
    if dropped.get("too_big"):
        notes.append(f"大きすぎて未解析が {dropped['too_big']} 件")
    if dropped.get("unreadable"):
        notes.append(f"読めなかったのが {dropped['unreadable']} 件")
    bad = [f.path for f in files if f.note and "構文" in f.note]
    if bad:
        notes.append(f"構文誤りで読めなかった: {', '.join(bad[:3])}")
    notes.append("ここに無い物・本体の中身は Glob / Read / Grep で必ず確かめること")
    lines.append("。".join(notes) + "。")
    return "\n".join(lines)


#: 予算に収まるまで、この順に落とす（上が豊か・下が痩せている）。
_PLANS = (
    dict(sig_near=SIG_CAP_NEAR, sig_far=SIG_CAP_FAR, far_sigs=True, others=True),
    dict(sig_near=SIG_CAP_NEAR, sig_far=SIG_CAP_FAR, far_sigs=True, others=False),
    dict(sig_near=12, sig_far=4, far_sigs=True, others=False),
    dict(sig_near=8, sig_far=2, far_sigs=True, others=False),
    dict(sig_near=6, sig_far=0, far_sigs=True, others=False),
    dict(sig_near=4, sig_far=0, far_sigs=False, others=False),
    dict(sig_near=2, sig_far=0, far_sigs=False, others=False),
    dict(sig_near=0, sig_far=0, far_sigs=False, others=False),
)


def render_map(files: list[FileInfo], fwd, dropped: dict, *,
               budget_tokens: int = DEFAULT_BUDGET_TOKENS, root: str = ".") -> str:
    """予算に収まる一番豊かな刷り方を選ぶ。**必ず収める**（超えたら切って書く）。"""
    budget = max(MIN_BUDGET_TOKENS, int(budget_tokens))
    text = ""
    for plan in _PLANS:
        text = _render(files, fwd, dropped, root=root, **plan)
        if est_tokens(text) <= budget:
            return text
    # 最痩の版でも入らない＝ファイル数そのものが多い。行を落として、落としたと書く。
    lines = text.split("\n")
    while lines and est_tokens("\n".join(lines) + "\n…") > budget - 40:
        lines.pop(len(lines) // 2)      # 頭（見出し）と尾（不完全の注記）は残す
    return "\n".join(lines) + f"\n…（予算 {budget} トークンに収めるため中略しました）"


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def repo_map_block(workspace: str, *, seed_text: str = "",
                   budget_tokens: int = DEFAULT_BUDGET_TOKENS) -> str:
    """工程プロンプトに前置する地図。**壊れたら黙って空を返す**（走行は止めない）。

    `seed_text` ＝ この工程の指示と検収コマンド。ここに出てくる実在のパスを
    地図の中心に置く。空なら入口らしい名前を種にする。

    ★**控え（キャッシュ）は置かない**（2026-08-29・作りかけて捨てた）。
      指紋で無効化する控えを一度書いたが、実トレース（run29/31/32/34 の
      **12 試行すべて**）で作業場は毎回大きく書き換わる（1 試行あたり
      Write/Edit 24〜115 回）。＝**控えは一度も当たらない。**
      この現場は「作ったのに一度も発火しない機構」を既に 2 つ作っている。
      毎回歩き直す実測費用は 64 ファイルで 9ms・352 ファイルで 431ms＝
      1 試行 20 分〜4 時間の走行に対して無視できる。
      毎回作り直すので、地図は常にいまの作業場を映す（＝「作業場が変わったら
      作り直す」は控えを持たないことで自動的に満たされる）。
    """
    if os.environ.get("LAPS_REPOMAP", "1") == "0":
        return ""
    try:
        root = str(Path(workspace).resolve())
        if not os.path.isdir(root):
            return ""
        files, dropped = scan(root)
        if not files:
            return ""
        fwd, rev = build_graph(files)
        seeds = _seed_paths(files, seed_text)
        if not seeds:
            seeds = [f.path for f in files
                     if os.path.basename(f.path) in _ENTRY_NAMES]
        if not seeds:                      # 入口も分からない＝大きい物から
            seeds = [f.path for f in sorted(files, key=lambda x: -len(x.sigs))[:5]]
        assign_hops(files, fwd, rev, seeds)
        text = render_map(files, fwd, dropped, budget_tokens=budget_tokens, root=root)
        return text + "\n\n"
    except Exception:
        return ""


def phase_seed_text(phase) -> str:
    """工程オブジェクトから種のもとを作る（`phases.Phase` を import しないで済む形）。"""
    try:
        return "\n".join([str(getattr(phase, "name", "")),
                          str(getattr(phase, "instructions", "")),
                          "\n".join(getattr(phase, "acceptance", []) or [])])
    except Exception:
        return ""
