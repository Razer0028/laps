"""Remember — 利用者が「覚えて」と頼んだことを、次の会話からも覚えている（2026-10-30）。

オーナー「メモリ機能の強化」（Claude のアプリのような使い方に）。Claude は「覚えて」と言われた事柄を会話をまたいで持ち、
利用者が見て消せる。Blackwall の Construct（oc/construct.py）は過去の作業を手掛かりで**思い出す**仕組みで、
ハーネスは記憶を添えない（2026-10-21 オーナー「自動で添える機能自体がだめ」）。ここはそれとは別の物:
利用者が**明示して**頼んだ決まりと好みだけを置く。

置き場は ~/.config/blackwall/REMEMBER.md（全体の指示のファイルと同じフォルダ）。prompt.instructions が毎回の前置きの
全体の指示の後ろに足す。★2026-10-30 0.1.601 は全体の指示のファイル（AGENTS.md）の区画に書いていたが、全体の指示は
「最初に見つかった 1 本だけ」を読むので、AGENTS.md を作った途端に ~/.claude/CLAUDE.md が読まれなくなった＝別のファイルに分けた。
0.1.601 で AGENTS.md に書いた区画は、最初に読むときにこちらへ移す（AGENTS.md が空になれば消す＝元の読み方に戻る）。
道具（Remember）と Windows の Memory 画面（`python -m laps.oc.remember list|add|remove` を呼ぶ）が同じこの口を使う。
★資格情報らしい物は覚えない（redact が当たる文は断る）。前置きに毎回入る＝鍵を書くと全部の会話へ配られる。
"""
from __future__ import annotations

from .. import paths as _lp
import json
import os
import sys
import time
from pathlib import Path

from ..tools import ToolResult

START = "<!-- blackwall:remember -->"
END = "<!-- /blackwall:remember -->"
HEAD = "## Remembered (the user asked you to keep these in every conversation)"
MAX_ITEMS = 100
MAX_CHARS = 300

DESCRIPTION = (
    "Keep a note for all future conversations, list the notes, or forget one. Use it when the user asks you to remember "
    "something (\"remember that...\", \"from now on...\", \"覚えておいて\") or to forget it. Save one short sentence in the "
    "user's words: a preference, a rule or a fact about their setup. The notes are shown to you at the start of every "
    "conversation, so do not save things that only matter now, and never save passwords, keys or tokens.\n"
    "Example: Remember(action=\"add\", text=\"Reply in Japanese.\")\n"
    "Example: Remember(action=\"list\")   Remember(action=\"remove\", n=2)"
)
SCHEMA_PROPS = {
    "action": {"type": "string", "enum": ["add", "list", "remove"], "description": "add a note, list them, or remove one"},
    "text": {"type": "string", "description": "the note to keep (add), or words of the note to remove (remove)"},
    "n": {"type": "integer", "description": "number of the note to remove, from list (remove)"},
}


def _cfg() -> Path:
    return _lp.config_dir()


def path() -> Path:
    """覚えていることの置き場（前置きに毎回入る・prompt.instructions）。"""
    return _cfg() / "REMEMBER.md"


def _migrate() -> None:
    """0.1.601 が全体の指示のファイル（AGENTS.md）の区画に書いた分を REMEMBER.md へ移す。"""
    old = _cfg() / "AGENTS.md"
    try:
        s = old.read_text(encoding="utf-8")
    except OSError:
        return
    a, b = s.find(START), s.find(END)
    if a < 0 or b < a:
        return
    moved = [ln.strip()[2:].strip() for ln in s[a + len(START):b].splitlines() if ln.strip().startswith("- ")]
    rest = (s[:a] + s[b + len(END):]).strip()
    have = _items_of(_read_raw())
    notes = have + [x for x in moved if x and x.casefold() not in {h.casefold() for h in have}]
    _write(notes)
    if rest:
        old.write_text(rest + "\n", encoding="utf-8")
    else:
        old.unlink()                       # 空になった＝作ったのは 0.1.601 だけ。消して ~/.claude/CLAUDE.md の読み方に戻す


def _read_raw() -> str:
    try:
        return path().read_text(encoding="utf-8")
    except OSError:
        return ""


def _read() -> str:
    try:
        _migrate()
    except OSError:
        pass
    return _read_raw()


def _items_of(s: str) -> list[str]:
    a, b = s.find(START), s.find(END)
    if a < 0 or b < a:
        return []
    return [ln.strip()[2:].strip() for ln in s[a + len(START):b].splitlines() if ln.strip().startswith("- ") and ln.strip()[2:].strip()]


def items() -> list[str]:
    return _items_of(_read())


def _write(notes: list[str]) -> None:
    p = path()
    if not notes:
        try:
            p.unlink()
        except OSError:
            pass
        return
    s = "\n".join([START, HEAD, *[f"- {x}" for x in notes], END]) + "\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(s, encoding="utf-8")
    os.replace(tmp, p)


def _secret(text: str) -> bool:
    try:
        from .. import redact
        return not redact.redact(text, salt=None).clean
    except Exception:
        return False


def add(text: str) -> tuple[bool, str]:
    t = " ".join(str(text or "").split())
    if not t:
        return False, "Nothing to remember: give the note as text."
    if len(t) > MAX_CHARS:
        return False, f"The note is too long ({len(t)} characters, at most {MAX_CHARS}). Say it in one short sentence."
    if _secret(t):
        return False, "This looks like a password, key or token, so it was not saved (the notes go into every conversation)."
    notes = items()
    if any(x.casefold() == t.casefold() for x in notes):
        return True, f"Already remembered: {t}"
    if len(notes) >= MAX_ITEMS:
        return False, f"There are already {MAX_ITEMS} notes. Remove one first (Remember(action=\"list\"))."
    notes.append(t)
    _write(notes)
    return True, f"Remembered: {t}"


def remove(n: int | None = None, text: str = "") -> tuple[bool, str]:
    notes = items()
    if not notes:
        return False, "There are no notes."
    i = None
    if n is not None:
        try:
            i = int(n) - 1
        except (TypeError, ValueError):
            i = None
    elif text.strip():
        hits = [k for k, x in enumerate(notes) if text.strip().casefold() in x.casefold()]
        if len(hits) > 1:
            return False, "More than one note matches; give its number: " + "; ".join(f"{k + 1}. {notes[k]}" for k in hits)
        i = hits[0] if hits else None
    if i is None or not (0 <= i < len(notes)):
        return False, "No such note. Notes: " + "; ".join(f"{k + 1}. {x}" for k, x in enumerate(notes))
    gone = notes.pop(i)
    _write(notes)
    return True, f"Forgot: {gone}"


def tool(inp: dict) -> ToolResult:
    a = inp if isinstance(inp, dict) else {}
    act = str(a.get("action") or ("add" if a.get("text") and a.get("n") is None else "list")).strip().lower()
    if act in ("save", "remember", "store"):
        act = "add"
    if act in ("delete", "forget", "del"):
        act = "remove"
    try:
        if act == "add":
            ok, msg = add(str(a.get("text") or ""))
        elif act == "remove":
            ok, msg = remove(a.get("n"), str(a.get("text") or ""))
        elif act == "list":
            notes = items()
            ok, msg = True, ("No notes yet." if not notes else
                             "Notes kept for every conversation:\n" + "\n".join(f"{k + 1}. {x}" for k, x in enumerate(notes)))
        else:
            return ToolResult("Error: action must be add, list or remove", ok=False, kind="error")
    except OSError as e:
        return ToolResult(f"Error: could not write {path()}: {e}", ok=False, kind="error")
    return ToolResult(msg if ok else f"Error: {msg}", ok=ok, kind="ok" if ok else "error")


def main(argv: list[str]) -> int:
    """Windows の Memory 画面の口: list / add <文> / remove <番号>。結果は JSON 1 行。"""
    cmd = argv[0] if argv else "list"
    ok, msg = True, ""
    try:
        if cmd == "add":
            ok, msg = add(" ".join(argv[1:]))
        elif cmd == "remove":
            ok, msg = remove(int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else None,
                             "" if len(argv) > 1 and argv[1].isdigit() else " ".join(argv[1:]))
        elif cmd != "list":
            ok, msg = False, f"unknown command: {cmd}"
    except OSError as e:
        ok, msg = False, str(e)
    print(json.dumps({"ok": ok, "message": msg, "items": items(), "path": str(path()), "t": int(time.time())},
                     ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
