"""Gemma 4 が思考の中に書いた道具の呼び出しを拾う（2026-10-10）。

★なぜ要るか（オーナーの走行・推論機の記録で確かめた）: Gemma は道具の結果の後に思考を開いたまま（書式が `<|channel>thought` を
  開いた状態で渡す）、思考を閉じる印 `<channel|>` を出さずに `<|tool_call>call:Read{file_path:<|"|>…<|"|>}<tool_call|>` を書き、
  `<|tool_response>` で手番を終えることがある。llama.cpp の解析器は思考の閉じまでを思考とみなすので、呼び出しは思考の文に埋もれ、
  道具の呼び出しとしては返らない＝Blackwall には「思考だけで終えた手番」に見え、促し直すとモデルは同じ呼び出しを書き直していた
  （Write なら中身ごと）。同じ会話の 5 手番（Edit・Bash・Write ×2・Read）で起きていた。
★拾うのは、思考の**末尾**が完全な呼び出し（`<tool_call|>` で閉じている）のときだけ。途中で切れた呼び出しや、思考の途中に
  例として書いたものは拾わない。
引数の書き方（llama.cpp の gemma4_to_json と同じ）: `{key:value,…}`・文字列は `<|"|>…<|"|>`・数・true/false/null・`[…]`・入れ子の `{…}`。
"""
from __future__ import annotations

import re

Q = '<|"|>'
OPEN = "<|tool_call>call:"
CLOSE = "<tool_call|>"


class _ParseError(ValueError):
    pass


def _ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _value(s: str, i: int):
    i = _ws(s, i)
    if s.startswith(Q, i):
        j = s.find(Q, i + len(Q))
        if j < 0:
            raise _ParseError("string not closed")
        return s[i + len(Q):j], j + len(Q)
    if i < len(s) and s[i] == "{":
        return _dict(s, i)
    if i < len(s) and s[i] == "[":
        out, i = [], i + 1
        while True:
            i = _ws(s, i)
            if i < len(s) and s[i] == "]":
                return out, i + 1
            v, i = _value(s, i)
            out.append(v)
            i = _ws(s, i)
            if i < len(s) and s[i] == ",":
                i += 1
            elif i < len(s) and s[i] == "]":
                return out, i + 1
            else:
                raise _ParseError("bad array")
    j = i
    while j < len(s) and s[j] not in ",}]":
        j += 1
    tok = s[i:j].strip()
    if not tok:
        raise _ParseError("empty value")
    low = tok.lower()
    if low == "true":
        return True, j
    if low == "false":
        return False, j
    if low in ("null", "none"):
        return None, j
    try:
        return (float(tok) if any(c in tok for c in ".eE") else int(tok)), j
    except ValueError:
        return tok, j


def _dict(s: str, i: int):
    if s[i] != "{":
        raise _ParseError("expected {")
    out: dict = {}
    i += 1
    while True:
        i = _ws(s, i)
        if i < len(s) and s[i] == "}":
            return out, i + 1
        j = s.find(":", i)
        if j < 0:
            raise _ParseError("key without value")
        key = s[i:j].strip().strip('"')
        if not key or any(c in key for c in "{}[],\n"):
            raise _ParseError("bad key")
        v, i = _value(s, j + 1)
        out[key] = v
        i = _ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1
        elif i < len(s) and s[i] == "}":
            return out, i + 1
        else:
            raise _ParseError("bad dict")


def salvage(think: str) -> tuple[list[dict], str]:
    """思考の末尾に並んだ完全な呼び出しを [{"name","input"}] にし、それを除いた思考と一緒に返す。拾えなければ ([], think)。"""
    if not think or OPEN not in think:
        return [], think
    calls: list[dict] = []
    first = None
    pos = think.find(OPEN)
    try:
        while pos >= 0:
            m = re.match(r"[A-Za-z_][\w.\-]*", think[pos + len(OPEN):])
            if not m:
                return [], think
            name = m.group(0)
            i = pos + len(OPEN) + len(name)
            args, i = _dict(think, i)
            if not think.startswith(CLOSE, i):
                return [], think                # 閉じていない＝途中で切れた
            i += len(CLOSE)
            if first is None:
                first = pos
            calls.append({"name": name, "input": args})
            nxt = think.find(OPEN, i)
            if nxt < 0:
                if think[i:].strip():
                    return [], think            # 呼び出しの後にまだ考えている＝例として書いた物かもしれない
                break
            if think[i:nxt].strip():
                return [], think                # 呼び出しの間に文がある＝末尾に並んだ呼び出しではない
            pos = nxt
    except _ParseError:
        return [], think
    return calls, think[:first].rstrip()


# ── 制御記号の文字列を記号にしない（2026-10-10）────────────────────────────────────────────
# ★Gemma 4 の `<|tool_call>`・`<channel|>`・`<|"|>` などは語彙で USER_DEFINED（変換の visible_tokens）なので、本文の中に文字列として
#   あるだけで本物の記号になる（推論機の /tokenize で実測: parse_special=false でも 48・101）。`<turn|>`・`<|think|>`・`<bos>` は
#   CONTROL だが、llama-server はチャットの写しを parse_special=true で切るので同じ。道具の結果やファイルにこの文字列があると、
#   モデルには「呼び出しが始まった」「思考が閉じた」に見える（vLLM #39043 でも原因の 1 つ。Blackwall 自身のコードや推論機のログを読むと当たる）。
# → 推論機へ送る写しの利用者の発言と道具の結果でだけ、`<` の直後に U+2060（WORD JOINER・見えない）を挟む（/tokenize で記号にならないと確かめた）。
#   モデルが写して道具の引数に書いた分は、実行の直前に外す（unmask）＝ Edit の照合・Bash の命令はファイルの実物どおりに戻る。
#   モデル自身の発言・思考・呼び出しの引数は生成したとおりに残す（読み込みの控えを崩さない）。
WJ = "\u2060"
_CTRL_RE = re.compile(r'<(?=\|[A-Za-z_"]{1,24}\|?>|[A-Za-z_]{1,24}\|>|(?:bos|eos|pad|unk|mask)>)')


def mask(text: str) -> str:
    if not text or "<" not in text:
        return text
    return _CTRL_RE.sub("<" + WJ, text)


def mask_message(m: dict) -> dict:
    """利用者の発言・道具の結果の本文だけを覆う（文字列か、text の部品の並び）。変えなければ同じ物を返す。"""
    if not isinstance(m, dict) or m.get("role") not in ("user", "tool"):
        return m
    c = m.get("content")
    if isinstance(c, str):
        mc = mask(c)
        return m if mc is c or mc == c else {**m, "content": mc}
    if isinstance(c, list):
        changed, parts = False, []
        for p in c:
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
                t = mask(p["text"])
                if t != p["text"]:
                    p, changed = {**p, "text": t}, True
            parts.append(p)
        return {**m, "content": parts} if changed else m
    return m


def unmask(obj):
    """道具の引数から、写しで挟んだ U+2060 を外す（`<` の直後のものだけ）。"""
    if isinstance(obj, str):
        return obj.replace("<" + WJ, "<") if WJ in obj else obj
    if isinstance(obj, dict):
        return {k: unmask(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [unmask(v) for v in obj]
    return obj
