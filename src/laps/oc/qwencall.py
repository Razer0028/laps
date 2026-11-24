"""本文に文字で書かれた Qwen 形式（Qwen3-Coder の XML）の道具の呼び出しを拾う（2026-10-15）。

★なぜ要るか（オーナー「ツール呼び出しが標準出力に出てきている時も結構ある」）: Qwen3.6 の走行 4 本のうち 3 本で、記録の答えの中に
  完全な呼び出し（`<tool_call><function=Bash><parameter=command>…</parameter></function></tool_call>`）が 1・3・7 回、文字のまま残っていた
  （思考の中には 0 回）。道具を止めた手番（途中経過の報告・tool_choice="none"）でモデルが作業を続けようとして書いた物で、推論機は
  道具を止めているので呼び出しとして解かず、本文として画面と答えに出ていた。報告の手番は Gemma 以外では道具を止めないように直した
  （loop._complete）。ここはそれでも本文に残ったときの受け皿。
★拾うのは、本文の**末尾**に並んだ完全な呼び出し（`</tool_call>` で閉じている）だけ。途中で切れた物・文の途中に例として書いた物は拾わない。
引数: `<parameter=名前>値</parameter>`。値の前後の改行 1 つは外す。JSON として読める値（数・true/false/null・[…]・{…}）は読む
（llama.cpp の Qwen3-Coder の解析器も道具の定義の型で読み直す）。
"""
from __future__ import annotations

import json
import re

_CALL = re.compile(r"<tool_call>\s*<function=([A-Za-z_][\w.\-]*)>(.*?)</function>\s*</tool_call>", re.S)
_PARAM = re.compile(r"<parameter=([A-Za-z_][\w.\-]*)>(.*?)</parameter>", re.S)


def _value(raw: str):
    v = raw
    if v.startswith("\n"):
        v = v[1:]
    if v.endswith("\n"):
        v = v[:-1]
    s = v.strip()
    if s and (s[0] in "[{" or s in ("true", "false", "null") or re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", s)):
        try:
            return json.loads(s)
        except ValueError:
            pass
    return v


def salvage(body: str) -> tuple[list[dict], str]:
    """本文の末尾に並んだ完全な呼び出しを [{"name","input"}] にし、それを除いた本文と一緒に返す。拾えなければ ([], body)。"""
    if not body or "<tool_call>" not in body:
        return [], body
    matches = list(_CALL.finditer(body))
    if not matches:
        return [], body
    # 末尾から、間に空白しか無い呼び出しの並びを取る（最後の呼び出しの後ろも空白だけ）
    if body[matches[-1].end():].strip():
        return [], body
    run = [matches[-1]]
    for m in reversed(matches[:-1]):
        if body[m.end():run[0].start()].strip():
            break
        run.insert(0, m)
    calls = []
    for m in run:
        args = {p.group(1): _value(p.group(2)) for p in _PARAM.finditer(m.group(2))}
        calls.append({"name": m.group(1), "input": args})
    return calls, body[:run[0].start()].rstrip()


# ── 崩れた呼び出し（2026-11-02）──
# Gyro-M が `<function=Bash=replace_all>` と書いた。推論機は宣言に無い名前を本文として返すので、答えとして走行が終わっていた（18:57〜19:04 に 7 回）。
_UNREAD = re.compile(r"<tool_call>\s*<function=|^[ \t]*<function=", re.M)
_HOLD_MARKS = ("<tool_call>", "<function=")


def unread(body: str) -> str | None:
    """本文の末尾が呼び出しの形（完全でも、途中で切れていても）なら、その前の本文を返す。違えば None。
    説明として書いた物は呼び出しに数えない（コードの囲みの中・バッククォートの直後・後ろに文が続く）。"""
    found = list(_UNREAD.finditer(body or ""))
    if not found:
        return None
    m = found[-1]
    before = body[:m.start()]
    if before.count("```") % 2 or before.endswith("`"):
        return None
    tail = body[m.end():]
    for close in ("</tool_call>", "</function>"):
        at = tail.rfind(close)
        if at >= 0:
            if tail[at + len(close):].strip():
                return None
            break
    return before.rstrip()


class Hold:
    """本文の流れから呼び出しの形を画面へ出さないために、目印が出たところから手元に留める。
    留めた分は手番の後で unread() を見て、説明だったときだけ流す（消えはしない）。"""

    def __init__(self) -> None:
        self.buf = ""
        self.held = False

    def feed(self, text: str) -> str:
        if self.held:
            self.buf += text
            return ""
        s, self.buf = self.buf + text, ""
        hits = [i for i in (s.find(m) for m in _HOLD_MARKS) if i >= 0]
        if hits:
            at = min(hits)
            self.held, self.buf = True, s[at:]
            return s[:at]
        keep = 0                       # 目印が字の途中で切れて届いた分
        for mark in _HOLD_MARKS:
            for n in range(min(len(mark) - 1, len(s)), 0, -1):
                if s.endswith(mark[:n]):
                    keep = max(keep, n)
                    break
        if keep:
            self.buf = s[-keep:]
            return s[:-keep]
        return s

    def rest(self) -> str:
        out, self.buf = self.buf, ""
        return out
