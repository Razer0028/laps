"""バイト列を見る・バイト列で探す（Read の hex=true・Grep の bytes=true）。

2026-10-09 オーナー「hex 関連で Blackwall が悩んでいるみたいなのですが、改善できませんか」。
走行記録では、ファイルの頭の印やバイトの位置を確かめるたびに、モデルがその場で Python を書いて走らせていた
（Windows には xxd も od も無い・Format-Hex は大きいファイルで遅く出力も長い）。さらに、文字列の 16 進を
**記憶から**書いて誤っていた（`MOC3` を `213D4C54` と書いた。正しくは 4d 4f 43 33）。
⇒ 16 進・位置・エンディアンの読みは**道具が計算して見せる**。モデルに暗算させない。

  Read(file_path, hex=true, byte_offset=0, byte_length=256)
      → 位置（16 進と 10 進）・16 進・文字の並び（hexdump -C と同じ形）と、先頭の数値の読み
  Grep(pattern, path, bytes=true[, glob])
      → pattern は文字列（UTF-8 と UTF-16LE の両方で探す）か `hex:4d 4f 43 33`、または `\\x89PNG` の書き方。
        バイナリのファイルも探し、当たった位置と前後のバイト列を返す。

★読むだけ。書き換えはしない。大きいファイルは塊ごとに読む（全体をメモリに載せない）。
"""
from __future__ import annotations

import fnmatch
import os
import re
import struct
import time

from ..tools import ToolResult

DEFAULT_LENGTH = 256
MAX_LENGTH = 4096
MAX_HITS_SHOWN = 40
HITS_DETAILED = 12                  # 16 進の前後を付ける当たりの数（残りは 1 行）
MAX_HITS_COUNTED = 100_000
CHUNK = 8 * 1024 * 1024
SCAN_SECONDS = 90.0
SCAN_BYTES = 16 * 1024 * 1024 * 1024
CTX_BEFORE = 16
CTX_AFTER = 32
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".blackwall", ".laps"}   # .blackwall＝作業ログ・解析の控え（2026-10-11）


def _err(msg: str) -> ToolResult:
    return ToolResult("Error: " + msg, ok=False, kind="error")


def _printable(b: int) -> str:
    return chr(b) if 0x20 <= b < 0x7F else "."


def hexdump(data: bytes, base: int, *, mark: tuple[int, int] | None = None, marks: set[int] | None = None) -> str:
    """hexdump -C の形。mark=(開始, 長さ)・marks=位置の集合 は行の下の ^^ で示す（位置は絶対値）。"""
    # ★印は行の下の ^^（2026-10-09: 大文字にする形は、数字だけの 16 進 `00` `55` では見分けられなかった）
    lines = []
    start = base - (base % 16)
    end = base + len(data)
    for row in range(start, end, 16):
        cells, chars, carets = [], [], []
        for i in range(row, row + 16):
            if base <= i < end:
                b = data[i - base]
                cells.append(f"{b:02x}")
                chars.append(_printable(b))
                hit = (mark and mark[0] <= i < mark[0] + mark[1]) or (marks and i in marks)
                carets.append("^^" if hit else "  ")
            else:
                cells.append("  ")
                chars.append(" ")
                carets.append("  ")
        left, right = " ".join(cells[:8]), " ".join(cells[8:])
        lines.append(f"{row:08x}  {left}  {right}  |{''.join(chars)}|")
        if any(c == "^^" for c in carets):
            lines.append(f"{'':8}  {' '.join(carets[:8])}  {' '.join(carets[8:])}".rstrip())
    return "\n".join(lines)


def values_at(data: bytes) -> str:
    """先頭のバイトを数として読んだ値（ヘッダの版・大きさ・位置の読み違いを防ぐ）。"""
    out = []
    if len(data) >= 1:
        out.append(f"u8={data[0]}")
    if len(data) >= 2:
        out.append(f"u16le={struct.unpack_from('<H', data)[0]} u16be={struct.unpack_from('>H', data)[0]}")
    if len(data) >= 4:
        u32le, u32be = struct.unpack_from("<I", data)[0], struct.unpack_from(">I", data)[0]
        f32 = struct.unpack_from("<f", data)[0]
        f32s = f"{f32:.6g}" if f32 == f32 and abs(f32) < 1e30 else "-"
        out.append(f"u32le={u32le} (0x{u32le:08x}) u32be={u32be} (0x{u32be:08x}) i32le={struct.unpack_from('<i', data)[0]}"
                   f" f32le={f32s}")
    if len(data) >= 8:
        out.append(f"u64le={struct.unpack_from('<Q', data)[0]}")
    return "  ".join(out)


def read_bytes(path: str, offset, length) -> ToolResult:
    """Read(hex=true)。offset は 0 始まり（負なら終わりから）。"""
    if os.path.isdir(path):
        return _err(f"{path} is a directory (hex=true reads a file)")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return _err(f"Cannot read {path}: {e}")
    try:
        off = int(offset or 0)
        n = int(length or DEFAULT_LENGTH)
    except (TypeError, ValueError):
        return _err("byte_offset and byte_length must be integers")
    if off < 0:
        off = max(0, size + off)
    n = max(1, min(MAX_LENGTH, n))
    if off >= size:
        hint = ""
        try:
            with open(path, "rb") as fh:
                if fh.read(8) == b"UnityFS\x00":
                    hint = (" This is a UnityFS bundle whose data is compressed, so offsets inside its objects (as Read "
                            "path_id reports them) are not file offsets. To see an object's bytes, call Read with "
                            "path_id=<n> and hex=true (byte_offset is then counted from the start of the object).")
        except OSError:
            pass
        return _err(f"byte_offset {off} is past the end of the file ({size:,} bytes).{hint}")
    try:
        with open(path, "rb") as fh:
            fh.seek(off)
            data = fh.read(n)
    except OSError as e:
        return _err(f"Cannot read {path}: {e}")
    end = off + len(data)
    head = (f"<path>{path}</path>\n<type>bytes</type>\n"
            f"bytes {off:,}–{end - 1:,} (0x{off:x}–0x{end - 1:x}) of {size:,}\n"
            f"values at 0x{off:x}: {values_at(data)}\n<content>\n")
    tail = ("\n</content>" + (f"\n(Next: byte_offset={end})" if end < size else "\n(End of file)")
            + "\nThe hex and the values above were computed by the tool; use them instead of converting by hand.")
    return ToolResult(head + hexdump(data, off) + tail, ok=True, kind="ok",
                      data={"offset": off, "length": len(data), "size": size})


_HEX_ESC = re.compile(r"\\x([0-9a-fA-F]{2})")


def parse_pattern(p: str) -> list[tuple[bytes, str]] | str:
    """[(探すバイト列, 呼び名)]。読めなければエラーの文。"""
    s = p or ""
    if s[:4].lower() == "hex:":
        h = re.sub(r"[\s:,_-]|0x", "", s[4:], flags=re.I)
        if not h or len(h) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", h):
            return "hex: pattern must be pairs of hex digits (e.g. hex:4d4f4333 or hex:4d 4f 43 33)"
        return [(bytes.fromhex(h), "hex")]
    if _HEX_ESC.search(s):
        out = bytearray()
        i = 0
        while i < len(s):
            m = _HEX_ESC.match(s, i)
            if m:
                out.append(int(m.group(1), 16))
                i = m.end()
            else:
                out += s[i].encode("utf-8")
                i += 1
        return [(bytes(out), "bytes")]
    if not s:
        return "pattern is empty"
    u8 = s.encode("utf-8")
    pats = [(u8, "utf-8")]
    u16 = s.encode("utf-16-le")
    if u16 != u8:
        pats.append((u16, "utf-16le"))
    # ★日本語などを含む語は Shift-JIS・EUC-JP・UTF-16BE でも探す（2026-10-14: 走行が語を UTF-8・UTF-16LE・Shift-JIS の 3 通りで探すのに
    #   Python を書き、Windows の引用符で 2 回構文エラーになった。日本語のゲームや古い Windows のファイルは Shift-JIS が多い）。
    if any(ord(ch) > 127 for ch in s):
        for codec, label in (("cp932", "shift-jis"), ("euc_jp", "euc-jp"), ("utf-16-be", "utf-16be")):
            try:
                b = s.encode(codec)
            except UnicodeEncodeError:
                continue
            if b not in [x for x, _ in pats]:
                pats.append((b, label))
    return pats


# ── ファイルの中の文字列（Read の strings=true）──────────────────────────────────
#: ★2026-10-14: 走行が「全部の .sav から UTF-16LE の文字列を抜き出す」台本を書いていた（strings の代わり）。ASCII・UTF-8・UTF-16LE・
#:   Shift-JIS の文字列を位置つきで並べる。Shift-JIS と UTF-8（多バイト）は、かなか漢字を 2 字以上含むときだけ（でたらめなバイト列を拾わない）。
STRINGS_SCAN_MAX = 32 * 1024 * 1024
STRINGS_LINES_MAX = 400
_KANA = re.compile(r"[\u3040-\u30ff]")
_JA = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
_JA_PUNCT = set("、。・ー「」『』！？　（）…〜")
_ASCII_PUNCT = set(" .,:;!?-_()[]/'\"+=#@&%*<>{}|\\~$^`")


def _good_ja(t: str) -> bool:
    """日本語の文字列らしいか: かなが 2 字以上・9 割以上が日本語か普通の英数字と記号（でたらめなバイト列は CJK の字に化けるので、かなで見る）。"""
    if len(_KANA.findall(t)) < 2:
        return False
    ok = sum(1 for c in t if _JA.match(c) or c in _JA_PUNCT or (c.isascii() and (c.isalnum() or c in _ASCII_PUNCT)))
    return ok >= 0.9 * len(t)


def _good_ascii(t: str, min_len: int) -> bool:
    letters = sum(1 for c in t if c.isascii() and c.isalpha())
    return sum(1 for c in t if c.isascii()) >= 0.9 * len(t) and letters >= max(3, min_len - 1)


def _str_candidates(data: bytes, min_len: int, enc: str):
    """(位置, 終わり, 文字コード, 文字列) を出す。"""
    m = max(2, int(min_len))
    if enc in ("auto", "ascii"):
        for x in re.finditer(rb"[\x20-\x7e\t]{%d,}" % m, data):
            yield x.start(), x.end(), "ascii", x.group().decode("ascii")
    if enc in ("auto", "utf-16le"):
        for x in re.finditer(rb"(?:[\x20-\x7e]\x00|[\x00-\xff][\x30-\x9f]|[\x00-\xff]\xff){%d,}" % m, data):
            raw = x.group()
            if len(raw) % 2:
                raw = raw[:-1]
            try:
                t = raw.decode("utf-16-le")
            except UnicodeDecodeError:
                continue
            if t.isprintable() and (_good_ja(t) or _good_ascii(t, m)):
                yield x.start(), x.start() + len(raw), "utf-16le", t
    if enc in ("auto", "shift-jis"):
        for x in re.finditer(rb"(?:[\x81-\x9f\xe0-\xef][\x40-\x7e\x80-\xfc]|[\x20-\x7e]){%d,}" % m, data):
            try:
                t = x.group().decode("cp932")
            except UnicodeDecodeError:
                continue
            if _good_ja(t) and t.isprintable():
                yield x.start(), x.end(), "shift-jis", t
    if enc in ("auto", "utf-8"):
        for x in re.finditer(rb"(?:[\x20-\x7e]|[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}){%d,}" % m, data):
            try:
                t = x.group().decode("utf-8")
            except UnicodeDecodeError:
                continue
            if _good_ja(t) and t.isprintable():
                yield x.start(), x.end(), "utf-8", t


def strings_of(path: str, min_len=4, encoding="auto", offset=None, length=None) -> ToolResult:
    """Read(strings=true)。文字列を位置・文字コードつきで。重なる候補は日本語として読める物（utf-8・shift-jis・utf-16le）を ascii より先に。"""
    if os.path.isdir(path):
        return _err(f"{path} is a directory (strings=true reads a file; to search many files use Grep with bytes=true)")
    enc = str(encoding or "auto").lower().replace("_", "-")
    enc = {"sjis": "shift-jis", "cp932": "shift-jis", "shift_jis": "shift-jis", "utf16": "utf-16le",
           "utf-16": "utf-16le", "utf8": "utf-8"}.get(enc, enc)
    if enc not in ("auto", "ascii", "utf-8", "utf-16le", "shift-jis"):
        return _err("encoding must be auto, ascii, utf-8, utf-16le or shift-jis")
    try:
        size = os.path.getsize(path)
        start = int(offset or 0)
        if start < 0:
            start = max(0, size + start)
        n = int(length) if length not in (None, "") else STRINGS_SCAN_MAX
        n = max(1, min(n, STRINGS_SCAN_MAX))
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(n)
        min_len = max(2, int(min_len or 4))
    except (OSError, ValueError) as e:
        return _err(f"Cannot read {path}: {e}")
    rank = {"utf-8": 0, "shift-jis": 1, "utf-16le": 2, "ascii": 3}
    cands = sorted(_str_candidates(data, min_len, enc), key=lambda c: (c[0], rank[c[2]], -(c[1] - c[0])))
    out, counts, last_end = [], {}, -1
    for off, end, e, t in cands:
        if off < last_end:                          # 先に採った（日本語として読める）候補の内側
            continue
        last_end = end
        counts[e] = counts.get(e, 0) + 1
        if len(out) < STRINGS_LINES_MAX:
            out.append(f"0x{start + off:08x}  {e:<9} {t[:200]}")
    total = sum(counts.values())
    head = (f"{os.path.basename(path)}: {total} strings (min_length {min_len}, encoding {enc}) in bytes "
            f"{start:,}–{start + len(data):,} of {size:,}"
            + (" — by encoding: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) if counts else ""))
    more = []
    if total > len(out):
        more.append(f"(showing the first {len(out)}; narrow with byte_offset/byte_length, min_length or encoding)")
    if start + len(data) < size:
        more.append(f"(scanned up to byte {start + len(data):,}; pass byte_offset={start + len(data)} to go on)")
    return ToolResult("\n".join([head, *out, *more]) if out else head + "\nNo strings found.", ok=True)


def _files(target: str, include: str | None):
    if os.path.isfile(target):
        yield target
        return
    for root, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for f in sorted(files):
            full = os.path.join(root, f)
            if include:
                rel = os.path.relpath(full, target).replace("\\", "/")
                if not (fnmatch.fnmatch(f, include) or fnmatch.fnmatch(rel, include)):
                    continue
            yield full


def _is_compressed_bundle(path: str) -> bool:
    """UnityFS で、データのブロックが圧縮されているか（ブロック情報の圧縮の種類で見る・速い近似）。"""
    try:
        with open(path, "rb") as fh:
            if fh.read(8) != b"UnityFS\x00":
                return False
        from . import fileformats as _ff
        lay = _ff.bundle_layout(path, os.path.getsize(path))
        return any(k for k in (lay.get("kinds") or {}) if k not in (0, "無圧縮"))
    except Exception:  # noqa: BLE001
        return True


def _moc3_check(hits: list[tuple[str, int, int, str]]) -> str:
    """'MOC3' の当たりのうち、本物の .moc3 の頭（予約領域 58 バイトがゼロ）はどれか（modelfile.moc3_header_problem の注記）。"""
    from .modelfile import moc3_header_problem
    good, bad = [], 0
    for path, off, n, _name in hits:
        try:
            with open(path, "rb") as fh:
                fh.seek(off)
                head = fh.read(0x40)
        except OSError:
            continue
        if not head.startswith(b"MOC3"):
            continue                        # 文字列の中の .moc3 など（頭ではない）
        if moc3_header_problem(head):
            bad += 1
        else:
            good.append(f"{os.path.basename(path)} @ 0x{off:x}")
    if not good and not bad:
        return ""
    msg = (f"MOC3 header check (of the matches shown): {len(good)} look like a real .moc3 header "
           f"(version 1-15, endianness 0/1, the 58 reserved bytes 0x06-0x3F all zero)")
    if good:
        msg += ": " + ", ".join(good[:10]) + (" …" if len(good) > 10 else "")
    if bad:
        msg += (f". Not a .moc3 header: {bad} (reserved bytes not zero) — the 4 bytes 'MOC3' occurred by chance, "
                "typically inside compressed data. Do not cut those out as models")
    return msg + "."


_RE_ESC = re.compile(r"\\([.^$*+?()\[\]{}|/\\-])")
_RE_SYNTAX = re.compile(r"\.\*|\.\+|\.\?|\[[^\]]+\]|\(\?|\\[dswbDSWB]")


def literal_pattern(p: str) -> tuple[str, str]:
    """バイト検索は字どおり。正規表現の書き方で来たもの（`\\.model3\\.json`）はエスケープを外す。(探す文字列, 結果に添える注記)。

    ★2026-10-11 の見回り（0.1.387 の走行）: Grep(bytes=true) に `\\.model3\\.json` が渡り、バックスラッシュごと
      （5c 2e 6d …）探して当たらなかった。モデルは「無い」と読んで UnityPy の台本を書き続けた（Write 37・Bash 57）。"""
    s = p or ""
    if s[:4].lower() == "hex:":
        return s, ""
    t = _RE_ESC.sub(r"\1", s)
    notes = []
    if t != s:
        notes.append(f"Byte search is literal, not a regex: removed the regex escapes and searched for {t!r}.")
    m = _RE_SYNTAX.search(t)
    if m:
        notes.append(f"The pattern contains regex syntax ({m.group(0)!r}) that is searched as literal bytes — "
                     "search for a fixed part of the text instead (e.g. one name, or a file extension like .moc3).")
    return t, " ".join(notes)


def find_bytes(target: str, pattern: str, include: str | None = None, icase: bool = False) -> ToolResult:
    """Grep(bytes=true)。"""
    pattern, re_note = literal_pattern(pattern)
    pats = parse_pattern(pattern)
    if isinstance(pats, str):
        return _err(pats)
    if icase:
        pats = [(p.lower(), name) for p, name in pats]
    longest = max(len(p) for p, _ in pats)
    t0 = time.time()
    scanned = nfiles = 0
    hits: list[tuple[str, int, int, str]] = []
    counted = 0
    stopped = ""
    for path in _files(target, include):
        if time.time() - t0 > SCAN_SECONDS:
            stopped = f"stopped after {SCAN_SECONDS:.0f} s"
            break
        if scanned > SCAN_BYTES:
            stopped = f"stopped after {SCAN_BYTES >> 30} GiB"
            break
        try:
            fh = open(path, "rb")
        except OSError:
            continue
        nfiles += 1
        with fh:
            pos = 0
            carry = b""
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                scanned += len(chunk)
                buf = carry + chunk
                base = pos - len(carry)
                hay = buf.lower() if icase else buf
                for p, name in pats:
                    i = hay.find(p)
                    while i != -1:
                        # 前の塊の重なりで既に数えた位置は飛ばす
                        if i + len(p) > len(carry):
                            counted += 1
                            if len(hits) < MAX_HITS_SHOWN:
                                hits.append((path, base + i, len(p), name))
                        if counted >= MAX_HITS_COUNTED:
                            break
                        i = hay.find(p, i + 1)
                pos += len(chunk)
                carry = buf[-(longest - 1):] if longest > 1 else b""
                if counted >= MAX_HITS_COUNTED or time.time() - t0 > SCAN_SECONDS:
                    break
        if counted >= MAX_HITS_COUNTED:
            stopped = f"stopped at {MAX_HITS_COUNTED:,} hits"
            break
    secs = time.time() - t0
    desc = " / ".join(f"{p.hex(' ')} ({name})" for p, name in pats)
    head = [f"Byte search for {pattern!r} → {desc}" + ("  [ASCII case-insensitive]" if icase else ""),
            f"Scanned {nfiles} file(s), {scanned / 1048576:,.1f} MB in {secs:.1f} s"
            + (f" — {stopped}; the count below is a lower bound" if stopped else "")]
    if re_note:
        head.append(re_note)
    if not counted:
        head.append("No matches.")
        return ToolResult("\n".join(head), ok=True, kind="empty")
    hits.sort(key=lambda h: (h[0], h[1]))
    head.append(f"{counted:,} match(es)" + (f", showing the first {len(hits)}" if counted > len(hits) else "") + ":")
    out = ["\n".join(head)]
    # ★結果を短く（2026-10-11 の見回り: 当たりが多いと 1 回 2.8 万字になり、数分おきに文脈の要約が走っていた）。
    #   16 進の前後は最初の HITS_DETAILED 件だけ。残りは 1 行（位置と前後の文字）。同じファイルの当たりはパスを 1 回だけ書く。
    detailed = 0
    cur = None
    for path, off, n, name in hits:
        if path != cur:
            cur = path
            out.append(f"{path}:")
        a = max(0, off - CTX_BEFORE)
        try:
            with open(path, "rb") as fh:
                fh.seek(a)
                data = fh.read(off - a + n + CTX_AFTER)
        except OSError:
            data = b""
        if detailed < HITS_DETAILED:
            detailed += 1
            after = data[off - a + n: off - a + n + 8]
            out.append(f"@ 0x{off:x} ({off:,}) [{name}]\n"
                       + (f"  values right after the match: {values_at(after)}\n" if after else "")
                       + hexdump(data, a, mark=(off, n)))
        else:
            around = "".join(_printable(b) for b in data)
            out[-1] += f"\n@ 0x{off:x} ({off:,}) [{name}]  |{around}|"
    if len(hits) > HITS_DETAILED:
        out.append(f"Hex context is shown for the first {HITS_DETAILED} matches; the rest show the offset and the "
                   f"printable bytes around it ({CTX_BEFORE} before, {CTX_AFTER} after).")
    moc = _moc3_check(hits) if any(p.upper() == b"MOC3" for p, _ in pats) else ""
    if moc:
        out.append(moc)
    # ★圧縮されたバンドルの中の当たりは、LZ4 の生の並び（文字列の断片）に当たっているだけ（2026-10-11 の見回り: 前後の値を
    #   読もうとしてモデルが同じ検索を繰り返した）。展開した中身で探す道を示す。
    bundles = sorted({h[0] for h in hits if _is_compressed_bundle(h[0])})
    if bundles:
        out.append("Note: " + ", ".join(os.path.basename(b) for b in bundles[:5])
                   + (" is a compressed UnityFS bundle" if len(bundles) == 1 else " are compressed UnityFS bundles")
                   + ": matches there are fragments of compressed data, and the values around them mean nothing. "
                   "To search the decompressed objects, use Read with file_path=<bundle> and find=<text> "
                   "(it returns the objects that contain the text, with path_id).")
    out.append("Matched bytes are marked with ^^ under the row. Offsets and values were computed by the tool. "
               "Read(file_path, hex=true, byte_offset=…) shows more bytes around a match.")
    return ToolResult("\n\n".join(out), ok=True, kind="ok", data={"matches": counted, "files": nfiles})


# ── 地図（Read の map=true）: 区間ごとの性質と、中に埋め込まれていそうな形式 ─────────────
# 2026-10-09 オーナー「できるところまで最大限やってください」。走行記録では「どこが圧縮か」「中に何が入っているか」を
# モデルが台本で 1 つずつ探していた。エントロピー（乱雑さ）はファイル全体で 1 つしか出していなかった。

_SIGS: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "PNG"), (b"\xff\xd8\xff", "JPEG"), (b"GIF8", "GIF"), (b"RIFF", "RIFF"),
    (b"OggS\x00\x02", "Ogg"), (b"fLaC", "FLAC"), (b"ID3", "MP3(ID3)"), (b"FSB5", "FSB5"),
    (b"UnityFS\x00", "UnityFS"), (b"PK\x03\x04", "ZIP"), (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!\x1a\x07", "RAR"),
    (b"\x28\xb5\x2f\xfd", "zstd"), (b"\xfd7zXZ\x00", "xz"), (b"MOC3", "MOC3"), (b"SQLite format 3\x00", "SQLite"),
    (b"%PDF-", "PDF"), (b"\x7fELF", "ELF"), (b"BKHD", "Wwise bank"), (b"AKPK", "Wwise pck"), (b"CRID", "CRI USM"),
    (b"@UTF", "CRI @UTF"), (b"AFS2", "CRI AWB"), (b"GDPC", "Godot pck"), (b"XP3\r\n \n\x1a\x8b\x67\x01", "XP3"),
    (b"RPA-3.0 ", "RPA"), (b"DDS |", "DDS"), (b"\xabKTX 11\xbb", "KTX"), (b"OTTO", "OpenType"), (b"wOFF", "WOFF"),
    (b"\x1f\x8b\x08", "gzip"),
    (b"\x00\x01\x00\x00\x00\xff\xff\xff\xff\x01\x00\x00\x00\x00\x00\x00\x00", "BinaryFormatter"),
]
_SIG_RE = re.compile(b"|".join(re.escape(s) for s, _ in _SIGS))
_SIG_NAME = {s: n for s, n in _SIGS}
_MAP_MAX_REGIONS = 64
_MAP_SAMPLE = 256 * 1024
_MAP_LIST = 12


def _entropy(b: bytes) -> float:
    if not b:
        return 0.0
    import math
    counts = [0] * 256
    for x in b:
        counts[x] += 1
    n = len(b)
    return -sum(c / n * math.log2(c / n) for c in counts if c)


def _classify(b: bytes) -> tuple[str, float]:
    if not b:
        return "空", 0.0
    top = max(b.count(bytes([v])) for v in set(b[:4096])) if len(b) <= 4096 else max(
        b.count(bytes([v])) for v in set(b[:: max(1, len(b) // 4096)]))
    if top >= len(b) * 0.98:
        return f"埋め草（0x{b[0]:02x} が続く）", 0.0
    h = _entropy(b if len(b) <= 65536 else b[:: max(1, len(b) // 65536)])
    printable = sum(1 for x in b[:65536] if 32 <= x < 127 or x in (9, 10, 13)) / min(len(b), 65536)
    if printable >= 0.9:
        return "文字（ASCII）", h
    if h >= 7.6:
        return "圧縮か暗号化（ほぼ一様）", h
    if h >= 5.0:
        return "構造のあるデータ（数値・表・符号化された音や画像など）", h
    return "まばら・規則的なデータ", h


def _sig_ok(name: str, buf: bytes, i: int) -> bool:
    """短い印の偶然の一致を減らす（続くバイトが形式として成り立つか）。"""
    nxt = buf[i:i + 16]
    if name == "MOC3":                     # 予約領域 58 バイトのゼロまで見る（modelfile.moc3_header_problem の注記）
        from .modelfile import moc3_header_problem
        return not moc3_header_problem(buf[i:i + 0x40])
    if name == "RIFF":
        return len(nxt) >= 12 and nxt[8:12] in (b"WAVE", b"AVI ", b"WEBP", b"RMID", b"XWMA")
    if name == "gzip":
        return len(nxt) >= 10 and nxt[3] < 0x20 and nxt[9] in (0, 3, 7, 11, 255)
    if name == "JPEG":
        return len(nxt) >= 4 and nxt[3] in (0xE0, 0xE1, 0xDB, 0xEE, 0xC0, 0xC4)
    if name == "ID3":
        return len(nxt) >= 10 and nxt[3] in (2, 3, 4) and all(x < 0x80 for x in nxt[6:10])
    return True


def _sig_size(fh, name: str, off: int, fsize: int) -> int | None:
    """分かる形式は大きさの見当（読めなければ None）。"""
    try:
        if name == "RIFF":
            fh.seek(off + 4)
            return struct.unpack("<I", fh.read(4))[0] + 8
        if name == "UnityFS":
            fh.seek(off + 12)
            while fh.read(1) not in (b"\x00", b""):
                pass
            while fh.read(1) not in (b"\x00", b""):
                pass
            return struct.unpack(">q", fh.read(8))[0]
        if name == "FSB5":
            fh.seek(off + 12)
            shs, nts, ds = struct.unpack("<III", fh.read(12))
            return 0x3C + shs + nts + ds
        if name == "PNG":
            p = off + 8
            for _ in range(100000):
                fh.seek(p)
                h = fh.read(8)
                if len(h) < 8:
                    return None
                ln = struct.unpack(">I", h[:4])[0]
                if ln > fsize:
                    return None
                p += 12 + ln
                if h[4:8] == b"IEND":
                    return p - off
            return None
    except (OSError, struct.error):
        return None
    return None


def map_file(path: str) -> ToolResult:
    if os.path.isdir(path):
        return _err(f"{path} is a directory (map=true reads a file)")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return _err(f"Cannot read {path}: {e}")
    if size == 0:
        return ToolResult(f"<path>{path}</path>\n(空のファイル)", ok=True, kind="empty")
    t0 = time.time()
    win = max(64 * 1024, -(-size // _MAP_MAX_REGIONS))
    win = -(-win // 4096) * 4096
    regions: list[list] = []
    sampled = False
    found: dict[str, list[int]] = {}
    counts: dict[str, int] = {}
    stopped = ""
    with open(path, "rb") as fh:
        # 区間ごとの性質（大きい区間は頭の 256KB を見本にする）
        for start in range(0, size, win):
            fh.seek(start)
            n = min(win, size - start)
            b = fh.read(min(n, _MAP_SAMPLE))
            if n > _MAP_SAMPLE:
                sampled = True
            kind, h = _classify(b)
            if regions and regions[-1][2] == kind:
                regions[-1][1] = start + n
                regions[-1][3].append(h)
            else:
                regions.append([start, start + n, kind, [h]])
        # 埋め込まれていそうな形式（全体を塊ごとに走査）
        fh.seek(0)
        pos, carry = 0, b""
        # _sig_ok は印の後ろを最大 64 バイト見る（MOC3 の頭）。塊の終わりから 64 バイト以内の当たりは頭が切れて落ちるので、
        # 見ずに次の塊へ持ち越す（持ち越すのはその当たりの頭から後ろだけ＝同じ当たりを 2 度数えない）。
        tail = 0x40
        while True:
            if time.time() - t0 > SCAN_SECONDS:
                stopped = f"{SCAN_SECONDS:.0f} 秒で打ち切り（{pos:,} バイトまで）"
                break
            chunk = fh.read(CHUNK)
            if not chunk:
                break
            buf = carry + chunk
            base = pos - len(carry)
            last = pos + len(chunk) >= size
            for m in _SIG_RE.finditer(buf):
                i = m.start()
                if not last and i + tail > len(buf):
                    break
                name = _SIG_NAME[m.group()]
                if not _sig_ok(name, buf, i):
                    continue
                counts[name] = counts.get(name, 0) + 1
                lst = found.setdefault(name, [])
                if len(lst) < _MAP_LIST:
                    lst.append(base + i)
            pos += len(chunk)
            carry = b"" if last else buf[-(tail - 1):]
        lines = [f"<path>{path}</path>\n<type>map</type>\n大きさ {size:,} バイト・区間 {win:,} バイトごと"
                 + ("（大きい区間は頭の 256KB を見本にした）" if sampled else "")]
        lines.append("区間ごとの性質（似た区間はまとめた）:")
        for a, b_, kind, hs in regions[:80]:
            hmax = max(hs)
            lines.append(f"  0x{a:08x}–0x{b_ - 1:08x}（{_human(b_ - a)}）{kind}"
                         + (f"・乱雑さ {min(hs):.2f}〜{hmax:.2f}/8" if hmax else ""))
        if counts:
            lines.append("中に埋め込まれていそうな形式（印と、続くバイトが形式として成り立つものだけ。偶然の一致もありうる）:")
            for name, c in sorted(counts.items(), key=lambda x: -x[1]):
                items = []
                for off in found[name]:
                    sz = _sig_size(fh, name, off, size)
                    items.append(f"0x{off:x}" + (f"（{_human(sz)}）" if sz and 0 < sz <= size - off else ""))
                lines.append(f"  {name} {c:,} 個: " + ", ".join(items) + (" …" if c > len(items) else ""))
        else:
            lines.append("中に埋め込まれていそうな形式: 見つかりません")
    if stopped:
        lines.append(f"★{stopped}。数は下限です。")
    lines.append(f"（{time.time() - t0:.1f} 秒）→ 位置のバイトは Read の hex=true（byte_offset）で見られます。"
                 "圧縮か暗号化に見える区間は、生のバイト列を探しても中身は読めません。")
    return ToolResult("\n".join(lines), ok=True, kind="ok", data={"regions": len(regions), "signatures": counts})


def _human(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KB"
    return f"{n} バイト"


# ── 比較（Read の compare_with）: 2 つのファイルのどこが違うか ─────────────────────────
_CMP_RANGES = 20
_CMP_DUMPS = 4


def compare_files(a_path: str, b_path: str) -> ToolResult:
    for p in (a_path, b_path):
        if not os.path.isfile(p):
            return _err(f"Not a file: {p}")
    sa, sb = os.path.getsize(a_path), os.path.getsize(b_path)
    t0 = time.time()
    ranges: list[list[int]] = []          # [開始, 終わり（含まない）]
    ndiff = 0
    common = min(sa, sb)
    stopped = ""
    with open(a_path, "rb") as fa, open(b_path, "rb") as fb:
        pos = 0
        while pos < common:
            if time.time() - t0 > SCAN_SECONDS:
                stopped = f"{SCAN_SECONDS:.0f} 秒で打ち切り（{pos:,} バイトまで）"
                break
            n = min(1 << 20, common - pos)
            x, y = fa.read(n), fb.read(n)
            if x != y:
                for k in range(len(x)):
                    if x[k] != y[k]:
                        ndiff += 1
                        at = pos + k
                        if ranges and at - ranges[-1][1] <= 16:
                            ranges[-1][1] = at + 1
                        elif len(ranges) < 10_000:
                            ranges.append([at, at + 1])
            pos += n
        dumps = []
        for a, b_ in ranges[:_CMP_DUMPS]:
            lo = max(0, a - (a % 16) - 16)
            hi = min(common, b_ + 16, lo + 128)
            fa.seek(lo)
            fb.seek(lo)
            da, db = fa.read(hi - lo), fb.read(hi - lo)
            diff = {lo + k for k in range(min(len(da), len(db))) if da[k] != db[k]}
            dumps.append(f"0x{a:x}–0x{b_ - 1:x}:\n A {hexdump(da, lo, marks=diff).replace(chr(10), chr(10) + '   ')}"
                         f"\n B {hexdump(db, lo, marks=diff).replace(chr(10), chr(10) + '   ')}")
    lines = [f"<type>compare</type>\nA: {a_path}（{sa:,} バイト）\nB: {b_path}（{sb:,} バイト）"]
    if sa != sb:
        lines.append(f"大きさが違う: B は A より {sb - sa:+,} バイト" + ("（短い方の後ろは比べていない）"))
    if not ranges:
        lines.append("共通の長さの範囲は一致" + ("（大きさだけが違う）" if sa != sb else "＝同じ中身"))
    else:
        lines.append(f"違うバイト {ndiff:,} 個・違う区間 {len(ranges):,} か所（16 バイト以内の違いはまとめた）:")
        for a, b_ in ranges[:_CMP_RANGES]:
            lines.append(f"  0x{a:08x}–0x{b_ - 1:08x}（{b_ - a:,} バイト）")
        if len(ranges) > _CMP_RANGES:
            lines.append(f"  …ほか {len(ranges) - _CMP_RANGES:,} か所")
        if ndiff > common * 0.5 and common > 4096:
            lines.append("★半分以上が違う: 別物か、途中にバイトの挿入・削除があって後ろがずれている可能性。"
                         "最初の違いの位置の前後を Read の hex=true で見比べてください。")
        lines.append("最初の違い（行の下の ^^ が違うバイト）:")
        lines += dumps
    if stopped:
        lines.append(f"★{stopped}。数は下限です。")
    return ToolResult("\n".join(lines), ok=True, kind="ok", data={"differ": ndiff, "ranges": len(ranges)})
