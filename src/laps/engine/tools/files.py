"""ファイルの道具（Read・Write・Edit・Glob・Grep）。手本: opencode の tool/read.ts・edit.ts・glob.ts・grep.ts。"""
from __future__ import annotations

import difflib
import errno
import fnmatch
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ...policy import PolicyDenied, authorize_path, authorize_read


def keep_image(path: str, ctx) -> str:
    """読んだ時点の画像の写し（会話の shots/）。★2026-11-16 全体のレビュー: 元の場所のまま覚えると、手番ごとにそこから読み直すので、
    描き直した後は前の画像の所まで新しい中身に替わり（前後を比べられない）、そこから先を毎回読み直していた。消すと文字に化けた。
    写せなければ空（元の場所を使う）。"""
    import uuid
    try:
        if os.path.getsize(path) > 20 * 1024 * 1024:
            return ""
        d = Path(ctx.root) / "shots"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / f"{uuid.uuid4().hex[:12]}{os.path.splitext(path)[1].lower()}"
        shutil.copyfile(path, dst)
        return str(dst)
    except (OSError, TypeError):
        return ""
from . import Ctx, Result, Tool, clip, fail, limit_bytes
from .edit_match import EditError, replace

_UMASK = os.umask(0)
os.umask(_UMASK)
MAX_LINE = 2000
READ_LINES = 2000
READ_BYTES = 50 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target", ".cache", ".next"}
BINARY_EXT = {".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".class", ".jar", ".war", ".7z", ".doc", ".docx", ".xls",
              ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".bin", ".dat", ".obj", ".o", ".a", ".lib", ".wasm",
              ".pyc", ".pyo", ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".avi", ".mov", ".webm", ".fbx",
              ".glb", ".pmx", ".vrm", ".unity3d", ".assets", ".pak", ".bundle"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
#: Read のバイナリの見方（中身は blackwall/oc の解析の部品。ゲームのバンドルや .NET の DLL も読める）。
BINARY_VIEWS = ("hex", "map", "strings", "find", "path_id", "compare_with")


def _fix_typo(path: str, workspace: str) -> str:
    """作業場の場所を 1〜2 文字写し間違えた絶対パスを直す（2026-10-30 実測: North が長い場所の英数字に "-" を 1 つ足して
    別の場所へ書き、59 手番「ファイルが無い」と探し続けた）。打ち間違えた親が実在しないときだけ直す。"""
    ws = workspace.rstrip("/\\")
    if len(ws) < 8 or path == ws or path.startswith((ws + "/", ws + "\\")):
        return path
    for k in range(max(1, len(ws) - 3), min(len(path), len(ws) + 4) + 1):
        if k < len(path) and path[k] not in "/\\":
            continue
        prefix = path[:k]
        # ★親が実在するなら新しく作る隣のフォルダ（myapp-v1 の作業中の myapp-v2）で、打ち間違いではない
        if (prefix != ws and not os.path.exists(prefix) and not os.path.exists(os.path.dirname(prefix))
                and difflib.SequenceMatcher(None, prefix, ws).ratio() >= 0.96):
            return ws + path[k:]
    return path


def _fix_lookalike(path: str, mode: str) -> str:
    """全角空白などで書き写し違えたパスを、実在のパスに直す（候補が 1 つのときだけ）。Write は親フォルダだけ直す。"""
    if os.path.exists(path if mode == "existing" else (os.path.dirname(path) or ".")):
        return path
    try:
        from ...tools import pathfix
        return pathfix.fix_for(path, mode=mode) or path
    except Exception:
        return path


#: 行が見つからなかった編集の後ろに添える一言（2026-11-17・記録で ApplyPatch の失敗 37/207 の多くが「その行が無い」＝記憶から書いていた）。
CODE_LOOKUP = ("\nDo not retype it from memory: look the place up first — Tools(name=\"Code\", input={\"action\": \"outline\", "
               "\"path\": \"<file>\"}) or {\"action\": \"definition\", \"name\": \"<function or class>\"} — then Read those "
               "lines and copy them exactly.")


def _diagnostics(path: str, text: str) -> str:
    """書いた後の検査（構文・Web の参照切れ・Python の未定義の名前・プロジェクト自身の linter）。書き込みは止めない。"""
    try:
        from ...oc.tools import _syntax_note
        return _syntax_note(path, text)
    except Exception:
        return ""


def resolve(ctx: Ctx, path: str, mode: str = "existing") -> str:
    p = str(path or "").strip().strip('"')
    if p.startswith("\\\\?\\"):
        p = p[4:]
    if os.name == "nt":
        m = re.match(r"^/([a-zA-Z])(/.*)?$", p)            # Git Bash の形（/c/Users）
        if m:
            p = m.group(1).upper() + ":" + (m.group(2) or "/")
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(ctx.workspace, p)
    return _fix_lookalike(_fix_typo(os.path.normpath(p), ctx.workspace), mode)


def short(ctx: Ctx, path: str) -> str:
    try:
        rel = os.path.relpath(path, ctx.workspace)
        return path if rel.startswith("..") else rel
    except ValueError:
        return path


_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


def bom_of(data: bytes) -> tuple[bytes, str]:
    """先頭の BOM と、それが示す文字コード（無ければ (b"", "")）。"""
    for mark, enc in _BOMS:
        if data.startswith(mark):
            return mark, enc
    return b"", ""


def encode_text(text: str, enc: str, bom: bool) -> bytes:
    """read_text で読んだ形（文字コードと BOM）のまま、書くバイトにする。"""
    mark = next((m for m, e in _BOMS if e == enc), b"") if bom else b""
    return mark + text.encode(enc)


def read_text(path: str) -> tuple[str, str, bool]:
    """(本文, 文字コード, BOM があるか)。改行はそのまま。"""
    data = Path(path).read_bytes()
    mark, benc = bom_of(data)
    if mark:                               # ★UTF-16 の BOM も見る（2026-11-16 全体のレビュー: UTF-16 のファイルを cp932 として読み、書き戻すと壊していた）
        return data[len(mark):].decode(benc, "replace"), benc, True
    for enc in ("utf-8", "cp932"):
        try:
            return data.decode(enc), enc, False
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace"), "utf-8", False


def _round_trips(path: str, text: str, enc: str, bom: bool) -> bool:
    """読んだ文字列から元のバイトへ戻せるか（戻せないファイルを書き戻すと、読めなかったバイトが壊れる）。"""
    try:
        return encode_text(text, enc, bom) == Path(path).read_bytes()
    except (OSError, UnicodeError):
        return False


def write_atomic(path: str, data: bytes) -> None:
    """一時ファイルから差し替える。差し替えられない場所（Docker の bind mount・別のファイルシステム）はその場に書く。"""
    if os.path.islink(path):           # リンクそのものを差し替えない（普通のファイルになり、リンク先は直らない）
        path = os.path.realpath(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".bw-", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            st = os.stat(path)
            if hasattr(os, "chown"):                           # root で直しても持ち主を変えない
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except OSError:
                    pass
            os.chmod(tmp, st.st_mode & 0o7777)                 # 書き換えは元の権限のまま
        except OSError:
            try:
                os.chmod(tmp, 0o666 & ~_UMASK)                 # 新しいファイルはふつうの権限（一時ファイルの 0600 を残さない）
            except OSError:
                pass
        try:
            os.replace(tmp, path)
        except OSError as e:
            if e.errno not in (errno.EBUSY, errno.EXDEV, errno.EPERM, errno.EACCES):
                raise
            with open(path, "wb") as fh:
                fh.write(data)
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stamp(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def remember(ctx: Ctx, path: str) -> None:
    """いまのファイルの指紋を覚える（読んだ・書いた直後）。手本: Qwen Code の FileReadCache（mtime と大きさのずれだけを見る）。"""
    try:
        ctx.seen[path] = _stamp(path)
    except OSError:
        pass


def _drifted(ctx: Ctx, path: str) -> bool:
    """最後に読んだ・書いた後でファイルが変わったか（コマンド・背景の仕事・人が書き換えた）。覚えていないファイルは見ない。"""
    if (os.environ.get("LAPS_STALE_GUARD") or "1").strip().lower() in ("0", "off", "false", "no"):
        return False
    old = ctx.seen.get(path)
    if old is None:
        return False
    try:
        return _stamp(path) != old
    except OSError:
        return False


def _note_change(ctx: Ctx, path: str) -> None:
    """この依頼の取り消しの控え（engine/undo.py）。最初の 1 回だけ元の中身を残す。失敗しても書き込みは止めない。"""
    rc = getattr(ctx, "changes", None)
    if rc is not None:
        try:
            rc.record(path)
        except Exception:
            pass


def _backup(ctx: Ctx, path: str) -> None:
    """書き換える前の中身を会話の置き場に残す（戻したいときのため）。"""
    try:
        d = ctx.root / "file-history"
        d.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%H%M%S-") + re.sub(r"[^\w.-]+", "_", path)[-120:]
        shutil.copy2(path, d / name)
    except OSError:
        pass


def _is_binary(path: str, sample: bytes) -> bool:
    if os.path.splitext(path)[1].lower() in BINARY_EXT:
        return True
    mark, enc = bom_of(sample)
    if enc.startswith("utf-16"):           # UTF-16 の文字は 0 のバイトを含む＝BOM があれば文字として読む
        try:
            sample[len(mark):len(mark) + (len(sample) - len(mark)) // 2 * 2].decode(enc)
            return False
        except UnicodeDecodeError:
            pass
    if b"\0" in sample:
        return True
    if not sample:
        return False
    odd = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return odd / len(sample) > 0.3


def _miss(path: str) -> str:
    d, base = os.path.split(path)
    near: list[str] = []
    try:
        low = base.lower()
        near = [os.path.join(d, x) for x in os.listdir(d) if low in x.lower() or x.lower() in low][:3]
    except OSError:
        pass
    msg = f"File not found: {path}"
    if near:
        msg += "\n\nDid you mean one of these?\n" + "\n".join(near)
    return msg


def read(inp: dict, ctx: Ctx) -> Result:
    raw = inp.get("file_path") or inp.get("path") or inp.get("filePath")
    if not raw:
        return fail("file_path is required")
    path = resolve(ctx, raw)
    head = short(ctx, path)
    try:
        authorize_read(path)
    except PolicyDenied as e:
        return fail(str(e), kind="denied", head=head)
    if not os.path.exists(path):
        return fail(_miss(path), head=head)
    if any(inp.get(k) not in (None, False, "") for k in BINARY_VIEWS):
        from . import ext as _ext
        ctx.read_files.add(path)
        remember(ctx, path)
        return _ext.call_legacy("Read", {**inp, "file_path": path}, ctx, head=head, effect=False)
    if os.path.isdir(path):
        try:
            names = sorted(os.listdir(path))
        except OSError as e:
            return fail(str(e), head=head)
        rows = [n + "/" if os.path.isdir(os.path.join(path, n)) else n for n in names[:1000]]
        more = f"\n... and {len(names) - 1000} more" if len(names) > 1000 else ""
        return Result(f"<path>{path}</path>\n<type>directory</type>\n<entries>\n" + "\n".join(rows) + more + "\n</entries>",
                      head=head, tail=f"{len(names)} 件")
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        ctx.read_files.add(path)
        remember(ctx, path)
        if ctx.vision:
            return Result(f"Image attached: {path}", head=head, tail="画像", images=[keep_image(path, ctx) or path])
        from . import ext as _ext          # 見られないモデルには中身の事実（大きさ・形式）を返す
        return _ext.call_legacy("Read", {**inp, "file_path": path}, ctx, head=head, effect=False)
    try:
        with open(path, "rb") as fh:
            sample = fh.read(4096)
    except OSError as e:
        return fail(str(e), head=head)
    if _is_binary(path, sample):
        from . import ext as _ext
        ctx.read_files.add(path)
        remember(ctx, path)
        # ★解いた道を渡す（2026-11-16 全体のレビュー: 相対の道や打ち間違いを直した道ではなく、元の文字のまま渡していた）
        return _ext.call_legacy("Read", {**inp, "file_path": path}, ctx, head=head, effect=False)
    try:
        size_all = os.path.getsize(path)
    except OSError as e:
        return fail(str(e), head=head)
    if size_all > BIG_FILE:                # ★大きなファイルは頭から流して読む（2026-11-16 全体のレビュー: 何 GB のログも丸ごと読み込んでいた）
        ctx.read_files.add(path)
        remember(ctx, path)
        return _read_big(path, inp, ctx, head, sample, size_all)
    try:
        text, _, _ = read_text(path)
    except OSError as e:
        return fail(str(e), head=head)
    ctx.read_files.add(path)
    remember(ctx, path)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    try:
        offset = max(1, int(inp.get("offset") or 1))
        limit = max(1, int(inp.get("limit") or READ_LINES))
    except (TypeError, ValueError):
        offset, limit = 1, READ_LINES
    total = len(lines)
    if total and offset > total:
        return fail(f"offset {offset} is past the end of the file ({total} lines)", head=head)
    out: list[str] = []
    size = 0
    max_bytes = limit_bytes(ctx, READ_BYTES)
    end = offset - 1
    cut = False
    for i in range(offset - 1, min(total, offset - 1 + limit)):
        line = lines[i]
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + f"... (line cut at {MAX_LINE} chars)"
        row = f"{i + 1}: {line}"
        size += len(row.encode("utf-8", "replace")) + 1
        if size > max_bytes:
            cut = True
            break
        out.append(row)
        end = i + 1
    if end < total:
        foot = f"(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to read more.)"
    else:
        foot = f"(End of file - total {total} lines)"
    body = "\n".join(out)
    return Result(f"<path>{head}</path>\n<type>file</type>\n<content>\n{body}\n\n{foot}\n</content>",
                  head=head, tail=f"{end - offset + 1 if out else 0} 行", truncated=cut or end < total)


BIG_FILE = 32 * 1024 * 1024


def _sample_encoding(sample: bytes) -> str:
    mark, enc = bom_of(sample)
    if mark:
        return enc
    for enc in ("utf-8", "cp932"):
        for cut in range(0, 4):            # 4096 バイトで切った所が字の途中のことがある
            try:
                sample[:len(sample) - cut].decode(enc)
                return enc
            except UnicodeDecodeError:
                continue
    return "utf-8"


def _read_big(path: str, inp: dict, ctx: Ctx, head: str, sample: bytes, size_all: int) -> Result:
    """offset から limit 行だけを流して読む。行の総数は数えない（数えるには全部読むことになる）。"""
    enc = _sample_encoding(sample)
    try:
        offset = max(1, int(inp.get("offset") or 1))
        limit = max(1, int(inp.get("limit") or READ_LINES))
    except (TypeError, ValueError):
        offset, limit = 1, READ_LINES
    max_bytes = limit_bytes(ctx, READ_BYTES)
    out: list[str] = []
    size, end, cut, eof = 0, offset - 1, False, True
    try:
        with open(path, encoding="utf-8-sig" if enc == "utf-8" else enc, errors="replace", newline="") as fh:
            for i, line in enumerate(fh, 1):
                if i < offset:
                    if ctx.stopped():
                        break
                    continue
                if i >= offset + limit:
                    eof = False
                    break
                line = line.rstrip("\n")
                if len(line) > MAX_LINE:
                    line = line[:MAX_LINE] + f"... (line cut at {MAX_LINE} chars)"
                row = f"{i}: {line}"
                size += len(row.encode("utf-8", "replace")) + 1
                if size > max_bytes:
                    cut, eof = True, False
                    break
                out.append(row)
                end = i
    except OSError as e:
        return fail(str(e), head=head)
    mb = size_all / 1024 / 1024
    if not out and eof:
        return fail(f"offset {offset} is past the end of the file", head=head)
    if eof:
        foot = f"(End of file - total {end} lines)"
    else:
        foot = (f"(Showing lines {offset}-{end} of a {mb:,.0f} MB file; the lines were not counted. "
                f"Use offset={end + 1} to read more, or Grep to find a line.)")
    body = "\n".join(out)
    return Result(f"<path>{head}</path>\n<type>file</type>\n<content>\n{body}\n\n{foot}\n</content>",
                  head=head, tail=f"{len(out)} 行・{mb:,.0f} MB", truncated=cut or not eof)


def _encoded_like(path: str, content: str) -> tuple[bytes, str]:
    """(書くバイト, 結果に添える一言)。既にあるファイルの文字コードと BOM を保つ（cp932 のバッチを丸ごと書くと UTF-8 に変わり、日本語が化ける）。"""
    try:
        _text, enc, bom = read_text(path)
        return encode_text(content, enc, bom), ""
    except OSError:
        return content.encode("utf-8"), ""
    except UnicodeEncodeError:
        return content.encode("utf-8"), f" It is now UTF-8: {enc} cannot hold this text."


def write(inp: dict, ctx: Ctx) -> Result:
    raw = inp.get("file_path") or inp.get("path") or inp.get("filePath")
    if not raw:
        return fail("file_path is required")
    if "content" not in inp:
        return fail("content is required")
    path = resolve(ctx, raw, mode="parent")
    head = short(ctx, path)
    try:
        authorize_path(path, write=True)
    except PolicyDenied as e:
        return fail(str(e), kind="denied", head=head)
    existed = os.path.exists(path)
    if existed and os.path.isdir(path):
        return fail(f"{path} is a directory", head=head)
    if existed and path not in ctx.read_files and path not in ctx.written:
        return fail(f"{path} already exists. Read it first, then Write again (or use Edit).", head=head)
    if existed and _drifted(ctx, path):
        return fail(f"{path} changed on disk after you last read or wrote it (a command, a background job or someone else "
                    "changed it), so it was not overwritten. Read it again, then Write.", head=head)
    content = str(inp.get("content") or "")
    data, kept = _encoded_like(path, content) if existed else (content.encode("utf-8"), "")
    try:
        _note_change(ctx, path)
        if existed:
            _backup(ctx, path)
        write_atomic(path, data)
    except OSError as e:
        return fail(f"could not write {path}: {e}", head=head)
    ctx.read_files.add(path)
    remember(ctx, path)
    ctx.written.append(path)
    if not existed:
        ctx.created.append(path)
    n = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    return Result(f"{'Updated' if existed else 'Created'} {head} ({n} lines).{kept}{_diagnostics(path, content)}", head=head, tail=f"{n} 行")


def _hint(text: str, old: str) -> str:
    """当たらなかったときの手掛かり（一番近い行とその周り）。当てにいくより、次の 1 手を短くする。"""
    want = next((x.strip() for x in old.split("\n") if x.strip()), "")
    if not want:
        return ""
    lines = text.split("\n")
    stripped = [x.strip() for x in lines]
    near = difflib.get_close_matches(want, stripped, n=1, cutoff=0.5)
    if not near:
        return " No similar line in the file; Read it again before editing."
    i = stripped.index(near[0])
    a, b = max(0, i - 2), min(len(lines), i + 4)
    block = "\n".join(f"{k + 1}: {lines[k]}" for k in range(a, b))
    return f" The closest text in the file:\n{block}"


def edit(inp: dict, ctx: Ctx) -> Result:
    raw = inp.get("file_path") or inp.get("path") or inp.get("filePath")
    if not raw:
        return fail("file_path is required")
    path = resolve(ctx, raw)
    head = short(ctx, path)
    old = str(inp.get("old_string") if inp.get("old_string") is not None else inp.get("oldString") or "")
    new = str(inp.get("new_string") if inp.get("new_string") is not None else inp.get("newString") or "")
    try:
        authorize_path(path, write=True)
    except PolicyDenied as e:
        return fail(str(e), kind="denied", head=head)
    if not os.path.exists(path):
        if old == "":                                   # opencode と同じ: 空の old_string は新しいファイル
            return write({"file_path": path, "content": new}, ctx)
        return fail(_miss(path), head=head)
    if os.path.isdir(path):
        return fail(f"{path} is a directory", head=head)
    try:
        text, enc, bom = read_text(path)
    except OSError as e:
        return fail(str(e), head=head)
    if not _round_trips(path, text, enc, bom):
        return fail(f"{path} is not UTF-8 or Shift_JIS text, so Edit would damage it; nothing was changed. "
                    "Change it with a script that reads and writes its real encoding.", head=head)
    crlf = "\r\n" in text and "\n" not in text.replace("\r\n", "")     # 混在のファイルはそのまま（全部を CRLF にすると全行が変更になる）
    work = text.replace("\r\n", "\n") if crlf else text
    ops = inp.get("edits") if isinstance(inp.get("edits"), list) and inp.get("edits") else [inp]
    changed = work
    for i, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            return fail(f"edit {i} is not an object; nothing was changed", head=head)
        o = str(op.get("old_string") if op.get("old_string") is not None else op.get("oldString") or "")
        n = str(op.get("new_string") if op.get("new_string") is not None else op.get("newString") or "")
        try:
            changed = replace(changed, o.replace("\r\n", "\n"), n.replace("\r\n", "\n"), bool(op.get("replace_all")))
        except EditError as e:
            msg = str(e)
            if msg == "NOT_FOUND":
                msg = "old_string was not found in the file." + _hint(changed, o) + CODE_LOOKUP
            where = f"Edit {i} of {len(ops)} failed, so nothing was changed. " if len(ops) > 1 else ""
            return fail(where + msg, head=head)
    try:
        data = encode_text(changed.replace("\n", "\r\n") if crlf else changed, enc, bom)
    except UnicodeEncodeError as e:
        return fail(f"this file is {enc}, which cannot hold {e.object[e.start]!r}; nothing was changed. Use other characters, "
                    "or convert the file to UTF-8 first.", head=head)
    try:
        _note_change(ctx, path)
        _backup(ctx, path)
        write_atomic(path, data)
    except OSError as e:
        return fail(f"could not write {path}: {e}", head=head)
    ctx.read_files.add(path)
    remember(ctx, path)
    ctx.written.append(path)
    diff = list(difflib.unified_diff(work.split("\n"), changed.split("\n"), lineterm="", n=2))[2:]
    plus = sum(1 for x in diff if x.startswith("+"))
    minus = sum(1 for x in diff if x.startswith("-"))
    shown = "\n".join(diff[:30])
    return Result(f"Edit applied to {head}.\n{shown}".rstrip() + _diagnostics(path, changed), head=head, tail=f"+{plus} -{minus}")


def _secs(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _walk(root: str):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        yield dp, fn


def _glob_regex(pattern: str):
    """** を含む型を正規表現にする（区切りは / と \\ の両方）。"""
    parts = re.split(r"(\*\*/|\*\*|\*|\?|\[[^\]]*\]|\{[^}]*\})", pattern.replace("\\", "/"))
    out = ""
    for p in parts:
        if p == "**/":
            out += "(?:.*/)?"
        elif p == "**":
            out += ".*"
        elif p == "*":
            out += "[^/]*"
        elif p == "?":
            out += "[^/]"
        elif p.startswith("[") and p.endswith("]"):
            out += p
        elif p.startswith("{") and p.endswith("}"):
            out += "(?:" + "|".join(re.escape(x) for x in p[1:-1].split(",")) + ")"
        else:
            out += re.escape(p)
    return re.compile("^" + out + "$", re.I if os.name == "nt" else 0)


def _glob_parts(pattern: str) -> tuple[str, str]:
    """(型, 探し始める場所)。型は相対パスに当てるので、頭の ./ を落とし、絶対パスの型は固定の部分を場所へ移す。"""
    pat = pattern.replace("\\", "/")
    while pat.startswith("./"):
        pat = pat[2:]
    if not os.path.isabs(pat):
        return pat, ""
    m = re.search(r"[*?\[{]", pat)
    base = (pat[:m.start()] if m else pat).rsplit("/", 1)[0] or "/"
    return pat[len(base):].lstrip("/"), base


def glob(inp: dict, ctx: Ctx) -> Result:
    head = str(inp.get("pattern") or "").strip()
    if not head:
        return fail("pattern is required")
    pattern, base = _glob_parts(head)
    root = resolve(ctx, base or inp.get("path") or ctx.workspace)
    try:
        authorize_read(root)
    except PolicyDenied as e:
        return fail(str(e), kind="denied", head=head)
    if not os.path.isdir(root):
        return fail(f"not a directory: {root}", head=head)
    rx = _glob_regex(pattern if "/" in pattern or "**" in pattern else "**/" + pattern)
    hits: list[tuple[float, str]] = []
    # 大きなフォルダで戻らなくならないように持ち時間を切る（2026-09-06 オーナー機で道具が走ったまま戻らなかった）。
    secs = _secs("LAPS_WALK_SECS", 10.0)
    t0 = time.monotonic()
    cut = False
    for dp, fn in _walk(root):
        if ctx.stopped():
            break
        if secs > 0 and time.monotonic() - t0 > secs:
            cut = True
            break
        for name in fn:
            full = os.path.join(dp, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rx.match(rel):
                try:
                    hits.append((os.path.getmtime(full), full))
                except OSError:
                    continue
    hits.sort(reverse=True)
    partial = f"\n(Stopped after {int(secs)} s: this folder is too large to search in full, so there may be more. Give a narrower path.)" if cut else ""
    if not hits:
        return Result("No files found." + partial, kind="empty", head=head, tail="0 件")
    shown = [h[1] for h in hits[:100]]
    more = f"\n(Showing 100 of {len(hits)}. Narrow the pattern or the path.)" if len(hits) > 100 else ""
    return Result("\n".join(shown) + more + partial, head=head, tail=f"{len(hits)} 件", truncated=len(hits) > 100 or cut)


def _rg() -> str:
    found = shutil.which("rg")
    if found:
        return found
    try:
        from ...oc.toolenv import child_path
        found = shutil.which("rg", path=child_path(os.environ.get("PATH", "")))
        if found:
            return found
    except Exception:
        pass
    d = os.environ.get("LAPS_TOOLS_DIR")
    if d:
        for n in ("rg.exe", "rg"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return ""


def grep(inp: dict, ctx: Ctx) -> Result:
    pattern = str(inp.get("pattern") or "")
    if not pattern:
        return fail("pattern is required")
    root = resolve(ctx, inp.get("path") or ctx.workspace)
    if inp.get("bytes"):                   # バイナリの中を探す（UTF-8・UTF-16LE・hex:…）
        from . import ext as _ext
        return _ext.call_legacy("Grep", {**inp, "path": root}, ctx, head=pattern[:120], effect=False)
    ignore_case = bool(inp.get("-i") or inp.get("ignore_case"))
    include = str(inp.get("glob") or inp.get("include") or "")
    files_only = str(inp.get("output_mode") or "") == "files_with_matches"
    head = pattern[:120]
    try:
        authorize_read(root)
    except PolicyDenied as e:
        return fail(str(e), kind="denied", head=head)
    if not os.path.exists(root):
        return fail(_miss(root), head=head)
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e:
        return fail(f"bad regular expression: {e}", head=head)
    rows: list[str] = []
    limit = 200
    stopped_at = 0.0
    rg = _rg()
    if rg:
        cmd = [rg, "--no-heading", "--line-number", "--color=never", "--max-columns=500", "-e", pattern]
        if files_only:
            cmd = [rg, "--files-with-matches", "--color=never", "-e", pattern]
        if ignore_case:
            cmd.append("-i")
        if include:
            cmd += ["--glob", include]
        cmd.append(root)
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=120, cwd=ctx.workspace,
                               creationflags=(0x08000000 if os.name == "nt" else 0))
            rows = p.stdout.decode("utf-8", "replace").splitlines()
        except (OSError, subprocess.SubprocessError) as e:
            return fail(f"search failed: {e}", head=head)
        if not rows and p.returncode not in (0, 1):    # rg が断った（先読み・後方参照は rg に無い）。0 件と言わず、下の Python の検索で探す
            rg = ""
    if not rg:
        pool = [("", [root])] if os.path.isfile(root) else _walk(root)
        secs = _secs("LAPS_GREP_SECS", 60.0)
        t0 = time.monotonic()
        for dp, fn in pool:
            if ctx.stopped() or len(rows) > limit * 5:
                break
            if secs > 0 and time.monotonic() - t0 > secs:
                stopped_at = secs
                break
            for name in fn:
                full = os.path.join(dp, name) if dp else name
                if include and not fnmatch.fnmatch(os.path.basename(full), include):
                    continue
                if os.path.splitext(full)[1].lower() in BINARY_EXT:
                    continue
                try:
                    with open(full, "rb") as fh:
                        data = fh.read(4 * 1024 * 1024)
                except OSError:
                    continue
                if b"\0" in data[:4096]:
                    continue
                for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                    if rx.search(line):
                        if files_only:
                            rows.append(full)
                            break
                        rows.append(f"{full}:{n}:{line[:500]}")
    partial = f"\n(Stopped after {int(stopped_at)} s: the search did not finish. Give a narrower path.)" if stopped_at else ""
    if not rows:
        return Result("No matches found." + partial, kind="empty", head=head, tail="0 件")
    total = len(rows)
    more = (f"\n(Showing {limit} of {total}. Narrow the pattern, the path or include.)" if total > limit else "") + partial
    body, cut = clip(ctx, "\n".join(rows[:limit]) + more)
    return Result(body, head=head, tail=f"{total} 件", truncated=cut or total > limit)


READ = Tool(
    "Read",
    "Read a file or list a directory. Lines come back as `<line>: <content>`. Default: 2000 lines from the start; use "
    "offset and limit for more. Read several files in one turn when you know you need them. Binary files come back as "
    "a description of their structure.",
    {"file_path": {"type": "string", "description": "Path of the file or directory, relative to the working directory"},
     "offset": {"type": "integer", "description": "First line to read (1-based)"},
     "limit": {"type": "integer", "description": "Number of lines"},
     "hex": {"type": "boolean", "description": "Show bytes as a hex dump"},
     "byte_offset": {"type": "integer", "description": "With hex: byte to start at (negative: from the end)"},
     "byte_length": {"type": "integer", "description": "With hex: bytes to show (default 256)"},
     "map": {"type": "boolean", "description": "Map a large binary: regions and embedded formats with offsets"},
     "strings": {"type": "boolean", "description": "List the text inside a binary with offsets"},
     "min_length": {"type": "integer", "description": "With strings: shortest string (default 4)"},
     "encoding": {"type": "string", "enum": ["auto", "ascii", "utf-8", "utf-16le", "shift-jis"], "description": "With strings: only this encoding"},
     "find": {"type": "string", "description": "Unity bundle, .assets, .NET DLL, Unreal pak: find objects, types or paths by name (a|b for several)"},
     "path_id": {"type": "string", "description": "Unity: show one object by its path_id"},
     "compare_with": {"type": "string", "description": "Second file: list where the two differ, byte by byte"}},
    read, required=("file_path",), parallel=True)

WRITE = Tool(
    "Write",
    "Create a file, or replace a whole file you have already read. Prefer Edit for changes to an existing file.",
    {"file_path": {"type": "string", "description": "Path of the file, relative to the working directory"},
     "content": {"type": "string", "description": "The full content"}},
    write, required=("file_path", "content"), effect=True)

EDIT = Tool(
    "Edit",
    "Replace text in a file. old_string must match the file, including indentation, and must identify one place: add "
    "surrounding lines if it does not, or set replace_all to change every match. Do not include the `<line>: ` prefix "
    "from Read. If the match fails you get the closest text in the file.",
    {"file_path": {"type": "string", "description": "Path of the file, relative to the working directory"},
     "old_string": {"type": "string", "description": "Text to replace"},
     "new_string": {"type": "string", "description": "Replacement text"},
     "replace_all": {"type": "boolean", "description": "Replace every match"},
     "edits": {"type": "array", "description": "Several replacements in this file, applied in order and written once; if one fails nothing changes",
               "items": {"type": "object", "properties": {"old_string": {"type": "string"}, "new_string": {"type": "string"},
                                                          "replace_all": {"type": "boolean"}}}}},
    edit, required=("file_path",), effect=True)

GLOB = Tool(
    "Glob",
    "Find files by name pattern such as `**/*.py` or `src/**/*.ts`. Newest first.",
    {"pattern": {"type": "string", "description": "Glob pattern"},
     "path": {"type": "string", "description": "Directory to search (default: the workspace)"}},
    glob, required=("pattern",), parallel=True)

GREP = Tool(
    "Grep",
    "Search file contents with a regular expression. Returns path:line:text.",
    {"pattern": {"type": "string", "description": "Regular expression"},
     "path": {"type": "string", "description": "File or directory (default: the workspace)"},
     "glob": {"type": "string", "description": "Only files matching this, for example *.py"},
     "-i": {"type": "boolean", "description": "Ignore case"},
     "bytes": {"type": "boolean", "description": "Search raw bytes in any file: a literal string, or hex written as hex:4d4f4333"},
     "output_mode": {"type": "string", "enum": ["content", "files_with_matches"], "description": "Default: content"}},
    grep, required=("pattern",), parallel=True)
