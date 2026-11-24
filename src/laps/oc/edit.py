"""Edit の照合と置換 — opencode の `tool/edit.ts` をそのまま移したもの。

出典: refs/opencode/packages/opencode/src/tool/edit.ts:217-737（MIT・v1.18.26）。
opencode 自身も cline と gemini-cli の照合法を集めている（同ファイル冒頭の出典）。

9 段の照合（Simple → LineTrimmed → BlockAnchor → WhitespaceNormalized →
IndentationFlexible → EscapeNormalized → TrimmedBoundary → ContextAware →
MultiOccurrence）を順に試し、**ファイル中にちょうど 1 か所**だけ在る候補で置換する。
候補が複数か所に在る段は飛ばして次の段へ進む（opencode と同じ）。

引数名だけ Claude Code 形（old_string / new_string / replace_all）に合わせた。
"""
from __future__ import annotations

import re
from typing import Callable, Iterator

Replacer = Callable[[str, str], Iterator[str]]

SINGLE_CANDIDATE_SIMILARITY_THRESHOLD = 0.65
MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD = 0.65


class EditError(Exception):
    """置換できなかった理由（モデルにそのまま返す文）。"""


def levenshtein(a: str, b: str) -> int:
    if a == "" or b == "":
        return max(len(a), len(b))
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        ca = a[i - 1]
        for j in range(1, len(b) + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[len(b)]


def _span(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """行 start..end（両端含む）が content の中で占める文字範囲。"""
    s = sum(len(lines[k]) + 1 for k in range(start))
    e = s
    for k in range(start, end + 1):
        e += len(lines[k])
        if k < end:
            e += 1
    return s, e


def simple_replacer(content: str, find: str) -> Iterator[str]:
    yield find


def line_trimmed_replacer(content: str, find: str) -> Iterator[str]:
    original = content.split("\n")
    search = find.split("\n")
    if search and search[-1] == "":
        search.pop()
    for i in range(len(original) - len(search) + 1):
        if all(original[i + j].strip() == search[j].strip() for j in range(len(search))):
            s, e = _span(original, i, i + len(search) - 1)
            yield content[s:e]


def _middle_similarity(original: list[str], start: int, end: int,
                       search: list[str], *, early_exit: bool) -> float:
    block = end - start + 1
    n = min(len(search) - 2, block - 2)
    if n <= 0:
        return 1.0
    sim = 0.0
    for j in range(1, min(len(search) - 1, block - 1)):
        a = original[start + j].strip()
        b = search[j].strip()
        m = max(len(a), len(b))
        if m == 0:
            continue
        if early_exit:
            sim += (1 - levenshtein(a, b) / m) / n
            if sim >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
                break
        else:
            sim += 1 - levenshtein(a, b) / m
    return sim if early_exit else sim / n


def block_anchor_replacer(content: str, find: str) -> Iterator[str]:
    original = content.split("\n")
    search = find.split("\n")
    if len(search) < 3:
        return
    if search[-1] == "":
        search.pop()
    first = search[0].strip()
    last = search[-1].strip()
    size = len(search)
    max_delta = max(1, size // 4)
    candidates: list[tuple[int, int]] = []
    for i in range(len(original)):
        if original[i].strip() != first:
            continue
        for j in range(i + 2, len(original)):
            if original[j].strip() == last:
                if abs((j - i + 1) - size) <= max_delta:
                    candidates.append((i, j))
                break
    if not candidates:
        return
    if len(candidates) == 1:
        i, j = candidates[0]
        if _middle_similarity(original, i, j, search, early_exit=True) >= SINGLE_CANDIDATE_SIMILARITY_THRESHOLD:
            s, e = _span(original, i, j)
            yield content[s:e]
        return
    best, best_sim = None, -1.0
    for i, j in candidates:
        sim = _middle_similarity(original, i, j, search, early_exit=False)
        if sim > best_sim:
            best, best_sim = (i, j), sim
    if best is not None and best_sim >= MULTIPLE_CANDIDATES_SIMILARITY_THRESHOLD:
        s, e = _span(original, best[0], best[1])
        yield content[s:e]


def whitespace_normalized_replacer(content: str, find: str) -> Iterator[str]:
    def norm(t: str) -> str:
        return re.sub(r"\s+", " ", t).strip()

    nfind = norm(find)
    lines = content.split("\n")
    for line in lines:
        nline = norm(line)
        if nline == nfind:
            yield line
        elif nfind and nfind in nline:
            words = find.strip().split()
            if words:
                pattern = r"\s+".join(re.escape(w) for w in words)
                m = re.search(pattern, line)
                if m:
                    yield m.group(0)
    flines = find.split("\n")
    if len(flines) > 1:
        for i in range(len(lines) - len(flines) + 1):
            block = "\n".join(lines[i:i + len(flines)])
            if norm(block) == nfind:
                yield block


def indentation_flexible_replacer(content: str, find: str) -> Iterator[str]:
    def dedent(t: str) -> str:
        ls = t.split("\n")
        nonempty = [ln for ln in ls if ln.strip()]
        if not nonempty:
            return t
        mi = min(len(ln) - len(ln.lstrip()) for ln in nonempty)
        return "\n".join(ln if not ln.strip() else ln[mi:] for ln in ls)

    nfind = dedent(find)
    clines = content.split("\n")
    flen = len(find.split("\n"))
    for i in range(len(clines) - flen + 1):
        block = "\n".join(clines[i:i + flen])
        if dedent(block) == nfind:
            yield block


_UNESCAPE_RE = re.compile(r"\\(n|t|r|'|\"|`|\\|\n|\$)")
_UNESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"',
                 "`": "`", "\\": "\\", "\n": "\n", "$": "$"}


def _unescape(s: str) -> str:
    return _UNESCAPE_RE.sub(lambda m: _UNESCAPE_MAP.get(m.group(1), m.group(0)), s)


def escape_normalized_replacer(content: str, find: str) -> Iterator[str]:
    ufind = _unescape(find)
    if ufind in content:
        yield ufind
    lines = content.split("\n")
    flen = len(ufind.split("\n"))
    for i in range(len(lines) - flen + 1):
        block = "\n".join(lines[i:i + flen])
        if _unescape(block) == ufind:
            yield block


def multi_occurrence_replacer(content: str, find: str) -> Iterator[str]:
    start = 0
    while True:
        idx = content.find(find, start)
        if idx == -1:
            break
        yield find
        start = idx + len(find)


def trimmed_boundary_replacer(content: str, find: str) -> Iterator[str]:
    tfind = find.strip()
    if tfind == find:
        return
    if tfind in content:
        yield tfind
    lines = content.split("\n")
    flen = len(find.split("\n"))
    for i in range(len(lines) - flen + 1):
        block = "\n".join(lines[i:i + flen])
        if block.strip() == tfind:
            yield block


def context_aware_replacer(content: str, find: str) -> Iterator[str]:
    flines = find.split("\n")
    if len(flines) < 3:
        return
    if flines[-1] == "":
        flines.pop()
    clines = content.split("\n")
    first = flines[0].strip()
    last = flines[-1].strip()
    for i in range(len(clines)):
        if clines[i].strip() != first:
            continue
        for j in range(i + 2, len(clines)):
            if clines[j].strip() == last:
                block_lines = clines[i:j + 1]
                if len(block_lines) == len(flines):
                    matching = total = 0
                    for k in range(1, len(block_lines) - 1):
                        bl = block_lines[k].strip()
                        fl = flines[k].strip()
                        if bl or fl:
                            total += 1
                            if bl == fl:
                                matching += 1
                    if total == 0 or matching / total >= 0.5:
                        yield "\n".join(block_lines)
                        break
                break


REPLACERS: tuple[Replacer, ...] = (
    simple_replacer,
    line_trimmed_replacer,
    block_anchor_replacer,
    whitespace_normalized_replacer,
    indentation_flexible_replacer,
    escape_normalized_replacer,
    trimmed_boundary_replacer,
    context_aware_replacer,
    multi_occurrence_replacer,
)


def is_disproportionate_match(search: str, old: str) -> bool:
    old_lines = len(old.split("\n"))
    search_lines = len(search.split("\n"))
    if search_lines >= max(old_lines + 3, old_lines * 2):
        return True
    if old_lines == 1:
        return False
    return len(search.strip()) > max(len(old.strip()) + 500, len(old.strip()) * 4)


def replace(content: str, old: str, new: str, replace_all: bool = False) -> str:
    """opencode の `replace()`（edit.ts:681-725）。失敗は EditError で理由を返す。"""
    if old == new:
        raise EditError("No changes to apply: old_string and new_string are identical.")
    if old == "":
        raise EditError(
            "old_string cannot be empty when editing an existing file. Provide the exact text "
            "to replace, or use Write for an intentional full-file replacement.")
    not_found = True
    for replacer in REPLACERS:
        for search in replacer(content, old):
            idx = content.find(search)
            if idx == -1:
                continue
            not_found = False
            if is_disproportionate_match(search, old):
                raise EditError(
                    "Refusing replacement because the matched span is much larger than old_string. "
                    "Re-read the file and provide the full exact old_string for the intended replacement.")
            if replace_all:
                return content.replace(search, new)
            if idx != content.rfind(search):
                continue
            return content[:idx] + new + content[idx + len(search):]
    if not_found:
        raise EditError(
            "Could not find old_string in the file. It must match exactly, including whitespace, "
            "indentation, and line endings.")
    raise EditError(
        "Found multiple matches for old_string. Provide more surrounding context to make the "
        "match unique, or set replace_all to change every instance.")


def normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n")


def detect_line_ending(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def convert_to_line_ending(text: str, ending: str) -> str:
    return text if ending == "\n" else text.replace("\n", "\r\n")


# ── 書き換えた行だけを当てる（3 者の突き合わせ）────────────────────────────────
# ★2026-10-12 オーナー「Gemma4 用にエージェントをさらに強化して…タスク実行能力を向上」。
#   Gemma 4 の走行（10-10〜10-11・推論機に残った依頼の全文で数えた）で、Edit は 37 回中 15 回外れた。
#   15 回中 11 回は自分で Write したファイルを、9〜138 行の大きな塊を記憶から写して書き換えようとし、
#   行の 5〜10 割は実物と同じなのに、どこかが違って全体が一致しなかった（12 回は要約の後＝正確な形を覚えていない）。
#   手掛かり（いちばん近い所と最初に違う行）を返しても、違う所が何か所もあると次でまた外れる。
# ★やること: 変更前（old）→変更後（new）の差分から「書き換えたい行」だけを取り出し、その行が実物に
#   そのまま（空白の違いを除いて）在れば、そこだけを書き換える。記憶違いで合わなかった周りの行は実物のまま残す
#   （git の 3 者の突き合わせと同じ考え: 元＝old・こちら＝実物・あちら＝new）。
#   ★書き換えたい行が実物と合わない・当てる場所が決まらない・塊が散らばっている、のどれかなら当てない（None）。
#   ★黙って直さない: 何行が違っていて、何か所を当てたかを呼び出し側が結果に書く。
def merge_replace(content: str, old: str, new: str) -> tuple[str, dict] | None:
    import difflib

    o = old.split("\n")
    n = new.split("\n")
    if sum(1 for x in o if x.strip()) < 3:
        return None                                   # 小さい old は手掛かりを返すほうがよい（当て違いの害が大きい）
    c = content.split("\n")
    if len(c) > 50000:
        return None

    def norm(s: str) -> str:
        return " ".join(s.split())

    on = [norm(x) for x in o]
    cn = [norm(x) for x in c]
    sm = difflib.SequenceMatcher(None, on, cn, autojunk=False)
    omap: dict[int, int] = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            if on[a + k]:                              # 空行どうしの一致は位置の手掛かりにしない
                omap[a + k] = b + k
    nonempty = [i for i, x in enumerate(on) if x]
    if not omap or len(omap) < max(3, int(len(nonempty) * 0.6)):
        return None                                   # 実物と合う行が少なすぎる＝別の場所・別の中身
    lo, hi = min(omap.values()), max(omap.values())
    if hi - lo + 1 > len(o) * 2 + 10:
        return None                                   # 合う行がファイルじゅうに散らばっている＝当てる場所が決まらない

    hunks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, o, n, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i1 < i2:                                   # 置き換え・削除: 書き換えたい行が全部、実物に連続して在ること
            if any(i not in omap and on[i] for i in range(i1, i2)):
                return None
            idx = [omap[i] for i in range(i1, i2) if i in omap]
            if not idx:
                return None                           # 空行だけを消す変更は場所が決まらない
            f1, f2 = min(idx), max(idx) + 1
            if f2 - f1 > (i2 - i1) + 2:
                return None
            # 短い・ありふれた行（「}」など）だけを書き換えるときは、隣の行も実物と並んでいること
            if sum(len(on[i]) for i in range(i1, i2)) < 20:
                if not ((i1 - 1) in omap and omap[i1 - 1] == f1 - 1) and not (i2 in omap and omap[i2] == f2):
                    return None
        else:                                         # 挿入: 前か後ろの行で場所を決める
            if (i1 - 1) in omap:
                f1 = f2 = omap[i1 - 1] + 1
            elif i1 in omap:
                f1 = f2 = omap[i1]
            else:
                return None
        # 字下げ: 書き換える最初の行の字下げが old と実物で違えば、new の行の頭を実物の字下げへ載せ替える
        new_lines = n[j1:j2]
        if i1 < i2 and i1 in omap:
            lead_o = o[i1][: len(o[i1]) - len(o[i1].lstrip())]
            lead_c = c[omap[i1]][: len(c[omap[i1]]) - len(c[omap[i1]].lstrip())]
            if lead_o != lead_c:
                new_lines = [lead_c + ln[len(lead_o):] if ln.startswith(lead_o) else ln for ln in new_lines]
        hunks.append((f1, f2, new_lines))
    if not hunks:
        return None
    hunks.sort(key=lambda h: h[0])
    for a, b in zip(hunks, hunks[1:]):
        if a[1] > b[0]:
            return None                               # 当てる場所が重なる
    out = list(c)
    for f1, f2, nl in reversed(hunks):
        out[f1:f2] = nl
    differ = [i for i in nonempty if i not in omap or c[omap[i]] != o[i]]
    first = None
    if differ:
        d0 = differ[0]
        if d0 in omap:
            first = omap[d0] + 1
        else:                                         # 合わなかった行は、手前の合った行からの数で位置を見積もる
            prev = max((i for i in omap if i < d0), default=None)
            first = (omap[prev] + (d0 - prev) + 1) if prev is not None else lo + 1
    return "\n".join(out), {"hunks": len(hunks), "differ": len(differ), "first_line": first,
                            "region": (lo + 1, hi + 1)}
