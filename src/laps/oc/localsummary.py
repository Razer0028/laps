"""小さいモデル向けの要約（2026-10-18）。モデルが書くのは考えの要る節だけ、機械で作れる節はハーネスが作る。

オーナー 2026-10-18「コンテキスト圧縮の仕組みなどもローカルモデル基準で頼みます。というかエージェント全体」
「タスクの時間が伸びた際に最初の方に行っていた作業で得た情報について忘れてしまったりしている」。

★何が問題だったか（走行記録の数えの欄・10-12〜18 の 225 走行）:
  要約は 136 回・合計 4.8 時間（1 回平均 127 秒）。10-15 に目標を約 8,000 字へ上げてから、要約は 3,900 → 9,200 字に伸び、
  1 回 109 → 221〜233 秒になった。雛形 8 節のうち「利用者の指示を原文のまま」「ファイルの一覧」は写すだけの作業で、
  小さいモデルが写すと時間がかかり、写し落とし・写し間違いも起きる（要約が忘れの入口になる）。
⇒ ここでは:
  ・モデルが書くのは 6 節（目的・確かめた事実・未確認の推測・行き止まり・作業の状態・次の一手）だけ・約 3,000 字。
  ・ハーネスが会話から機械で足す: 利用者の発言の原文／書いた・読んだファイル／道具の呼び出し 1 回 1 行の記録。
    ★前の要約の同じ節を引き継ぐ。道具の記録は最初の 15 行を必ず残す（最初の方の作業を忘れないため）。
★帳面（oc/findings）が読む見出し（## Verified Facts・## Dead Ends）は変えない。
"""
from __future__ import annotations

import json
import re

#: モデルが書く節（この順）。欠けていれば切れた要約（loop._summary_complete）。
SECTIONS = ("## Objective", "## Verified Facts", "## Unverified Assumptions", "## Dead Ends", "## Work State",
            "## Next Move")
TAIL = ("## Next Move",)
#: ハーネスが足す節。
H_USER, H_FILES, H_LOG = "## User Instructions", "## Files", "## Work Log"
MECH = (H_USER, H_FILES, H_LOG)

REQUEST = """[Harness] The context is almost full. Do not call any tools now. Write a summary of the conversation above for yourself, so that you can continue the work after the older messages are removed.

Write the summary in English, even when the conversation is in another language (you will keep replying to the user in their language). Copy file paths, values, commands and error messages exactly."""

REQUEST_UPDATE = """The answer to "What did we do so far?" at the start of this conversation is your earlier summary. Write one new summary that keeps everything from it that is still needed and adds what happened after it: it replaces the earlier one, so what you leave out is lost.
- Keep its Verified Facts and Dead Ends unless the later conversation proved them wrong.
- Move an assumption to Verified Facts only when a later tool result confirmed it."""

TEMPLATE = """Write exactly these six sections, in this order:
<template>
## Objective
- [what the user wants, in one or two sentences]

## Verified Facts
- [a fact that a tool result showed, then how: which tool or command and what it returned; or "(none)"]

## Unverified Assumptions
- [a belief or guess that no tool result has confirmed yet; or "(none)"]

## Dead Ends
- [an approach that was tried and failed, and why, so it is not tried again; or "(none)"]

## Work State
- Done: [finished work and changes]
- In progress: [current work]
- Blocked: [problems and open questions, or "(none)"]

## Next Move
1. [the next concrete action]
</template>

Rules:
- Write only these six sections. After your summary the harness adds the user's messages word for word, the files that were read and written, and a one-line log of every tool call, so do not copy those.
- Each bullet must make sense on its own: name the file, value, object or command it is about.
- Short bullets, no paragraphs. Keep the whole summary under about 3,000 characters.
- Do not mention the summary process."""

SHORTER = ("Your previous answer was cut off before the end (missing: {missing}). Write the whole summary again, complete "
           "through ## Next Move, with shorter bullets.")

#: 前置きと会話の包みで、利用者の発言ではない物。
_NOT_USER = ("[Harness]", "<tool_response>", "What did we do so far?", "Continue if you have next steps",
             "Continue with the next step", "The previous request exceeded", "[Request interrupted by user]")
_SYS_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
#: 依頼の写しに付けた言語の一言（loop の reply_lang・「(Write every message to me in Japanese. …)」）。
_LANG_NOTE = re.compile(r"\s*\(Write every message to me in [^)]*\)\s*$")
_EXIT = re.compile(r"exit code:?\s*(-?\d+)")
_LAB_EXIT = re.compile(r"\[exit (-?\d+) ·")
_READ_LINE = re.compile(r"^\d+: ", re.M)
LOG_HEAD, LOG_TAIL = 15, 45
USER_MAX_EACH, USER_MAX_TOTAL = 600, 4000
LINE_MAX = 200


def request(previous: bool, note: str = "") -> str:
    parts = [REQUEST]
    if previous:
        parts.append(REQUEST_UPDATE)
    parts.append(TEMPLATE)
    if note:
        parts.append(note)
    return "\n\n".join(parts)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _one(s: str, n: int = 120) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


_CWD = ""


def _rel(p: str) -> str:
    """作業場（mechanical の cwd）の中なら相対の形に（記録を短くする）。"""
    p = str(p or "")
    c = _CWD.rstrip("/\\")
    if c and (p.startswith(c + "/") or p.startswith(c + "\\")):
        return p[len(c) + 1:]
    return p


def _args(name: str, a: dict) -> str:
    g = a.get
    if name == "Bash":
        return _one(str(g("command") or "").split("\n", 1)[0])
    if name in ("Read", "Edit", "Write"):
        p = _rel(str(g("file_path") or g("path") or ""))
        return p + (f", offset {g('offset')}" if name == "Read" and g("offset") else "")
    if name == "Grep":
        return _one(f"{g('pattern')!r}" + (f" in {_rel(g('path'))}" if g("path") else "") + (f" {g('glob')}" if g("glob") else ""))
    if name == "Glob":
        return _one(str(g("pattern") or "") + (f" in {_rel(g('path'))}" if g("path") else ""))
    if name == "WebSearch":
        return _one(str(g("query") or ""))
    if name == "WebFetch":
        return _one(str(g("url") or ""))
    if name == "Tools":
        return _one(str(g("call") or g("names") or ""))
    try:
        return _one(json.dumps(a, ensure_ascii=False), 100)
    except (TypeError, ValueError):
        return ""


def _result(name: str, content: str) -> str:
    """結果の 1 行（状態と中身の要点）。"""
    t = str(content or "")
    if not t.strip():
        return "(no output)"
    if t.startswith("[Old tool result content cleared]"):
        return "(output cleared before this summary)"
    m = _EXIT.search(t[-800:]) or _LAB_EXIT.search(t[-800:])
    status = f"exit {m.group(1)}" if m and m.group(1) != "0" else ("error" if t.lstrip().startswith("Error") else "ok")
    body = t.split("<shell_metadata>", 1)[0]
    lines = [ln.strip() for ln in body.splitlines()
             if ln.strip() and not ln.lstrip().startswith(("[Host]", "<path>", "<type>", "<content>", "</content>",
                                                           "(End of file"))]
    if name == "Read":
        rows = [_READ_LINE.sub("", ln, count=1).strip() for ln in body.splitlines() if _READ_LINE.match(ln)]
        n = len(rows)
        # ★短いファイル（設定など）は中身そのものを残す: 最初に確かめた小さな値が、要約をまたいでも消えない（2026-10-18
        #   オーナー「最初の方に行っていた作業で得た情報について忘れてしまったり」）。
        if 0 < n <= 8:
            return f"{status}: {n} lines: " + _one(" | ".join(r for r in rows if r), 170)
        return f"{status}: {n} lines" if n else f"{status}: " + _one(lines[0] if lines else "", 140)
    if not lines:
        return status
    # 命令の結果は最後の行に要点があることが多い（試験の件数・数えた値）。ほかは最初の行。
    pick = lines[-1] if name in ("Bash", "Lab", "BashOutput") else lines[0]
    return f"{status}: {_one(pick, 140)}"


def call_line(name: str, args: dict, content: str) -> str:
    """道具の呼び出し 1 回の記録の 1 行（要約の ## Work Log と、途中の思い出させる一言で同じ形）。"""
    return _one(f"- {name}({_args(name, args if isinstance(args, dict) else {})}) → {_result(name, content)}", LINE_MAX)


def log_head(summary: str, n: int = LOG_HEAD) -> list[str]:
    """要約の ## Work Log の頭 n 行（いちばん古い呼び出し）。"""
    return [x for x in _prev_section(summary, H_LOG) if not x.startswith("- … ")][:n]


def _prev_section(previous: str | None, head: str) -> list[str]:
    if not previous:
        return []
    out, on = [], False
    for line in previous.splitlines():
        if re.match(r"^\s*## ", line):
            on = line.strip().startswith(head)
            continue
        if on and line.strip().startswith("- "):
            out.append(line.rstrip())
    return out


def _bound_log(lines: list[str], hist_note: str) -> list[str]:
    if len(lines) <= LOG_HEAD + LOG_TAIL:
        return lines
    cut = len(lines) - LOG_HEAD - LOG_TAIL
    return lines[:LOG_HEAD] + [f"- … {cut} calls in between are not listed{hist_note}"] + lines[-LOG_TAIL:]


def mechanical(msgs: list[dict], previous: str | None, user_part=None, history: str = "", cwd: str = "") -> str:
    """会話（と前の要約の同じ節）から、利用者の発言・ファイル・道具の記録の 3 節を作る。cwd の中のパスは相対の形で書く。"""
    global _CWD
    _CWD = cwd or ""
    users = _prev_section(previous, H_USER)
    calls: dict[str, tuple[str, dict]] = {}
    log = [x for x in _prev_section(previous, H_LOG) if not x.startswith("- … ")]
    written: list[str] = []
    read: list[str] = []
    for line in _prev_section(previous, H_FILES):
        m = re.match(r"^- (written|read): (.+)$", line)
        if m:
            (written if m.group(1) == "written" else read).extend(x.strip() for x in m.group(2).split(", ") if x.strip())
    for m in msgs:
        role = m.get("role")
        if role == "user":
            t = _SYS_REMINDER.sub("", _text(m.get("content"))).strip()
            if user_part is not None:
                try:
                    t = str(user_part(t)).strip()
                except Exception:
                    pass
            # ★写しに付けた <git…>/<tool_hints> と言語の一言は利用者の言葉ではない（oc/reading.user_messages と同じ切り方）
            t = _LANG_NOTE.sub("", t.split("\n\n<", 1)[0]).strip()
            if t and not t.startswith(_NOT_USER):
                q = f"- 「{_one(t, USER_MAX_EACH)}」"
                if q not in users:
                    users.append(q)
        elif role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    a = {}
                calls[str(tc.get("id"))] = (str(fn.get("name") or ""), a if isinstance(a, dict) else {})
        elif role == "tool":
            name, a = calls.get(str(m.get("tool_call_id")), ("", {}))
            if not name or name in ("Done", "TodoWrite"):
                continue
            res = _result(name, _text(m.get("content")))
            log.append(call_line(name, a, _text(m.get("content"))))
            p = _rel(str(a.get("file_path") or ""))
            if p and name in ("Edit", "Write") and not res.startswith(("error", "exit")):
                written.append(p)
            elif p and name == "Read":
                read.append(p)
    while len(users) > 1 and sum(len(x) for x in users) > USER_MAX_TOTAL:
        users.pop(1)                       # 最初の依頼は残し、古い順に外す
    uniq = lambda xs: list(dict.fromkeys(xs))
    written, read = uniq(written)[-40:], [p for p in uniq(read) if p not in written][-30:]
    hist = f" (the full text is in {history})" if history else ""
    parts = [H_USER + " (the user's messages, word for word, added by the harness)\n" + ("\n".join(users) or "- (none)")]
    files = ([f"- written: {', '.join(written)}"] if written else []) + ([f"- read: {', '.join(read)}"] if read else [])
    parts.append(H_FILES + " (added by the harness)\n" + ("\n".join(files) or "- (none)"))
    parts.append(H_LOG + " (every tool call so far, oldest first, added by the harness)\n"
                 + ("\n".join(_bound_log(log, hist)) or "- (none)"))
    return "\n\n".join(parts)


def strip_mechanical(summary: str) -> str:
    """モデルが雛形に無い機械の節を写してしまったら外す（ハーネスが作り直す）。"""
    out, skip = [], False
    for line in (summary or "").splitlines():
        if re.match(r"^\s*## ", line):
            skip = any(line.strip().startswith(h) for h in MECH)
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip()
