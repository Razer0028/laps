"""書けなかったときに**元のファイルを壊さない**（第三者レビュー E4・E5）。

2026-08-31 に再現してから直した。`Path.write_text` は「開いた瞬間に 0 バイトへ
切り詰めてから書く」ので、途中で失敗すると元の中身は既に無い。実測 2 通り:

  ① 符号化できない文字（`\\ud800`）が本文に 1 つ混ざる
     → 道具は `Error: ... surrogates not allowed` を返し、**35 バイトのファイルが 0 バイト**
  ② 書ける量が尽きる（RLIMIT_FSIZE 4KiB で再現・ディスク満杯や quota と同じ形）
     → 90 バイトの設定ファイルが **4096 バイトの途中で切れた別物**に

どちらも道具は「Error」と言うので、モデルは「何も起きていない」と読む。
**エラーを返しながら破壊している**のが、この 2 件のいちばん悪いところ。
"""
from __future__ import annotations

import os
import pathlib
import resource
import signal

import pytest

from laps.tools import apply_diff as ad
from laps.tools import write as w
from laps.tools.atomic import write_text_atomic

_ORIG = "ORIGINAL CONTENT THAT MUST SURVIVE\n"


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


# ── E4: Write ────────────────────────────────────────────────────

def test_符号化できない本文でも元のファイルが残る(tmp_path):
    p = tmp_path / "important.txt"
    p.write_text(_ORIG, encoding="utf-8")
    out = str(w._run({"file_path": str(p), "content": "new \ud800 stuff"}))
    assert out.startswith("Error:")
    assert p.read_text(encoding="utf-8") == _ORIG, "エラーを返しながら中身を消している"


def test_書ける量が尽きても元のファイルが残る(tmp_path):
    """★子プロセスで RLIMIT_FSIZE を絞る（親に制限を残さない）。"""
    p = tmp_path / "conf.txt"
    p.write_text(_ORIG, encoding="utf-8")
    pid = os.fork()
    if pid == 0:                                   # 子: 4KiB しか書けない
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
        resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
        try:
            w._run({"file_path": str(p), "content": "X" * 100_000})
        except BaseException:
            pass
        os._exit(0)
    os.waitpid(pid, 0)
    assert p.read_text(encoding="utf-8") == _ORIG
    assert not list(tmp_path.glob("*.bw-tmp")), "一時ファイルが残っている"


def test_普通の書き込みは今まで通り(tmp_path):
    p = tmp_path / "new.txt"
    assert "OK: wrote" in str(w._run({"file_path": str(p), "content": "hello"}))
    assert p.read_text(encoding="utf-8") == "hello"
    assert p.stat().st_mode & 0o777 == 0o666 & ~_umask()


def test_既存ファイルの権限を引き継ぐ(tmp_path):
    p = tmp_path / "secret.txt"
    p.write_text("x", encoding="utf-8")
    p.chmod(0o600)
    w._run({"file_path": str(p), "content": "y"})
    assert p.stat().st_mode & 0o777 == 0o600, "書き換えで権限が緩んだ"


def test_記号リンクは実体を書き換える(tmp_path):
    """差し替えでリンクを実ファイルに置き換えると、今までと挙動が変わる。"""
    tgt = tmp_path / "target.txt"
    tgt.write_text("old", encoding="utf-8")
    ln = tmp_path / "link.txt"
    ln.symlink_to(tgt)
    w._run({"file_path": str(ln), "content": "new"})
    assert ln.is_symlink()
    assert tgt.read_text(encoding="utf-8") == "new"


def _umask() -> int:
    cur = os.umask(0)
    os.umask(cur)
    return cur


# ── E5: ApplyDiff も同じ書き方をしていた ─────────────────────────

def _block(path: pathlib.Path, old: str, new: str) -> str:
    return f"{path}\n<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE\n"


def test_ApplyDiffも書けないとき元のファイルを残す(tmp_path, monkeypatch):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n", encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    # ★ApplyDiff は 2026-09-20 から `write_bytes_atomic`（読んだ符号化で書く）を使う。
    #   試験が古い名前だけを潰していると、**書けてしまって**緑にならない＝意図（書けないとき元を残す）を
    #   確かめられていない。実際に使う名前を潰す。
    monkeypatch.setattr(ad, "write_bytes_atomic", boom)
    monkeypatch.setattr(ad, "write_text_atomic", boom)
    out = str(ad._run({"diff": _block(p, "x = 1", "x = 99")}))
    assert out.startswith("Error:")
    assert p.read_text(encoding="utf-8") == "x = 1\n"


# ── 土台そのもの ─────────────────────────────────────────────────

def test_不可分書き込みは失敗しても中身を触らない(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text(_ORIG, encoding="utf-8")
    with pytest.raises(UnicodeEncodeError):
        write_text_atomic(p, "\ud800")
    assert p.read_text(encoding="utf-8") == _ORIG
    assert not list(tmp_path.glob("*.bw-tmp"))


def test_ディレクトリに書けなくてもファイルは書ける(tmp_path):
    """一時ファイルを作れない置き場（読み取り専用ディレクトリ）でも、
    ファイル自体が書けるなら書く＝**今までより悪くしない**。"""
    if os.geteuid() == 0:
        pytest.skip("root は書き込み権限を無視するので再現できない")
    d = tmp_path / "ro"
    d.mkdir()
    p = d / "f.txt"
    p.write_text("old", encoding="utf-8")
    d.chmod(0o555)
    try:
        write_text_atomic(p, "new")
        assert p.read_text(encoding="utf-8") == "new"
    finally:
        d.chmod(0o755)


def test_マウントの口でも書ける(tmp_path, monkeypatch):
    """Docker が bind した /etc/hosts などは差し替え（rename）が EBUSY で断られる（2026-10-30 TB）。
    その場書きへ落ちて書けること・一時ファイルを残さないこと。"""
    import errno
    from laps.tools import atomic

    p = tmp_path / "hosts"
    p.write_text(_ORIG, encoding="utf-8")

    def busy(src, dst):
        raise OSError(errno.EBUSY, "Device or resource busy")

    monkeypatch.setattr(atomic.os, "replace", busy)
    write_text_atomic(p, "127.0.0.1 localhost\n")
    assert p.read_text(encoding="utf-8") == "127.0.0.1 localhost\n"
    assert not list(tmp_path.glob("*.bw-tmp"))


def test_ほかの差し替えの失敗は今まで通り伝える(tmp_path, monkeypatch):
    import errno
    from laps.tools import atomic

    p = tmp_path / "f.txt"
    p.write_text(_ORIG, encoding="utf-8")

    def denied(src, dst):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(atomic.os, "replace", denied)
    with pytest.raises(OSError):
        write_text_atomic(p, "new")
    assert p.read_text(encoding="utf-8") == _ORIG
    assert not list(tmp_path.glob("*.bw-tmp"))
