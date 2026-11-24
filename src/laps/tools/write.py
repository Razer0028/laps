"""Write tool: create or overwrite a file."""
from __future__ import annotations
import os
import re
from pathlib import Path

from . import pathfix as _pathfix
from . import ToolSpec
from .atomic import write_text_atomic, write_bytes_atomic
from ..policy import authorize_path, PolicyDenied

# Ω-N rev6 (2026-05-24): omega-j SFT leak — model writes to absolute paths
# that exist only in training data (e.g. /opt/blackwall-sandbox/workspace/...
# /tmp/blackwall_bench_xxx/...). On any other host, these dirs don't exist
# or aren't where the user expects the file. Detect & rewrite into CWD.
_SFT_PATH_FINGERPRINTS = re.compile(
    r"^(/opt/blackwall-sandbox/|/tmp/blackwall_(?:bench|sandbox)_)"
)


def _sanitize_path(raw_path: str) -> tuple[str, str | None]:
    """Return (effective_path, warning_or_None). If the model gave us an
    absolute path that looks like SFT training-data fingerprint, redirect
    into the current working directory using the basename only."""
    if not raw_path:
        return raw_path, None
    if _SFT_PATH_FINGERPRINTS.match(raw_path):
        base = os.path.basename(raw_path.rstrip("/"))
        new_path = os.path.join(os.getcwd(), base)
        return new_path, f"[Ω-N] Redirected SFT-leaked path {raw_path!r} → {new_path!r}"
    return raw_path, None


def _run(inp: dict) -> str:
    path = inp.get("file_path") or inp.get("path")
    content = inp.get("content", "")
    if not path:
        # ★2026-08-30（レビュー P0-4・再現済み）: **本文からパスを推測しない**。
        #
        #   以前は file_write_recovery.recover_path(content) に落として当て推量していた。
        #   実測（2026-08-30）で、本文だけから次のように決めていた:
        #       "FROM python:3.11 ..."  → Dockerfile
        #       "class Foo: ..."        → foo.py
        #       "def helper(): ..."     → helper.py
        #       "MIT License ..."       → LICENSE
        #   どれも**利用者も呼び出し側も一度も口にしていない名前**で、書き先は cwd。
        #   モデルが引数を落としただけのときに、黙って別の場所へ物を作る。
        #
        #   落としたなら、落としたと言って出し直させるほうが速いし安全。
        #   どこへ書くかを決めるのは依頼者であって、この関数ではない。
        return ("Error: 'file_path' がありません。書き込み先は推測しません"
                "（本文から名前を当てると、依頼と無関係な場所に作ってしまうため）。\n"
                "file_path を明示してもう一度呼んでください。例: "
                '{"file_path": "src/app.py", "content": "..."}')
    # ★バイナリ形式のファイルはテキストの Write では作れない（2026-08-24 run12: works.db を
    #   Write→失敗→rm→Write の 3 連で走行が打ち切られた）。書く前に断り、正しい作り方を返す。
    low = str(path).lower()
    if low.endswith((".db", ".sqlite", ".sqlite3")):
        return ("Error: SQLite データベースはテキストの Write では作れません（壊れたファイルになります）。\n"
                "正しい作り方: python3 の sqlite3 モジュールか `sqlite3` コマンドでスキーマを実行してください。例:\n"
                "  sqlite3 " + str(path) + " \"CREATE TABLE works (slug TEXT, title TEXT, author TEXT, "
                "source TEXT, created_at TEXT, updated_at TEXT, deleted INTEGER DEFAULT 0, image_count INTEGER)\"")
    if low.endswith((".mp4", ".mkv", ".webm", ".avi", ".mov", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".zip", ".gz", ".tar")):
        return ("Error: バイナリ形式（" + low.rsplit(".", 1)[-1] + "）はテキストの Write では作れません。"
                "実際の生成コマンド（ffmpeg・PIL・zip 等）で作ってください。偽物のファイルで代用しないこと。")
    # Ω-N rev6: sandbox path leak guard
    path, warn = _sanitize_path(path)
    p = Path(path)
    try:
        # ★門を**先に**通す（2026-09-02）。旧実装は `mkdir` が門の外に置かれていて、
        #   policy が書き込みを拒否した呼び出しでも**ディレクトリだけは作られていた**
        #   （拒否されたはずの場所に痕跡が残る）。同じ形の穴を Glob でも見つけている
        #   （走査の門が、実際に歩く場所と別のものを見ていた）。
        # ★`mkdir` 自体も try の中へ。親が普通のファイルのときの NotADirectoryError や
        #   権限の PermissionError が**素の例外**になって
        #   「Error in Write: ...」の生文字列で返っていた（下の except が拾えない位置）。
        try:
            authorize_path(str(p), write=True, kind="write")
        except PolicyDenied as e:
            return f"Error: policy denied: {e}"
        if p.parent and str(p.parent):
            p.parent.mkdir(parents=True, exist_ok=True)
        # ★不可分に書く（2026-08-31・レビュー E4 を再現して直した）。
        #   旧実装 `p.write_text(content)` は**開いた瞬間に元の中身を捨てる**ので、
        #   符号化できない文字が 1 つ混ざっただけで（実測: `\ud800`）既存ファイルが
        #   0 バイトになり、道具は「Error」とだけ言う。モデルは「何も起きていない」と
        #   読むので、消えたことに誰も気づかない。書けなかったときは元のまま残す。
        # ★既にあるファイルを上書きするときは、**元の符号化を保つ**（2026-09-20・敵対レビュー）。
        #   旧: 常に UTF-8。cp932 の `run.bat` を Write で書き直すと UTF-8 になり、cmd.exe が日本語を化かす。
        #   Read は cp932 を正しく見せるので、モデルは符号化が変わったことを知る術が無い。
        #   元の符号化で表せない字があるときだけ UTF-8 にし、**そうしたと返事に書く**（黙って変えない）。
        _keep_note = ""
        from .textcodec import NotEditable as _NotEditable, encode_back as _encode_back, existing_encoding as _existing_encoding
        _prev = _existing_encoding(p) if p.exists() else None
        # ★上書きなら、書く**前**の中身を取っておく（2026-09-21）。
        #   出典: opencode `packages/opencode/src/tool/write.ts:47` が常に差分を返す。
        #   ★Edit 側は「差分を返さないとモデルが確認のため Read/Bash を何度も呼ぶ」と
        #   自分たちで実測して make_patch を足したのに、**Write の上書きには
        #   同じ手当てが無かった**（片側だけ直していた）。
        _before = None
        if p.exists():
            try:
                # ★`decode_for_edit(raw: bytes)` ——**パスではなくバイト列**を受ける。
                #   最初パスを渡して黙って失敗させた（差分が出ないだけなので気づきにくい）。
                from .textcodec import decode_for_edit as _dfe
                _before = _dfe(p.read_bytes())[0]
            except Exception:                      # noqa: BLE001 — 差分は飾り。読めなければ諦める
                _before = None
        if _prev and (_prev[0] != "utf-8" or _prev[1]):
            try:
                write_bytes_atomic(p, _encode_back(content, _prev[0], _prev[1]))
                _keep_note = f"（元の文字コード {_prev[0]}{'・BOM つき' if _prev[1] else ''} を保ちました）"
            except _NotEditable:
                write_text_atomic(p, content)
                _keep_note = (f"（★元は {_prev[0]} でしたが、その文字コードで表せない字があるので UTF-8 で書きました。"
                              "Windows のバッチなど、文字コードに依存する物なら確かめてください）")
        else:
            write_text_atomic(p, content)
    except Exception as e:
        return f"Error: {e}（書き込みは行われていません。元のファイルはそのままです）"
    # ★実際に書いたバイト数を言う（2026-09-03）。旧: `len(content)`＝**文字数**を
    #   「bytes」と称して返していた。`write_text_atomic` は UTF-8 で符号化してから
    #   書くので、日本語（この現場のコメント・文言のほとんど）を含む本文では
    #   1 文字が 3 バイトになり、申告と実体がずれる（例: 100 文字の日本語は
    #   「wrote 100 bytes」と出るが、ディスクには約 300 バイト書かれている）。
    #   `ls -l` で確かめたモデルが「言われた量と違う」と誤読する余地を残さない。
    try:
        _nbytes = p.stat().st_size            # ★符号化を保つようになったので、数えるのは実物
    except OSError:
        _nbytes = len(content.encode("utf-8"))
    msg = f"OK: wrote {_nbytes} bytes to {path}{_keep_note}"
    # ★上書きしたなら何が変わったかを見せる（読み直しの往復を減らす）。
    #   新規作成では出さない（全部が追加＝情報にならない）。
    if _before is not None and _before != content:
        try:
            from .edit import make_patch as _mk, _render_patch as _rp
            msg += "\n" + _rp(_mk(_before, content, path))
        except Exception:                          # noqa: BLE001 — 飾りで書き込みを失敗にしない
            pass
    if warn:
        msg = warn + "\n" + msg
    # ★成功も型で名乗る（2026-09-21・read.py / glob.py と同じ理由）。素の str で
    #   返すと `as_result` が**出力の中身**で成否を当てにいく。
    #   ★Write は**書き込み先の名前**が本文に入る。罠の語を含む名前へ書くと
    #   成功しても ok=False になり、agent 側の `files_written` に載らない
    #   （＝停滞判定と「宣言して止まった」の入力が欠ける）。
    from . import ToolResult
    return ToolResult(msg, ok=True, kind="ok")


SPEC = ToolSpec(
    name="Write",
    description="Create or overwrite a file. Parent dirs auto-created.",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path", "content"],
    },
    mutates=True,
    # ★見た目が同じで文字だけ違うパスを入る前に直す（2026-10-05・tools/pathfix.py）
    handler=_pathfix.wrap(_run, "file_path", "path", mode="parent"),
)
