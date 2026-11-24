"""Edit tool: exact-string replacement(s) in an existing file.

Supports two shapes:
1. Single edit (Claude-Code-compat): file_path + old_string + new_string [+ replace_all]
2. Multi edit: file_path + edits=[{old_string, new_string, replace_all?}, ...]
   Applied sequentially. If any step fails, the file is left unchanged (atomic).
"""
from __future__ import annotations
import os
import re
import tempfile
from pathlib import Path
from .. import procrun  # 固まらない subprocess（2026-09-06）

from . import pathfix as _pathfix
from . import ToolSpec, ToolResult
from .atomic import write_bytes_atomic
from ..policy import authorize_path, PolicyDenied


def _syntax_error(path: str, text: str) -> str | None:
    """A syntax error in `text` for known, checkable-without-executing file types,
    else None. Covers the languages a coding/infra agent edits most: Python, JSON,
    YAML. Unknown types return None (never gated)."""
    if len(text) > 2_000_000:
        # Oversize: skip. The gate is best-effort; a full re-parse per edit on a
        # multi-MB file is not worth the latency, and it avoids YAML alias-
        # expansion ('billion laughs') CPU/memory blowups on model-authored input.
        return None
    p = path.lower()
    t = text.lstrip("\ufeff")  # a leading BOM must not make valid json/yaml look broken
    try:
        if p.endswith(".py"):
            import ast
            ast.parse(t)
        elif p.endswith(".json"):
            import json as _json
            _json.loads(t)
        elif p.endswith((".yaml", ".yml")):
            try:
                import yaml  # optional dependency
            except ImportError:
                return None
            # safe_load raises on VALID multi-document YAML ('---' separators);
            # k8s/Ansible/Helm files are routinely multi-doc, so consume the
            # generator to surface only real per-document parse errors.
            list(yaml.safe_load_all(t))
        elif p.endswith((".php", ".sh", ".bash", ".go", ".lua", ".rb", ".ps1", ".psm1")):
            return _external_syntax_error(p, t)
        elif p.endswith(".toml"):
            try:
                import tomllib
            except ImportError:
                return None
            tomllib.loads(t)
        elif p.endswith((".xml", ".svg", ".xaml", ".csproj", ".props", ".targets", ".plist")):
            import xml.etree.ElementTree as _ET
            _ET.fromstring(t.encode("utf-8"))
        elif p.endswith((".js", ".mjs", ".cjs")):
            return _js_syntax_error(p, t)
        elif p.endswith(".css"):
            return _css_syntax_error(t)
        else:
            return None
    except SyntaxError as e:
        return f"{type(e).__name__}: {e.msg}" + (f" (行 {e.lineno})" if e.lineno else "")
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return None


#: 外部の構文検査。**実行はしない**（php -l も bash -n も構文を見るだけ）。
_EXT_CHECKERS = {
    ".php": ("php", ["-l"]),
    ".sh": ("bash", ["-n"]),
    ".bash": ("bash", ["-n"]),
    # ★2026-10-10（オーナー「フルコード対応にしましょう」）: 構文だけを見る検査器（どれも実行しない）。無い機械では黙って通す。
    ".go": ("gofmt", ["-e", "-l"]),
    ".lua": ("luac", ["-p"]),
    ".rb": ("ruby", ["-c"]),
}
#: 構文エラーだと判断する印。★起動時の警告（実測: pdo_sqlite が読めない等）を
#: 構文エラーと取り違えると、正しい編集を延々と断ることになる。
#: ★**言語設定で日本語になる**（実測: bash は「構文エラー: 予期しないファイル終了」と出す）。
#:   英語の印だけで書いたら、実環境でだけ素通りするゲートになっていた——
#:   「試験は緑なのに本番だけ死ぬ」の逆で「試験だけ緑」。両方の語を持つ。
_SYNTAX_MARKS = (
    "Parse error", "syntax error", "unexpected", "PHP Parse error", "expected", "ParserError",
    # ★2026-09-07（vdir の実用評価 v_php_bulk_edit6・page2.php）: php -l は
    #   `declare(strict_types=1)` の位置ずれを **Fatal error**「must be the very
    #   first statement」＋「Errors parsing <file>」で出す。これらは従来の印に
    #   無く、非0終了なのに「印なし＝素通り」で壊れた php を通していた。
    #   `Errors parsing` は php -l が**そのファイルが壊れている**ときだけ出す最も確実な語。
    "Errors parsing", "Fatal error", "must be the very first", "致命的なエラー",
    "構文エラー", "予期しない",
)


def _external_syntax_error(path_lower: str, text: str) -> str | None:
    """.php / .sh の構文検査（2026-08-28・監査 S10）。

    ★なぜ要るか: 全編集の 84〜93% が php/sh なのに、構文ゲートは .py/.json/.yaml
      しか見ていなかった＝**この現場でいちばん編集される言語に検査が無い**。
      モデルが自分で `php -l` を打ったのは 84 回中 2 回だけ（実測）。
    ★検査器が無い環境では何もしない（黙って通す）。ゲートは best-effort。
    """
    import shutil
    import subprocess
    ext = "." + path_lower.rsplit(".", 1)[-1]
    if ext in (".ps1", ".psm1"):
        return _ps_syntax_error(text)
    prog_args = _EXT_CHECKERS.get(ext)
    if not prog_args:
        return None
    prog, args = prog_args
    if os.environ.get("LAPS_EXT_SYNTAX", "1") == "0" or not shutil.which(prog):
        return None
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        # 言語に依存しない読み取りのため C ロケールで走らせる（それでも日本語で
        # 出す実装があるので、印は両方の語を持つ）。
        _env = dict(os.environ, LC_ALL="C", LANG="C")
        r = procrun.run([prog] + args + [tmp], capture_output=True, timeout=10, env=_env)
        out = (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace"))
        if r.returncode == 0:
            return None
        # 起動時の警告だけで終了コードが立つ実装があるので、印で確かめる
        lines = [ln for ln in out.splitlines() if any(k in ln for k in _SYNTAX_MARKS)]
        if not lines:
            return None
        # 理由の分かる行（Fatal/Parse/must be…）を優先し、総括の「Errors parsing」は後回し。
        lines.sort(key=lambda ln: ("Errors parsing" in ln, "Fatal error" not in ln and "Parse error" not in ln and "エラー" not in ln))
        return (lines[0].replace(tmp, path_lower).strip()
                + "（構文が壊れています。書き込みは行っていません）")
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _js_syntax_error(path_lower: str, text: str) -> str | None:
    """JS の構文（2026-10-10・Web の開発）。Node の `--check` は構文を見るだけで実行しない。Node が無ければ黙って通す。
    ★`import`/`export` で書いたブラウザのモジュールは .mjs として見る（.js のまま渡すと CommonJS と読まれ、正しい
      モジュールを「import は使えない」と断る）。"""
    import re as _re
    import shutil
    import subprocess
    node = shutil.which("node")
    if os.environ.get("LAPS_EXT_SYNTAX", "1") == "0" or not node:
        return None
    module = path_lower.endswith(".mjs") or (not path_lower.endswith(".cjs") and
                                             _re.search(r"^\s*(?:import\s|import\(|export\s)", text, _re.M))
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".mjs" if module else ".cjs")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        r = procrun.run([node, "--check", tmp], capture_output=True, timeout=10)
        if r.returncode == 0:
            return None
        out = r.stderr.decode("utf-8", "replace") + r.stdout.decode("utf-8", "replace")
        # ★トップレベルの await は、import/export が無くてもモジュール（<script type="module">）なら正しい。
        #   CommonJS として断られたら、モジュールとして見直す（2026-10-10 の点検）。
        if not module and ("await is only valid" in out or "Cannot use import" in out):
            os.unlink(tmp)
            fd, tmp = tempfile.mkstemp(suffix=".mjs")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            r = procrun.run([node, "--check", tmp], capture_output=True, timeout=10)
            if r.returncode == 0:
                return None
            out = r.stderr.decode("utf-8", "replace") + r.stdout.decode("utf-8", "replace")
        # ★JSX（.js に書いた React など）は Node では読めない＝構文の誤りとは言えない。黙って通す（2026-10-10 の点検）。
        if "Unexpected token '<'" in out:
            return None
        line = ""
        m = _re.search(_re.escape(tmp) + r":(\d+)", out)
        if m:
            line = f"（行 {m.group(1)}）"
        err = next((ln.strip() for ln in out.splitlines() if "SyntaxError" in ln), "")
        if not err:
            return None                     # 構文以外の理由で落ちた（Node の不調など）＝止めない
        return f"{err}{line}"
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _css_syntax_error(text: str) -> str | None:
    """CSS の括弧・コメント・文字列の閉じ忘れ（2026-10-10）。中身の正しさは見ない（未知の書き方で止めない）。"""
    depth, line, i, n = 0, 1, 0, len(text)
    open_lines: list[int] = []
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
        elif c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                return f"コメント /* が閉じていません（行 {line}）"
            line += text.count("\n", i, j)
            i = j + 2
            continue
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            if j >= n or text[j] == "\n":
                return f"文字列 {c} が閉じていません（行 {line}）"
            i = j + 1
            continue
        elif c == "{":
            depth += 1
            open_lines.append(line)
        elif c == "}":
            depth -= 1
            if depth < 0:
                return f"余分な }} があります（行 {line}）"
            open_lines.pop()
        i += 1
    if depth > 0:
        return f"{{ が閉じていません（行 {open_lines[-1]} で開いた {depth} 個）"
    return None


def _ps_syntax_error(text: str) -> str | None:
    """PowerShell の構文（2026-10-10）。言語の構文解析器（Parser.ParseInput）で見るだけで実行しない。Windows の powershell が無ければ通す。"""
    import base64
    import shutil
    import subprocess
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if os.environ.get("LAPS_EXT_SYNTAX", "1") == "0" or not exe:
        return None
    src = base64.b64encode(text.encode("utf-8")).decode("ascii")
    script = ("$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + src + "'));"
              "$e=$null;[void][System.Management.Automation.Language.Parser]::ParseInput($t,[ref]$null,[ref]$e);"
              "if($e){$x=$e[0];'{0} (行 {1})' -f $x.Message,$x.Extent.StartLineNumber}")
    try:
        r = procrun.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, timeout=20)
        out = r.stdout.decode("utf-8", "replace").strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def _match_sites(body: str, old: str) -> list[int]:
    sites, pos = [], -1
    while True:
        pos = body.find(old, pos + 1)
        if pos == -1:
            return sites
        sites.append(pos)


def _line_bounds(body: str, pos: int) -> tuple[int, int]:
    ls = body.rfind("\n", 0, pos) + 1
    le = body.find("\n", pos)
    return ls, (len(body) if le == -1 else le)


def _prev_nonempty(body: str, line_start: int) -> str:
    """Nearest non-empty line before line_start, stripped — the site's context."""
    end = line_start
    while end > 0:
        pe = end - 1  # the '\n' that ends the previous line
        ps = body.rfind("\n", 0, pe) + 1
        prev = body[ps:pe].strip()
        if prev:
            return prev
        end = ps
    return ""


def _heterogeneous_full_line(body: str, old: str, sites: list[int]) -> bool:
    """The P5 corruption shape, recognised structurally so it can be refused.

    `mul`'s body was identical to `add`'s; the model matched "return a + b",
    hit the cnt>1 guard, and answered it with replace_all=true — changing both
    functions and breaking the one it was not asked to touch. The guard's wording
    could not stop that because the choice is the model's.

    So the harness refuses replace_all in exactly this shape and no other: `old`
    is a whole statement (each matched line, minus indentation, *is* `old`) that
    recurs under DIFFERENT surroundings — i.e. genuinely different places that
    merely share a line. Substring renames are not this: their line stripped is
    not equal to `old`, so `foo_bar`→`baz` across many lines still sweeps freely.
    """
    if "\n" in old:
        return False
    target = old.strip()
    sigs = set()
    for pos in sites:
        ls, le = _line_bounds(body, pos)
        if body[ls:le].strip() != target:
            return False  # a substring within a line, not a whole-line statement
        sigs.add(_prev_nonempty(body, ls))
    return len(sigs) > 1


def _norm_trailing(s: str) -> str:
    """各行の行末空白を落とす（改行は保つ）。"""
    return "\n".join(line.rstrip() for line in s.split("\n"))


def _norm_spaces(s: str) -> str:
    """行内の空白の連なりを 1 個に畳み、行末空白も落とす。"""
    return "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in s.split("\n"))


def _find_unique_by(body: str, old: str, norm) -> tuple[int, int] | None:
    """正規化して一致する箇所が**ちょうど1つ**なら、元本文での範囲を返す。

    ★行単位で見る。正規化した文字列を単純に find すると、元本文の位置へ
      戻す計算が正規化のたびに変わって狂う。行の対応表を持つほうが正確。
    """
    b_lines = body.split("\n")
    o_lines = old.split("\n")
    if not o_lines:
        return None
    nb = [norm(x) for x in b_lines]
    no = [norm(x) for x in o_lines]
    # 末尾の空行は照合の邪魔になるだけ（old の末尾改行はよくある）
    while no and no[-1] == "":
        no.pop()
    if not no:
        return None
    hits = []
    for i in range(0, len(nb) - len(no) + 1):
        if nb[i:i + len(no)] == no:
            hits.append(i)
            if len(hits) > 1:
                return None            # 曖昧＝発動しない
    if len(hits) != 1:
        return None
    i = hits[0]
    start = sum(len(x) + 1 for x in b_lines[:i])
    end = start + sum(len(x) + 1 for x in b_lines[i:i + len(no)]) - 1
    return start, min(end, len(body))


def _loose_match(body: str, old: str, *, _depth: int = 0) -> tuple[int, int, str] | None:
    """完全一致しない old を、**段を上げながら**緩く照合する（aider の照合カスケード）。

    段: ①行末空白を無視 ②行内空白を畳む ③先頭行と末尾行が一意に挟む範囲
    どの段も**候補が1つのときだけ**当てる。当たった段名を返し、呼び手が必ず報告する。
    """
    if os.environ.get("LAPS_EDIT_FUZZY", "1") == "0":
        return None
    for norm, how in ((_norm_trailing, "行末の空白の違いを無視して一致"),
                      (_norm_spaces, "行内の空白の詰め方の違いを無視して一致")):
        r = _find_unique_by(body, old, norm)
        if r:
            return r[0], r[1], how
    # ④ ★モデルが `\n` `\t` を**文字のまま**出した事故を戻す（2026-09-22）。
    #   出典: opencode `tool/edit.ts:498-545`（EscapeNormalizedReplacer）。
    #   ★実測: 再生できた失敗 51 件のうち **2 件**がこれで当たる（4%）。小さいが安全。
    if "\\n" in old or "\\t" in old or '\\"' in old:
        _un = (old.replace("\\r\\n", "\n").replace("\\n", "\n")
                  .replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'"))
        if _un != old and _depth == 0:
            i = body.find(_un)
            if i >= 0 and body.find(_un, i + 1) < 0:
                return i, i + len(_un), "エスケープ（\\n 等）を文字に戻して一致"
            # ★戻したうえで**残りの段も掛け直す**（2026-09-22）。
            #   完全一致だけを見ていたら 0 件だった——実測で当たった 2 件は
            #   どちらも「戻したあとに空白の段で一致」だった。
            _r = _loose_match(body, _un, _depth=1)
            if _r:
                return _r[0], _r[1], "エスケープを戻したうえで " + _r[2]

    # ③ 先頭行と末尾行で挟む（中身の写し間違いを許す）。2 行以上のときだけ。
    o_lines = [x for x in old.split("\n") if x.strip()]
    if len(o_lines) >= 2:
        b_lines = body.split("\n")
        nb = [_norm_spaces(x) for x in b_lines]
        first, last = _norm_spaces(o_lines[0]), _norm_spaces(o_lines[-1])
        starts = [i for i, x in enumerate(nb) if x == first and x]
        if len(starts) == 1:
            i = starts[0]
            ends = [j for j in range(i + 1, len(nb)) if nb[j] == last and nb[j]]
            if len(ends) >= 1:
                j = ends[0]
                # ★挟む範囲の暴走を止める（無条件の蓋・2026-08-27）:
                #   中身の写し間違いなら行数はほぼ変わらない。大きく膨らむのは
                #   「たまたま同じ行で始まり同じ行で終わる**別の場所**」を掴んだ形で、
                #   そのまま置換すると**間の本物のコードを消す**。
                #   救える件数より、間違って消さないことを優先する。
                span = j - i + 1
                if span > max(len(o_lines) + 2, int(len(o_lines) * 1.5)):
                    # ★2026-09-22: ここは `return None` だった＝**関数ごと終わって**
                    #   下の段⑤（中身の類似度を見る）へ一度も届いていなかった。
                    #   蓋に当たったのはこの段が危ないという意味であって、
                    #   **次の段を試さない理由にはならない。**
                    i = j = -1
                if i >= 0:
                    start = sum(len(x) + 1 for x in b_lines[:i])
                    end = start + sum(len(x) + 1 for x in b_lines[i:j + 1]) - 1
                    return (start, min(end, len(body)),
                            "先頭行と末尾行が一意に挟む範囲として一致")

    # ⑤ ★アンカー（先頭行・末尾行）＋**中身の類似度**（2026-09-22）。
    #   出典: opencode `tool/edit.ts:284-380`（BlockAnchorReplacer・閾値 0.65）
    #         ＋ `edit.ts:731-736`（isDisproportionateMatch＝過大置換の歯止め）
    #   ★段③との違い: ③は**挟めれば中身を見ない**ので、行数の蓋だけが安全弁だった。
    #     ここは**中身が 65% 以上似ていること**を条件にするので、
    #     「たまたま同じ行で始まり同じ行で終わる別の場所」を掴みにくい。
    #   ★実測（再生できた失敗 51 件）: ③で救えない分から**さらに 2 件**。
    #     そのどれも一致度 80% 以上で、**危ない一致は 1 件も出なかった**。
    if len(o_lines) >= 3:
        import difflib
        b_lines = body.split("\n")
        ol_all = old.split("\n")
        first, last = o_lines[0].strip(), o_lines[-1].strip()
        best = None
        for i, x in enumerate(b_lines):
            if x.strip() != first:
                continue
            lo = max(1, int(len(ol_all) * 0.75))
            hi = int(len(ol_all) * 1.25) + 1
            for j in range(i + lo - 1, min(len(b_lines), i + hi)):
                if b_lines[j].strip() != last:
                    continue
                win = b_lines[i:j + 1]
                r = difflib.SequenceMatcher(
                    None, [y.strip() for y in ol_all],
                    [y.strip() for y in win]).ratio()
                if r >= 0.65 and (best is None or r > best[0]):
                    best = (r, i, j)
        if best is not None:
            r, i, j = best
            span = j - i + 1
            # ★過大置換の歯止め。一致しても、掴んだ範囲が old より**大きく膨らんで**
            #   いたら**当てない**（無関係なコードを消すより、失敗したほうがよい）。
            if not (span - len(ol_all) >= 3 and span >= 2 * len(ol_all)):
                start = sum(len(x) + 1 for x in b_lines[:i])
                end = start + sum(len(x) + 1 for x in b_lines[i:j + 1]) - 1
                return (start, min(end, len(body)),
                        f"先頭行と末尾行が挟む範囲に中身が {r:.0%} 一致")
    return None


def _nearest_region(body: str, old: str, ctx: int = 3, lang: str = "ja") -> str:
    """`old` に**いちばん近い場所**を、行の範囲つきで返す。近いものが無ければ空文字。

    ★なぜ要るか（2026-09-02・直近 4 走行 run44/46/47/48 の実測）:
      Edit の失敗 122 件のうち 73 件が `old_string not found` で、
      **そのうち 70 件は本当にその文字列がファイルに無い**（照合の緩め方では
      1 件も救えない＝カスケードの段を増やしても意味が無い、と測って分かった）。
      モデルは記憶や想像から old_string を書いている。

      そのとき道具が返していたのは**ファイルの先頭 2,000 字**だった。73 件中 18 件
      （25%）はそこで切れており、**直すべき場所が一度も見えていない**。
      「無い」と言うだけでなく「では**どこが近いか**」を返す。

    ★行番号は付けない。付けると次の呼び出しで old_string に行番号ごと
      貼り付けてくる（実測 1 件）。範囲は見出しの文で言い、本文はそのまま写せる形にする。
    """
    o_lines = [ln for ln in old.split("\n") if ln.strip()]
    b_lines = body.split("\n")
    if not o_lines or not b_lines or len(b_lines) > 20000:
        return ""
    # ★2026-09-22 実測で広げた。**先頭行だけ**を鍵にしていたので、
    #   照合で救えない失敗 44 件のうち**この案内が出たのは 14 件（32%）だけ**だった。
    #   `old` の**どの行でも**近い所を探し、床を 0.6 → 0.45 に下げると **35 件（80%）**。
    #   ★床を下げても危なくない: これは**案内であって自動適用ではない**。
    #     一致度を本文に書くので、外れていればモデルが捨てられる。
    #   ★先頭 8 行までにするのは費用の蓋（長い old で本文を何周もしない）。
    import difflib
    keys = [k for k in (_norm_spaces(x) for x in o_lines[:8]) if k]
    if not keys:
        return ""
    sm = difflib.SequenceMatcher()
    best_i, best_r = -1, 0.0
    for key in keys:
        sm.set_seq2(key)
        for i, ln in enumerate(b_lines):
            n = _norm_spaces(ln)
            if not n:
                continue
            sm.set_seq1(n)
            if sm.real_quick_ratio() < best_r or sm.quick_ratio() < best_r:
                continue
            r = sm.ratio()
            if r > best_r:
                best_i, best_r = i, r
    if best_i < 0 or best_r < 0.45:
        return ""
    span = len(old.split("\n"))
    s = max(0, best_i - ctx)
    e = min(len(b_lines), best_i + span + ctx)
    # ★一致度は「いちばん近い 1 行」ではなく「old 全体と、そこから始まる同じ行数の塊」で出す（2026-10-11 の見回り）。
    #   旧: 1 行が同じなら「100% similar」と出ていた。オーナーの走行（0.1.383）では、中身の違う（記憶で書いた・既に直した）
    #   old_string に「100% similar」と返し、モデルは写し方の問題だと思って同じ物を送り直していた（3 件・同じ走行で 2 回ずつ）。
    #   違う所の最初の 1 行も示す（どこが記憶と違うかが分かる）。
    first = next((j for j, ln in enumerate(old.split("\n")) if ln.strip()), 0)
    key_line = _norm_spaces(o_lines[0]) if o_lines else ""
    anchor = best_i
    for j, ln in enumerate(old.split("\n")):
        if _norm_spaces(ln) and _norm_spaces(ln) == _norm_spaces(b_lines[best_i]):
            anchor = best_i - j
            break
    else:
        anchor = best_i - first if key_line == _norm_spaces(b_lines[best_i]) else best_i
    anchor = max(0, anchor)
    o_all = old.split("\n")
    window = b_lines[anchor:anchor + len(o_all)]
    sm2 = difflib.SequenceMatcher(None, "\n".join(_norm_spaces(x) for x in o_all),
                                  "\n".join(_norm_spaces(x) for x in window))
    block_r = sm2.ratio()
    diff_at = None
    for j, ln in enumerate(o_all):
        fl = window[j] if j < len(window) else None
        if fl is None or _norm_spaces(fl) != _norm_spaces(ln):
            diff_at = (anchor + j + 1, fl, ln)
            break
    if lang == "en":
        # oc 中核の Edit（英語の文面）から使う（2026-10-10: oc の Edit は「無い」としか返していなかった）。
        where = ""
        if diff_at:
            ln_no, fl, ol = diff_at
            where = (f" The first difference is at line {ln_no}: the file has `{(fl or '(end of file)').strip()[:160]}`,"
                     f" your old_string has `{ol.strip()[:160]}`.")
        return (f"\n--- The closest text is at lines {s + 1}-{e} (the block is {block_r:.0%} similar).{where} "
                f"Your old_string was not copied from the current file; copy it from the text below ---\n"
                + "\n".join(b_lines[s:e]))
    where = ""
    if diff_at:
        ln_no, fl, ol = diff_at
        where = (f" 最初に違うのは {ln_no} 行目で、ファイルは「{(fl or '（ファイルの終わり）').strip()[:160]}」、"
                 f"old_string は「{ol.strip()[:160]}」。")
    return (f"\n--- いちばん近いのは {s + 1}〜{e} 行目です（塊の一致度 {block_r:.0%}）。{where}"
            f"この通りに写してください ---\n" + "\n".join(b_lines[s:e]))


def _fit_indent(body: str, start: int, end: int, new: str) -> str | None:
    """`new` を、置き換える範囲 body[start:end] の字下げに合わせ直した版（2026-10-05）。

    ★なぜ要るか（実測・09-01 以降の走行 99 本）: Edit の失敗 135 件のうち **33 件**が
      「この編集は構文を壊します（IndentationError）」。形は 2 つ:
        ① 照合の段②③⑤は**行頭の字下げを無視して**一致させるのに、`new` はそのまま差し込む
        ② 字下げの後ろにある 1 行（old）を複数行の new で置き換えると、2 行目以降が
           行頭（0 桁）から始まる（モデルは new を 0 桁基準で書く）
      どちらも門が拒否して「ファイルは変更していません」で返り、モデルは同じ new を
      送り直していた（同じ走行で 4 回目、の記録がある）。
    ★やること: new の 1 行目の字下げを基準にした**相対**の字下げを保ったまま、
      置き換え先の字下げへ載せ替える。1 行目は、範囲の前にある字下げ（lead）の分を除く。
    ★作れないときは None（＝今まで通りの拒否に任せる）:
      - 範囲の前が字下げ以外（式の途中など）
      - new に 1 行目より浅い行がある（意図が読めない）
    ★呼び手は**門が拒否した後にだけ**使い、結果が構文を通るときだけ採る。
    """
    line_start = body.rfind("\n", 0, start) + 1
    lead = body[line_start:start]
    if lead.strip():
        return None
    base_file = None
    for ln in body[line_start:max(end, start)].split("\n"):
        if ln.strip():
            base_file = ln[:len(ln) - len(ln.lstrip(" \t"))]
            break
    if base_file is None or not base_file.startswith(lead):
        return None
    lines = new.split("\n")
    first = next((ln for ln in lines if ln.strip()), None)
    if first is None:
        return None
    bn = len(first) - len(first.lstrip(" \t"))
    out = []
    for k, ln in enumerate(lines):
        if not ln.strip():
            out.append("")
            continue
        if len(ln) - len(ln.lstrip(" \t")) < bn:
            return None
        prefix = base_file[len(lead):] if k == 0 else base_file
        out.append(prefix + ln[bn:])
    return "\n".join(out)


def _apply_one(body: str, old: str, new: str, replace_all: bool,
               occurrence: int | None = None, *, reindent: bool = False) -> tuple[str, int, str]:
    """戻り: (新しい本文, 置換件数, 緩い照合の説明)。説明は完全一致なら空文字。

    reindent=True のときは、1 箇所の置換で new を置き換え先の字下げに合わせる
    （`_fit_indent`）。replace_all は合わせない（箇所ごとに字下げが違いうる）。
    """
    if not old:
        if new:
            # ★実測（2026-09-02・直近 4 走行）: **8 件**がこの形だった。
            #   file_path と new_string（＝ファイルの全文）だけを渡してくる＝
            #   「丸ごと入れ替えたい」という意図がはっきりしている。
            #   旧実装の答えは `missing 'old_string'` の 1 行だけで、次の一手が無い。
            #   走行の記録では同じ誤りを 3 回以上繰り返していた。
            #   ★診断だけでは足りない——**次の一手を名指しする**（この現場の作法）。
            raise ValueError(
                "old_string がありません（new_string だけを受け取りました）。\n"
                "Edit は「いま入っている文字列」を置き換える道具です。\n"
                "  ・一部を直す → old_string に**実際に入っている文字列**をそのまま入れる\n"
                "  ・**丸ごと入れ替える** → Edit ではなく Write を使う:\n"
                '      {"file_path": "<いまと同じパス>", "content": "<新しい全文>"}\n'
                "    ※Write は既存の内容を全部捨てます。全文を持っているときだけ。"
            )
        raise ValueError("missing 'old_string'")
    if old not in body:
        # ★照合カスケード（aider 由来・2026-08-27 に run29 の実測から追加）:
        #   run29 の Edit は 134 回中 69 回失敗（51.5%・最長 11 連続）。ほぼ全部
        #   `old_string not found` で、原因の大半は「直前に読んだ内容の書き写しの
        #   微差」——救えた例は**空行の行末に空白 2 文字が足されただけ**だった。
        #   弱いモデルに一字一句の書き写しを求める設計そのものが誤り。
        #   段を上げながら緩く照合し、**当たったら黙って直さず必ず報告する**。
        #   ★曖昧なとき（候補が2つ以上）は発動しない。意図と違う箇所を直すのが最悪。
        loose = _loose_match(body, old)
        if loose is not None:
            start, end, how = loose
            if reindent:
                new = _fit_indent(body, start, end, new) or new
            return body[:start] + new + body[end:], 1, how
    if old not in body:
        # Hand back what the file actually contains now. Measured cause of
        # repeated S1 failures: after its own first edit the model kept
        # matching against the original text it remembered, and got a bare
        # "not found" that carried no way to recover. Published results put
        # error-feedback retries at ~+22pt for one corrective step.
        near = _nearest_region(body, old)
        if near:
            # ★近い場所が分かったなら、**そこだけ**を返す。頭から 2,000 字を
            #   貼っても、直すべき場所がその中にあるとは限らない（実測 25% は外）。
            raise ValueError(
                f"old_string not found: {old[:60]!r}\n"
                f"ファイルにこの文字列はありません（記憶ではなく実物に合わせてください）。"
                + near
            )
        snippet = body if len(body) <= 2000 else body[:2000] + "\n…(切り詰め)"
        raise ValueError(
            f"old_string not found: {old[:60]!r}\n"
            f"--- ファイルの現在の内容（これに対して old_string を合わせること）---\n"
            f"{snippet}"
        )
    sites = _match_sites(body, old)
    cnt = len(sites)

    # occurrence: pick one site deterministically. This is the safe answer to an
    # ambiguous match — one edit, chosen by the harness, no sweep. It is mutually
    # exclusive with replace_all so the model cannot ask for "one" and "all".
    if occurrence is not None:
        try:
            occurrence = int(occurrence)
        except (TypeError, ValueError):
            raise ValueError(f"occurrence は整数で指定してください（受け取った値: {occurrence!r}）")
        if replace_all:
            raise ValueError(
                "occurrence と replace_all は同時に指定できません"
                "（1箇所だけなら occurrence、全部変えるなら replace_all）"
            )
        if not (1 <= occurrence <= cnt):
            raise ValueError(
                f"occurrence={occurrence} は範囲外です"
                f"（一致は {cnt} 箇所、1〜{cnt} で指定してください）"
            )
        pos = sites[occurrence - 1]
        if reindent:
            new = _fit_indent(body, pos, pos + len(old), new) or new
        return body[:pos] + new + body[pos + len(old):], 1, ""

    if replace_all:
        if cnt > 1 and _heterogeneous_full_line(body, old, sites):
            where = []
            for i, pos in enumerate(sites[:6], 1):
                where.append(
                    f"  occurrence={i}: 行 {body.count(chr(10), 0, pos) + 1}"
                    f"（直前: {_prev_nonempty(body, _line_bounds(body, pos)[0])!r}）"
                )
            raise ValueError(
                f"replace_all は拒否しました: {old[:50]!r} は {cnt} 箇所すべてで"
                f"“行まるごと”一致し、しかも前後が異なります"
                f"（別々の場所が同じ行をたまたま共有しているだけ）。\n"
                f"全部を同じに変えると、直すつもりのない箇所まで壊れます"
                f"（実測: add を壊して mul を直した）。\n"
                f"1箇所だけ直すには occurrence=N を指定してください:\n"
                + "\n".join(where) + "\n"
                f"本当に全箇所を同一に変えるなら、前後の行を old_string に足して"
                f"一意にしたうえで1件ずつ行ってください。"
            )
        return body.replace(old, new), cnt, ""

    if cnt > 1:
        # Ambiguous single edit. Lead with occurrence (the harness picks the
        # site, no sweep), then the manual disambiguation; keep 前後の行 before
        # replace_all so the dangerous all-occurrences option stays last.
        # ★行番号だけでなく**直前の行**も見せる（2026-09-21）。
        #   出典: goose `crates/goose/.../developer/edit.rs:169-187` は一致 2 件まで
        #   行番号＋前後の実テキストを添える。
        #   ★同じファイルの `_heterogeneous_full_line` 分岐（上）は**既に直前の行を
        #   見せている**のに、こちらは番号だけだった＝**自己不整合**。
        #   番号だけだと、モデルは正しい occurrence を選ぶのにもう一度 Read する。
        where = []
        for i, pos in enumerate(sites[:3], 1):
            _ln = body.count("\n", 0, pos) + 1
            _prev = _prev_nonempty(body, _line_bounds(body, pos)[0])
            where.append(f"{_ln}" + (f"（直前: {_prev[:40]!r}）" if _prev else ""))
        raise ValueError(
            f"old_string が {cnt} 箇所に一致します（occurrence=1..{cnt} / 行 "
            f"{', '.join(where)}"
            f"{' ほか' if cnt > 3 else ''}）: {old[:50]!r}\n"
            f"1箇所だけ直すには occurrence=N（N は上の一致順・1〜{cnt}）を指定するか、"
            f"直したい箇所の**前後の行を old_string に足して**一意にしてください。\n"
            f"全部を同じに変えるのが本当に意図なら replace_all=true を指定してください"
            f"（その場合、上の全箇所が変わります）。"
        )
    if reindent:
        pos = body.find(old)
        fit = _fit_indent(body, pos, pos + len(old), new)
        if fit is not None:
            return body[:pos] + fit + body[pos + len(old):], 1, ""
    return body.replace(old, new, 1), 1, ""


def _run(inp: dict) -> str:
    path = inp.get("file_path") or inp.get("path")
    edits = inp.get("edits")
    if not path:
        return "Error: missing 'file_path'"
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    try:
        # ★2026-09-08 監査（tools high）: Read は authorize_read で資格情報のパスを拒むのに、
        #   Edit/ApplyDiff は素で読み、不一致時に中身を返していた＝Read の関門の迂回路。同じ関門を通す。
        try:
            from ..policy import authorize_read as _authorize_read, PolicyDenied as _PolicyDenied
            _authorize_read(str(p), kind="edit")
        except _PolicyDenied as _e:
            return ToolResult(f"Error: {_e}", ok=False, kind="error")
        raw = p.read_bytes()
    except Exception as e:
        return f"Error: {e}"
    # Preserve the file's newline style. read_text/write_text normalise CRLF→LF,
    # which turns a one-line edit of a Windows-origin file (.ps1/.bat/some YAML)
    # into a whole-file rewrite — the 'whole-file rewrite corrupts' failure via
    # the stdlib default. Edit in LF space so the model's LF old_string matches,
    # then restore the original ending on write.
    crlf = raw.count(b"\r\n") > 0 and raw.count(b"\r\n") * 2 >= raw.count(b"\n")
    # ★**損なわずに読めたものだけを編集する**（2026-09-20・敵対レビュー）。
    #   旧: `raw.decode("utf-8", "replace")  # best-effort: keep the file editable`。
    #   cp932 の .bat を ASCII の old_string で直すと「OK: 1 replacement」と返しながら、
    #   **日本語を全部 U+FFFD に潰して書き戻していた**（92→147 バイト・実測）。バイナリも同じ形で壊れた。
    #   Read は cp932 を正しく見せるので、モデルにも利用者にも気づく材料が無かった。
    from .textcodec import NotEditable as _NotEditable, decode_for_edit as _decode_for_edit, encode_back as _encode_back
    try:
        body, _enc, _bom = _decode_for_edit(raw)
    except _NotEditable as _ne:
        return ToolResult(f"Error: {_ne}", ok=False, kind="error")
    # ★改行の流儀は**復号した後の文字**で数える。生バイトの `b"\r\n"` は UTF-16 では一度も現れない
    #   （`\r\x00\n\x00`）ので、UTF-16 の PowerShell を直すと CRLF が LF に変わっていた（試験で判明）。
    crlf = body.count("\r\n") > 0 and body.count("\r\n") * 2 >= body.count("\n")
    body = body.replace("\r\n", "\n")

    # Normalize to list of edits
    if edits is None:
        edits = [{
            "old_string": inp.get("old_string"),
            "new_string": inp.get("new_string", ""),
            "replace_all": bool(inp.get("replace_all", False)),
            "occurrence": inp.get("occurrence"),
        }]
    if not isinstance(edits, list) or not edits:
        return "Error: 'edits' must be a non-empty list"

    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            # ★dict 以外が混ざると `e.get` が AttributeError になり、
            #   下の `except ValueError` を素通りして**素の例外**になっていた
            #   （道具の失敗が `Error in Edit: ...` の生文字列で返る族）。
            return (f"Error: edits[{i}] は object ではありません（{type(e).__name__}）。"
                    '各要素は {"old_string": "...", "new_string": "..."} の形にしてください。')

    new_body = body
    loose_notes: list[str] = []
    total = 0
    try:
        for i, e in enumerate(edits):
            new_body, n, _how = _apply_one(
                new_body,
                e.get("old_string", ""),
                e.get("new_string", ""),
                bool(e.get("replace_all", False)),
                e.get("occurrence"),
            )
            if _how:
                loose_notes.append(f"  ★{_how}（{e.get('old_string','')[:40]!r}）")
            total += n
    except ValueError as err:
        return f"Error: edit[{i}]: {err}"

    # Syntax gate (SWE-agent pattern): if this edit turns a syntactically valid
    # file invalid, refuse it and leave the file unchanged, so the model fixes
    # the edit rather than editing broken code again. Only fires when the edit
    # INTRODUCED the error (old valid → new invalid), so an incremental fix of an
    # already-broken file is never blocked. Disable with LAPS_EDIT_SYNTAX_GATE=0.
    indent_fitted = False
    if os.environ.get("LAPS_EDIT_SYNTAX_GATE", "1") != "0":
        new_err = _syntax_error(str(p), new_body)
        if new_err and _syntax_error(str(p), body) is None:
            # ★拒否する前に**一度だけ**、new を周りの字下げに合わせた版を試す（2026-10-05）。
            #   字下げの食い違いは Edit の失敗の約 1/4（`_fit_indent` の注記）。
            #   ★採るのは「合わせた版が構文を通るとき」だけ。通らなければ今まで通り拒否する
            #     ＝門を緩めていない（壊れた物は書かない、は変わらない）。
            #   ★合わせたことは必ず結果に書く（黙って直さない＝緩い照合と同じ作法）。
            if os.environ.get("LAPS_EDIT_FIT_INDENT", "1") != "0":
                try:
                    _fb = body
                    for e in edits:
                        _fb, _, _ = _apply_one(
                            _fb, e.get("old_string", ""), e.get("new_string", ""),
                            bool(e.get("replace_all", False)), e.get("occurrence"),
                            reindent=True)
                except ValueError:
                    _fb = None
                if _fb and _fb != new_body and _syntax_error(str(p), _fb) is None:
                    new_body, new_err, indent_fitted = _fb, None, True
        if new_err and _syntax_error(str(p), body) is None:
            return (
                f"Error: この編集は構文を壊します（{new_err}）。ファイルは変更していません。\n"
                f"編集後の内容が壊れないよう old_string/new_string を見直してください。"
            )

    try:
        authorize_path(str(p), write=True, kind="edit")
    except PolicyDenied as e:
        return f"Error: policy denied: {e}"
    # Atomic write via temp-in-same-dir + os.replace: a crash / disk-full / SIGKILL
    # mid-write leaves the original intact, not a truncated config or .py. Restore
    # CRLF if that was the file's style, and keep the original file mode.
    # ★new_string に CRLF が混ざっていても二重化しない（2026-09-02）。
    #   本文は LF 空間で扱っているのに、モデルの new_string はそのまま入るので、
    #   CRLF のファイルでは `\r\n` → `\r\r\n` になっていた（1 行直しただけで
    #   その行が壊れる）。書き出す前に一度 LF へ揃える。
    text = new_body.replace("\r\n", "\n")
    try:
        data = _encode_back(text.replace("\n", "\r\n") if crlf else text, _enc, _bom)
    except _NotEditable as _ne:
        return ToolResult(f"Error: {_ne}", ok=False, kind="error")
    try:
        # ★書き込みは**共有の 1 本**（atomic.write_bytes_atomic）に寄せる（2026-09-02）。
        #   ここにあった自前の mkstemp+os.replace は**記号リンクを辿らない**ので、
        #   `sites-enabled/default` のようなリンクを Edit すると**リンクを実ファイルで
        #   置き換えて**しまい、リンク先は古いまま残る（インフラ運用では日常の形）。
        #   Write と ApplyDiff は既に共有の 1 本を使っており、Edit だけが自前だった
        #   ＝この現場が何度も踏んだ「片方で直して片方に残した」。
        #   共有版は権限の引き継ぎと、置き場に一時ファイルを作れないときの退避も持つ。
        write_bytes_atomic(p, data)
    except Exception as e:
        return f"Error: write failed: {e}（このファイルは書き換えていません）"
    patch = make_patch(body, new_body, str(path))
    # ★差分を**結果の本文**にも載せる（2026-08-27・run29 の実測）:
    #   旧実装は patch を data に入れて画面にだけ渡し、モデルへの本文は
    #   「OK: wrote N bytes」だけだった。だからモデルは編集の直後に
    #   Read 48 回・Bash 45 回で手で確かめ直し、api.php を 161 回読んでいた。
    #   ＝計算済みの観測を渡していない「書いたが繋いでいない」の再発。
    head = f"OK: {total} replacement(s) across {len(edits)} edit(s) in {path}"
    lines = [head]
    if loose_notes:
        lines.append("★old_string は完全一致しませんでした。次の緩い照合で当てています"
                     "（意図と違えば元に戻してから指定し直してください）:")
        lines += loose_notes
    if indent_fitted:
        lines.append("★new_string の字下げが周りと合わず、そのままでは構文が壊れるため、"
                     "**周りの字下げに合わせて**入れました（構文が通ることを確かめ済み）。"
                     "下の差分で意図どおりか確かめてください。")
    body_diff = _render_patch(patch)
    if body_diff:
        lines.append("--- 実際に入った差分 ---")
        lines.append(body_diff)
    return ToolResult("\n".join(lines), ok=True, kind="ok", data={"patch": patch, "indent_fitted": indent_fitted})


#: 結果本文に載せる差分の上限（文脈を食いすぎない。run29 の編集は 260 バイト級が中心）。
DIFF_BODY_MAX_LINES = 60


def _render_patch(patch: dict) -> str:
    """make_patch の hunk を、行番号つきの読める差分にする（本文用・上限つき）。"""
    try:
        hunks = patch.get("hunks") or []
    except AttributeError:
        return ""
    out: list[str] = []
    for h in hunks:
        start = h.get("oldStart") or h.get("old_start") or 1
        out.append(f"@@ 行 {start} @@")
        for ln in (h.get("lines") or []):
            out.append(str(ln))
            if len(out) >= DIFF_BODY_MAX_LINES:
                out.append(f"…（差分が長いので {DIFF_BODY_MAX_LINES} 行で切りました）")
                return "\n".join(out)
    return "\n".join(out)


# ★差分は**適用時に旧内容と新内容から作り**、結果に載せる（問題9・2026-08-23）。
#   Claude Code は structuredPatch（文脈3行）を tool_result の構造化データに保存し、
#   画面はそれを読むだけ（再描画でもファイルを読み直さない）。引数の
#   old_string/new_string から画面で組むと、replace_all や複数編集で実際の変化と食い違う。
PATCH_CONTEXT = 3
PATCH_MAX_LINES = 400      # 画面に送る上限。超えたら切って truncated を立てる
PATCH_MAX_LINE_CHARS = 2000  # 1 行の上限（超えた分は切る）


def make_patch(old: str, new: str, path: str) -> dict:
    """unified diff を hunk の列にする。行は '+'/'-'/' ' 接頭で、旧ファイル基準の開始行つき。"""
    import difflib
    # 末尾の改行は「行」ではない（split すると空の最終行が生まれ、差分の末尾に
    # 意味の無い " " が付く）。
    a = old.split("\n"); b = new.split("\n")
    if a and a[-1] == "": a.pop()
    if b and b[-1] == "": b.pop()
    hunks: list[dict] = []
    added = removed = 0
    cur: dict | None = None
    total = 0
    truncated = False
    for ln in difflib.unified_diff(a, b, n=PATCH_CONTEXT, lineterm=""):
        # ★ヘッダ（---/+++）は最初の @@ の**前**にしか来ない。中身が `---`（YAML）や `++i` で始まる
        #   行まで捨てていた（2026-08-23 レビュー）。
        if cur is None and ln.startswith(("---", "+++")):
            continue
        if ln.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
            cur = {"old_start": int(m.group(1)) if m else 0,
                   "new_start": int(m.group(3)) if m else 0, "lines": []}
            hunks.append(cur)
            continue
        if cur is None:
            continue
        if ln.startswith("+"): added += 1
        elif ln.startswith("-"): removed += 1
        if total < PATCH_MAX_LINES:
            # 1 行の長さも切る（minified の 1 行 600KB で tool_result が 1.2MB になった）
            cur["lines"].append(ln if len(ln) <= PATCH_MAX_LINE_CHARS
                                else ln[:PATCH_MAX_LINE_CHARS] + " …(切り詰め)")
        else:
            truncated = True
        total += 1
    return {"path": path, "added": added, "removed": removed,
            "hunks": [h for h in hunks if h["lines"]], "truncated": truncated}


SPEC = ToolSpec(
    name="Edit",
    description=(
        "Replace exact string(s) in a file. Single-edit: pass old_string/new_string. "
        "When old_string is not unique, pass occurrence=N (1-based) to edit one site, "
        "not replace_all. Multi-edit: pass edits=[{old_string,new_string,replace_all?,occurrence?}, ...] "
        "— applied sequentially, atomic (file unchanged if any step fails)."
    ),
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
            "occurrence": {"type": "integer", "description": "1-based index of which match to edit when old_string is not unique; use instead of replace_all"},
            "edits": {
                "type": "array",
                "description": "Optional list of {old_string,new_string,replace_all?,occurrence?} for multi-edit",
                "items": {"type": "object"},
            },
        },
        "required": ["file_path"],
    },
    mutates=True,
    # ★見た目が同じで文字だけ違うパスを入る前に直す（2026-10-05・tools/pathfix.py）
    handler=_pathfix.wrap(_run, "file_path", "path", mode="existing"),
)
