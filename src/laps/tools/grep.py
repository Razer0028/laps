"""Grep tool: search for pattern via ripgrep (rg) or fallback grep -r."""
from __future__ import annotations
import shutil
import subprocess
from .. import procrun  # 固まらない subprocess（2026-09-06）

from collections import OrderedDict

import os
import re
import time

from . import limits
from . import fold
from . import pathfix as _pathfix
from . import ToolSpec, ToolResult
from ..policy import authorize_walk, PolicyDenied


# ── 検索エンジンの食い違いを埋める（2026-08-31・レビュー E1・再現済み）────────
#
# 同じ問いが、**その機械に ripgrep が入っているかどうかで別の答えになる**。
# 2026-08-31 に同じ木で測った（4 パターン）:
#
#   pattern        rg の結果              grep -e（旧実装）の結果
#   ------------   --------------------   ------------------------
#   cat|dog        2 件                   **0 件**   ← 選択が BRE では効かない
#   foo\d+         1 件                   **0 件**   ← \d が BRE に無い
#   foo[0-9]\+     **0 件**               1 件      ← \+ は BRE だけの綴り
#   alpha          4 件（隠しは飛ばす）    5 件（`.hidden/` も読む）
#
# ＝ rg の無い機械では、**普通の正規表現が「該当なし」になる**。しかも道具は
# 「(no matches)」としか言わないので、モデルは「その文字列は無い」と読んで先へ進む。
# これは終了コードを見ていなかった頃の不具合（下の returncode 判定）と同じ形で、
# 直したはずの穴が**エンジンの違いという別の入口から**開いていた。
#
# 方針: **rg の綴りを正とし、代替エンジンをそちらへ寄せる**（rg がこの現場の既定で、
# 説明文も「OS を問わず同じように動く」と約束しているため）。
#   1. grep -P があれば使う（PCRE＝rg の綴りにいちばん近い）
#   2. 無ければ grep -E（選択・`+`・`?` は一致する）。ただし -E で表せない綴り
#      （`\d` 等）が入っていたら**黙って違う答えを返さず、断る**。
#   3. grep も無い機械（Windows など）は Python の re で歩く。旧実装はここで
#      `Error: [Errno 2] ... 'grep'` になり、道具が丸ごと使えなかった。

#: -E では表せず、そのまま渡すと**黙って別のものに一致する**綴り。
#: 例: `foo\d+` は GNU grep -E では `food+`（d の繰り返し）として一致してしまう。
_PCRE_ONLY = re.compile(r"\\[dDhHRvVpP]|\(\?|[*+}]\?")

_ERE_HINT = {
    r"\d": "[0-9]", r"\D": "[^0-9]", r"\h": "[ \t]",
}


def _grep_regex_flag(grep_bin: str) -> str:
    """代替 grep で使う正規表現の方言。-P が使えるなら -P、駄目なら -E。"""
    global _GREP_FLAG
    if _GREP_FLAG is None:
        try:
            r = procrun.run([grep_bin, "-P", "-q", "-e", "x", os.devnull],
                               capture_output=True, timeout=10)
            _GREP_FLAG = "-P" if r.returncode in (0, 1) else "-E"
        except Exception:
            _GREP_FLAG = "-E"
    return _GREP_FLAG


_GREP_FLAG: str | None = None


def _hidden_excludes(path: str) -> list[str]:
    """rg に合わせて隠しファイル/ディレクトリを飛ばす指定。

    ★ただし**利用者が隠しディレクトリを名指ししたときは飛ばさない**
      （rg は `rg pat .hidden` を検索する。ここで一律に除外すると、
      食い違いを別方向に作り直すだけになる — 実測して気づいた）。
    """
    parts = [q for q in re.split(r"[\\/]+", path or ".") if q not in ("", ".", "..")]
    if any(q.startswith(".") for q in parts):
        return []
    # ★綴りは `.?*`（点のあとに最低1文字）。`.*` にすると**検索の起点 `.` 自身が
    #   除外され、どんな語でも 0 件になる**（2026-08-31 実測）。
    #   ⚠ このとき私は自分の shell の `grep` を使って「大丈夫」と確認していたが、
    #     その `grep` は別実装への差し替えで、本物の /usr/bin/grep と挙動が違った。
    #     道具が使うのは `shutil.which("grep")`＝本物のほう。**器を疑うこと。**
    return ["--exclude-dir=.?*", "--exclude=.?*"]


def _git_ignored(root: str) -> frozenset[str]:
    """rg が既定で飛ばす場所を、**git 自身に聞いて**絶対パスで返す。

    ★なぜ要るか（2026-08-31 実測・E1 の続き）。E1 は綴りの方言だけを揃えたが、
      **どこを探すか**が揃っていなかった。同じ問いを同じ木（/home/user/project）で:

        engine          一致      表示できた行   何を余計に読んだか
        ------------    ------    ------------   ---------------------------
        rg              167 件    31 行          —
        grep -P         201 件    19 行          app/pyruntime・target/…（13 本）
        Python の re    197 件    11 行          同上

      余計な 13 本は **`agent.py` `serve.py` の複製**（配布用の写しとビルド出力）で、
      git が無視している場所にある。害は 2 つあって、どちらも実測できる:
        ① 表示の枠は**ファイルごとに公平**に配るので、複製が増えるほど
           本物の一致が押し出される（31 → 19 → 11 行）。
        ② モデルは出てきた道を読み書きする。**ビルド出力の写しを直す**のは
           この現場が何度も踏んだ形（直したのに何も変わらない）。

    ★規則を自分で書かない。「.venv や node_modules を外す」式の一覧は、
      その名前を**正当に使っている木**で嘘（0 件）を作る。rg が見ているのと
      同じ土台（git の無視規則）だけを使えば、**rg が見せるものを隠すことはない**。

    ★根そのものが無視対象のときは、呼ぶ側が丸ごと外す（`_root_is_ignored`）。
      rg は名指しされた無視対象は探すため（実測: `rg needle build` は `build/` が
      .gitignore にあっても一致を返す）。`_hidden_excludes` が隠しディレクトリに
      対してしているのと同じ約束。
    """
    if not os.path.isdir(root) or not shutil.which("git"):
        return frozenset()
    try:
        r = procrun.run(
            ["git", "-C", root, "ls-files", "--others", "--ignored",
             "--exclude-standard", "--directory"],
            capture_output=True, timeout=20)
        if r.returncode != 0:
            return frozenset()          # リポジトリ外・git の不調 → 何も外さない
        out = r.stdout.decode("utf-8", "replace")
    except Exception:
        return frozenset()              # ★迷ったら「外さない」側へ倒す（隠すより多く見せる）
    base = os.path.abspath(root)
    got = set()
    for ln in out.splitlines():
        ln = ln.strip().rstrip("/")
        if ln:
            got.add(os.path.abspath(os.path.join(base, ln)))
    return frozenset(got)


def _root_is_ignored(root: str) -> bool:
    """検索の起点そのものが git の無視対象か。

    ★そうなら**一切の除外をやめる**。rg は名指しされた無視対象を探すので
      （実測）、ここで外すと「`build/` の中を見たい」という指定に 0 件を返す
      ——**この道具でいちばんやってはいけない嘘**になる。
    """
    if not shutil.which("git"):
        return False
    ap = os.path.abspath(root or ".")
    if not os.path.exists(ap):
        return False
    # ★2026-09-01（反証で再現）: ここは `os.path.isdir` で門前払いしていたので、
    #   **1 本のファイルを名指ししたとき**に働かなかった。実測（rg の無い機械）:
    #     repo/.gitignore = "work/" ／ cwd = repo/work ／ Grep(path="f.txt")
    #       → rg は 8 件返す（名指しされた無視対象は探す）
    #       → こちらは **(no matches)**（後段の `_ignored_for_files` が落とす）
    #   ＝「その語は無い」という、この道具でいちばん高くつく嘘。
    if os.path.isdir(ap):
        d, target = ap, "."
    else:
        d, target = (os.path.dirname(ap) or "."), os.path.basename(ap)
    try:
        # 0=無視されている / 1=されていない / 128=リポジトリ外。
        r = procrun.run(["git", "-C", d, "check-ignore", "-q", "--", target],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


def _repo_root_of(fp: str) -> str | None:
    """`fp` を含むリポジトリの根（`.git` を持つ最も近い上位）。無ければ None。

    ★subprocess を使わない。一致したファイルの数だけ git を起こすと、
      速さのために足した仕組みが**それ自体で遅くなる**。
    """
    d = os.path.dirname(os.path.abspath(fp))
    prev = None
    while d and d != prev:
        if os.path.exists(os.path.join(d, ".git")):   # worktree では .git はファイル
            return d
        prev, d = d, os.path.dirname(d)
    return None


def _ignored_for_files(paths, memo: dict) -> frozenset[str]:
    """一致したファイルたちが属するリポジトリの無視一覧をまとめて返す。

    ★根が git リポジトリでない木でも効かせるため（2026-08-31 実測）。
      `/home/user/project` 自体はリポジトリではないが、その下に 5 つのリポジトリがあり
      （src・app・scripts・ops・scratch/v2check）、**rg はそれぞれの規則を降りながら適用する**。
      根に 1 回聞くだけでは、この形の木でまったく効かなかった（201 件のまま）。
    """
    got: set[str] = set()
    for p in paths:
        r = _repo_root_of(p)
        if not r:
            continue
        if r not in memo:
            memo[r] = _git_ignored(r)
        got |= memo[r]
    return frozenset(got)


def _is_ignored(fp: str, ignored: frozenset[str]) -> bool:
    """その道が、無視対象そのものか、その下にあるか。"""
    if not ignored:
        return False
    p = os.path.abspath(fp)
    if p in ignored:
        return True
    return any(p.startswith(i + os.sep) for i in ignored)


def _walk_timeout() -> float:
    """Python で歩くときの制限時間（秒）。rg / grep の `timeout=30` に合わせる。"""
    try:
        return max(0.0, float(os.environ.get("LAPS_GREP_TIMEOUT", "30")))
    except ValueError:
        return 30.0


def _py_walk(pattern: str, path: str, glob: str | None,
             ignored: frozenset[str] = frozenset(),
             prune: bool = True) -> tuple[list[str], str | None, bool]:
    """rg も grep も無い機械のための検索（Python の re で歩く）。

    戻り値 (行, エラー, 打ち切ったか)。**打ち切ったときは正直に言う**（0 件と偽らない）。

    ★制限時間を持たせた（2026-08-31 実測）。rg と grep には `timeout=30` があるのに、
      **この経路にだけ上限が無かった**。同じ木・同じ問いで rg 0.06 秒 / grep 7.5 秒に対し
      Python は 11.1 秒で、しかも木が大きくなるほど止まらない。
      ここは Windows の出荷物が必ず通る経路（rg も grep も無い）＝**黙って固まる**。
    """
    import fnmatch
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return [], f"正規表現が読めません: {e}", False
    out: list[str] = []
    root = path or "."
    if not os.path.exists(root):
        # ★「無い場所を探して 0 件」は 0 件ではない（rg/grep は終了コード 2 で言う）。
        return [], f"path がありません: {root}", False
    limit = _walk_timeout()
    deadline = time.monotonic() + limit if limit > 0 else None

    def _expired() -> bool:
        return deadline is not None and time.monotonic() > deadline

    files: list[str] = []
    if os.path.isfile(root):
        files = [root]
    else:
        _memo: dict = {}
        for dirpath, dirnames, filenames in os.walk(root):
            # ★降りながらリポジトリを見つけたら、その規則も足す。rg はそうしている
            #   （実測: /home/user/project 自体はリポジトリでないが、下に 5 つある）。
            if prune and os.path.exists(os.path.join(dirpath, ".git")):
                key = os.path.abspath(dirpath)
                if key not in _memo:
                    _memo[key] = _git_ignored(dirpath)
                ignored = ignored | _memo[key]
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".")
                           and not _is_ignored(os.path.join(dirpath, d), ignored)]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                fp = os.path.join(dirpath, fn)
                if _is_ignored(fp, ignored):
                    continue
                files.append(fp)
                if len(files) > 200_000:
                    return [], "対象が多すぎます（20 万ファイル超）。path で範囲を絞ってください。", False
            if _expired():
                # 集めている途中で尽きた。集まったぶんだけ読んで、打ち切ったと言う。
                break
    for fp in files:
        if _expired():
            return out, None, True
        if glob and not fnmatch.fnmatch(os.path.basename(fp), glob):
            continue
        try:
            with open(fp, "rb") as f:
                # ★頭だけ先に読む。バイナリと分かったファイルを丸ごと読むのは、
                #   この経路（上限つき）では**残り時間をそのまま捨てる**ことになる。
                if b"\0" in f.read(8192):   # rg / grep -I と同じくバイナリは飛ばす
                    continue
            # ★流し読みにする（2026-08-31 実測）。旧実装は `f.read(8*1024*1024)` で
            #   **8MB より後ろを黙って捨てていた**: 47MB の実ファイル
            #   （scratch/claude-app/strings.txt・NUL 無しの普通のテキスト）で、
            #   8.45MB の位置にある `tool_failed` 4 件が rg には見え、こちらには
            #   見えなかった。道具は「(no matches)」としか言わないので、
            #   **「その語は無い」という嘘**になる。この道具でいちばん高くつく形。
            # ★行の切り方も grep に合わせる。`str.splitlines()` は \r・\f・  でも
            #   切るので、そういう字を含むファイルでは**行番号が grep とずれる**。
            with open(fp, "r", encoding="utf-8", errors="replace", newline="\n") as tf:
                for i, line in enumerate(tf, 1):
                    if rx.search(line):
                        out.append(f"{fp}:{i}:{line.rstrip(chr(10)).rstrip(chr(13))}")
                    if (i & 0x3FFF) == 0 and _expired():
                        # 1 本のファイルが長すぎて時間が尽きた場合も、黙って止めない。
                        return out, None, True
        except OSError:
            continue
    return out, None, _expired()


def _split_file(ln: str) -> str:
    """`file:line:text` の**ファイル名の部分**を返す。

    ★Windows のパスは `C:\\src\\a.py:12:text` の形で、素朴に先頭の `:` で
      切ると **"C"** になる（2026-09-02）。すると
      ・束ね方が全部 1 つの束になり、ファイルごとに公平に配る仕組みが死ぬ
      ・「一致したファイル: N 本」が**常に 1 本**になる
      ・git の無視規則を当てる相手が "C" になり、当たらなくなる
      この道具は説明文で「OS を問わず同じように動く」と約束しているのに、
      その約束が**この 1 行で崩れていた**。ドライブ文字の `:` は区切りではない。
    """
    i = 2 if (len(ln) > 2 and ln[1] == ":" and ln[0].isalpha()
              and ln[2] in "\\/") else 0
    j = ln.find(":", i)
    return ln[:j] if j > 0 else "(不明)"


def _run(inp: dict) -> str:
    pattern = inp.get("pattern")
    if not pattern:
        return "Error: missing 'pattern'"
    path = inp.get("path", ".")
    try:
        authorize_walk(path)
    except PolicyDenied as e:
        return f"Error: policy denied: {e}"
    glob = inp.get("glob")
    # ★大文字小文字を無視するか（2026-09-21）。文字列の "true" でも受ける——
    #   モデルは真偽値を文字列で渡すことがあり、そこで黙って false になると
    #   「指定したのに効かない」になる（今日 8 道具で直したのと同じ型）。
    _ci = inp.get("case_insensitive", False)
    ci = _ci if isinstance(_ci, bool) else str(_ci).strip().lower() in ("1", "true", "yes")
    from .read import BadArg, int_arg
    try:
        max_count = int_arg(inp, "max_results", 200)
    except BadArg as e:
        return f"Error: {e}"
    rg = shutil.which("rg")
    engine = "rg"
    # ★rg のときは git に一度も聞かない（rg 自身が同じ規則を持っている）。
    #   ここで初期化しておくのは、下の条件式が短絡に頼らないため
    #   （この木は「片方の枝でだけ定義される変数」で何度か転んでいる）。
    _memo: dict = {}
    _prune = False
    ignored: frozenset[str] = frozenset()
    if rg:
        # ★`--with-filename` を必ず付ける（2026-09-01・反証で再現）。
        #   rg も grep も、**引数が 1 本のファイルのときはファイル名を出さない**。
        #   すると出力は `12:…` になり、下の `_render` が `ln.split(":",1)[0]` で
        #   束を作るので、**行番号がファイル名の位置に入る**。実測（修正前）:
        #     Grep(pattern="def ", path="laps/tools/fold.py")
        #       → 「一致したファイル: 6 本。」（1 本です）
        #   さらに代替エンジンの経路では、その "12" が `_repo_root_of` に渡って
        #   **cwd 基準**で解決されるため、cwd が git の無視対象の中にあると
        #   一致が全部落ちて「(no matches)」になる＝**その語は無いという嘘**。
        #   実記録での出現率: 単一ファイル指定の Grep は 28/237（12%）。
        cmd = [rg, "--line-number", "--with-filename", "--no-heading",
               *(["-i"] if ci else []),
               "--color", "never", "-e", pattern, path]
        if glob:
            cmd[-1:-1] = ["--glob", glob]
    else:
        grep_bin = shutil.which("grep")
        # ★rg 以外のエンジンは、rg が既定で飛ばす場所（git の無視規則）を自分では見ない。
        #   聞いてから渡す（_git_ignored の説明にある実測を参照）。
        _prune = not _root_is_ignored(path)
        ignored = _git_ignored(path) if _prune else frozenset()
        if not grep_bin:
            # rg も grep も無い（Windows 等）。旧実装はここで例外になり、
            # **この機械では Grep 道具が丸ごと使えなかった**。
            lines, err, cut = _py_walk(pattern, path, glob, ignored, prune=_prune)
            if err:
                return f"Error: {err}"
            if not lines:
                if cut:
                    # ★打ち切った 0 件は 0 件ではない。ここを "(no matches)" にすると、
                    #   **時間切れが「その文字列は無い」に化ける**。
                    return (f"Error: 検索を {_walk_timeout():.0f} 秒で打ち切りました"
                            "（この機械には ripgrep も grep も無いため Python で走査しています）。\n"
                            "  ★これは「0件」ではありません。path や glob で範囲を絞って引き直してください。")
                return ToolResult("(no matches)  ※この機械には ripgrep も grep も無いため "
                                  "Python の正規表現で検索しました"
                                  + ("（git が無視する場所は rg と同様に除いています）。"
                                     if ignored else "。"),
                                  ok=True, kind="empty")
            out = _render(lines, max_count)
            if cut:
                out += (f"\n★検索は {_walk_timeout():.0f} 秒で打ち切りました。"
                        "**これで全部ではありません**（path や glob で範囲を絞ってください）。")
            return out
        flag = _grep_regex_flag(grep_bin)
        if flag == "-E" and _PCRE_ONLY.search(pattern):
            hint = "".join(f"\n  {k} → {v}" for k, v in _ERE_HINT.items() if k in pattern)
            return ("Error: この機械には ripgrep が無く、代わりの grep も PCRE(-P) を持たないため、"
                    f"この綴りをそのまま検索できません: {pattern!r}\n"
                    "  ★これは「0 件」ではありません。POSIX の書き方に直して引き直してください。"
                    + hint)
        engine = f"grep {flag}"
        # -H: 単一ファイル指定でもファイル名を出す（上の rg と同じ理由）。
        cmd = [grep_bin, "-rnIH", flag, *(["-i"] if ci else []),
               *_hidden_excludes(path), "-e", pattern, path]
        if glob:
            cmd[1:1] = ["--include", glob]
    try:
        # ★`text=True` は UTF-8 として復号するので、**不正バイトが 1 つあるだけで
        #   Grep が丸ごと失敗する**（2026-08-30 実測:
        #   `Error: 'utf-8' codec can't decode byte 0xff in position 36`）。
        #   ログを grep する作業では普通に起きる。バイト列で受けて置換復号する。
        r = procrun.run(cmd, capture_output=True, timeout=30)
        r = subprocess.CompletedProcess(
            r.args, r.returncode,
            r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else r.stdout,
            r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else r.stderr)
        all_lines = (r.stdout or "").splitlines()
        if engine != "rg" and _prune and all_lines:
            # ★grep は git の無視規則を知らない。`--exclude-dir` は**基底名しか見ない**ので
            #   `app/pyruntime` のような道は指定できない（`pyruntime` を一律に外すと、
            #   その名前を正当に使う木で嘘になる）。読んでから落とすほうが正確で、
            #   30 秒の上限の内側に収まる（実測 7.5 秒）。
            hit_files = {_split_file(ln) for ln in all_lines}
            ignored = ignored | _ignored_for_files(hit_files, _memo)
            if ignored:
                all_lines = [ln for ln in all_lines
                             if not _is_ignored(_split_file(ln), ignored)]
        # ★終了コードを見る。grep と rg は「見つからない」を 1、「エラー」を 2 で返す。
        #   見ていなかったので、**エラーが「0件」として報告されていた**
        #   （壊れた正規表現・読めないディレクトリ・存在しない grep がすべて
        #   「その文字列は無い」になる）。エージェントは誤った前提で先へ進む。
        #   実機で Grep は 37/37 が「(no matches)」だった。標本は少ないが、
        #   **区別できない仕組みでその数字を見ても意味がない**のが問題。
        if r.returncode not in (0, 1):
            detail = " ".join((r.stderr or "").split())[:300] or f"終了コード {r.returncode}"
            return (f"Error: 検索そのものが失敗しました（{detail}）。\n"
                    f"  ★これは「0件」ではありません。条件を変えて引き直してください。")
        if not all_lines:
            # ★0件は失敗ではない。ただし**どのエンジンで引いた 0 件か**は書く
            #   （rg の無い機械では綴りの解釈が変わるため。上の表を参照）。
            note = "" if engine == "rg" else (
                f"  ※ripgrep が無いため {engine} で検索しました"
                + ("（git が無視する場所は rg と同様に除いています）。" if ignored else "。"))
            return ToolResult("(no matches)" + note, ok=True, kind="empty")
        return _render(all_lines, max_count)
    except subprocess.TimeoutExpired:
        return "Error: grep timeout"
    except Exception as e:
        return f"Error: {e}"


def _render(all_lines: list[str], max_count: int) -> str:
    """一致を**件数つきで**返す。文字数で無言に切らない。

    ★直したのはここ（2026-08-29・run29/31/32/34 の実測）。旧実装は

        lines = (r.stdout or "").splitlines()[:max_count]
        return "\n".join(lines)[:limits.out_cap(30000)]

    で、**二重に無言だった**:
      ① `[:max_count]` が 200 件超を黙って捨てる
      ② `[:cap]` が**行の途中**で切る（実測 158 回中 4 回が 16,384 文字ちょうど
         ＝上限に当たったのに、本文にはその旨が一言も無い）
    どちらもモデルには「これで全部」に見える。実際、同じ木の Glob が
    0 件を返してモデルが「存在しません」と正直に報告した事故と同じ形である。

    ★配分は**ファイルごとに公平**にする。先頭から詰めると、行数の多い 1 本
      （実測: `site/logs/server.log` の同じ警告 71 行）が枠を食い尽くし、
      他のファイルの一致が 1 件も見えない。「どこに書いてあるか」を知りたい
      問いに対して、それは答えを消している。
    """
    total = len(all_lines)
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for ln in all_lines:
        groups.setdefault(_split_file(ln), []).append(ln)
    nfiles = len(groups)

    cap = limits.body_cap(30000)
    # 末尾の件数行のぶんを先に取っておく（あとから足して cap を超えない）。
    reserve = 200 + min(8, nfiles) * 40
    clipped_line = False
    shown, omitted = fold.round_robin(groups, cap, reserve=reserve)
    if not shown and all_lines:
        # ★1本も見せないのは「切った」ではなく「答えていない」。
        #   1行が予算より大きいときでも、必ず先頭の1本は切って見せる。
        head = fold.clip_bytes(all_lines[0], max(200, cap // 2))
        shown = [head + " …(この1行だけで予算を超えるため切りました)"]
        clipped_line = True
        omitted = {k: len(v) for k, v in groups.items()}
        omitted[next(iter(groups))] -= 1
        omitted = {k: v for k, v in omitted.items() if v > 0}

    if max_count > 0 and len(shown) > max_count:
        for ln in shown[max_count:]:
            f = _split_file(ln)
            omitted[f] = omitted.get(f, 0) + 1
        shown = shown[:max_count]

    body = "\n".join(shown)
    tail = "\n" + fold.coverage(
        total, len(shown), what="一致",
        hint="絞るには glob=/path= を足すか、pattern をより具体的にしてください。")
    if clipped_line:
        tail += "（※1行が長すぎるため、その行の中身も切っています）"
    note = fold.omitted_note(omitted)
    if note:
        tail += "\n" + note
    if nfiles > 1:
        tail += f"\n  一致したファイル: {nfiles:,} 本。"
    out = body + tail
    # 外側の安全網。ここに当たるのは、上の配分が読み違えたときだけ。
    hard = limits.out_cap(30000)
    if len(out) > hard:
        out = out[:hard] + limits.note(len(out), hard)
    # ★成功も型で名乗る（2026-09-21・read.py / glob.py と同じ理由）。素の str で
    #   返すと `as_result` が**出力の中身**で成否を当てにいく。
    #   ★Grep はいちばん確実に当たる: **探した語が必ず出力に入る**ので、
    #   `Grep(pattern="denied")`（＝権限の問題をログから探す、という開発者が
    #   まさにやる操作）が「道具が失敗した」ことになっていた（実測）。
    return ToolResult(out, ok=True, kind="ok")


SPEC = ToolSpec(
    name="Grep",
    # ★旧文面は "Search for pattern in files. Uses ripgrep if available."（55字）。
    #   二つ問題があった。①「files の中の pattern」が「pattern に合う file」と読め、
    #   Glob と区別がつかない（実測でグレップ課題の 92% が Glob に流れた）。
    #   ②「ripgrep があれば」と**道具が自分の当てにならなさを宣伝していた**。
    #   rg が無ければ grep に落ちるのは実装の都合で、選ぶ側には関係がない。
    description=(
        "**中身に書かれている語を探す道具。** 走査して一致した行を file:line つきで返す"
        "（OS を問わず同じように動く）。"
        "「`def main` はどこに書いてあるか」「api_key を使っているのはどれか」"
        "「TODO と書かれている箇所を全部出す」— こうした問いはこれ1回で答えが出る。"
        "**シェルで grep / findstr / Select-String を組み立てるより、こちらを使うこと。**"
        # ★「ファイル名を並べても中身は分からない」と否定形で書いた版は効かなかった
        #   （Glob で学んだのと同じ: 否定しても語は残り、その用途で引かれる）。
        #   対象を先に集める手順が不要であることを、他の道具の語彙なしで言う。
        "★再帰的に走査するので、対象を先に集めておく必要はない。"
        "path で範囲、glob で対象を絞れる。"
    ),
    schema={
        "type": "object",
        "properties": {
            # ★Glob も必須引数が `pattern` で、名前が同じせいで区別がつきにくい
            #   （実測: 検索課題の 92% が Glob に流れた）。ここで何に当てるかを言う。
            "pattern": {"type": "string",
                        "description": "ファイルの中身に当てる文字列・正規表現。"
                                       "例: `def main`, `api_key`"},
            "path": {"type": "string", "default": "."},
            "glob": {"type": "string", "description": "File glob filter (e.g. '*.py')"},
            "max_results": {"type": "integer", "default": 200},
            # ★大文字小文字を無視する口（2026-09-21）。
            #   それまで**指定する手段が無く**、rg 既定の「区別する」のみだった。
            #   出典: cline `sdk/.../executors/search.ts:172` は `-i` を常時付ける。
            #   ★こちらは既定を変えない（区別したい検索が壊れるため）。**選べるようにする**だけ。
            "case_insensitive": {"type": "boolean", "default": False,
                                 "description": "大文字小文字を区別しない（既定は区別する）"},
        },
        "required": ["pattern"],
    },
    readonly=True, parallel_safe=True,
    # ★見た目が同じで文字だけ違うパスを入る前に直す（2026-10-05・tools/pathfix.py）
    handler=_pathfix.wrap(_run, "path", mode="existing"),
)
