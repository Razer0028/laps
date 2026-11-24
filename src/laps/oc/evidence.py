"""証拠の帳簿と終わりの判定（2026-10-06 オーナーの図「Observer → Diagnoser → 成功？ → Repair」・「一旦その案は任せます」）。

★図の箱を別々の推論として作らない。理由（2026-10-06 の検討・メモリ listen-and-hints-20261006）:
  ・推論機は 1 手番 ~68 秒（Qwen3.8・12〜15 tok/s）。判定役を毎手番足すと所要が 1.5 倍前後になる見込み。
  ・旧中核はほぼこの図の形（差し戻し・関門）だったが、厚い中核と薄い中核の差は検出できなかった
    （95%CI −19〜+15pp）。oc に替えて「性能や動作としては申し分ない」と言われた。
  ・推論機は 4 スロットで KV 64K を共有する。別の文脈の診断役は本流のキャッシュを追い出しうる。
  ⇒ ここでは**コードで**作る。推論機は呼ばない。

1. 帳簿（Observer）: 書き換え（Write / Edit の成功）と確認（試験・ビルド・検査・編集したファイルの実行）を順に記す。
2. 判定（成功？）: 最後の書き換えの後の、最後の確認で決める。
     verified … 通った ／ failed … 落ちた ／ unverified … 確認していない ／ none … 書き換えが無い（調べ物）
   ★モデルの「完了しました」では決めない（71 バイトの文字ファイルを .mp4 と名付けて完了と言った実例）。
3. 差し戻し（Repair）: failed なのに答えが失敗に触れていないときだけ、**1 回だけ**戻す。それ以外は知らせるだけ。
4. 同じ失敗の繰り返し（Diagnoser の代わり）: 同じ型の失敗が 3 回目・6 回目になったら、これまでの試みを
   並べて結果に添える。opencode・goose・cline の繰り返し検知は「同じ名前＋同じ引数」だけで、
   引数を少しずつ変える形は捕まらない（knowledge/エージェントの詰まりは中身を読めば一言で終わる.md）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# 確認に当たるコマンド（試験・ビルド・型や構文の検査・設定の検査）。★命令の位置の語だけを見る
#   （`mkdir make` の make を確認と数えない）。区切り（&& ; | &）ごとに、前置き（sudo・環境変数・
#   python -m・uv run・cd …）を外した先頭で照らす。
_CHECK = re.compile(r"""(?ix)(?:
      pytest | py\.test | unittest | tox | nox | compileall | py_compile | ruff | mypy | pyright | flake8 | pylint
    | (?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|build|lint|typecheck|check|tsc)
    | npx\s+(?:tsc|eslint|vitest|jest|playwright|prettier\s+--check)
    | tsc | eslint | vitest | jest
    | cargo\s+(?:test|build|check|clippy|run)
    | go\s+(?:test|build|vet|run)
    | make | ctest | ninja | msbuild | cmake\s+--build
    | dotnet\s+(?:build|test|run)
    | (?:\.[\\/])?gradlew?(?:\.bat)? | mvn
    | node\s+--check | bash\s+-n | sh\s+-n | shellcheck | php\s+-l
    | docker\s+(?:build|compose\s+(?:up|build|config)) | docker-compose\s+(?:up|build|config)
    | terraform\s+(?:validate|plan) | nginx\s+-t | apachectl\s+configtest | systemd-analyze\s+verify
    | godot[\w.-]*\s+.*--headless | unity[\w.-]*\s+.*-batchmode
)(?![\w-])""")
_SEG = re.compile(r"&&|\|\||[;&|\n]")
_PREFIX = re.compile(r"""(?ix)^\s*(?:(?:sudo|time|env|nice|timeout\s+\S+|[a-z_][a-z0-9_]*=\S*)\s+)*
    (?:(?:python3?|py(?:\s+-3(?:\.\d+)?)?)\s+-m\s+|uv\s+run\s+|poetry\s+run\s+|pipenv\s+run\s+)?""")
# 見る・動かすだけで「確かめた」にならないもの（編集したファイルの名前を含んでいても確認とは数えない）。
_NOT_RUN = re.compile(r"""(?ix)^\s*(?:cat|type|more|less|head|tail|get-content|gc|notepad|code|vim?|nano|ls|dir|
    copy|cp|mv|move|rm|del|remove-item|git\s+(?:add|diff|commit|status|log|show|stash)|echo|findstr|grep|rg|
    select-string|wc|stat|file|xxd|certutil|cd|set-location|pushd)\b""")
_RUN_FILE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".ps1", ".bat", ".cmd", ".sh", ".rb", ".pl", ".php", ".go",
                 ".lua", ".exe", ".jar"}

_ACK = re.compile(r"""(?ix)(失敗|エラーが(?:残|出|続)|通りません|通らない|通っていません|動きません|動かない|
    動いていません|できませんでした|できていません|未解決|未完了|解決していません|直っていません|まだ.{0,8}(?:ない|ません)|
    \bfail(?:ed|s|ing)?\b|not\s+(?:yet\s+)?work|doesn'?t\s+work|does\s+not\s+work|still\s+(?:broken|failing)|
    could\s*n[o']t|unable\s+to|not\s+fixed|unresolved)""")

_ERRWORD = re.compile(r"(?i)error|failed|failure|exception|traceback|not found|no such|denied|invalid|cannot|"
                      r"unable|refused|timed? ?out|見つかりません|ありません|できません|拒否|失敗|エラー|認識されていません")


@dataclass
class Ledger:
    seq: int = 0
    edits: list[tuple[int, str]] = field(default_factory=list)          # (順番, パス)
    checks: list[dict] = field(default_factory=list)                    # {seq, cmd, ok, exit, tail}
    failures: dict[str, list[str]] = field(default_factory=dict)        # 型 → 試みの一覧
    pushbacks: int = 0
    pending: dict[str, str] = field(default_factory=dict)              # 背景で起こした確かめ（pid → 命令）

    def edited_names(self) -> set[str]:
        return {os.path.basename(p).lower() for _, p in self.edits if len(os.path.basename(p)) >= 4}


#: ヒアドキュメントの本文は命令ではない（2026-10-31 FINDINGS への追記文にスクリプト名があっただけで「✓ Check passed」と出た）。
_HEREDOC = re.compile(r"""(?<!<)<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\1([^\n]*)\n.*?(?:\n[ \t]*\2[ \t]*(?=\n|$)|\Z)""", re.S)


def is_check(cmd: str, edited: set[str]) -> bool:
    for seg in _SEG.split(_HEREDOC.sub(lambda m: "<<" + m.group(2) + m.group(3), cmd or "")):
        head = _PREFIX.sub("", seg, count=1).strip()
        if not head:
            continue
        if _CHECK.match(head):
            return True
        if not edited or _NOT_RUN.match(head):
            continue
        low = head.lower()
        for name in edited:
            if os.path.splitext(name)[1] in _RUN_FILE_EXT and \
                    re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", low):
                return True
    return False


def _exit_code(text: str) -> int | None:
    m = re.search(r"exit code:\s*(-?\d+)", text)
    return int(m.group(1)) if m else None


def _tail(text: str, n: int = 4) -> str:
    body = text.split("<shell_metadata>")[0]
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])[-600:]


def observe(led: Ledger, name: str, inp: dict, ok: bool, text: str) -> None:
    """道具 1 回ぶんを帳簿に記す。"""
    led.seq += 1
    if name in ("Write", "Edit") and ok and isinstance(inp, dict) and inp.get("file_path"):
        led.edits.append((led.seq, str(inp["file_path"])))
    elif name == "Task" and ok:
        # ★作業を任せた子（oc/delegate）が変えたファイル＝親の書き換えとして数える（2026-10-18）。子が自分で走らせた確かめは
        #   数えない（親が自分で読むか確かめる・終える前の自動の確かめもこれで働く）。並びはハーネスが作業フォルダから作った節。
        m = re.search(r"\[Harness\] Files the helper actually changed[^\n]*\n((?:- .*\n?)+)", text)
        if m:
            for ln in m.group(1).splitlines():
                mm = re.match(r"- (?:created|changed) (.+)$", ln.strip())
                if mm:
                    led.edits.append((led.seq, mm.group(1).strip()))
    elif name == "Bash" and isinstance(inp, dict):
        cmd = str(inp.get("command") or "")
        if is_check(cmd, led.edited_names()):
            # ★背景で起こしただけの命令は、まだ終わっていない＝確かめに数えない（2026-10-10: v7 を背景で起こした 11 秒後に
            #   「✓ 編集後の確認が通りました」と出て走行が閉じた）。終わりを BashOutput で見たときに数える。
            m = re.search(r"launched in background[\s\S]{0,80}?pid:\s*(\d+)", text)
            if m or str(inp.get("run_in_background")).lower() in ("true", "1"):
                if m and ok:
                    led.pending[m.group(1)] = cmd
            else:
                led.checks.append({"seq": led.seq, "cmd": " ".join(cmd.split())[:160], "ok": bool(ok),
                                   "exit": _exit_code(text), "tail": "" if ok else _tail(text)})
    elif name == "BashOutput" and isinstance(inp, dict):
        pid = str(inp.get("pid") or "").strip()
        if pid in led.pending and re.search(r"alive:\s*False", text):
            cmd = led.pending.pop(pid)
            bad = re.search(r"Traceback \(most recent call last\)|^\s*\w*(?:Error|Exception):|\bFAILED\b", text, re.M)
            led.checks.append({"seq": led.seq, "cmd": " ".join(cmd.split())[:160], "ok": not bad, "exit": None,
                               "tail": _tail(text) if bad else ""})
    elif name == "Check" and isinstance(inp, dict):
        # ★道具 Check はプロジェクト自身の検査・試験を走らせた＝確かめに数える（oc/checktool.py・2026-10-17）。
        #   何も見つからなかったとき（ok=False・「nothing ran」）は数えない＝確かめたことにしない。
        if "nothing ran" not in text:
            led.checks.append({"seq": led.seq, "cmd": f"Check {inp.get('kind') or 'auto'}", "ok": bool(ok),
                               "exit": None, "tail": "" if ok else _tail(text, 8)})
    elif name == "Serve" and isinstance(inp, dict) and str(inp.get("screenshot")).lower() in ("true", "1") and ok:
        # ★Web のページを撮って確かめた＝確かめに数える（2026-10-10）。コンソールの誤り・欠けたファイルがあれば失敗。
        bad = [ln.strip() for ln in text.splitlines()
               if ln.startswith("  ") and ("Uncaught" in ln or "Error" in ln or "HTTP 4" in ln or "HTTP 5" in ln)]
        shot = "Screenshot of" in text
        led.checks.append({"seq": led.seq, "cmd": f"Serve screenshot {inp.get('path') or '/'}",
                           "ok": shot and not bad, "exit": None, "tail": "\n".join(bad[:8])})


# 文書だけの書き換え（README・メモ・表）は「確かめる」対象にしない（注記が雑音になる）。
_DOC_EXT = {".md", ".markdown", ".txt", ".rst", ".adoc", ".org", ".csv", ".tsv", ".log"}


def verdict(led: Ledger) -> dict:
    edits = [(q, p) for q, p in led.edits if os.path.splitext(p)[1].lower() not in _DOC_EXT]
    if not edits:
        return {"status": "none"}
    last_edit = edits[-1][0]
    files = sorted({os.path.basename(p) for _, p in edits})
    paths = list(dict.fromkeys(p for _, p in edits))
    after = [c for c in led.checks if c["seq"] > last_edit]
    if not after:
        return {"status": "unverified", "files": files, "paths": paths}
    last = after[-1]
    return {"status": "verified" if last["ok"] else "failed", "files": files, **last}


def passed_checks(led: Ledger, limit: int = 8) -> list[str]:
    """最後の書き換えの後に通った確かめの命令（同じ命令は 1 つに）。最後の確かめが落ちているときは空＝引用させない。"""
    if verdict(led).get("status") != "verified":
        return []
    last_edit = max(q for q, p in led.edits if os.path.splitext(p)[1].lower() not in _DOC_EXT)
    return list(dict.fromkeys(c["cmd"] for c in led.checks if c["seq"] > last_edit and c["ok"]))[-limit:]


def acknowledges_failure(answer: str) -> bool:
    return bool(_ACK.search(answer or ""))


def pushback_message(v: dict) -> str:
    code = f"exited with {v['exit']}" if v.get("exit") is not None else "failed"
    tail = f"\n{v['tail']}" if v.get("tail") else ""
    return ("[Harness] Before you finish: the last check after your edits failed — "
            f"`{v['cmd']}` {code}:{tail}\n"
            "Either fix it and run the check again, or say plainly in your answer that it does not work yet "
            "and why. (This reminder is sent only once.)")


#: 依頼が「実行しない」ことを求めている（書くだけ・見るだけ）。未確認の差し戻しをしない。
_NO_RUN = re.compile(r"実行しないで|実行せず|走らせないで|動かさないで|書くだけ|作るだけ|コードだけ|試さないで|"
                     r"don'?t run|do not run|without running|no need to run", re.I)


def asks_not_to_run(text: str) -> bool:
    return bool(_NO_RUN.search(text or ""))


def suggest_checks(paths: list[str], cwd: str) -> list[str]:
    """書き換えたファイルの種類と、プロジェクトの試験・ビルドの命令から、確かめ方を具体的に挙げる（2026-10-12・Gemma の強化）。

    ★なぜ要るか（Gemma 4 の 37 走行）: 「動かすか試験をしてから終えて」と 1 回促しても、確かめないまま終えた走行が 5 本あった。
      何を打てばよいかを自分で決めきれない。命令を名指しすれば、そのまま打てる。★ここでは打たない（決めるのはモデル）。"""
    import shutil
    py = "py" if os.name == "nt" and shutil.which("py") else ("python3" if shutil.which("python3") else "python")
    out: list[str] = []
    try:
        from .hostfacts import project_commands
        pc = project_commands(cwd) or ""
        for row in pc.splitlines():
            m = re.match(r"-\s*(test|build|lint):\s*`([^`]+)`", row.strip())
            if m:
                out.append(f"`{m.group(2)}` (the project's {m.group(1)})")
    except Exception:
        pass
    for p in paths[-6:]:
        ext = os.path.splitext(p)[1].lower()
        q = f'"{p}"'
        if ext == ".py":
            out.append(f"`{py} -m py_compile {q}` (syntax), then run it the way the task needs")
        elif ext in (".js", ".mjs", ".cjs") and shutil.which("node"):
            out.append(f"`node --check {q}` (syntax), then run it the way the task needs")
        elif ext == ".json":
            out.append(f"`{py} -c \"import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))\" {q}` (valid JSON)")
        elif ext in (".html", ".htm"):
            out.append(f"Serve the page with screenshot=true ({os.path.basename(p)}): it reports console errors and missing files")
        elif ext == ".ps1":
            out.append(f"run {q} with `powershell -NoProfile -File {q}` if it is safe to run, or check it with `Get-Command -Syntax`")
        elif ext in (".sh",):
            out.append(f"`bash -n {q}` (syntax)")
    seen: set[str] = set()
    res = []
    for x in out:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res[:5]


#: 答えの中の場所（Windows の絶対パス・/ で始まるパス）。空白を含む場所は `…` で囲まれているときだけ拾う。
_WIN_PATH = re.compile(r"(?<![\w/\\])[A-Za-z]:\\[^\s\"'<>|*?`「」『』（）()、。，,]+")
_POSIX_PATH = re.compile(r"(?<![\w/.~:-])/(?:[\w.-]+/)+[\w.-]+")
_TICKED = re.compile(r"`([^`\n]{4,260})`")
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)
#: 「作った・保存した・書き出した」と言っている文だけを確かめる（参照しただけの場所・別の機械の場所を数えない）。
_MADE = re.compile(r"保存|作成|作り|作っ|書き出|書き込|書きま|出力|生成|置きま|置いた|配置|追加し|変換し|抽出し|"
                   r"\b(?:saved|created|wrote|written|generated|exported|extracted|output to)\b", re.I)
#: その場所が「無い・消した・見つからない」と言っている文では、無くて当たり前。
_ABSENT = re.compile(r"見つかりません|見つからない|ありません|存在しません|存在しない|無い|ない|消し|削除|除き|作れませんでした|"
                     r"not found|missing|doesn'?t exist|does not exist|deleted|removed|could not|failed", re.I)


def missing_claims(answer: str, cwd: str, remote: bool = False) -> list[str]:
    """最後の答えで「作った・保存した」と挙げた場所のうち、実在しない物（2026-10-12・Gemma の強化）。

    ★なぜ要るか: 「保存しました: D:\\…」と言った物が無いまま完了になると、利用者が開いて初めて分かる
      （71 バイトの文字ファイルを .mp4 と名付けて完了と言った実例は evidence の冒頭）。
    ・見るのは「作った・保存した」と言っている文だけ。その文が「無い・消した」とも言っていれば数えない。
    ・別の機械で作業した走行（ssh など・remote=True）は見ない（その場所はこの機械に無くて当たり前）。
    ・この機械の流儀の場所だけ見る（Windows なら C:\\…、ほかなら /…）。* や ? を含む型は数えない。
    ・空白で切れた場所（C:\\Program Files\\…）は、続きを足した形が 1 つでも在れば在ると見なす。"""
    out: list[str] = []
    for p, cut in claimed_paths(answer, cwd, remote):
        if os.path.exists(p) or cut:
            continue                                # 空白で切れたかもしれない場所は数えない（誤って差し戻さない）
        if p not in out:
            out.append(p)
    return out[:5]


def claimed_paths(answer: str, cwd: str, remote: bool = False) -> list[tuple[str, bool]]:
    """最後の答えで「作った・保存した」と言っている文に出てくる場所（絶対の場所, 空白で切れたかもしれないか）。"""
    if remote or not answer:
        return []
    out: list[tuple[str, bool]] = []
    win = os.name == "nt"
    for line in re.split(r"(?<=[。\n])", answer):
        if not _MADE.search(line) or _ABSENT.search(line):
            continue
        line = _URL.sub(" ", line)
        cands: list[tuple[str, bool]] = []         # (場所, 空白で切れたかもしれないか)
        for m in _TICKED.finditer(line):
            t = m.group(1).strip()
            if (win and re.match(r"[A-Za-z]:\\", t)) or (not win and t.startswith("/") and " " not in t):
                cands.append((t, False))
        rest = _TICKED.sub(" ", line)
        for m in (_WIN_PATH if win else _POSIX_PATH).finditer(rest):
            if "…" in rest[m.end():m.end() + 2]:      # 「/srv/x/…/y.txt」は途中で切れる。切れた先を場所として確かめない
                continue
            # Windows の場所は空白で切れることがある（C:\\Program Files\\…）。続きが英数字の名前なら切れたかもしれない。
            cut = win and re.match(r" [A-Za-z0-9_()&'+,-]+(?:[ \\]|\.\w)", rest[m.end():m.end() + 80]) is not None
            cands.append((m.group(0), cut))
        for p, cut in cands:
            p = p.rstrip(".:;)]）」』、。")
            if len(p) < 6 or any(ch in p for ch in "*?<>{}$%…") or "..." in p:      # 省略した書き方は場所ではない
                continue
            full = p if os.path.isabs(p) else os.path.join(cwd, p)
            if all(full != q for q, _c in out):
                out.append((full, cut))
    return out[:12]


def bad_outputs(answer: str, cwd: str, since: float, remote: bool = False) -> tuple[int, list[tuple[str, str]]]:
    """答えで「作った・保存した」と挙げた場所のうち、この走行で書かれた画像・音声・動画などが拡張子どおりの中身か
    （oc/mediacheck・2026-10-12 オーナー「.png に変換されたものを閲覧しようとしたらサポートされていない形式」）。
    (確かめた数, 合わなかった (場所, 説明))。"""
    from . import mediacheck
    paths = [p for p, _cut in claimed_paths(answer, cwd, remote) if os.path.exists(p)]
    if not paths:
        return 0, []
    return mediacheck.check_outputs(paths, since)


def ran_remote(msgs: list[dict]) -> bool:
    """この走行が別の機械で作業したか（ssh・scp・リモートの道具を呼んだか）。missing_claims を見送るのに使う。"""
    for m in msgs:
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            args = str(fn.get("arguments") or "")
            if name.lower().startswith(("remote", "mcp__")) or re.search(
                    r"\b(?:ssh|scp|sftp|rsync|plink|pscp)\b|Invoke-Command|Enter-PSSession|-ComputerName", args):
                return True
    return False


def weak_final(body: str, tools_used: int, japanese: bool = True) -> str:
    """終わりの答えが報告になっていない理由（なっていれば ""）。2026-10-12 オーナー「Gemma4 だと途中経過を報告しないで
    終了するときが多いです」: 復号の Python を貼っただけ・聞き返しだけ・空、で走行が終わっていた。"""
    t = (body or "").strip()
    if not t:
        return "empty"
    prose = re.sub(r"```.*?```", " ", t, flags=re.S)
    prose = re.sub(r"`[^`]*`", " ", prose)
    words = len(re.findall(r"[ぁ-んァ-ヶ一-龠]", prose)) if japanese else len(prose.split())
    short = 60 if japanese else 40
    if t.startswith("```") and words < short:
        return "code_only"
    sents = [x.strip() for x in re.split(r"(?<=[。！？!?])\s*", prose) if len(x.strip()) >= 15]
    if len(sents) >= 4 and len(set(sents)) <= len(sents) * 0.7:
        return "repeated"
    if tools_used >= 5 and words < short:
        return "too_short"
    return ""


def unverified_message(v: dict, suggestions: list[str] | None = None) -> str:
    """編集の後に何も実行・試験していないまま終えようとしたときの一言（2026-10-10・Gemma）。"""
    files = v.get("files") or []
    fl = ", ".join(files[:5]) + (f" and {len(files) - 5} more" if len(files) > 5 else "")
    # ★Web のファイルなら、ブラウザで開いて確かめる手を名指しする（2026-10-10 0.1.374 の走行: この一言の後に
    #   Serve を撮影なしで呼んで終え、「確認していません」のまま終わった。撮影なしは「ファイルが返る」しか見ない）。
    web = any(str(f).lower().endswith((".html", ".htm", ".js", ".mjs", ".css", ".jsx", ".tsx", ".vue", ".svelte"))
              for f in files)
    how = (" For a web page, call Serve with screenshot=true: it opens the page in a browser and reports console "
           "errors and missing files (serving without a screenshot only checks that the file is returned)."
           if web else "")
    sug = ""
    if suggestions:
        sug = " Ways to check: " + "; ".join(suggestions) + "."
    return ("[Harness] Before you finish: you changed " + fl + " but have not run, built or tested anything since "
            "the last change. Run the program or the relevant test/build now to check that it works, and fix what "
            "fails; then give your final answer." + how + sug + " If it cannot be run here, say so plainly in your answer.")


def notice_for(v: dict, acknowledged: bool) -> tuple[str, str] | None:
    """(kind, 文言)。画面の注記。kind は表示側の表（wire.rs の GATE/PLAIN・term の _NOTICE）に既にあるものだけ使う。"""
    st = v.get("status")
    files = v.get("files") or []
    fl = ", ".join(files[:3]) + (f" +{len(files) - 3}" if len(files) > 3 else "")
    if st == "verified":
        return "harness", f"✓ Check passed: `{v['cmd']}`"
    if st == "unverified":
        return "harness", f"⚠ Edits not verified: {fl}"
    if st == "failed":
        code = f" → exit {v['exit']}" if v.get("exit") is not None else ""
        if acknowledged:
            return "harness", f"⚠ Check still failing (reported): `{v['cmd']}`{code}"
        return "verifier", f"Claims done but check fails: `{v['cmd']}`{code}"
    return None


# ── 同じ失敗の繰り返し ──────────────────────────────────────────────────────

def failure_signature(name: str, text: str) -> tuple[str, str]:
    """(型, 代表の 1 行)。数字・引用・パスを潰して、同じ種類の失敗を同じ型にする。"""
    body = text.split("<shell_metadata>")[0]
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()
             and not ln.strip().startswith(("...output truncated", "Full output saved"))]
    pick = next((ln for ln in reversed(lines) if _ERRWORD.search(ln)), lines[-1] if lines else "")
    s = pick.lower()
    s = re.sub(r"(['\"`]).*?\1", "Q", s)
    s = re.sub(r"[a-z]:\\\S*|(?<!\w)/\S+", "P", s)
    s = re.sub(r"\d+", "N", s)
    s = re.sub(r"\s+", " ", s).strip()
    return f"{name}:{s[:120]}", pick[:200]


def _attempt(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return name
    if name == "Bash":
        return f"`{' '.join(str(inp.get('command') or '').split())[:120]}`"
    key = next((k for k in ("file_path", "path", "pattern", "url", "query", "name") if inp.get(k)), None)
    return f"{name} {str(inp[key])[:100]}" if key else name


def repeat_note(led: Ledger, name: str, inp: dict, text: str) -> tuple[int, str]:
    """(同じ型の何回目か, 結果に添える文)。添えるのは 3 回ごと（3・6・9…・それ以外は ""）。

    ★2026-10-19 3 回目・6 回目だけ → 3 回ごと（オーナー「2 で進めて。ClaudeCode の仕組みとかも調べて小さいモデル用に最適化」）。
      KAT-Coder の走行（178 手番）で、この一言が 14 回出ても引数を少しずつ変えて同じ失敗を続け、7 回目より後は何も言われなかった。
      6 回を超えたら、次の一手を 1 つに絞って言う（小さいモデルは抽象的な助言より具体的な次の 1 手に従う）。
      走らせ方の関門（次の手番は文を先に書く）と文脈の作り直しは oc/loop の側（_note_stuck）。"""
    sig, pick = failure_signature(name, text)
    tries = led.failures.setdefault(sig, [])
    tries.append(_attempt(name, inp))
    n = len(tries)
    if n % 3:
        return n, ""
    listed = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(tries[-6:], start=max(0, n - 6)))
    if n < 6:
        return n, (f"\n\n[Harness] This is failure #{n} of the same kind in this request: «{pick}». Attempts so far:\n"
                   f"{listed}\nRepeating small variations is unlikely to help. Find the cause first (read the full error, "
                   "check the path, version or environment), then change the approach — or ask the user if something "
                   "is missing.")
    return n, (f"\n\n[Harness] Failure #{n} of the same kind: «{pick}». The last attempts:\n{listed}\n"
               "Stop varying this command. Next, do exactly one of these:\n"
               "1. Run one command that checks the assumption behind the error (does the file/folder exist, which "
               "version, is the tool installed, what does the config say).\n"
               "2. Read the file or config that the error points to.\n"
               "3. Take a different route to the goal (another tool, another method) — or tell the user what is missing.")
