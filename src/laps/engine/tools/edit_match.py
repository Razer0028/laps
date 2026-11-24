"""Edit の照合。opencode の tool/edit.ts の replacer（MIT）を Python へ移した。

厳密な一致から順に試し、同じ箇所が 1 つに決まったものを使う。"""
from __future__ import annotations

import re
from typing import Callable, Iterator


class EditError(Exception):
    pass


def _spans(content: str, start: int, end: int) -> str:
    lines = content.split("\n")
    a = sum(len(lines[k]) + 1 for k in range(start))
    b = a + sum(len(lines[k]) + (1 if k < end else 0) for k in range(start, end + 1))
    return content[a:b]


def simple(content: str, find: str) -> Iterator[str]:
    yield find


#: 字形だけが違う文字をそろえる（手本: Codex の apply-patch/src/seek_sequence.rs の normalise）。モデルは曲がった引用符や
#: 全角でないダッシュ・ノーブレークスペースを ASCII で書き直しがち。1 文字を 1 文字へ写すので、位置はそのまま元の文へ戻せる。
_PUNCT = str.maketrans({**{c: "-" for c in "‐‑‒–—―−"},
                        **{c: "'" for c in "‘’‚‛"},
                        **{c: '"' for c in "“”„‟"},
                        **{c: " " for c in "            　"}})


def punct_normalized(content: str, find: str) -> Iterator[str]:
    want = find.translate(_PUNCT)
    flat = content.translate(_PUNCT)
    if want == find and flat == content:
        return
    at = flat.find(want)
    if want and at >= 0 and flat.find(want, at + 1) < 0:   # そろえて 2 か所に当たるなら決めない（後の段と「複数」の答えに任せる）
        yield content[at:at + len(want)]


def line_trimmed(content: str, find: str) -> Iterator[str]:
    lines = content.split("\n")
    want = find.split("\n")
    if want and want[-1] == "":
        want.pop()
    if not want:
        return
    for i in range(0, len(lines) - len(want) + 1):
        if all(lines[i + j].strip() == want[j].strip() for j in range(len(want))):
            yield _spans(content, i, i + len(want) - 1)


def _levenshtein(a: str, b: str) -> int:
    if not a or not b:
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def block_anchor(content: str, find: str) -> Iterator[str]:
    want = find.split("\n")
    if len(want) < 3:
        return
    if want[-1] == "":
        want.pop()
    lines = content.split("\n")
    first, last, size = want[0].strip(), want[-1].strip(), len(want)
    delta = max(1, size // 4)
    cands: list[tuple[int, int]] = []
    for i, line in enumerate(lines):
        if line.strip() != first:
            continue
        for j in range(i + 2, len(lines)):
            if lines[j].strip() == last:
                if abs(j - i + 1 - size) <= delta:
                    cands.append((i, j))
                break
    best, best_sim = None, -1.0
    for i, j in cands:
        block = j - i + 1
        n = min(size - 2, block - 2)
        if n <= 0:
            sim = 1.0
        else:
            total = 0.0
            for k in range(1, min(size - 1, block - 1)):
                a, b = lines[i + k].strip(), want[k].strip()
                m = max(len(a), len(b))
                if m:
                    total += 1 - _levenshtein(a, b) / m
            sim = total / n
        if sim > best_sim:
            best, best_sim = (i, j), sim
    if best is not None and best_sim >= 0.65:
        yield _spans(content, best[0], best[1])


def whitespace_normalized(content: str, find: str) -> Iterator[str]:
    norm = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    want = norm(find)
    lines = content.split("\n")
    for line in lines:
        n = norm(line)
        if n == want:
            yield line
        elif want and want in n:
            pat = r"\s+".join(re.escape(w) for w in find.split())
            m = re.search(pat, line)
            if m:
                yield m.group(0)
    count = len(find.split("\n"))
    if count > 1:
        for i in range(0, len(lines) - count + 1):
            block = "\n".join(lines[i:i + count])
            if norm(block) == want:
                yield block


def indentation_flexible(content: str, find: str) -> Iterator[str]:
    def strip(text: str) -> str:
        ls = text.split("\n")
        body = [x for x in ls if x.strip()]
        if not body:
            return text
        m = min(len(x) - len(x.lstrip()) for x in body)
        return "\n".join(x[m:] if x.strip() else x for x in ls)
    want = strip(find)
    lines = content.split("\n")
    count = len(find.split("\n"))
    for i in range(0, len(lines) - count + 1):
        block = "\n".join(lines[i:i + count])
        if strip(block) == want:
            yield block


_ESC = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "`": "`", "\\": "\\", "\n": "\n", "$": "$"}


def escape_normalized(content: str, find: str) -> Iterator[str]:
    un = lambda s: re.sub(r"\\(n|t|r|'|\"|`|\\|\n|\$)", lambda m: _ESC[m.group(1)], s)  # noqa: E731
    want = un(find)
    if want in content:
        yield want
    lines = content.split("\n")
    count = len(want.split("\n"))
    for i in range(0, len(lines) - count + 1):
        block = "\n".join(lines[i:i + count])
        if un(block) == want:
            yield block


def trimmed_boundary(content: str, find: str) -> Iterator[str]:
    want = find.strip()
    if want == find:
        return
    if want in content:
        yield want
    lines = content.split("\n")
    count = len(find.split("\n"))
    for i in range(0, len(lines) - count + 1):
        block = "\n".join(lines[i:i + count])
        if block.strip() == want:
            yield block


def context_aware(content: str, find: str) -> Iterator[str]:
    want = find.split("\n")
    if len(want) < 3:
        return
    if want[-1] == "":
        want.pop()
    lines = content.split("\n")
    first, last = want[0].strip(), want[-1].strip()
    for i, line in enumerate(lines):
        if line.strip() != first:
            continue
        for j in range(i + 2, len(lines)):
            if lines[j].strip() == last:
                block = lines[i:j + 1]
                if len(block) == len(want):
                    same = total = 0
                    for k in range(1, len(block) - 1):
                        a, b = block[k].strip(), want[k].strip()
                        if a or b:
                            total += 1
                            same += a == b
                    if total == 0 or same / total >= 0.5:
                        yield "\n".join(block)
                        return
                break


def multi_occurrence(content: str, find: str) -> Iterator[str]:
    if find and find in content:
        yield find


REPLACERS: tuple[Callable[[str, str], Iterator[str]], ...] = (
    simple, punct_normalized, line_trimmed, block_anchor, whitespace_normalized, indentation_flexible,
    escape_normalized, trimmed_boundary, context_aware, multi_occurrence)


def _disproportionate(search: str, old: str) -> bool:
    a, b = old.count("\n") + 1, search.count("\n") + 1
    if b >= max(a + 3, a * 2):
        return True
    if a == 1:
        return False
    return len(search.strip()) > max(len(old.strip()) + 500, len(old.strip()) * 4)


def replace(content: str, old: str, new: str, replace_all: bool = False) -> str:
    if old == new:
        raise EditError("No changes to apply: old_string and new_string are identical.")
    if old == "":
        raise EditError("old_string cannot be empty when editing an existing file. Give the exact text to replace, "
                        "or use Write to create a file.")
    not_found = True
    for rep in REPLACERS:
        for search in rep(content, old):
            at = content.find(search)
            if at < 0 or not search:
                continue
            not_found = False
            if _disproportionate(search, old):
                raise EditError("Refusing the replacement: the matched text is much larger than old_string. "
                                "Read the file again and give the exact text.")
            if replace_all:
                return content.replace(search, new)
            if content.rfind(search) != at:
                continue
            return content[:at] + new + content[at + len(search):]
    if not_found:
        raise EditError("NOT_FOUND")
    raise EditError("Found several matches for old_string. Give more surrounding lines so that the match is unique, "
                    "or use replace_all to change every one.")
