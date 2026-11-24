"""Check 道具: プロジェクトの検査・試験・ビルドを見つけて走らせ、失敗を「ファイル:行」で返す（2026-10-17）。

オーナー 2026-10-17「本格的に Blackwall の開発機能を最大限まで強化したい」。
★走らせるのは**既存の Bash の道具**（oc/tools.bash_tool）に頼む。自前でプロセスを起こさない:
  - 許可の門（policy）・利用者の deny_patterns・サンドボックス・監査・打ち切りの後片付け・符号化の判定が、
    すでにそこに一本化されている。二つ目の実行の口を作ると、片方だけ直る（この現場が何度も踏んだ形）。
  - 走らせる命令は文字列で組み立て、`cwd` と `timeout` を渡すだけ。
★見つけ方は「手元に在る道具だけ」。無い物は挙げない（勧めても打てない）。
★出力の解析は oc/checkparse.py（文字を読むだけ・材料は 作業用のコンテナ で実際に走らせた出力）。

使う所: 道具 Check ／ 編集の後の速い検査（autolint）／ 終わる前の自動の確かめ（oc/loop.py）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass

from . import checkparse as cp

#: プロジェクトの根の目印。
_MARKERS = ("package.json", "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "Cargo.toml",
            "go.mod", "composer.json", "tsconfig.json", ".git",
            # ★2026-10-19 JVM・C/C++・Deno（それまでは Java や CMake のプロジェクトで「プロジェクトではない」と断っていた）
            "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "pom.xml",
            "CMakeLists.txt", "Makefile", "deno.json", "deno.jsonc")
_PROJ_EXT = (".csproj", ".sln", ".fsproj")
#: 種類ごとの既定の待ち（秒）。lint は速い・試験とビルドは長い。
TIMEOUT = {"lint": 90, "types": 240, "test": 600, "build": 600}
KINDS = ("auto", "lint", "types", "test", "build")
MAX_DIAGS = 20


@dataclass
class Cmd:
    """走らせる 1 本。`reader` は出力を読む名前（checkparse.parse）。"""
    name: str
    line: str                      # そのまま Bash に渡す命令
    kind: str                      # lint / types / test / build
    cwd: str
    reader: str = ""

    @property
    def parser(self) -> str:
        return self.reader or self.name


def _q(p: str) -> str:
    """空白を含む場所だけ引用する（Windows の \\ はそのまま）。"""
    return f'"{p}"' if p and (" " in p or "\t" in p) else p


def project_root(path: str, max_up: int = 8) -> str | None:
    """path（ファイルでもフォルダでも）から上へ、目印のある最初のフォルダ。無ければ None。

    ★None ＝「使い捨ての台本」。自動の検査はそこでは走らせない（走行記録では .py の書き捨てが最も多い）。
    ★ホームそのものは根にしない（.git があっても、そこは「プロジェクト」ではない）。
    """
    try:
        d = os.path.abspath(path)
    except (OSError, ValueError):
        return None
    if not os.path.isdir(d):
        d = os.path.dirname(d)
    try:
        home = os.path.abspath(os.path.expanduser("~"))
    except (OSError, RuntimeError):
        home = ""
    for _ in range(max_up):
        try:
            names = set(os.listdir(d))
        except OSError:
            return None
        if names & set(_MARKERS) or any(n.endswith(_PROJ_EXT) for n in names):
            return None if d == home else d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up
    return None


def _which(name: str) -> str | None:
    """Bash の子と同じ PATH で探す（棚から入れた道具も見つかる）。プロセスの PATH だけだと、Bash では動く ruff を
    「無い」と言い、終える前の自動の確かめも黙って飛んだ（2026-10-30 実測）。"""
    try:
        from . import toolenv
        return shutil.which(name, path=toolenv.child_path())
    except Exception:
        return shutil.which(name)


def _node_bin(root: str, name: str) -> str | None:
    """プロジェクトが持っている道具（node_modules/.bin）。PATH には入れない（版が違う）。"""
    d = os.path.join(root, "node_modules", ".bin")
    return shutil.which(name, path=d) if os.path.isdir(d) else None


def _python() -> str:
    if os.name == "nt" and _which("py"):
        return "py"
    return "python3" if _which("python3") else ("python" if _which("python") else sys.executable)


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeError):
        return ""


def _scripts(root: str) -> dict:
    try:
        return (json.loads(_read(os.path.join(root, "package.json")) or "{}") or {}).get("scripts") or {}
    except ValueError:
        return {}


def _pkg_mgr(names: set) -> str:
    return "pnpm" if "pnpm-lock.yaml" in names else "yarn" if "yarn.lock" in names else "npm"


def _has_py_tests(root: str) -> bool:
    for d in (root, os.path.join(root, "tests"), os.path.join(root, "test")):
        try:
            if any(n.endswith(".py") and (n.startswith("test_") or n.endswith("_test.py")) for n in os.listdir(d)):
                return True
        except OSError:
            continue
    return False


def _tests_here(path: str) -> bool:
    """そのフォルダ（ファイルならその親）に試験のファイルがあるか（Python の test_*.py・JS の *.test.js／package.json の test）。"""
    d = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    if _has_py_tests(d):
        return True
    for sub in (d, os.path.join(d, "test"), os.path.join(d, "tests")):
        try:
            if any(n.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts", ".test.mjs")) for n in os.listdir(sub)):
                return True
        except OSError:
            continue
    return bool(_scripts(d).get("test"))


def _gradle(root: str, names: set) -> str | None:
    """プロジェクトの gradlew を先に（版が揃う）。無ければ PATH の gradle。"""
    if os.name == "nt" and "gradlew.bat" in names:
        return "gradlew.bat"
    if os.name != "nt" and "gradlew" in names:
        return "./gradlew" if os.access(os.path.join(root, "gradlew"), os.X_OK) else "sh ./gradlew"
    return "gradle" if _which("gradle") else None


def _is_android(root: str) -> bool:
    """Android の Gradle（classes の仕事が無い・assembleDebug／testDebugUnitTest を使う）。"""
    for sub in ("", "app"):
        for f in ("build.gradle", "build.gradle.kts"):
            if "com.android" in _read(os.path.join(root, sub, f)):
                return True
    return False


#: CMake の作り先として見る場所（在る物を使う・無ければ build を作る）。
_CMAKE_DIRS = ("build", "out/build", "cmake-build-debug", "cmake-build-release")


def _cmake_dir(root: str) -> str | None:
    for d in _CMAKE_DIRS:
        if os.path.isfile(os.path.join(root, d, "CMakeCache.txt")):
            return d
    return None


def _make_target(root: str, *targets: str) -> str | None:
    body = _read(os.path.join(root, "Makefile"))
    for t in targets:
        if f"\n{t}:" in "\n" + body:
            return t
    return None


def _has_eslint_config(root: str) -> bool:
    try:
        return any(n.startswith(("eslint.config.", ".eslintrc")) for n in os.listdir(root))
    except OSError:
        return False


def detect(root: str, kind: str = "auto", paths: list[str] | None = None, name_filter: str = "") -> list[Cmd]:
    """種類ごとに、手元に在る道具で走らせる命令。auto＝lint＋types（どちらも無ければ test）。

    paths を渡すと、その種類のファイルだけを見る道具（ruff・eslint・php -l）はそこに絞る。
    """
    if kind == "auto":
        cmds = detect(root, "lint", paths) + detect(root, "types", paths)
        return cmds or detect(root, "test", paths, name_filter)
    try:
        names = set(os.listdir(root))
    except OSError:
        return []
    rels = [p.replace("\\", "/") for p in (paths or []) if p]
    out: list[Cmd] = []
    pyproj = _read(os.path.join(root, "pyproject.toml")) if "pyproject.toml" in names else ""
    is_py = bool(names & {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "requirements.txt"}) \
        or any(n.endswith(".py") for n in names)
    projs = [n for n in names if n.endswith(_PROJ_EXT)]

    def pick(exts: tuple[str, ...]) -> list[str]:
        """paths のうちこの種類の物（paths を渡していなければプロジェクト全体）。空なら走らせない。"""
        if not rels:
            return ["."]
        return [p for p in rels if os.path.splitext(p)[1].lower() in exts]

    gradle = _gradle(root, names) if names & {"build.gradle", "build.gradle.kts", "settings.gradle",
                                               "settings.gradle.kts"} else None
    android = bool(gradle) and _is_android(root)
    mvn = "mvn" if "pom.xml" in names and _which("mvn") else None
    cmake = "CMakeLists.txt" in names and _which("cmake")
    cdir = _cmake_dir(root) if cmake else None
    make = "Makefile" in names and not cmake and _which("make")
    deno = names & {"deno.json", "deno.jsonc"} and _which("deno")
    is_py = is_py or any(p.endswith((".py", ".pyi")) for p in rels)

    if kind == "lint":
        if is_py and _which("ruff"):
            t = pick((".py", ".pyi"))
            if t:
                out.append(Cmd("ruff", "ruff check --output-format concise --no-cache " + " ".join(_q(x) for x in t),
                               kind, root))
        es = _node_bin(root, "eslint")
        if es and _has_eslint_config(root):
            t = pick((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte"))
            if t:
                out.append(Cmd("eslint", f"{_q(es)} -f json " + " ".join(_q(x) for x in t), kind, root))
        php = _which("php")
        if php:
            for p in [x for x in rels if x.endswith(".php")][:5]:
                out.append(Cmd("php", f"php -l {_q(p)}", kind, root))
        sh = [x for x in rels if x.endswith((".sh", ".bash"))]
        if sh and _which("shellcheck"):
            out.append(Cmd("shellcheck", "shellcheck -f gcc " + " ".join(_q(x) for x in sh[:20]), kind, root))
        if deno:
            out.append(Cmd("deno", "deno lint" + ("".join(" " + _q(x) for x in pick((".ts", ".tsx", ".js", ".jsx")))
                                                 if rels else ""), kind, root, reader="generic"))
    elif kind == "types":
        tsc = _node_bin(root, "tsc")
        if tsc and "tsconfig.json" in names:
            out.append(Cmd("tsc", f"{_q(tsc)} --noEmit --pretty false -p .", kind, root))
        if "Cargo.toml" in names and _which("cargo"):
            out.append(Cmd("cargo", "cargo check --message-format short", kind, root))
        if "go.mod" in names and _which("go"):
            out.append(Cmd("go", "go vet ./...", kind, root))
        if projs and _which("dotnet"):
            out.append(Cmd("dotnet", "dotnet build -nologo -v q -clp:NoSummary", kind, root))
        if "[tool.mypy" in pyproj and _which("mypy"):
            out.append(Cmd("mypy", "mypy --no-error-summary --hide-error-context .", kind, root, reader="generic"))
        # ★JVM と C/C++ には「型だけ」の検査が無いので、組み立て（コンパイル）をここに置く（cargo check と同じ扱い）。
        if gradle and not android:
            out.append(Cmd("gradle", f"{gradle} -q --console=plain classes testClasses", kind, root, reader="jvm"))
        elif mvn and not gradle:
            out.append(Cmd("maven", "mvn -q -B test-compile", kind, root, reader="jvm"))
        if cdir:                                       # 作り先が在るときだけ（差分の組み立て・速い）
            out.append(Cmd("cmake", f"cmake --build {_q(cdir)}", kind, root, reader="generic"))
        if deno:
            out.append(Cmd("deno", "deno check .", kind, root, reader="generic"))
    elif kind == "test":
        vitest, jest = _node_bin(root, "vitest"), _node_bin(root, "jest")
        scripts = _scripts(root)
        if vitest:
            out.append(Cmd("vitest", f"{_q(vitest)} run" + (f" -t {_q(name_filter)}" if name_filter else ""), kind, root))
        elif jest:
            out.append(Cmd("jest", f"{_q(jest)} --ci" + (f" -t {_q(name_filter)}" if name_filter else ""), kind, root))
        elif scripts.get("test") and "no test specified" not in str(scripts["test"]):
            pm = _pkg_mgr(names)
            if _which(pm):
                out.append(Cmd("npm-test", f"{pm} test", kind, root, reader="npm-test"))
        if names & {"pytest.ini", "conftest.py"} or _has_py_tests(root) or "[tool.pytest" in pyproj:
            out.append(Cmd("pytest", f"{_python()} -m pytest -q -rf --tb=line -p no:cacheprovider"
                           + (f" -k {_q(name_filter)}" if name_filter else ""), kind, root))
        if "Cargo.toml" in names and _which("cargo"):
            out.append(Cmd("cargo", "cargo test -q" + (f" {_q(name_filter)}" if name_filter else ""), kind, root))
        if "go.mod" in names and _which("go"):
            out.append(Cmd("go", "go test" + (f" -run {_q(name_filter)}" if name_filter else "") + " ./...", kind, root))
        if projs and _which("dotnet") and any("test" in n.lower() for n in projs):
            out.append(Cmd("dotnet", "dotnet test -nologo -v q", kind, root, reader="generic"))
        if not (vitest or jest) and names & {"bun.lockb", "bun.lock"} and _which("bun"):
            out = [c for c in out if c.name != "npm-test"]
            out.append(Cmd("bun", "bun test" + (f" -t {_q(name_filter)}" if name_filter else ""), kind, root))
        if deno:
            out.append(Cmd("deno", "deno test" + (f" --filter {_q(name_filter)}" if name_filter else ""), kind, root,
                           reader="generic"))
        if gradle:
            task = "testDebugUnitTest" if android else "test"
            out.append(Cmd("gradle", f"{gradle} --console=plain {task}"
                           + (f" --tests {_q(name_filter)}" if name_filter else ""), kind, root, reader="jvm"))
        elif mvn:
            out.append(Cmd("maven", "mvn -q -B test" + (f" -Dtest={_q(name_filter)}" if name_filter else ""), kind,
                           root, reader="jvm"))
        if os.path.isfile(os.path.join(root, "vendor", "bin", "phpunit")) and _which("php"):
            out.append(Cmd("phpunit", "php vendor/bin/phpunit" + (f" --filter {_q(name_filter)}" if name_filter else ""),
                           kind, root))
        if cmake and _which("ctest"):
            d = cdir or "build"
            pre = "" if cdir else f"cmake -S . -B {d} && "
            out.append(Cmd("ctest", f"{pre}cmake --build {_q(d)} && ctest --test-dir {_q(d)} --output-on-failure"
                           + (f" -R {_q(name_filter)}" if name_filter else ""), kind, root))
        elif make:
            t = _make_target(root, "test", "check")
            if t:
                out.append(Cmd("make", f"make {t}", kind, root, reader="generic"))
    elif kind == "build":
        if _scripts(root).get("build"):
            pm = _pkg_mgr(names)
            if _which(pm):
                out.append(Cmd("build", f"{pm} run build", kind, root, reader="tsc"))
        if "Cargo.toml" in names and _which("cargo"):
            out.append(Cmd("cargo", "cargo build --message-format short", kind, root))
        if "go.mod" in names and _which("go"):
            out.append(Cmd("go", "go build ./...", kind, root))
        if projs and _which("dotnet"):
            out.append(Cmd("dotnet", "dotnet build -nologo -v q -clp:NoSummary", kind, root))
        if gradle:
            out.append(Cmd("gradle", f"{gradle} -q --console=plain " + ("assembleDebug" if android else "build -x test"),
                           kind, root, reader="jvm"))
        elif mvn:
            out.append(Cmd("maven", "mvn -q -B package -DskipTests", kind, root, reader="jvm"))
        if cmake:
            d = cdir or "build"
            out.append(Cmd("cmake", ("" if cdir else f"cmake -S . -B {d} && ") + f"cmake --build {_q(d)}", kind, root,
                           reader="generic"))
        elif make:
            out.append(Cmd("make", "make -k", kind, root, reader="generic"))
    return out


# ── 走らせる（既存の Bash の道具に頼む）───────────────────────────────────

@dataclass
class Result:
    cmd: Cmd
    ok: bool
    out: str
    diags: list
    failed: list
    denied: str = ""

    @property
    def summary(self) -> str:
        return cp.summary(self.diags, self.failed)


def _looks_failed(text: str, diags: list, failed: list) -> bool:
    """Bash の結果から成否を読む。★`exit code:` が出ていればそれが正（道具によっては指摘が 0 でも落ちる）。"""
    import re
    m = re.search(r"exit code:\s*(-?\d+)", text)
    if m:
        return m.group(1) != "0"
    return bool(failed) or any(getattr(d, "sev", "") in ("error", "fail") for d in diags)


def run_one(cmd: Cmd, timeout: int | None = None) -> Result:
    """1 本走らせて解析する。実行は oc/tools.bash_tool（許可の門・監査・サンドボックスを通る唯一の口）。"""
    from .tools import bash_tool
    secs = int(timeout or TIMEOUT.get(cmd.kind, 180))
    # ★鍵は `workdir`（`cwd` ではない）。`timeout` はミリ秒。
    #   2026-10-17: ここを `cwd` で渡して黙って無視され、見本ではなく**そのとき居たフォルダ**で pytest が走った
    #   （Blackwall の全試験が始まった）。道具ごとに引数の名前が違う（Lab は cwd も workdir も受ける）ので、
    #   渡した場所で本当に走ったかを結果で確かめる。
    res = bash_tool({"command": cmd.line, "workdir": cmd.cwd, "timeout": secs * 1000})
    text = str(res)
    if getattr(res, "kind", "") == "denied":
        return Result(cmd, False, text, [], [], denied=text)
    diags, failed = cp.parse(cmd.parser, text, cmd.cwd)
    ok = bool(getattr(res, "ok", True)) and not _looks_failed(text, diags, failed)
    return Result(cmd, ok, text, diags, failed)


def report(results: list[Result], kind: str, root: str) -> tuple[str, bool]:
    """(モデルへ返す本文, 全部通ったか)。通った物は 1 行、落ちた物は指摘の一覧。"""
    if not results:
        return ("No check command was found here (nothing installed that fits this project), so nothing ran. "
                "Run the project's own check with Bash and say what it reported."), False
    lines: list[str] = []
    all_ok = True
    for r in results:
        head = f"{r.cmd.name} ({r.cmd.kind}): `{r.cmd.line}`"
        if r.denied:
            lines.append(f"- {head} — not run: {r.denied.strip().splitlines()[0][:200]}")
            all_ok = False
            continue
        if r.ok:
            lines.append(f"- {head} — passed" + (f" ({r.summary})" if r.summary else ""))
            continue
        all_ok = False
        lines.append(f"- {head} — FAILED" + (f" ({r.summary})" if r.summary else ""))
        if r.failed:
            lines.append("  failing tests: " + ", ".join(r.failed[:10])
                         + (f" …and {len(r.failed) - 10} more" if len(r.failed) > 10 else ""))
        shown = [d for d in r.diags if d.sev in ("error", "fail")] or r.diags
        for d in shown[:MAX_DIAGS]:
            lines.append("  " + d.text())
        if len(shown) > MAX_DIAGS:
            lines.append(f"  …and {len(shown) - MAX_DIAGS} more")
        if not r.diags and not r.failed:               # 場所が読めない＝末尾をそのまま見せる
            tail = [x.rstrip() for x in r.out.split("<shell_metadata>")[0].splitlines() if x.strip()][-12:]
            lines += ["  " + x[:300] for x in tail]
    head = f"Checked {os.path.basename(root) or root} ({kind}): " + ("all passed" if all_ok else "problems found")
    return head + "\n" + "\n".join(lines), all_ok


def _unittest_cmd(root: str, name_filter: str = "") -> Cmd:
    """pytest の無い Python で同じ試験を走らせる命令（unittest は標準ライブラリ）。
    tests/ が package でなければ、そこを起点に探す（package なら root から探せば入る）。"""
    t = os.path.join(root, "tests")
    start = " -s tests" if os.path.isdir(t) and not os.path.exists(os.path.join(t, "__init__.py")) else ""
    return Cmd("unittest", f"{_python()} -m unittest discover -v{start}" + (f" -k {_q(name_filter)}" if name_filter else ""),
               "test", root)


def run(root: str, kind: str = "auto", paths: list[str] | None = None, name_filter: str = "",
        timeout: int | None = None) -> tuple[str, bool, list[Result]]:
    """見つけて走らせて本文にする。(本文, 全部通ったか, 結果)。"""
    cmds = detect(root, kind, paths, name_filter)
    results = []
    for c in cmds:
        r = run_one(c, timeout)
        # ★pytest が入っていない（作業用のコンテナ の Python・Windows の素の Python）なら、同じ試験を unittest で走らせ直す
        #   （2026-10-18: Check は「No module named pytest」を落ちた試験として返し、モデルは自分で走らせ直していた）。
        if c.name == "pytest" and not r.ok and "No module named pytest" in r.out:
            r = run_one(_unittest_cmd(root, name_filter), timeout)
        results.append(r)
    text, ok = report(results, kind, root)
    return text, ok, results


# ── 道具としての入口 ─────────────────────────────────────────────────────────

DESCRIPTION = """Run this project's own checks (linter, type checker, tests, build) and get the failures as file:line.

It finds what the project actually uses - ruff, eslint, tsc, mypy, pytest, jest, vitest, bun, deno, cargo, go, dotnet,
Gradle, Maven, CMake/ctest, make, shellcheck, php, phpunit - from its config files, and only offers tools that are
installed here (a project's own gradlew is preferred). Node tools come from the project's node_modules/.bin.
Output comes back as `path:line:col severity CODE: message` plus the names of failing tests, so you can go straight to
the place to fix instead of reading a long log.

Use it after you change code, and again after you fix something. `kind="auto"` runs the fast checks (linter and type
checker) and falls back to the tests when there is no fast check. Use `kind="test"` to run the tests, `paths` to limit
the linter to the files you touched, and `filter` to run one test by name."""

SCHEMA_PROPS = {
    "kind": {"type": "string", "enum": list(KINDS),
             "description": "auto (fast checks, falls back to tests), lint, types, test or build. Default auto"},
    "path": {"type": "string", "description": "A file or folder in the project (default: the working directory). The "
             "project root is found from here by its config files"},
    "paths": {"type": "array", "items": {"type": "string"},
              "description": "Only check these files (the linter is limited to them; whole-project checks like tsc "
                             "and cargo still look at everything)"},
    "filter": {"type": "string", "description": "Run only tests whose name matches (pytest -k, jest/vitest -t, "
               "go -run, cargo's filter, gradle --tests, mvn -Dtest, ctest -R)"},
    "timeout": {"type": "number", "description": "Seconds to allow each command (default: 90 lint, 240 types, 600 "
                "test and build)"},
}


def check_tool(inp: dict):
    """道具 Check。走らせるのは既存の Bash の経路（許可の門・監査を通る）。"""
    from ..tools import ToolResult
    inp = inp if isinstance(inp, dict) else {}
    kind = str(inp.get("kind") or "auto").strip().lower()
    if kind not in KINDS:
        return ToolResult(f"Error: kind must be one of {', '.join(KINDS)}.", ok=False, kind="error")
    where = str(inp.get("path") or "").strip() or os.getcwd()
    if not os.path.exists(where):
        return ToolResult(f"Error: {where} does not exist.", ok=False, kind="error")
    root = project_root(where)
    if not root and _tests_here(where):
        # ★目印（pyproject.toml など）が無くても、試験のファイルがあるフォルダはそこで試験を走らせる
        #   （2026-10-18 オーナー機の Haiku の走行: `_scratch\evalbench` で「プロジェクトの中ではない」と断っていた）。
        #   自動の検査（autolint・autoverify）は今までどおり project_root だけを見る＝使い捨ての台本では走らない。
        root = where if os.path.isdir(where) else os.path.dirname(os.path.abspath(where))
    if not root:
        return ToolResult(
            f"Error: {where} is not inside a project (no package.json, pyproject.toml, Cargo.toml, go.mod, "
            "*.csproj, build.gradle, pom.xml, CMakeLists.txt, Makefile or .git above it), so there is no project check to run. Run the program "
            "itself with Bash instead.", ok=False, kind="error")
    raw = inp.get("paths")
    paths = [str(x) for x in raw if str(x).strip()] if isinstance(raw, list) else \
        ([str(raw)] if isinstance(raw, str) and raw.strip() else [])
    paths = [cp.rel(os.path.abspath(p) if os.path.isabs(p) else p, root) for p in paths]
    try:
        secs = int(float(inp.get("timeout"))) if inp.get("timeout") not in (None, "") else None
    except (TypeError, ValueError):
        secs = None
    text, ok, results = run(root, kind, paths, str(inp.get("filter") or "").strip(), secs)
    # ★何も見つからなかったときは「通った」にしない（確かめていないのに確かめた扱いになる）。
    counters = {f"check:{kind}:{'ok' if ok else 'fail'}": 1} if results else {"check:none_detected": 1}
    return ToolResult(text, ok=bool(ok), kind="ok" if ok else "error", data={"counters": counters})
