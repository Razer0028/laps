"""作業したモデル自身の敵対レビュー（2026-10-21）。

オーナー「レビュー機能を強化したいですね。せっかく解析や操作系のツールがあるので」→ 決めたこと: できるだけタスク完了前に
レビューさせる／敵対レビューでモデルの思い込みをできるだけ減らす／道具は全部（読む・走らせる・Check・解析・画面の操作・知識サーバー・Web）
／結果はエージェントに返して画面にも出す。最初は別の文脈の子（Task の review・0.1.577）で作ったが、オーナー「子を使わずに
さっきの要件通りに親モデルが自分で敵対レビューするようにする。子にやらせるとモデルが呼んでくれないので。レビューしてとかでも
反応するようにしたい」。

同じ文脈では自分の思い込みが残りやすい（小さいモデルは同じ文脈で自分の誤りに気づきにくい・RESEARCH-harness-20261021.md）。
そこを次の 3 つで和らげる:
- 依頼の原文と、作業フォルダから読んだ変更をハーネスが目の前に置き直す（自分の語りではなく事実から始めさせる）。
- 前に走らせた確かめは数えない。見直しの中で新しく道具を走らせ、名前ではなく中身を見て、壊しにいく。
- 道具を 1 つも走らせずに見直しを終えたら、もう 1 回だけ差し戻す（oc/loop._self_review）。
"""
from __future__ import annotations

import os
import re
import sys

#: 利用者が見直しを頼んだ言い方（「レビューして」「見直して」「検証して」…）。「確認して」は普段の言い方なので含めない。
ASKS = re.compile(r"レビュー|見直し|見直して|検証|ダブルチェック|敵対|粗探し|あら探し|チェックして|"
                  r"\b(?:review|double[- ]check|audit)\b", re.I)
#: 答えの中の見直しの行（✓ と ✗）。
OK_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:✓|✔|☑)", re.M)
NG_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:✗|✘|☐)", re.M)


def enabled() -> bool:
    return (os.environ.get("LAPS_SELF_REVIEW") or "1").strip().lower() not in ("0", "off", "false", "no")


def asks_for_review(text: str) -> bool:
    return bool(ASKS.search(text or ""))


def counts(body: str) -> tuple[int, int]:
    """答えの ✓ と ✗ の行の数。"""
    return len(OK_LINE.findall(body or "")), len(NG_LINE.findall(body or ""))


def _fresh_check() -> str:
    """使う側と同じ条件で確かめる一文（Windows では cmd の形）。"""
    if sys.platform == "win32":
        return ("   Check it the way it will be used next, not in your own setup: from another folder in a new window that has "
                "none of your changes to the environment (`cmd /c \"cd /d %TEMP% && <command>\"`: no venv, no variables you "
                "set), with the default commands (the command name on PATH), and from a clean state (delete the outputs and "
                "make them again).\n")
    return ("   Check it the way it will be used next, not in your own setup: from another folder in a new shell that has none "
            "of your changes to the environment (`cd /tmp && bash -c '<command>'`: no venv, no exported variables, no "
            "~/.bashrc), with the default commands (python3, the command name on PATH), and from a clean state (delete the "
            "outputs and make them again; `make clean` before `make`). Look at files byte for byte (`cat -A`) and at "
            "permissions (`ls -l`).\n")


def _steps(lang: str, fix: bool, cite: bool = False) -> str:
    # ★cite: 最後の書き換えの後に通った確かめをハーネスが並べたとき。引用してよく、同じ物を走らせ直さない（2026-10-31 North の
    #   完走 7 本で見直しが走行時間の 35%・呼び出しの 57% が見直し前と同じ・10 回で直った不具合 0 件。オーナー「任せます」）。
    #   見直しそのものは残す（オーナーの決まり）＝まだ試していない壊し方を少なくとも 1 つ走らせる。
    check = ("2. For each one, check it with a tool. A check under \"Already passed\" below ran after your last edit, so it "
             "counts: cite it and do not run it again. For everything it does not cover, and for at least one thing that "
             "could really fail and has not been tried, run a NEW check now. "
             if cite else
             "2. For each one, run a NEW check now with a tool; checks you ran before this review do not count. ")
    never = ("Never write ✓ for an item that neither a listed check nor a new check of yours covers."
             if cite else
             "Never write ✓ for an item you did not check with a tool in this review.")
    return (
        "Assume the work is wrong somewhere and try to prove it. Do not trust your earlier conclusions or what you "
        "remember of earlier output. Keep it proportionate: a few checks that could really fail are enough for a small "
        "change. If a check of yours fails, first ask whether the check itself is wrong: read the request again. Never "
        "undo your work (git checkout, git reset, git stash) to investigate; fix it forward.\n"
        "1. List the claims your answer will make (what works, what was made, what matches the request) and each thing "
        "the request asks for, in the request's own words. Every path, file name and value the request names is an "
        "item: check it at exactly that name and place, not at a similar one you used instead. Where the request can "
        "be read two ways, say which reading you used and prefer the plain default (the tool's default behavior) over "
        "a custom one. A file the request names without a folder belongs directly in the working folder, not in a "
        "subfolder you made.\n"
        + check +
        "Run the program, the tests or the commands; open what was made and look at what is inside it, not at its name (images, "
        "audio, video, documents, data); use the app or page on screen when you have the tools; try an input that could "
        "break it (missing, empty, wrong type, negative); if you fixed a bug, search the code for other places of the same "
        "kind (every place that compares, parses or matches the same values or keywords, not only the word you changed); look up formats, APIs or error messages you are not sure about.\n"
        # ★2026-10-30 Sonnet と KAT の Terminal-Bench の突き合わせ: KAT の見直しは自分のシェル（venv・~/.bashrc の PATH）で確かめて
        #   ✓ を付けた。Sonnet は別のフォルダから新しいシェルで呼び、出力を消して作り直し、cat -A で中身を見ていた。
        + _fresh_check()
        # ★2026-11-02 Terminal-Bench の git-multibranch: 確かめのために push した commit を hosts する側に残し、最初の利用者の push が
        #   ぶつかって配置が更新されなかった。見直しの ✓ は付いていた。
        + ("3. Fix what fails and check it again. Then take out what your own checks left in what you made (test commits "
           "pushed to a repository it hosts, test accounts, test rows, test files), so that it starts from the state the "
           "request describes, and check that it still works.\n" if fix else
           "3. Do not change anything unless the user asked you to fix it; report what you find.\n")
        + f"4. In your answer, add the review in {lang}, one line per item: \"✓ <item> — <what you ran> → <what it "
        "showed>\" or \"✗ <item> — <what is wrong, or why it could not be checked>\". A result you cannot justify from the "
        "request is ✗ even if nothing crashed. " + never)


#: 依頼文に書かれた具体的な名前（絶対パス・引用符の中の値・ファイル名・ポート）。
_ABS_PATH = re.compile(r"(?<![\w.:/])/(?:[\w.\-]+/?)+")
_QUOTED = re.compile(r"[`'\"‘“「]([^`'\"’”」\s]{2,80})[`'\"’”」]")
_FILE_NAME = re.compile(r"(?<![\w/.-])[\w-]+\.[A-Za-z][A-Za-z0-9]{0,5}\b(?!\.\w)")
_PORT = re.compile(r"\bport\s+(\d{2,5})\b", re.I)


def exact_names(request: str, limit: int = 25) -> list[str]:
    """依頼文に書かれた具体的な名前（★2026-10-22 Terminal-Bench の modernize-fortran-build: 依頼は「/app/Makefile を直す」なのに
    /app/src/Makefile を直し、見直しも自分の解釈＝/app/src を確かめて ✓ を付けた）。見直しの前にハーネスが並べ、その名前のまま
    確かめさせる。"""
    text = request or ""
    found: list[str] = []
    for m in _ABS_PATH.finditer(text):
        found.append(m.group(0).rstrip(".,;:)"))
    for m in _QUOTED.finditer(text):
        found.append(m.group(1).rstrip(".,;:"))
    for m in _FILE_NAME.finditer(text):
        w = m.group(0)
        if w.lower() not in ("e.g", "i.e", "etc.") and not re.fullmatch(r"v?\d+\.\d+.*", w):
            found.append(w)
    for m in _PORT.finditer(text):
        found.append(f"port {m.group(1)}")
    out: list[str] = []
    for w in found:
        if len(w) > 1 and w not in out and not any(w != o and w in o.split("/") for o in out if "/" in o):
            out.append(w)
    return out[:limit]


def _facts(request: str, changes: list[str], todos: list[str] | None = None, passed: list[str] | None = None) -> str:
    out = ""
    if request:
        out += "\nThe request (check against this, not against your own summary of it):\n" + request[:1500]
        names = exact_names(request)
        if names:
            out += ("\nExact names the request gives (check each at exactly this name and place; a file you made under a "
                    "different path does not count; a bare file name means directly in the working folder):\n" + "\n".join(f"- {n}" for n in names))
    # ★予定表の項目も 1 つずつ確かめる（2026-10-30 オーナー「プランニングに関しても Sonnet などを参考に」: 項目は依頼が求める
    #   結果で書かせている＝見直しの照らし合わせにそのまま使える）。
    if todos:
        out += ("\nYour plan (TodoWrite) — check each item, including ones marked completed:\n"
                + "\n".join(f"- {t}" for t in todos[:15]))
    if changes:
        out += ("\nChanges in this task (recorded by the harness):\n"
                + "\n".join(f"- {x}" for x in changes[:30])
                + (f"\n- … and {len(changes) - 30} more" if len(changes) > 30 else ""))
    if passed:
        out += ("\nAlready passed after your last edit (recorded by the harness):\n"
                + "\n".join(f"- {c}" for c in passed))
    return out


def before_finish(request: str, changes: list[str], lang: str, todos: list[str] | None = None,
                  passed: list[str] | None = None) -> str:
    """終える前の見直し（作業したモデル自身が行う）。passed は最後の書き換えの後に通った確かめ（evidence.passed_checks）。"""
    return ("[Harness] Before you finish, review your own work as an adversarial reviewer who did not do it. "
            + _steps(lang, fix=True, cite=bool(passed)) + _facts(request, changes, todos, passed))


def on_request(lang: str) -> str:
    """利用者が見直しを頼んだとき、その依頼に添える。"""
    return ("[Harness] The user asked for a review. Review as an adversarial reviewer: the target is what the user "
            "names, or else the work done earlier in this conversation. " + _steps(lang, fix=False))


NO_TOOLS = ("[Harness] You ended the review without running any tool, so it checked nothing. Run a new check for each "
            "item now (run it, open the contents, use it on screen, or look it up), then give the ✓/✗ list again.")
NO_TOOLS_CITED = ("[Harness] You ended the review without running a new check. The checks that already passed still count: "
                  "do not run them again. Run one new check that could really fail and has not been tried (an input that "
                  "could break it, or using it from a fresh shell), then give the ✓/✗ list again.")


def changes_of(agent) -> list[str]:
    """この走行で変わったファイル（作業フォルダの写しから。無ければ道具の帳簿から）。"""
    out: list[str] = []
    ch = getattr(agent, "_ws_changes", None) or {}
    for how in ("created", "modified", "deleted"):
        out += [f"{how} {p}" for p in sorted(ch.get(how) or ())]
    if not out:
        led = getattr(agent, "_ledger", None)
        seen = dict.fromkeys(str(p) for _, p in (getattr(led, "edits", None) or []))
        out = [f"edited {p}" for p in seen]
    listed = [x.split(" ", 1)[1] for x in out]
    for p in dict.fromkeys(str(p) for p in (getattr(agent, "_created_files", None) or ())):
        if not any(p.endswith(q) or q.endswith(p) for q in listed):    # 作業フォルダの外に作った物（写しに出ない）
            out.append(f"created {p}")
    return out
