"""シェルの道具（Bash）の中でだけ起きる失敗を、走らせる前・後で直す（2026-10-08）。

オーナー「エージェントへの機能追加や最適化を検討して」→ 走行記録（10/5〜10/8・41 走行・道具 836 回）で
失敗は 146 回（17%）。1 回の失敗は 1 手番（平均 約 75 秒）を食う。その中に、**シェルの癖だけで落ちる形**があった:

1. cmd.exe で複数行の `python -c "…"` が行ごとに分かれて落ちる（「'import' は、内部コマンドまたは…」・
   0.1.308 でも 2 回）。複数行の命令は .cmd に書いて走らせるので、2 行目以降が別の命令になる。
   ⇒ 中身を一時ファイルに置き、`-c "exec(compile(open(…).read(), '<string>', 'exec'))"` の 1 行にする。
     `-c` のまま走るので、sys.path[0]（＝作業フォルダ）・__name__・__file__ が無いことは元と同じ。
2. `timeout /t N` は道具の中ではすぐ終わる（「入力のリダイレクトはサポートされていません」）。
   道具は標準入力を持たない（止まらないように DEVNULL にしてある）ので、待ったつもりで待っていない（5 回）。
   ⇒ PowerShell の Start-Sleep に置き換える。
3. 最後の絞り込み（findstr / find / grep）が 1 行も拾わないと終了コードは 1。失敗ではないのに失敗として
   数え、次の手番は「失敗の後」として長く考えていた。⇒ 失敗にしない（一言は添える）。

★置き換えたときは必ず一言をモデルに返す（何が走ったかを道具が黙らない）。
"""
from __future__ import annotations

import os
import re
import tempfile
import textwrap
import time

# ── 1. 複数行の python -c ─────────────────────────────────────────────────

#: `<前置き><python> [選択肢] -c "<中身>"<後ろ>`。前置き・後ろは 1 行（改行なし）、中身だけ複数行。
#: 実行ファイルは py / python / python3 / python3.11 / pythonw（.exe・パスつき・引用符つきも）。
_PY_C_RE = re.compile(
    r'^(?P<pre>[^\n]*?)'
    r'(?P<exe>(?<![^\s&|(])'                 # 先頭か、空白・& | ( の直後
    r'(?:"[^"\n]*[\\/](?:py|python[0-9.]*|pythonw)(?:\.exe)?"'
    r'|(?:[^\s"&|()<>]*[\\/])?(?:py|python[0-9.]*|pythonw)(?:\.exe)?))'
    r'(?P<opts>(?:[ \t]+-(?!c[ \t])[^\s"]+)*)'
    r'[ \t]+-c[ \t]+"(?P<code>(?:\\.|[^"\\])*)"'
    r'(?P<post>[^\n]*)$',
    re.DOTALL | re.IGNORECASE)


def _argv_unescape(s: str) -> str:
    """Windows の引数の読み方（MSVCRT）で `\\"` を `"` に戻す（奇数個の \\ ＋ " → 半分の \\ ＋ "）。
    元の 1 行の `-c "…"` で Python が受け取ったはずの文字列にそろえる。"""
    return re.sub(r'(\\+)"', lambda m: "\\" * (len(m.group(1)) // 2) + '"', s)


def _scratch_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "blackwall-pyc")
    os.makedirs(d, exist_ok=True)
    # ★走らせた後すぐには消さない（`start /b py -c …` のように背景に回ると、読む前に消える）。
    #   代わりに 1 時間より古いものをここで片付ける。
    try:
        cutoff = time.time() - 3600
        for n in os.listdir(d):
            p = os.path.join(d, n)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass
    return d


def _escaped_newlines_outside_strings(code: str) -> str | None:
    """引用符の外にある文字の `\\n`（改行のつもりで書かれたもの）を本物の改行にした文字列。外に無ければ None。"""
    out, i, q, changed = [], 0, "", False
    while i < len(code):
        c = code[i]
        if q:
            if c == "\\" and i + 1 < len(code):
                out.append(code[i:i + 2]); i += 2; continue
            if code.startswith(q, i):
                out.append(q); i += len(q); q = ""; continue
            out.append(c); i += 1; continue
        if code.startswith(('"""', "'''"), i):
            q = code[i:i + 3]; out.append(q); i += 3; continue
        if c in "\"'":
            q = c; out.append(c); i += 1; continue
        if c == "\\" and code[i + 1:i + 2] == "n":
            out.append("\n"); i += 2; changed = True; continue
        out.append(c); i += 1
    return "".join(out) if changed else None


def hoist_multiline_python(cmd: str) -> tuple[str, str] | None:
    """複数行の `python -c "…"` を 1 行に直す。(新しい命令, 一言) か、当たらなければ None。

    ★2026-10-11 の見回り（0.1.400）: 1 行の `py -c "import x;\\nfor o in …:\\n    if …"` ＝改行のつもりで文字の `\\n` を書き、
      Python が「unexpected character after line continuation character」で落ちた。元のままでは読めず、引用符の外の `\\n` を
      改行にすると読めるときだけ、改行にしてファイルから走らせる（引用符の中の \\n は触らない）。"""
    if "\n" not in (cmd or ""):
        m = _PY_C_RE.match((cmd or "").strip("\r\n"))
        if not m or "\\n" not in m.group("code"):
            return None
        raw = _argv_unescape(m.group("code"))
        fixed = _escaped_newlines_outside_strings(raw)
        if fixed is None:
            return None
        try:
            compile(raw, "<string>", "exec")
            return None                              # 元のままで読める＝直さない
        except SyntaxError:
            pass
        try:
            compile(fixed, "<string>", "exec")
        except SyntaxError:
            return None
        code = fixed.strip("\n") + "\n"
        try:
            fd, path = tempfile.mkstemp(prefix="bwpy-", suffix=".py", dir=_scratch_dir())
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(code)
        except OSError:
            return None
        if "'" in path:
            return None
        new = (f"{m.group('pre')}{m.group('exe')}{m.group('opts')} -c "
               f"\"exec(compile(open(r'{path}','rb').read(),'<string>','exec'))\"{m.group('post')}")
        return new, ("The `python -c` code used the two characters \\n as line breaks, which Python cannot read; it was run "
                     "from a file with real line breaks. For longer scripts, Write a .py file and run it.")
    m = _PY_C_RE.match(cmd.strip("\r\n"))
    if not m or "\n" not in m.group("code"):
        return None
    code = textwrap.dedent(_argv_unescape(m.group("code")).replace("\r\n", "\n")).strip("\n") + "\n"
    try:
        fd, path = tempfile.mkstemp(prefix="bwpy-", suffix=".py", dir=_scratch_dir())
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(code)
    except OSError:
        return None
    if "'" in path:
        return None
    new = (f"{m.group('pre')}{m.group('exe')}{m.group('opts')} -c "
           f"\"exec(compile(open(r'{path}','rb').read(),'<string>','exec'))\"{m.group('post')}")
    note = ("The multi-line `python -c` code was run from a file (cmd.exe splits a command at line breaks). "
            "For longer scripts, Write a .py file and run it.")
    return new, note


# ── 2. timeout /t N ──────────────────────────────────────────────────────

#: timeout.exe の待ち。★命令の頭（行頭か & | ( ; の後）にあるものだけ（`echo timeout /t 5` は触らない）。
#: 後ろは行末・/nobreak・リダイレクト・& | ) のどれか（GNU の `timeout 5 cmd` は触らない）。
_TIMEOUT_RE = re.compile(
    r'(?im)(?P<lead>^|[&|(;])(?P<sp>[ \t]*)timeout(?:\.exe)?[ \t]+(?:/t[ \t]+)?(?P<n>\d{1,5})'
    r'(?:[ \t]+/nobreak)?(?=[ \t]*(?:$|[&|>)\r\n]|\d>))')


def rewrite_timeout(cmd: str) -> tuple[str, str] | None:
    if "timeout" not in (cmd or "").lower():
        return None
    new, n = _TIMEOUT_RE.subn(
        lambda m: (f'{m.group("lead")}{m.group("sp")}powershell -NoProfile -NonInteractive -Command '
                   f'"Start-Sleep -Seconds {int(m.group("n"))}"'), cmd)
    if not n:
        return None
    return new, ("`timeout /t` was run as PowerShell Start-Sleep "
                 "(timeout.exe exits at once here because the tool has no console input).")


# ── 4. findstr に grep の「または」（\|）を書いた ─────────────────────────────
# ★2026-10-09 の走行記録（0.1.341）: `dir /s /b … | findstr /i /r "\.moc3$\|\.json$"` が「(no output)」で、
#   モデルは「その種類のファイルは無い」と結論していた。findstr は `\|` を「または」と読まず、`|` という字を
#   探す（findstr の「または」は空白区切り、または /c: を並べる）。⇒ 黙って誤った「無い」を作る。
#   `"A\|B"` を `/c:"A" /c:"B"`（/c: を並べると「どれか」に一致）へ直す。`\(` の組み分けがあるときは直さない。
_FINDSTR_ALT_RE = re.compile(
    r'(?P<head>(?<![\w.-])findstr(?:\.exe)?(?:[ \t]+/[A-Za-z]+(?::[^\s"]+)?)*)[ \t]+'
    r'(?:/[cC]:)?"(?P<pat>[^"\n]*\\\|[^"\n]*)"', re.IGNORECASE)


def rewrite_findstr_alternation(cmd: str) -> tuple[str, str] | None:
    if "findstr" not in (cmd or "").lower() or "\\|" not in cmd:
        return None
    changed: list[str] = []

    def sub(m: re.Match) -> str:
        pat = m.group("pat")
        if "\\(" in pat or "\\)" in pat:
            return m.group(0)
        alts = [a for a in pat.split("\\|") if a != ""]
        if len(alts) < 2:
            return m.group(0)
        changed.append(pat)
        head = m.group("head")
        # ★/c: は既定で「字のまま」。元が /c: でなければ findstr は既定で正規表現として読んでいたので、
        #   /r も /l も無いときは /r を足して意味を保つ（`\.moc3$` が字のままになるのを防ぐ）。
        was_c = m.group(0)[len(head):].lstrip().lower().startswith("/c:")
        opts = [o.lower() for o in re.findall(r"[ \t]/([A-Za-z]+)", head)]
        if not was_c and not any(o in ("r", "l") or (len(o) <= 3 and ("r" in o or "l" in o)) for o in opts):
            head += " /r"
        return head + " " + " ".join(f'/c:"{a}"' for a in alts)

    new = _FINDSTR_ALT_RE.sub(sub, cmd)
    if not changed:
        return None
    return new, ("findstr does not understand `\\|` (it searched for a literal `|`), so the pattern was rewritten "
                 "as several /c:\"...\" options (findstr matches a line if any of them matches). "
                 "For findstr, list alternatives as /c:\"A\" /c:\"B\" yourself next time.")


# ── 5. Windows の ssh に単引用符の命令 ───────────────────────────────────────
# ★cmd.exe は単引用符を引用符として扱わない（2026-10-09 に 利用者の PC で確かめた: `'uname -a'` は `'uname` と `-a'`
#   の 2 つに割れる）。ssh はそれを空白でつないで送るので、相手のシェルは `'uname -a'` を 1 語の命令名と読み
#   「command not found」になる。前置きが教えている `ssh <host> '<command>'` が Windows では全部こうなる。
#   さらに `|` `>` `&` は cmd.exe がこちら側で解釈する（相手で動くはずのパイプが手元で走る）。
#   ⇒ 命令が `ssh … '<相手の命令>'` の 1 本だけのときは、cmd.exe を通さず ssh を引数の並びで直接起こす
#     （POSIX のシェルで打ったときと同じものが相手に届く）。それ以外の形には触らない。
def ssh_direct_argv(cmd: str) -> list[str] | None:
    c = (cmd or "").strip()
    if not c or "\n" in c or "'" not in c:
        return None
    toks: list[str] = []
    cur, in_tok, had_single, i = "", False, False, 0
    while i < len(c):
        ch = c[i]
        if ch in " \t":
            if in_tok:
                toks.append(cur)
                cur, in_tok = "", False
            i += 1
            continue
        if ch == "'":
            j = c.find("'", i + 1)
            if j < 0:
                return None
            cur += c[i + 1:j]
            in_tok = had_single = True
            i = j + 1
            continue
        if ch == '"':
            j, buf = i + 1, ""
            while j < len(c) and c[j] != '"':
                if c[j] == "\\" and j + 1 < len(c) and c[j + 1] in '"\\':
                    buf += c[j + 1]
                    j += 2
                    continue
                buf += c[j]
                j += 1
            if j >= len(c):
                return None
            cur += buf
            in_tok = True
            i = j + 1
            continue
        if ch == "\\" and i + 1 < len(c) and c[i + 1] == "'":
            cur += "'"                       # 'it'\''s' の \'（POSIX の書き方）
            in_tok = True
            i += 2
            continue
        if ch in "|&<>^;()%!`$":
            return None                      # 引用符の外の演算子・変数＝こちら側のシェルの仕事。触らない
        cur += ch
        in_tok = True
        i += 1
    if in_tok:
        toks.append(cur)
    if not had_single or len(toks) < 3:
        return None
    exe = toks[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if exe not in ("ssh", "ssh.exe"):
        return None
    return toks


# ── 4. python・pip が PATH に無い（py はある）──────────────────────────────
# ★2026-10-10（オーナー機の走行）: `python --version`・`pip install UnityPy` が「内部コマンドまたは外部コマンド…として認識されて
#   いません」で落ち、次の手番で `py` に打ち直していた（1 回 1 手番）。Windows の Python は py ランチャーだけが PATH にあることが多い。
#   Microsoft Store の偽の python.exe（WindowsApps の中・起動するとストアを案内するだけ）も「無い」と数える。
_LAUNCH_RE = re.compile(r"(^|[&|;(]\s*)(python3?|pip3?)(\.exe)?(?=[ \t]|$)", re.M | re.I)


def _real_exe(name: str) -> bool:
    import shutil
    p = shutil.which(name)
    return bool(p) and "windowsapps" not in p.lower()


def rewrite_python_launcher(cmd: str) -> tuple[str, str] | None:
    """`python …`→`py …`・`pip …`→`py -m pip …`（python/pip が無く py があるときだけ）。"""
    if not _LAUNCH_RE.search(cmd):
        return None
    import shutil
    if not shutil.which("py"):
        return None
    used: list[str] = []

    def rep(m: re.Match) -> str:
        word = m.group(2).lower()
        if _real_exe(word):
            return m.group(0)
        used.append(word)
        return m.group(1) + ("py -m pip" if word.startswith("pip") else "py")
    out = _LAUNCH_RE.sub(rep, cmd)
    if not used or out == cmd:
        return None
    names = ", ".join(sorted(set(used)))
    return out, f"`{names}` is not on PATH here, so it was run through the `py` launcher (py / py -m pip)."


def strip_hash_comments(cmd: str) -> tuple[str, str] | None:
    """`# …` だけの行を落とす（cmd.exe に # の注釈は無く、`#` を命令として探して落ちる）。

    ★2026-10-10 オーナーの走行（0.1.364）: 1 行目に `# Use a Python script to …` と書いた命令が
      「`#` is not on PATH」（exit 255）で落ち、同じ種類の失敗に数えられた。
    ・行頭（空白の後）が `#` の行だけ。行の途中の `#`（URL の #・echo の中身）は触らない。
    ・注釈の行しか無い命令は触らない（落としても走る物が無い）。PowerShell でも `#` は注釈なので落として同じ。"""
    if "#" not in (cmd or ""):
        return None
    lines = cmd.splitlines()
    keep = [ln for ln in lines if not ln.lstrip().startswith("#")]
    if len(keep) == len(lines) or not any(ln.strip() for ln in keep):
        return None
    return "\n".join(keep), ("Lines starting with # were dropped before running: cmd.exe has no # comments "
                             "(use REM, or put the notes in your message).")


# ── 自分を撃たない: python をまとめて止める命令 ────────────────────────────────────────────

#: `taskkill … /IM python.exe`（python・pythonw・python3・ワイルドカード）。1 つの命令（& | の間）だけを見る。
_TASKKILL_PY_RE = re.compile(
    r'(?i)(?P<lead>^|(?<=[&|(\n]))(?P<sp>[ \t]*)taskkill(?:\.exe)?(?=[^&|\n]*?/IM\s+"?python[w3]?(?:\.exe|\*)?"?(?:\s|$|[&|]))'
    r'[^&|\n]*')
#: 置き換えられない形（PowerShell・wmic・Unix）。走らせずに理由と代わりを返す。
_KILL_PY_OTHER_RE = re.compile(
    r'(?i)(?:Stop-Process\b[^|&;\n]*-Name\s+["\']?python|Get-Process\s+["\']?python[^|&;\n]*\|\s*Stop-Process|'
    r'\bkill\s+-Name\s+python|wmic\b[^&|\n]*python(?:w)?\.exe[^&|\n]*\b(?:delete|terminate)\b|'
    r'\bpkill\b(?:\s+-\w+)*\s+["\']?python[w\d.]*["\']?(?=\s*(?:$|[;&|\n]))|'
    r'\bkillall\b(?:\s+-\w+)*\s+["\']?python[w\d.]*["\']?(?=\s*(?:$|[;&|\n])))')
#   ★2026-10-16 の点検: `killall python3.12`（ラボの venv の名前）を通し、`pkill -f "python server.py"`（自分の台本を名指し）を
#   断っていた。止めるのは名前だけの python 撃ち。名前の後に台本の名前が続く物は、上の作業場の絞り（tools.bash_tool）に任せる。


def _own_runtime_dir() -> str:
    import sys
    return os.path.dirname(os.path.abspath(sys.executable))


#: PID を指定して止める形（taskkill /PID・Stop-Process -Id・kill・tskill・wmic … processid=）。数を全部拾う。
_KILL_PID_RE = re.compile(
    r'(?i)(?:\btaskkill(?:\.exe)?\b[^&|\n]*?/PID\s+"?(?P<a>\d+)'
    r'|\b(?:Stop-Process|spps)\b[^|&;\n]*?-Id\s+(?P<b>\d+(?:\s*,\s*\d+)*)'
    r'|\b(?:kill|tskill)\b(?P<c>(?:\s+-[\w]+)*(?:\s+\d+)+)'
    r'|\bprocessid\s*=\s*(?P<d>\d+))')
_EXTRA_PID_RE = re.compile(r'(?i)/PID\s+"?(\d+)')


def _own_pids() -> set[int]:
    """Blackwall 自身（エージェント・アプリ本体・アプリの画面の WebView2）。oc/selfproc（2026-10-19）。"""
    from . import selfproc
    return {p for p in selfproc.guard_pids() if p and p > 4}


#: 名前で止める命令の動詞（1 つの命令＝& | ; の間だけを見る）。
_KILL_VERB_RE = re.compile(r'(?i)\b(?:taskkill|Stop-Process|spps|tskill)\b|\bwmic\b.*\b(?:delete|terminate)\b|^\s*kill\s+-Name\b')


def _app_names_in_kill(cmd: str, names: list[str]) -> list[str]:
    """名前で止める命令に、Blackwall のアプリ本体か画面（WebView2）の名前が入っているか。入っていた名前。"""
    hit: list[str] = []
    for seg in re.split(r"[&;\n]", cmd or ""):        # | では切らない（Get-Process 名前 | Stop-Process）
        if not _KILL_VERB_RE.search(seg):
            continue
        low = seg.lower()
        for n in names:
            base = n[:-4] if n.endswith(".exe") else n
            if base and (base in low or base.replace(" ", "-") in low) and n not in hit:
                hit.append(n)
    return hit


def _pids_in_kill(cmd: str) -> set[int]:
    out: set[int] = set()
    for m in _KILL_PID_RE.finditer(cmd or ""):
        for g in ("a", "b", "c", "d"):
            v = m.group(g)
            if v:
                out.update(int(x) for x in re.findall(r"\d+", v) if not x.startswith("0"))
    for seg in re.split(r"[&|;\n]", cmd or ""):          # taskkill /PID 1 /PID 2 の 2 つ目以降
        if re.search(r"(?i)\btaskkill\b", seg):
            out.update(int(x) for x in _EXTRA_PID_RE.findall(seg))
    return out


def guard_self_kill(cmd: str, *, windows: bool) -> tuple[str | None, str]:
    """python をまとめて止める命令から Blackwall 自身を外す。(新しい命令 or None＝走らせない, 一言)。当たらなければ (cmd, "")。

    ★2026-10-10 オーナー「これなんで勝手に終わっちゃうの」: モデルが自作の試験用サーバーを止め直そうとして
      `taskkill /F /IM python.exe` を走らせ、同じ名前で動いていた Blackwall の本体（同梱の pyruntime の python.exe）と
      中継の係まで止めた（18:25:48・走行は「Python の出力が閉じました」で失敗）。pkill -f で自分を撃つのと同じ形。
    ・taskkill の形は、Blackwall の Python（このプロセスの実行ファイルの置き場）から動いているものを除いて止める PowerShell に置き換える。
    ・PowerShell の Stop-Process・wmic・pkill/killall python は走らせず、PID で止める代わりの命令を返す。"""
    # ★PID で自分（Blackwall の本体とその親）を止める命令は走らせない（2026-10-11 の見回り・0.1.394: Serve で 5173 を配ったあと、
    #   netstat で 5173 を聞いている PID を vite の残りだと思って `taskkill /F /PID 150140` ＝本体を止め、走行が失敗した）。
    named = _pids_in_kill(cmd) if cmd else set()
    own = named & _own_pids() if named else set()
    if own:
        ports = []
        try:
            from . import servetool as _st
            ports = _st.ports()
        except Exception:
            pass
        held = (f" It is also the process that holds port(s) {', '.join(map(str, ports))} for the server(s) you started with "
                "Serve — stop those with Serve(stop=<port>).") if ports else ""
        return None, (f"Not run: PID {', '.join(map(str, sorted(own)))} is L.A.P.S itself (this agent's own process); "
                      f"stopping it ends this session.{held} Stop only the programs you started (their PIDs are shown when "
                      "you start them with run_in_background, and BashOutput(kill=true) stops them).")
    # ★アプリ本体や画面（WebView2）を名前で止めると Blackwall が落ちる（2026-10-19 オーナー「自身のアプリを終了してしまった」）。
    if windows and cmd and _KILL_VERB_RE.search(cmd):
        try:
            from . import selfproc
            names = selfproc.app_names()
        except Exception:
            names = []
        hit = _app_names_in_kill(cmd, names)
        if hit:
            return None, (f"Not run: {', '.join(hit)} is L.A.P.S itself (the app you run in, or its window's web view); "
                          "stopping it by name ends this session. Stop only the program you started, by its PID "
                          "(`taskkill /F /PID <pid>`; other apps' web views have their own PIDs).")
    if not cmd or "python" not in cmd.lower():
        return cmd, ""
    rt = _own_runtime_dir()
    if windows and _TASKKILL_PY_RE.search(cmd):
        ps = ("powershell -NoProfile -Command \"Get-Process python,pythonw,python3 -ErrorAction SilentlyContinue | "
              f"Where-Object {{ $_.Path -and $_.Path -notlike '{rt}\\*' }} | Stop-Process -Force -PassThru | "
              "ForEach-Object { 'stopped ' + $_.Id + ' ' + $_.Path }; exit 0\"")
        new = _TASKKILL_PY_RE.sub(lambda m: m.group("lead") + m.group("sp") + ps
                                  + m.group(0)[len(m.group(0).rstrip()):], cmd)
        return new, ("`taskkill /IM python.exe` would also stop L.A.P.S itself (it runs on python.exe), so it was run as a "
                     "command that stops every python process except Blackwall's own. Next time stop the program you "
                     "started by its PID (`taskkill /F /PID <pid>`), or start it with run_in_background and stop that job.")
    if _KILL_PY_OTHER_RE.search(cmd):
        alt = ('taskkill /F /PID <pid>  (find the PID with: wmic process where "name=\'python.exe\'" get '
               'processid,commandline)' if windows else "pkill -f <your script name>  (or kill <pid>)")
        return None, ("Not run: this stops every python process by name, which includes L.A.P.S itself (it runs on "
                      f"python). Stop only the program you started: {alt}.")
    return cmd, ""


# ── 終わらない命令（サーバー）────────────────────────────────────────────────────────────
#: 起こすと待ち受けを続けて終わらない命令（はっきりしたものだけ。`py xxx_server.py` のような名前だけの物は含めない）。
_SERVER_CMD_RE = re.compile(
    r'(?i)(?:^|[&|;(]\s*)(?:'
    r'(?:py|python3?|pythonw?)(?:\.exe)?\s+(?:-\S+\s+)*-m\s+(?:http\.server|uvicorn|flask\s+run|streamlit\s+run|gunicorn)\b'
    r'|uvicorn\s+\S'
    r'|flask\s+run\b'
    r'|streamlit\s+run\b'
    r'|gunicorn\s+\S'
    r'|(?:(?:py|python3?)(?:\.exe)?\s+)?\S*manage\.py\s+runserver\b'
    r'|(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:dev|serve)\b'
    r'|npx\s+(?:vite|next\s+dev|serve|http-server)\b'
    r'|php\s+-S\s'
    r'|jupyter\s+(?:notebook|lab)\b'
    r')')
#: 前面で走らせて時間切れになった出力に、待ち受けを始めた印がある（名前では分からないサーバー・`py sandbox_server.py` など）。
_SERVER_OUT_RE = re.compile(
    r'(?i)Uvicorn running on|Application startup complete|Running on https?://|Serving HTTP on|Listening on|'
    r'listening at|Server (?:is )?(?:running|started|listening)|Local:\s+https?://|ready in \d|Started server|'
    r'Press CTRL\+C to quit|Ctrl\+C to (?:quit|stop)')


def looks_like_server(cmd: str) -> bool:
    """起こすと終わらないサーバーの命令か（背景で走らせる）。既に背景へ回す書き方（start /b・末尾の &・nohup）は除く。"""
    c = (cmd or "").strip()
    if not c or re.search(r'(?i)\bstart\s+(?:/b\b|"")|&\s*$|\bnohup\b|\bStart-Process\b|\s--?(?:h|help|version|V)\b', c):
        return False
    return bool(_SERVER_CMD_RE.search(c))


def server_timeout_note(output: str, timeout_ms: int) -> str | None:
    """前面で時間切れになった出力がサーバーの起動なら、背景で走らせる一言（それ以外は None）。"""
    if not _SERVER_OUT_RE.search(output or ""):
        return None
    return (f"This command started a server that keeps running, so it can never finish in the foreground; it was "
            f"stopped after {timeout_ms} ms and is NOT running now. Start it again with run_in_background=true, "
            "then check it with curl (or BashOutput(pid) for its log).")


def prepare_windows(cmd: str) -> tuple[str, list[str]]:
    """Windows で走らせる前の直し。(命令, モデルへの一言の並び)。当たらなければ元のまま。"""
    notes: list[str] = []
    for fix in (strip_hash_comments, rewrite_python_launcher, hoist_multiline_python, rewrite_timeout,
                rewrite_findstr_alternation):
        try:
            r = fix(cmd)
        except Exception:
            r = None
        if r:
            cmd, note = r
            notes.append(note)
    return cmd, notes


# ── 3. 絞り込みの 0 件 ─────────────────────────────────────────────────────

# ★2026-10-11 の見回り（0.1.393）: オーナーの機械には Git の grep があり、`grep -aob … data.unity3d` の当たりなし（exit 1）が
#   失敗として返っていた。無い grep は標準エラーに「認識されていません」が出る＝is_no_match が None を返すので足して安全。
_FILTERS_WIN = {"findstr", "find", "grep", "egrep", "fgrep", "rg"}
_FILTERS_POSIX = {"grep", "egrep", "fgrep", "rg"}


def _last_stage(cmd: str, windows: bool = False) -> str:
    """パイプの最後の段（`||` は区切りに数えない）。パイプが無ければ命令の全体。
    ★引用符の中の `|` は区切りではない（2026-10-09: `findstr /r "a\\|b"` の `|` で割って最後の段を
      取り違え、一致なしの終了コード 1 を失敗と数えていた）。cmd.exe では単引用符は引用符ではない。"""
    c = cmd or ""
    last, q, i = 0, "", 0
    while i < len(c):
        ch = c[i]
        if q:
            if ch == q:
                q = ""
        elif ch == '"' or (ch == "'" and not windows):
            q = ch
        elif ch == "|":
            if i + 1 < len(c) and c[i + 1] == "|":
                i += 2
                continue
            if i > 0 and c[i - 1] == "|":
                i += 1
                continue
            last = i + 1
        i += 1
    return c[last:].strip()


_LEADING_CD = re.compile(r"""^(?:\s*(?:cd|pushd)\s+(?:/d\s+)?(?:"[^"]*"|'[^']*'|[^\s&;|]+)\s*(?:&&|;)\s*)+""", re.I)


def ends_with_filter(cmd: str, *, windows: bool) -> str | None:
    """命令の最後が絞り込み（findstr / grep 等）なら、その名前。パイプが無いときは単独の絞り込みだけ。"""
    c = (cmd or "").strip()
    if not c or "\n" in c:
        return None
    c = _LEADING_CD.sub("", c)         # `cd d && grep x` は grep x と同じ（cd の失敗は出力に理由が出るので is_no_match が失敗と読む）
    stage = _last_stage(c, windows)
    if stage == c and re.search(r'[&;]', c):
        return None                    # `dir & findstr …` のような並べ書きは判断しない
    tok = stage.split(None, 1)[0].strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower() if stage else ""
    if tok.endswith(".exe"):
        tok = tok[:-4]
    return tok if tok in (_FILTERS_WIN if windows else _FILTERS_POSIX) else None


_NOMATCH_OUT = re.compile(r"[\s0]*")


def is_no_match(cmd: str, rc: int | None, stdout: str, stderr: str, *, windows: bool) -> str | None:
    """終了コード 1 が「絞り込みが何も拾わなかった」だけなら、その絞り込みの名前。
    標準エラーに何か出ている（前の段の失敗・パスが無い等）ときは None＝今までどおり失敗。"""
    if rc != 1 or (stderr or "").strip():
        return None
    if not _NOMATCH_OUT.fullmatch(stdout or ""):
        return None
    return ends_with_filter(cmd, windows=windows)
