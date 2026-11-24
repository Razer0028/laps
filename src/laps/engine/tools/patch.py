"""ApplyPatch: 1 回で複数のファイルを変える差分の道具（2026-11-11・オーナー了承）。
手本: Codex の apply-patch（codex-rs/apply-patch・Apache-2.0）。形は Codex と同じ:

    *** Begin Patch
    *** Update File: src/app.py
    @@ def main():
    -    print("hi")
    +    print("hello")
    *** Add File: notes.txt
    +first line
    *** Delete File: old.txt
    *** End Patch

- 読み取りは緩く（前後の空白・`<<'EOF'` の包み・最初の塊の @@ 無し・空行は空の文脈行）。
- 当て方は 4 段（そのまま → 行末の空白を無視 → 前後の空白を無視 → 字形をそろえる）。塊は前から順に探す。
- **全部のファイルを当ててから書く**。1 つでも外れたら何も書かない（Codex と同じ）。
- 外れたときは、Codex と違い、ファイルの中で一番近い所を見せる（Edit と同じ _hint）。
Edit は残す（1 か所の置き換えはそちらが短い）。どちらを使うかはモデルに任せる。
"""
from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field

from ...policy import PolicyDenied, authorize_path
from . import Ctx, Result, Tool, fail
from .edit_match import _PUNCT

BEGIN, END = "*** Begin Patch", "*** End Patch"
ADD, DELETE, UPDATE, MOVE, EOF_MARK = "*** Add File: ", "*** Delete File: ", "*** Update File: ", "*** Move to: ", "*** End of File"
_HEREDOC = re.compile(r"^\s*(?:apply_?patch\s*)?<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*)\n\s*\1\s*$", re.S)
#: Bash に書かれた `apply_patch <<'EOF' … EOF`（と `cd 場所 && …`）。Codex で覚えたモデルはこの形で書く。
SHELL_FORM = re.compile(r"^\s*(?:cd\s+(\"[^\"]+\"|'[^']+'|\S+)\s*&&\s*)?(?:apply_patch|applypatch)\s+<<-?\s*['\"]?(\w+)['\"]?\s*\n(.*)\n\s*\2\s*$", re.S)


class PatchError(Exception):
    pass


@dataclass
class Chunk:
    anchor: str = ""                       # @@ の後ろの 1 行（無ければ空）
    old: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    ops: list[tuple[str, str]] = field(default_factory=list)   # (' ' / '-' / '+', 行) の並び。文脈の行は元のファイルの行を残す
    eof: bool = False
    restart: bool = False                  # 同じファイルの 2 つ目の節の頭＝ファイルの頭から探し直す（2026-11-17）


@dataclass
class Hunk:
    kind: str                              # add / delete / update
    path: str
    move_to: str = ""
    lines: list[str] = field(default_factory=list)      # add の中身
    chunks: list[Chunk] = field(default_factory=list)


def parse(text: str) -> list[Hunk]:
    text = text.replace("\r\n", "\n").strip("\n")
    m = _HEREDOC.match(text)
    if m:
        text = m.group(2)
    lines = text.strip().split("\n")
    if not lines or lines[0].strip() != BEGIN:
        raise PatchError(f"The first line of the patch must be '{BEGIN}'.")
    if lines[-1].strip() != END:
        raise PatchError(f"The last line of the patch must be '{END}'.")
    hunks: list[Hunk] = []
    body = lines[1:-1]
    i = 0
    while i < len(body):
        line = body[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if line.startswith(ADD):
            h = Hunk("add", line[len(ADD):].strip())
            i += 1
            while i < len(body) and not body[i].startswith("*** "):
                if body[i].startswith("+"):
                    h.lines.append(body[i][1:])
                elif body[i].strip():
                    raise PatchError(f"Line {i + 2} in Add File {h.path!r} must start with '+': {body[i][:80]!r}")
                else:
                    h.lines.append("")
                i += 1
            hunks.append(h)
            continue
        if line.startswith(DELETE):
            hunks.append(Hunk("delete", line[len(DELETE):].strip()))
            i += 1
            continue
        if line.startswith(UPDATE):
            h = Hunk("update", line[len(UPDATE):].strip())
            i += 1
            if i < len(body) and body[i].startswith(MOVE):
                h.move_to = body[i][len(MOVE):].strip()
                i += 1
            cur: Chunk | None = None
            while i < len(body) and not (body[i].startswith("*** ") and not body[i].startswith(EOF_MARK)):
                ln = body[i]
                if ln.startswith("@@"):
                    if cur is not None and (cur.old or cur.new):
                        h.chunks.append(cur)
                    cur = Chunk(anchor=ln[2:].strip())
                elif ln.strip() == EOF_MARK:
                    if cur is not None:
                        cur.eof = True
                else:
                    if cur is None:                         # 最初の塊は @@ が無くてよい
                        cur = Chunk()
                    tag, rest = (ln[:1], ln[1:]) if ln else (" ", "")
                    if tag == " ":
                        cur.old.append(rest)
                        cur.new.append(rest)
                    elif tag == "-":
                        cur.old.append(rest)
                    elif tag == "+":
                        cur.new.append(rest)
                    else:
                        raise PatchError(f"Unexpected line in Update File {h.path!r}: {ln[:80]!r}. Every line must start "
                                         "with ' ' (context), '-' (removed) or '+' (added).")
                    cur.ops.append((tag, rest))
                i += 1
            if cur is not None and (cur.old or cur.new):
                h.chunks.append(cur)
            if not h.chunks and not h.move_to:
                raise PatchError(f"Update File {h.path!r} has no changes.")
            hunks.append(h)
            continue
        raise PatchError(f"{line[:80]!r} is not a valid hunk header. Use '*** Add File: path', '*** Delete File: path' or "
                         "'*** Update File: path'.")
    if not hunks:
        raise PatchError("The patch is empty.")
    # ★同じファイルの Update File が 2 つ以上あれば 1 つにまとめる（2026-11-17・記録で 1 手番を断って書き直させていた）。
    #   後の節は前の節と順番が逆のことがあるので、その頭の塊はファイルの頭から探す（restart）。
    #   追加・削除・移動が混ざるときは意味が決まらないので、これまでどおり断る。
    merged: list[Hunk] = []
    first: dict[str, Hunk] = {}
    for h in hunks:
        prev = first.get(h.path)
        if prev is None:
            first[h.path] = h
            merged.append(h)
            continue
        if prev.kind == h.kind == "update" and not prev.move_to and not h.move_to and h.chunks:
            h.chunks[0].restart = True
            prev.chunks.extend(h.chunks)
            continue
        raise PatchError(f"Two sections change {h.path!r}; put all changes to one file in one Update File section.")
    return merged


def _seek(lines: list[str], want: list[str], start: int, eof: bool) -> int:
    """want を lines の start 以降で探す（4 段）。見つからなければ -1。"""
    if not want:
        return start
    n = len(want)
    tries = (lambda s: s, lambda s: s.rstrip(), lambda s: s.strip(), lambda s: s.strip().translate(_PUNCT))
    for norm in tries:
        w = [norm(x) for x in want]
        if eof:                                              # *** End of File: まずファイルの末尾に当てる
            i = len(lines) - n
            if i >= start and all(norm(lines[i + k]) == w[k] for k in range(n)):
                return i
        for i in range(start, len(lines) - n + 1):
            if all(norm(lines[i + k]) == w[k] for k in range(n)):
                return i
    return -1


def apply_chunks(text: str, chunks: list[Chunk], path: str) -> str:
    lines = text.split("\n")
    trailing = bool(lines) and lines[-1] == ""
    if trailing:
        lines = lines[:-1]
    at = 0
    for c in chunks:
        if c.restart:
            at = 0
        if c.anchor:
            k = _seek(lines, [c.anchor], at, False)
            if k < 0:
                raise PatchError(f"Failed to find the context line {c.anchor!r} in {path}.")
            at = k + 1
        old, ops = list(c.old), list(c.ops)
        if not old:                                          # 足すだけ: 目印の直後か、目印が無ければ末尾
            pos = at if c.anchor else len(lines)
            lines[pos:pos] = list(c.new)
            at = pos + len(c.new)
            continue
        k = _seek(lines, old, at, c.eof)
        if k < 0 and old and old[-1] == "":                  # 末尾の空行の有無だけの違い
            old = old[:-1]
            last = max(i for i, (t, _) in enumerate(ops) if t in " -")
            ops = ops[:last] + ops[last + 1:]
            k = _seek(lines, old, at, c.eof)
        if k < 0:
            raise PatchError(f"Failed to find these lines in {path}:\n" + "\n".join(c.old[:12]))
        out: list[str] = []
        j = k
        for tag, line in ops:                                # 文脈の行は元の行（行末の空白・字形）を残す
            if tag == " ":
                out.append(lines[j])
                j += 1
            elif tag == "-":
                j += 1
            else:
                out.append(line)
        lines[k:k + len(old)] = out
        at = k + len(out)
    # ★2026-11-16 全体のレビュー: 上の for の変数が text を上書きし、最後の行が空の変更で、末尾に改行の無いファイルへ改行を足していた。
    return "\n".join(lines) + ("\n" if trailing or not lines else "")


def apply_patch(text: str, ctx: Ctx, base: str = "") -> Result:
    """差分を当てる。全部当ててから書く。"""
    from . import files
    try:
        hunks = parse(text)
    except PatchError as e:
        return fail(f"Patch not applied: {e}", head="ApplyPatch")
    plans: list[tuple[str, str, bytes | None, str]] = []    # (種類, パス, 書く中身, 動かす先)
    counts: dict[str, tuple[int, int]] = {}                  # パス → (+行, −行)
    root = base or ctx.workspace

    def where(p: str) -> str:
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(root, p))
    for h in hunks:
        path = where(h.path)
        try:
            authorize_path(path, write=True)
            if h.move_to:
                authorize_path(where(h.move_to), write=True)
        except PolicyDenied as e:
            return fail(str(e), kind="denied", head="ApplyPatch")
        if h.kind == "add":
            if os.path.exists(path):
                return fail(f"Patch not applied: {h.path} already exists. Use '*** Update File:' to change it.", head="ApplyPatch")
            plans.append(("A", path, ("\n".join(h.lines) + "\n").encode("utf-8"), ""))
            counts[path] = (len(h.lines), 0)
        elif h.kind == "delete":
            if not os.path.isfile(path):
                return fail(f"Patch not applied: {h.path} does not exist, so it cannot be deleted.", head="ApplyPatch")
            plans.append(("D", path, None, ""))
            try:
                counts[path] = (0, files.read_text(path)[0].count("\n"))
            except OSError:
                counts[path] = (0, 0)
        else:
            if not os.path.isfile(path):
                return fail(f"Patch not applied: {h.path} does not exist. Use '*** Add File:' to create it.", head="ApplyPatch")
            try:
                body, enc, bom = files.read_text(path)
            except OSError as e:
                return fail(f"Patch not applied: could not read {h.path}: {e}", head="ApplyPatch")
            if not files._round_trips(path, body, enc, bom):
                return fail(f"Patch not applied: {h.path} is not UTF-8, UTF-16 or Shift_JIS text.", head="ApplyPatch")
            crlf = "\r\n" in body and "\n" not in body.replace("\r\n", "")
            work = body.replace("\r\n", "\n") if crlf else body
            try:
                changed = apply_chunks(work, h.chunks, h.path)
            except PatchError as e:
                hint = ""
                missing = next((c for c in h.chunks if c.old), None)
                if missing is not None and "Failed to find these lines" in str(e):
                    hint = files._hint(work, "\n".join(missing.old)) + files.CODE_LOOKUP
                return fail(f"Patch not applied (nothing was changed): {e}{hint}", head="ApplyPatch")
            try:
                data = files.encode_text(changed.replace("\n", "\r\n") if crlf else changed, enc, bom)
            except UnicodeEncodeError as e:
                return fail(f"Patch not applied: {h.path} is {enc}, which cannot hold {e.object[e.start]!r}.", head="ApplyPatch")
            plans.append(("M", path, data, where(h.move_to) if h.move_to else ""))   # BOM は encode_text が付ける（UTF-16 も）
            counts[path] = _count(work, changed)
    done: list[str] = []
    names: list[str] = []
    for kind, path, data, move in plans:                    # ここから書く（当てるのは全部済んだ）
        try:
            files._note_change(ctx, path)
            if kind == "D":
                files._backup(ctx, path)
                os.unlink(path)
            else:
                if kind == "M":
                    files._backup(ctx, path)
                os.makedirs(os.path.dirname(move or path) or ".", exist_ok=True)
                if move:
                    files._note_change(ctx, move)
                files.write_atomic(move or path, data or b"")
                if move and move != path:
                    os.unlink(path)
                target = move or path
                ctx.read_files.add(target)
                files.remember(ctx, target)
                ctx.written.append(target)
                if kind == "A":
                    ctx.created.append(target)
            plus, minus = counts.get(path, (0, 0))
            done.append(f"{kind} {files.short(ctx, move or path)} (+{plus} -{minus})")
            names.append(os.path.basename(move or path))
        except OSError as e:
            return fail("Patch partly applied: " + ("; ".join(done) or "nothing") + f". Then {path}: {e}", head="ApplyPatch")
    plus = sum(a for a, _ in counts.values())
    minus = sum(b for _, b in counts.values())
    head = ", ".join(names[:3]) + (f" … +{len(names) - 3}" if len(names) > 3 else "")
    return Result("Success. Updated the following files:\n" + "\n".join(done), head=head or "ApplyPatch",
                  tail=f"{len(done)} files +{plus} -{minus}")


def _count(before: str, after: str) -> tuple[int, int]:
    plus = minus = 0
    for line in difflib.unified_diff(before.split("\n"), after.split("\n"), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return plus, minus


def enabled() -> bool:
    """道具の一覧に載せるか（Bash の apply_patch の読み替えは、外しても残す）。"""
    return (os.environ.get("LAPS_APPLY_PATCH") or "1").strip().lower() not in ("0", "off", "false", "no")


def run(inp: dict, ctx: Ctx) -> Result:
    text = str(inp.get("patch") or inp.get("input") or "")
    if not text.strip():
        return fail("patch is required")
    return apply_patch(text, ctx)


APPLY_PATCH = Tool(
    "ApplyPatch",
    "Change one or more files with one patch. Format: '*** Begin Patch', then sections '*** Update File: <path>' (lines "
    "starting with ' ' are unchanged context, '-' removed, '+' added; start a block with '@@' or '@@ <a nearby line>'), "
    "'*** Add File: <path>' (every line starts with '+') or '*** Delete File: <path>', then '*** End Patch'. Give 2-3 "
    "context lines around each change. Every file is checked before anything is written; if one does not match, nothing "
    "changes. For one small replacement Edit is shorter.",
    {"patch": {"type": "string", "description": "The whole patch, from *** Begin Patch to *** End Patch"}},
    run, required=("patch",), effect=True)
