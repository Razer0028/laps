"""検査・試験・ビルドの出力を「ファイル:行」に揃える（2026-10-17・開発の強化 第 1 段）。

オーナー 2026-10-17「本格的に Blackwall の開発機能を最大限まで強化したい」。
★なぜ要るか（走行記録・直近 10 日の 154 本・数えの欄だけ）: 確かめは Bash に任せていて、モデルは長い出力の末尾から
  失敗を探していた。pytest・jest・tsc・cargo・go・dotnet はそれぞれ違う形で場所を書くので、読み違える。
  ここで揃えて渡せば「どのファイルの何行目を直すか」が一目で決まる。

★この部品は**文字を読むだけ**（命令は起こさない）。走らせるのは oc/checktool.py が既存の Bash の道具に頼む。
★解析の材料は作り物ではなく、作業用のコンテナ で実際に走らせた出力（tests/fixtures/checktool/*.txt）。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass


@dataclass
class Diag:
    """1 件の指摘。sev は error / warning / fail（試験の失敗）。"""
    path: str
    line: int
    col: int = 0
    sev: str = "error"
    msg: str = ""
    code: str = ""

    def text(self) -> str:
        loc = f"{self.path}:{self.line}" + (f":{self.col}" if self.col else "")
        return f"{loc} {self.sev}{(' ' + self.code) if self.code else ''}: {self.msg}"


#: Windows のドライブ名（C:\…）も場所として拾う。
_DRIVE = r"(?:[A-Za-z]:)?"
_RE_RUFF = re.compile(rf"^(?P<p>{_DRIVE}[^:\n]+?):(?P<l>\d+):(?P<c>\d+): (?P<code>[A-Z]+\d+) (?:\[\*\] )?(?P<m>.+)$", re.M)
#: tsc と dotnet（`src/a.ts(1,7): error TS2322: …`／末尾の `[…csproj]` は落とす）。
_RE_PAREN = re.compile(rf"^(?P<p>{_DRIVE}[^()\n]+?)\((?P<l>\d+),(?P<c>\d+)\): (?P<sev>error|warning) "
                       r"(?P<code>[A-Za-z]+\d+): (?P<m>.+?)(?: \[[^\]]+\])?$", re.M)
_RE_CARGO = re.compile(rf"^(?P<p>{_DRIVE}[^:\s][^:\n]*?):(?P<l>\d+):(?P<c>\d+): (?P<sev>error|warning)"
                       r"(?:\[(?P<code>E\d+)\])?: (?P<m>.+)$", re.M)
_RE_PANIC = re.compile(rf"panicked at (?P<p>{_DRIVE}[^:\n]+?):(?P<l>\d+):(?P<c>\d+):\s*\n(?P<m>[^\n]*)")
_RE_RUST_FAILED = re.compile(r"^(?:test )?(?P<t>[\w:]+) (?:\.\.\. FAILED|--- FAILED)$", re.M)
_RE_GO = re.compile(rf"^(?:vet: )?(?P<p>{_DRIVE}[^:\s\n]+\.go):(?P<l>\d+):(?P<c>\d+): (?P<m>.+)$", re.M)
_RE_GO_FAIL = re.compile(r"^--- FAIL: (?P<t>\S+)", re.M)
_RE_GO_TLOC = re.compile(rf"^\s+(?P<p>{_DRIVE}[^:\s]+\.go):(?P<l>\d+): (?P<m>.+)$", re.M)
_RE_PY_LOC = re.compile(rf"^(?P<p>{_DRIVE}[^:\n]+\.py):(?P<l>\d+): (?P<m>.+)$", re.M)
_RE_PY_FAILED = re.compile(r"^(?:FAILED|ERROR) (?P<t>\S+?)(?: - (?P<m>.+))?$", re.M)
_RE_JS_AT = re.compile(rf"\((?P<p>{_DRIVE}[^():\n]+?):(?P<l>\d+):(?P<c>\d+)\)")
_RE_VITEST_LOC = re.compile(rf"^\s*❯ (?P<p>{_DRIVE}[^:\s]+):(?P<l>\d+):(?P<c>\d+)\s*$", re.M)
_RE_STYLISH = re.compile(r"^\s+(?P<l>\d+):(?P<c>\d+)\s+(?P<sev>error|warning)\s+(?P<m>.+?)"
                         r"(?:\s{2,}(?P<code>[\w@/-]+))?\s*$")
_RE_PHP = re.compile(r"(?:PHP )?(?:Parse|Fatal) error:\s+(?P<m>.+?) in (?P<p>.+?) on line (?P<l>\d+)")
#: mypy・gcc 形（`path:12:3: error: …`）。知らない道具の落ち先にも使う。
_RE_GENERIC = re.compile(rf"^(?P<p>{_DRIVE}[^:\s][^:\n]*?):(?P<l>\d+)(?::(?P<c>\d+))?: (?P<sev>error|warning|note)"
                         r"(?:\[(?P<code>[^\]]+)\])?:? (?P<m>.+)$", re.M)


#: javac（gradle の中も同じ・`A.java:3: error: …`／日本語の JDK は `エラー:`・`警告:`）。
_RE_JAVAC = re.compile(rf"^\s*(?P<p>{_DRIVE}[^:\n]+?\.(?:java|kt|kts|groovy|scala)):(?P<l>\d+): "
                       r"(?P<sev>error|warning|エラー|警告): (?P<m>.+)$", re.M)
#: kotlinc（gradle の Kotlin・`e: file:///p/A.kt:3:13 msg`）。
_RE_KOTLIN = re.compile(r"^(?P<sev>[ew]): (?:file://)?(?P<p>[^\s:][^:\n]*?\.kts?):(?P<l>\d+):(?P<c>\d+) (?P<m>.+)$", re.M)
#: maven（`[ERROR] /p/A.java:[3,13] msg`）と surefire（`[ERROR]   ATest.fails:7 expected …`）。
_RE_MVN = re.compile(rf"^\[(?P<sev>ERROR|WARNING)\] (?P<p>{_DRIVE}[^:\n\[]+?):\[(?P<l>\d+),(?P<c>\d+)\] (?P<m>.+)$", re.M)
_RE_SUREFIRE = re.compile(r"^\[ERROR\]\s{2,}(?P<t>[\w$.]+?)(?::(?P<l>\d+))? (?P<m>.+)$", re.M)
#: gradle の試験（`ATest > fails() FAILED` の次の行に `    …Error at ATest.java:7`）。
_RE_GRADLE_FAIL = re.compile(r"^(?P<t>[\w$.]+ > .+?) FAILED\s*\n\s+(?P<m>[^\n]+?)(?: at (?P<p>[\w$]+\.\w+):(?P<l>\d+))?$",
                             re.M)
#: ctest（`  2 - breaks (Failed)`）。
_RE_CTEST = re.compile(r"^\s*\d+ - (?P<t>\S+) \((?:Failed|Timeout|SEGFAULT|Subprocess aborted|Exception|Not Run)[^)]*\)",
                       re.M)
_RE_PHPUNIT = re.compile(r"^\d+\) (?P<t>\S+)\n(?P<m>[^\n]*)\n(?:[^\n]*\n)*?(?P<p>/[^\n:]+\.php):(?P<l>\d+)$", re.M)
_RE_BUN_FAIL = re.compile(r"^\(fail\) (?P<t>.+?)(?: \[[\d.]+m?s\])?$", re.M)


#: Windows の絶対パス（C:\… と \\server\share\…）。★動いている OS では決めない——Linux の 開発機 や 作業用のコンテナ から
#: Windows の機械の出力を読むことがあり、posixpath では C:\… を絶対と見ない（2026-10-17 の試験で実際に外した）。
_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def rel(p: str, cwd: str = "") -> str:
    """場所を作業フォルダからの相対に（外に出るなら元のまま）。Windows のパスは動いている OS に関わらず読める。"""
    p = (p or "").strip()
    if not p:
        return p
    try:
        if cwd:
            win = bool(_WIN_ABS.match(p)) or bool(_WIN_ABS.match(cwd))
            mod = __import__("ntpath") if win else os.path
            if mod.isabs(p):
                r = mod.relpath(p, cwd)
                if not r.startswith(".."):
                    return r.replace("\\", "/")
    except (ValueError, TypeError):                     # 別のドライブなど＝相対にできない
        pass
    return p[2:] if p.startswith("./") else p


def _eslint_json(text: str, cwd: str) -> list[Diag]:
    i = text.find("[")
    if i < 0:
        return []
    try:
        rows, _ = json.JSONDecoder().raw_decode(text[i:])
    except ValueError:
        return []
    out: list[Diag] = []
    for f in rows if isinstance(rows, list) else []:
        if not isinstance(f, dict):
            continue
        for m in f.get("messages") or []:
            out.append(Diag(rel(str(f.get("filePath") or ""), cwd), int(m.get("line") or 0), int(m.get("column") or 0),
                            "error" if m.get("severity") == 2 or m.get("fatal") else "warning",
                            str(m.get("message") or "").strip(), str(m.get("ruleId") or "")))
    return out


def _eslint_stylish(text: str, cwd: str) -> list[Diag]:
    """eslint の既定の見た目（ファイル名の行 → 字下げした `行:列 重さ 本文 規則`）。"""
    out, cur = [], ""
    for ln in text.splitlines():
        s = ln.strip()
        if ln[:1] not in (" ", "\t", "") and (os.path.isabs(s) or re.match(r"^[A-Za-z]:[\\/]", s)):
            cur = rel(s, cwd)
            continue
        m = _RE_STYLISH.match(ln)
        if m and cur:
            out.append(Diag(cur, int(m["l"]), int(m["c"]), m["sev"], m["m"].strip(), m["code"] or ""))
    return out


def _jest(text: str, cwd: str) -> tuple[list[Diag], list[str]]:
    """jest: 「● 名前」の塊ごとに、最初の本文と、最初の (ファイル:行:列)。"""
    diags, names = [], []
    for b in re.split(r"^\s+● ", text, flags=re.M)[1:]:
        name = b.splitlines()[0].strip()
        if not name or name.startswith("Console"):
            continue
        names.append(name)
        body = [x.strip() for x in b.splitlines()[1:] if x.strip()]
        msg = next((x for x in body if not x.startswith(">") and not re.match(r"^\d+ \|", x) and not x.startswith("at ")), "")
        m = _RE_JS_AT.search(b)
        if m:
            diags.append(Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), "fail", f"{name}: {msg}".strip(": ")))
    return diags, names


def _vitest(text: str, cwd: str) -> tuple[list[Diag], list[str]]:
    diags, names = [], []
    for part in re.split(r"^ FAIL  ", text, flags=re.M)[1:]:
        name = part.splitlines()[0].strip()
        names.append(name)
        msg = next((x.strip() for x in part.splitlines()[1:] if x.strip()), "")
        m = _RE_VITEST_LOC.search(part)
        if m:
            diags.append(Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), "fail", f"{name}: {msg}".strip(": ")))
    return diags, names


#: unittest（`python -m unittest discover -v`）。★pytest が入っていない Python（作業用のコンテナ・Windows の素の Python）の落ち先
#: （2026-10-18: Check が pytest しか知らず、そこでは「No module named pytest」で落ちていた）。
_RE_UT_HEAD = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<test>\S+) \((?P<where>[^)\s]+)\)\s*$", re.M)
_RE_UT_FRAME = re.compile(r'^\s+File "(?P<p>[^"]+)", line (?P<l>\d+), in (?P<fn>\S+)\s*$', re.M)
_RE_UT_SEP = re.compile(r"^(?:=|-){20,}\s*$", re.M)


def _unittest(text: str, cwd: str) -> tuple[list[Diag], list[str]]:
    """失敗ごとに、作業場の中のいちばん深い行（標準ライブラリの行は飛ばす）と最後の誤りの 1 行。"""
    diags: list[Diag] = []
    failed: list[str] = []
    heads = list(_RE_UT_HEAD.finditer(text))
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        block = text[h.end():end]
        stop = re.search(r"^-{20,}\s*\nRan \d+ tests?", block, re.M)
        if stop:
            block = block[:stop.start()]
        where = h["where"]
        name = where if where.endswith("." + h["test"]) else f"{where}.{h['test']}"
        failed.append(name)
        frames = [m for m in _RE_UT_FRAME.finditer(block)]
        own = [m for m in frames if not re.search(r"[\\/](?:lib[\\/]python[\d.]*|Lib)[\\/](?:unittest|site-packages)[\\/]",
                                                   m["p"])]
        last = (own or frames or [None])[-1]
        msg_lines = [ln.strip() for ln in _RE_UT_SEP.sub("", block).splitlines() if ln.strip()]
        msg = next((ln for ln in reversed(msg_lines) if re.match(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt)\b", ln)
                    or ln.startswith("AssertionError")), msg_lines[-1] if msg_lines else "")
        if last is not None:
            diags.append(Diag(rel(last["p"], cwd), int(last["l"]), 0, "fail", f"{h['kind']} {name}: {msg}"[:300]))
    return diags, failed


def _jvm(text: str, cwd: str) -> tuple[list[Diag], list[str]]:
    """gradle・maven（javac・kotlinc・surefire・gradle の試験）。同じ場所の重複（gradle は誤りを 2 回書く）は 1 つに。"""
    diags: list[Diag] = []
    seen: set = set()

    def add(d: Diag) -> None:
        k = (d.path, d.line, d.msg)
        if k not in seen:
            seen.add(k)
            diags.append(d)
    for m in _RE_JAVAC.finditer(text):
        add(Diag(rel(m["p"].strip(), cwd), int(m["l"]), 0, "warning" if m["sev"] in ("warning", "警告") else "error",
                 m["m"].strip()))
    for m in _RE_KOTLIN.finditer(text):
        add(Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), "error" if m["sev"] == "e" else "warning", m["m"].strip()))
    for m in _RE_MVN.finditer(text):
        add(Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), "error" if m["sev"] == "ERROR" else "warning",
                 m["m"].strip()))
    failed: list[str] = []
    for m in _RE_GRADLE_FAIL.finditer(text):
        failed.append(m["t"])
        if m["p"]:
            add(Diag(m["p"], int(m["l"]), 0, "fail", f"{m['t']}: {m['m'].strip()}"))
    if "[ERROR] Failures:" in text or "[ERROR] Errors:" in text:
        part = text[max(text.find("[ERROR] Failures:"), text.find("[ERROR] Errors:")):]
        for m in _RE_SUREFIRE.finditer(part):
            if m["t"].startswith("Run"):
                continue
            failed.append(m["t"])
            if m["l"]:
                cls = m["t"].rsplit(".", 1)[0].split("$")[0]
                add(Diag(cls.replace(".", "/") + ".java", int(m["l"]), 0, "fail", f"{m['t']}: {m['m'].strip()}"))
    return diags, list(dict.fromkeys(failed))


def parse(name: str, text: str, cwd: str = "") -> tuple[list[Diag], list[str]]:
    """(場所つきの指摘, 落ちた試験の名前)。知らない道具は汎用の形だけ拾う（取りこぼす方に倒す）。"""
    t = text or ""
    if name == "ruff":
        return [Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), "error", m["m"].strip(), m["code"])
                for m in _RE_RUFF.finditer(t)], []
    if name == "eslint":
        d = _eslint_json(t, cwd)
        return (d if d or t.lstrip().startswith("[") else _eslint_stylish(t, cwd)), []
    if name in ("tsc", "dotnet"):
        return [Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), m["sev"], m["m"].strip(), m["code"])
                for m in _RE_PAREN.finditer(t)], []
    if name == "cargo":
        d = [Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), m["sev"], m["m"].strip(), m["code"] or "")
             for m in _RE_CARGO.finditer(t)]
        d += [Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), "fail", m["m"].strip()) for m in _RE_PANIC.finditer(t)]
        return d, list(dict.fromkeys(m["t"] for m in _RE_RUST_FAILED.finditer(t)))
    if name == "go":
        seen = set()
        d = []
        for m in _RE_GO.finditer(t):
            d.append(Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"]), "error", m["m"].strip()))
            seen.add((d[-1].path, d[-1].line))
        for m in _RE_GO_TLOC.finditer(t):               # 試験の中の t.Errorf の場所（列が無い）
            p, ln = rel(m["p"], cwd), int(m["l"])
            if (p, ln) not in seen:
                d.append(Diag(p, ln, 0, "fail", m["m"].strip()))
        return d, [m["t"] for m in _RE_GO_FAIL.finditer(t)]
    if name == "pytest":
        return ([Diag(rel(m["p"], cwd), int(m["l"]), 0, "fail", m["m"].strip()) for m in _RE_PY_LOC.finditer(t)],
                [m["t"] for m in _RE_PY_FAILED.finditer(t)])
    if name == "unittest":
        return _unittest(t, cwd)
    if name == "jest":
        return _jest(t, cwd)
    if name == "vitest":
        return _vitest(t, cwd)
    if name == "npm-test":                              # 中身が jest か vitest か分からない
        return _vitest(t, cwd) if (" FAIL  " in t and "❯" in t) else _jest(t, cwd)
    if name == "jvm":
        return _jvm(t, cwd)
    if name == "ctest":
        d, _ = parse("generic", t, cwd)                # 先に組み立てるので、コンパイラの誤りも拾う
        return d, list(dict.fromkeys(m["t"] for m in _RE_CTEST.finditer(t)))
    if name == "shellcheck":
        out = []
        for m in _RE_GENERIC.finditer(t):
            if m["sev"] == "note":
                continue
            msg, code = m["m"].strip(), ""
            sc = re.search(r"\s*\[(SC\d+)\]$", msg)
            if sc:
                msg, code = msg[:sc.start()], sc.group(1)
            out.append(Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"] or 0), m["sev"], msg, code))
        return out, []
    if name == "phpunit":
        return ([Diag(rel(m["p"], cwd), int(m["l"]), 0, "fail", f"{m['t']}: {m['m'].strip()}")
                 for m in _RE_PHPUNIT.finditer(t)], [m["t"] for m in _RE_PHPUNIT.finditer(t)])
    if name == "bun":
        d, _ = _jest(t, cwd)
        return d, [m["t"] for m in _RE_BUN_FAIL.finditer(t)]
    if name == "php":
        return [Diag(rel(m["p"], cwd), int(m["l"]), 0, "error", m["m"].strip()) for m in _RE_PHP.finditer(t)], []
    return [Diag(rel(m["p"], cwd), int(m["l"]), int(m["c"] or 0), m["sev"], m["m"].strip(), m["code"] or "")
            for m in _RE_GENERIC.finditer(t) if m["sev"] != "note"], []


def summary(diags: list[Diag], failed: list[str]) -> str:
    """件数の一行（「error 3・warning 1・試験の失敗 2」）。"""
    n_e = sum(1 for d in diags if d.sev == "error")
    n_w = sum(1 for d in diags if d.sev == "warning")
    n_f = len(failed) or sum(1 for d in diags if d.sev == "fail")
    parts = [f"{n} {k}" for n, k in ((n_e, "error"), (n_w, "warning"), (n_f, "failing test")) if n]
    return "・".join(parts) if parts else ""
