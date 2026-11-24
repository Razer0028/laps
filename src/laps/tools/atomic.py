"""ファイルを**壊さずに**書き換えるための 1 本（2026-08-31・線D E4/E5）。

★なぜ要るか（実測してから書いた）:
`Path.write_text` は「開いて（＝その瞬間に 0 バイトへ切り詰め）から書く」。
途中で失敗すると、**元の中身は既に消えている**。2026-08-31 に 2 通り再現した:

  ① 符号化できない文字が本文に混ざっていた場合
     Write("important.txt", "... \\ud800 ...")
       → 道具は `Error: 'utf-8' codec can't encode character ...` を返す
       → **ファイルは 0 バイト**（元の 35 バイトは失われた）
     道具が「エラー」と言うので、モデルは「何も起きていない」と読む。実際は消えている。

  ② 書ける量が尽きた場合（RLIMIT_FSIZE 4KiB で再現）
     元 90 バイトの設定ファイルが、**4096 バイトの途中で切れた別物**になった。
     ディスク満杯・quota・NFS のエラーはこれと同じ形で起きる。

対策は昔からある形: **同じディレクトリに一時ファイルを作り、書き切ってから
`os.replace` で差し替える**。差し替えは同一ファイルシステム上では不可分なので、
読み手が見るのは「古い中身」か「新しい中身」のどちらかだけになる。

⚠ 正直に書いておく副作用:
  * inode が変わる。**ハードリンクは切れる**（`ln` で束ねた設定は追随しない）。
  * ディレクトリに書けないと一時ファイルを作れない。その場合だけ従来どおり
    その場書きへ落ちる（＝**今までより悪くはならない**）。
  * 記号リンクは辿ってから差し替える（辿らないと、リンクを実ファイルで
    置き換えてしまい、これも今までの挙動と違う）。
  * 書き先がマウントの口だと差し替えは断られる（EBUSY）。Docker はコンテナの
    /etc/hosts・/etc/resolv.conf・/etc/hostname をファイル単位で bind するので、
    中から Edit すると必ずここに当たる（2026-10-30 TB の cron-broken-network で North が
    2 回断られ、直せないまま時間切れ）。その場合はその場書きへ落ちる。中身は一時ファイルに
    書き切れている＝①（符号化）は既に除けている。残るのは②だけで、今までより悪くはならない。
"""
from __future__ import annotations

import errno
import os
import stat
import tempfile
import threading
from pathlib import Path

#: 新規ファイルの権限を umask から求めるときの排他。
#: （`os.umask` は「読む」手段が無く、一度 0 にして戻すしかない＝プロセス全体に効く）
_UMASK_LOCK = threading.Lock()


def _default_mode() -> int:
    """新しく作るファイルの権限。`open()` が使う 0o666 & ~umask に合わせる。"""
    with _UMASK_LOCK:
        cur = os.umask(0)
        os.umask(cur)
    return 0o666 & ~cur


def write_bytes_atomic(path: str | os.PathLike, data: bytes) -> None:
    """`data` を `path` へ、途中経過が見えない形で書く。

    失敗したときは **元のファイルがそのまま残る**（これがこの関数の存在理由）。
    """
    p = Path(path)
    # 記号リンクは実体へ辿る（辿らないと差し替えでリンクを潰す）。
    if p.is_symlink():
        p = Path(os.path.realpath(p))
    d = p.parent if str(p.parent) else Path(".")

    try:
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=f".{p.name}.", suffix=".bw-tmp")
    except OSError:
        # ディレクトリに書けない（ファイル自体は書ける）場合。従来どおりその場で書く。
        # ★ここに落ちたときだけ、上の①②の危険が残る。黙って落ちるのではなく、
        #   「不可分に書けない置き場」であることが呼び出し側から分かるよう例外は伝えない
        #   （書けるなら書くのが利用者の期待）。
        with open(p, "wb") as f:
            f.write(data)
        return

    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(p.stat().st_mode))   # 既存の権限を引き継ぐ
        except FileNotFoundError:
            os.chmod(tmp, _default_mode())                  # 新規は open() 相当
        try:
            os.replace(tmp, p)
        except OSError as e:
            if e.errno not in (errno.EBUSY, errno.EXDEV):
                raise
            with open(p, "wb") as f:                        # マウントの口: その場で書く（上の注記）
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_text_atomic(path: str | os.PathLike, text: str, encoding: str = "utf-8") -> None:
    """テキスト版。**符号化を先に済ませる**（失敗しても元のファイルを触らない）。"""
    data = text.encode(encoding)      # ★ここで失敗しても、まだ何も壊していない
    write_bytes_atomic(path, data)
