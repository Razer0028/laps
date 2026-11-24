"""Python の書き込みの後に、定義の無い名前（import の書き忘れなど）を知らせる（2026-10-10）。

★なぜ要るか: 構文検査（compile）は通るが、走らせると最初の行で NameError になる形がある
  （`sys.argv` を使って `import sys` を書き忘れる・別の関数で作った変数を使う）。走行記録では、
  モデルはファイルを丸ごと書いて走らせ、エラーを読んで書き直す（1 往復に数十秒〜数分）。
  pyflakes の F821（undefined name）の軽い版をここで持つ（Windows の同梱 Python に pyflakes は無い）。

★誤検知を避ける作り（見逃しは許す・誤報はしない）:
  ・スコープを見ない。ファイルのどこかで代入・引数・import・def・class・except as・with as・for・
    global などで名前が作られていれば「定義あり」とみなす。
  ・`from x import *`・exec・globals()・locals()・vars()・__builtins__ を使うファイルは調べない。
  ・組み込みとモジュールの dunder は定義済み。
  ・書き込みは止めない（_syntax_note が「直すように」と添えるだけ）。
"""
from __future__ import annotations

import ast
import builtins
import sys

_MODULE_DUNDERS = {
    "__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__", "__builtins__",
    "__path__", "__annotations__", "__dict__", "__class__", "__qualname__", "__module__", "__debug__",
    "__cached__", "__all__", "__version__",
}

# よく使う名前 → 足すべき import（標準ライブラリだけ）。
_FROM = {
    "Path": "from pathlib import Path",
    "PurePath": "from pathlib import PurePath",
    "Counter": "from collections import Counter",
    "defaultdict": "from collections import defaultdict",
    "OrderedDict": "from collections import OrderedDict",
    "deque": "from collections import deque",
    "namedtuple": "from collections import namedtuple",
    "dataclass": "from dataclasses import dataclass",
    "field": "from dataclasses import field",
    "Any": "from typing import Any",
    "Optional": "from typing import Optional",
    "Union": "from typing import Union",
    "List": "from typing import List",
    "Dict": "from typing import Dict",
    "Tuple": "from typing import Tuple",
    "Iterable": "from typing import Iterable",
    "Iterator": "from typing import Iterator",
    "Callable": "from typing import Callable",
    "partial": "from functools import partial",
    "lru_cache": "from functools import lru_cache",
    "wraps": "from functools import wraps",
    "reduce": "from functools import reduce",
    "pprint": "from pprint import pprint",
    "glob": "import glob  (or: from glob import glob)",
    "datetime": "import datetime  (or: from datetime import datetime)",
    "timedelta": "from datetime import timedelta",
    "BytesIO": "from io import BytesIO",
    "StringIO": "from io import StringIO",
    "Enum": "from enum import Enum",
    "ThreadPoolExecutor": "from concurrent.futures import ThreadPoolExecutor",
}

_SKIP_CALLS = {"exec", "globals", "locals", "vars"}

# 外から入る・環境ごとにある名前（誤報しない）: gettext の `_`、IPython、Windows の組み込み、Python 2 互換の分岐。
_AMBIENT = {
    "_", "get_ipython", "display", "WindowsError", "unicode", "basestring", "long", "raw_input", "xrange",
    "unichr", "cmp", "buffer", "file", "reload", "execfile", "apply", "intern", "coerce", "reduce_ex",
}


def _stdlib() -> set[str]:
    names = getattr(sys, "stdlib_module_names", None)
    return set(names) if names else {"os", "sys", "re", "json", "time", "math", "subprocess", "shutil",
                                     "struct", "zlib", "hashlib", "random", "argparse", "itertools",
                                     "functools", "collections", "pathlib", "logging", "base64", "csv",
                                     "io", "glob", "typing", "threading", "traceback", "tempfile"}


def undefined_names(text: str, limit: int = 8) -> list[tuple[str, int]]:
    """[(名前, 最初に使った行)]。調べないファイル・構文エラーなら []。"""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    defined = set(dir(builtins)) | _MODULE_DUNDERS | _AMBIENT
    used: dict[str, int] = {}
    guarded: set[int] = set()
    # NameError を受ける try の中は「無いかもしれない」と承知で使っている（互換の分岐）。
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                h.type is None or any(isinstance(t, ast.Name) and t.id in ("NameError", "Exception", "BaseException")
                                      for t in (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]))
                for h in node.handlers):
            for b in node.body:
                guarded.update(id(x) for x in ast.walk(b))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(a.name == "*" for a in node.names):
                return []
            for a in node.names:
                defined.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                defined.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                if node.id == "__builtins__":
                    return []
                if id(node) not in guarded:
                    used.setdefault(node.id, node.lineno)
            else:
                defined.add(node.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SKIP_CALLS:
            return []
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)
        else:
            # match 文（MatchAs/MatchStar/MatchMapping）と型引数（3.12 の TypeVar など）の名前。
            for attr in ("name", "rest"):
                v = getattr(node, attr, None)
                if isinstance(v, str) and type(node).__name__.startswith(("Match", "TypeVar", "ParamSpec")):
                    defined.add(v)
    out = [(n, ln) for n, ln in used.items() if n not in defined]
    out.sort(key=lambda x: x[1])
    return out[:limit]


# ── 実際に走らせる Python で構文を確かめる（2026-10-10）────────────────────────────────────
# ★オーナーの走行（0.1.379・21:10〜21:30）: f 文字列の {} の中にバックスラッシュ（`{x.replace('\\n', ' ')}`）を書き、`py` で走らせて
#   3 回とも SyntaxError（同じ種類の失敗の 3 回目）。この書き方は Python 3.12 から（PEP 701）。書き込みの後の構文検査は同梱の
#   Python 3.12 で compile するので通ってしまい、実際に走る `py`（オーナーの PC は 3.10.11）でだけ落ちた。
# → 利用者の Python（Windows は py・それ以外は python3）が同梱より古ければ、その Python で ast.parse して誤りを返す。
#   版は 1 度だけ調べて覚える。見つからない・WindowsApps の偽物・同梱と同じか新しいなら何もしない。
_TARGET: object = None


def _target_python():
    global _TARGET
    if _TARGET is not None:
        return _TARGET or None
    import os
    import shutil
    try:
        from . import toolenv
        path = toolenv.child_path()
    except Exception:
        path = os.environ.get("PATH", "")
    names = ("py", "python") if sys.platform == "win32" else ("python3", "python")
    for n in names:
        exe = shutil.which(n, path=path)
        if not exe or "windowsapps" in exe.lower():
            continue
        try:
            from .. import procrun
            r = procrun.run([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"], timeout=15, text=True)
            ver = tuple(int(x) for x in (r.stdout or "").strip().split(".")[:2])
        except Exception:
            continue
        if len(ver) == 2:
            _TARGET = (exe, n, ver)
            return _TARGET
    _TARGET = False
    return None


def target_syntax_error(path: str) -> str | None:
    """利用者の Python が同梱より古いとき、その Python での構文の誤り（無ければ None）。"""
    if not path.lower().endswith((".py", ".pyw")):
        return None
    t = _target_python()
    if not t or tuple(t[2]) >= tuple(sys.version_info[:2]):
        return None
    exe, name, ver = t
    try:
        from .. import procrun
        r = procrun.run([exe, "-c", "import ast,sys;ast.parse(open(sys.argv[1],'rb').read(),sys.argv[1])", path],
                        timeout=20, text=True)
    except Exception:
        return None
    if r.returncode == 0:
        return None
    lines = [ln.rstrip() for ln in (r.stderr or "").splitlines() if ln.strip()]
    tail = "\n".join(lines[-4:]) if lines else "SyntaxError"
    if "Error" not in tail:
        return None
    return (f"`{name}` here is Python {ver[0]}.{ver[1]} (older than the checker), and it cannot parse this file:\n{tail}"
            + ("\nBackslashes inside the {} of an f-string need Python 3.12: compute the value in a variable first."
               if "backslash" in tail.lower() else ""))


def diagnose(path: str, text: str) -> list[str]:
    low = path.lower()
    if not low.endswith((".py", ".pyw")):
        return []
    try:
        names = undefined_names(text)
    except Exception:
        return []
    if not names:
        return []
    std = _stdlib()
    items = []
    for n, ln in names:
        fix = _FROM.get(n) or (f"import {n}" if n in std else "")
        items.append(f"line {ln}: undefined name `{n}`" + (f" — missing `{fix}`?" if fix else
                                                              " — not defined or imported anywhere in this file"))
    return items
