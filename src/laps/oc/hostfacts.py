"""作業する機械と届く先の事実 — 開発・インフラ向け（2026-10-05 オーナー「より開発及びインフラ特化に」）。

前置き（system）には**変わらない物だけ**を置く。前置きは依頼ごとに作り直され、会話の履歴の前に
並ぶので、中身が変わると会話全体の読み直し（キャッシュ外れ）になる:
  - OS の版・Bash の道具が使うシェル
  - 動くインタプリタ（envpack._works＝「在るか」でなく「動くか」）と PATH にある調査向けの道具
  - ~/.ssh/config の Host 別名（名前・HostName・User・Port だけ。鍵のパスは載せない）
変わる物は**依頼の側**に付ける（oc/loop.py の run）:
  - git の状態（Claude Code の gitStatus と同じく「依頼が始まった時点の写し」）

根拠: オーナー機の 0.1.280 の走行で `python --version` が 2 走行とも「認識されていません」で失敗し、
手番を使って探していた（`py -3` は動く）。oc の `<env>` は opencode の 6 行だけで、旧中核の
envpack は oc に繋がっていなかった。
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from ..envpack import _TOOLBOX, _kill_tree, _works

_CREATE_NO_WINDOW = 0x08000000
_INTERPRETERS = ("python3", "python", "py", "node", "git", "make", "pytest", "bash")
_SSH_HOSTS_MAX = 40
_GIT_STATUS_MAX = 30


def os_version() -> str:
    try:
        return platform.platform() or sys.platform
    except Exception:
        return sys.platform


def shell_name() -> str:
    """Bash の道具が実際に使うシェル（oc/tools.py の _bash_description と同じ言い方）。"""
    if sys.platform == "win32":
        if True:   # 中核は Git Bash を先に選ぶ（2026-11-19 旧い中核を外したので切り替えの LAPS_ENGINE も外した）
            try:
                from ..engine.tools.shell import resolve_shell
                return {"bash": "bash (Git Bash)", "powershell": "PowerShell", "cmd": "cmd.exe"}[resolve_shell()[0]]
            except Exception:
                pass
        return "cmd.exe (PowerShell syntax is detected and run with PowerShell)"
    return "bash" if shutil.which("bash") else "sh"


@lru_cache(maxsize=1)
def interpreters() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(動くもの, 在るのに動かないもの)。探りは 1 プロセスにつき 1 回。"""
    ok, broken = [], []
    for name in _INTERPRETERS:
        if _works(name):
            ok.append(name)
        elif shutil.which(name):
            broken.append(name)
    return tuple(ok), tuple(broken)


@lru_cache(maxsize=1)
def toolbox() -> tuple[str, ...]:
    key = "win32" if sys.platform == "win32" else ("darwin" if sys.platform == "darwin" else "linux")
    skip = set(_INTERPRETERS)
    return tuple(n for n in _TOOLBOX[key] if n not in skip and shutil.which(n))


def ssh_hosts(path: Path | None = None) -> list[str]:
    """~/.ssh/config の Host 別名を「name (user@hostname:port)」で返す。型（* ? !）は除く。
    Include は辿らない（書いてある別名だけ）。読めなければ空。"""
    p = path or (Path.home() / ".ssh" / "config")
    try:
        lines = p.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return []
    hosts: list[dict] = []
    cur: list[dict] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # ★2026-10-19: 行末の「   # ★…」の注記が値ごと前置きに載っていた（corridor-pi の行）
        line = re.sub(r"\s+#.*$", "", line)
        kv = re.split(r"[\s=]+", line, maxsplit=1)
        key, val = kv[0].lower(), (kv[1] if len(kv) > 1 else "").strip().strip('"')
        if key == "match":
            cur = []
        elif key == "host":
            cur = [{"name": n} for n in val.split() if not any(ch in n for ch in "*?!")]
            hosts.extend(cur)
        elif key in ("hostname", "user", "port"):
            for h in cur:
                h.setdefault(key, val)
    out = []
    for h in hosts:
        where = h.get("hostname", "")
        if h.get("user"):
            where = f"{h['user']}@{where or h['name']}"
        if h.get("port") and h["port"] != "22":
            where = f"{where or h['name']}:{h['port']}"
        out.append(f"{h['name']} ({where})" if where and where != h["name"] else h["name"])
    return out


def env_lines() -> list[str]:
    """`<env>` に足す行（変わらない物だけ）。"""
    ok, broken = interpreters()
    lines = [f"  OS: {os_version()}",
             f"  Shell used by the Bash tool: {shell_name()}",
             f"  Working interpreters and build tools: {', '.join(ok) or '(none found)'}"]
    if broken:
        lines.append(f"  On PATH but not working (e.g. the Microsoft Store python stub; do not use): {', '.join(broken)}")
    # ★利用者の Python の版（2026-10-10: オーナーの PC の py は 3.10.11。3.12 の f 文字列の書き方で 3 回 SyntaxError）。
    try:
        from .pycheck import _target_python
        t = _target_python()
        if t:
            _exe, name, ver = t
            ver = tuple(ver)
            rules = []
            if ver < (3, 12):
                rules.append("no backslash or the same quote character inside the {} of an f-string")
            if ver < (3, 11):
                rules.append("no except*, no tomllib, no typing.Self")
            if ver < (3, 10):
                rules.append("no match statement, no X | Y in isinstance or annotations at runtime")
            lines.append(f"  Python run by `{name}`: {ver[0]}.{ver[1]}"
                         + (f" (write code for this version: {'; '.join(rules)})" if rules else ""))
    except Exception:
        pass
    tb = toolbox()
    if tb:
        lines.append(f"  Other commands on PATH: {', '.join(tb)}")
    try:
        from .toolenv import tools_dir, installed_tools
        lines.append(f"  Tools folder (on PATH for Bash, no admin rights needed; unpack portable tools here): "
                     f"{tools_dir()}")
        # ★置き場に入っている道具（2026-10-15・知識サーバー の棚から入れた物も）。固定の表（envpack._TOOLBOX）には載らず、
        #   モデルからは見えていなかった。
        _inst = installed_tools()
        if _inst:
            lines.append(f"  Tools installed in the tools folder: {', '.join(_inst)}")
    except Exception:
        pass
    hosts = ssh_hosts()
    if hosts:
        more = f" (+{len(hosts) - _SSH_HOSTS_MAX} more)" if len(hosts) > _SSH_HOSTS_MAX else ""
        lines.append(f"  SSH hosts in ~/.ssh/config: {', '.join(hosts[:_SSH_HOSTS_MAX])}{more}")
    return lines


def _git(cwd: str, *args: str, timeout: float = 5.0) -> str | None:
    """git を 1 回。失敗・時間切れは None。★時間切れのあと communicate() で EOF を待たない
    （孫がパイプを握ると戻らない＝envpack._works の注記と同じ理由）。"""
    kw: dict = {"cwd": cwd, "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = _CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(["git", "-c", "core.quotepath=false", *args], **kw)
    except Exception:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        return None
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return out.decode("utf-8", errors="replace").rstrip()


def git_snapshot(cwd: str) -> str:
    """依頼が始まった時点の git の状態（Claude Code の gitStatus）。git の作業木でなければ空。"""
    if not shutil.which("git"):
        return ""
    if _git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return ""
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "(unknown)"
    status = _git(cwd, "status", "--short")
    log = _git(cwd, "log", "--oneline", "-5")
    parts = ["This is the git status at the start of this request. It is a snapshot and does not update "
             "during the request.", f"Current branch: {branch}"]
    if status is not None:
        rows = status.splitlines()
        if not rows:
            parts.append("Status: clean")
        else:
            cut = rows[:_GIT_STATUS_MAX]
            if len(rows) > _GIT_STATUS_MAX:
                cut.append(f"... ({len(rows) - _GIT_STATUS_MAX} more; run `git status` for all)")
            parts.append("Status:\n" + "\n".join(cut))
    if log:
        parts.append("Recent commits:\n" + log)
    return "<system-reminder>\n" + "\n\n".join(parts) + "\n</system-reminder>"


def _has_pytest(py: str) -> bool:
    """その python で pytest を読めるか（Bash の道具と同じ環境で問う）。確かめられなければ読めるとする。
    無いときは出力なしで 1 を返す。起動に失敗すると例外の文が出るので、それは「無い」と読まない。"""
    try:
        from ..engine.tools.shell import child_env
        r = subprocess.run([py, "-c", "import importlib.util as u,sys;sys.exit(u.find_spec('pytest') is None)"],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=5, env=child_env(),
                           creationflags=_CREATE_NO_WINDOW if os.name == "nt" else 0)
        return r.returncode == 0 or bool(r.stderr.strip())
    except (ImportError, OSError, subprocess.SubprocessError):
        return True


_TESTCASE = re.compile(r"\bunittest\.\w*TestCase\b|\bfrom\s+unittest\s+import\b[^\n]*TestCase")   # unittest.mock だけの試験は unittest で走らない


def _unittest_files(d: str) -> list[str]:
    """このフォルダの、unittest の形で書いてある試験のファイル。"""
    try:
        files = [n for n in os.listdir(d) if n.startswith("test_") and n.endswith(".py") or n.endswith("_test.py")]
    except OSError:
        return []
    out = []
    for n in files[:20]:
        try:
            with open(os.path.join(d, n), encoding="utf-8", errors="replace") as f:
                if _TESTCASE.search(f.read(16384)):
                    out.append(n)
        except OSError:
            pass
    return out


def _discover(files: list[str]) -> str:
    """unittest discover の型。既定の test*.py に当たらない名前だけなら型を添える（当たらないと 0 件で「通った」に見える）。"""
    return "" if any(n.startswith("test") for n in files) else " -p '*_test.py'"


def _py_test_row(cwd: str, names: set, py: str) -> str:
    """Python の試験の命令。★pytest が無い機械で「見つけた命令」として pytest を渡すと、モデルは失敗してから pip や apt-get へ進む
    （2026-10-31 課題集: 9/9 が No module named pytest・1 回 4〜34 秒・apt-get で機械の設定まで変えた）。"""
    if _has_pytest(py):
        return f"- test: `{py} -m pytest -q`"
    sub = _unittest_files(os.path.join(cwd, "tests")) if "tests" in names else []
    if sub:
        top = " -t ." if os.path.exists(os.path.join(cwd, "tests", "__init__.py")) else ""
        return f"- test: `{py} -m unittest discover -s tests{top}{_discover(sub)}`"
    here = _unittest_files(cwd)
    if here:
        return f"- test: `{py} -m unittest discover{_discover(here)}`"
    return f"- test: `{py} -m pytest -q` (pytest is not installed for {py})"


def project_commands(cwd: str) -> str:
    """作業フォルダの直下から、試験・ビルド・起動の命令を見つけて短く渡す（2026-10-10・開発の強化）。

    ★なぜ要るか: 編集の後に確かめずに終える手番がある（oc._unverified_pushback）。何で確かめればよいかを最初から知っていれば、
      確かめる手番が速く正しくなる。見るのは直下のファイルだけ（深く探さない＝速い）。見つからなければ空。"""
    import json as _json
    try:
        names = set(os.listdir(cwd))
    except OSError:
        return ""
    py = "py" if os.name == "nt" and shutil.which("py") else ("python3" if shutil.which("python3") else "python")
    rows: list[str] = []
    if "package.json" in names:
        try:
            with open(os.path.join(cwd, "package.json"), encoding="utf-8") as f:
                scripts = (_json.load(f) or {}).get("scripts") or {}
        except Exception:
            scripts = {}
        pm = "pnpm" if "pnpm-lock.yaml" in names else "yarn" if "yarn.lock" in names else "npm"
        for k in ("test", "build", "lint", "dev", "start"):
            if k in scripts:
                run = f"{pm} test" if k == "test" else f"{pm} run {k}" if pm != "yarn" else f"yarn {k}"
                rows.append(f"- {k}: `{run}`  (package.json: {str(scripts[k])[:60]})")
    def _py_tests(d: str) -> bool:
        try:
            return any(n.startswith("test_") and n.endswith(".py") or n.endswith("_test.py") for n in os.listdir(d))
        except OSError:
            return False
    if names & {"pytest.ini", "conftest.py"} or _py_tests(cwd) or ("tests" in names and _py_tests(os.path.join(cwd, "tests"))):
        rows.append(_py_test_row(cwd, names, py))
    elif "pyproject.toml" in names:
        try:
            with open(os.path.join(cwd, "pyproject.toml"), encoding="utf-8") as f:
                if "[tool.pytest" in f.read():
                    rows.append(_py_test_row(cwd, names, py))
        except OSError:
            pass
    if "Cargo.toml" in names:
        rows += ["- build: `cargo build`", "- test: `cargo test`"]
    if "go.mod" in names:
        rows += ["- build: `go build ./...`", "- test: `go test ./...`"]
    if any(n.endswith((".sln", ".csproj")) for n in names):
        rows += ["- build: `dotnet build`", "- test: `dotnet test`"]
    if "Makefile" in names or "makefile" in names:
        rows.append("- make: `make` (see the Makefile for targets such as `make test`)")
    if "index.html" in names:
        rows.append("- web page: Serve(directory=<this folder>, screenshot=true) to look at index.html in a browser")
    if not rows:
        return ""
    return ("<system-reminder>\nCommands found in this folder for building, testing or running the project "
            "(use them to check your changes):\n" + "\n".join(rows[:10]) + "\n</system-reminder>")

