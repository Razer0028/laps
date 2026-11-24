"""`bw-undo` コマンド — 作業場の巻き戻しを CLI として置く。

★なぜ薄い中核に要るか（2026-09-12）
  新しい中核は端末に直接キーを打つ。旧中核が持っていた「編集が壊したら戻す」安全網が無く、
  `sed -i` を外したときに取り返しがつかない。薄さを保ったまま、戻す手段だけは残したい。

★なぜ checkpoint.py を使わないか（実装時に実際に踏んだ）
  `Checkpoint.has_watermark` は `self._watermark`（**メモリ上の状態**）を見ており、
  印はディスクに引き継がれない＝**1 プロセス内で完結する設計**。
  CLI は毎回別プロセスなので、`mark` の直後に「印がありません」になる（実測）。
  → 無理に流用せず、**git を使う**。薄い中核の思想（端末でできることは端末でやる）に合い、
    モデルも git を知っているので説明が要らない。git が無い作業場では tar に退避する。

★これは「門」ではない
  自動で止めたり差し戻したりしない。**モデルが自分の判断で使う道具**として置くだけ。
  ハーネスがモデルを上書きしないというオーナー方針（2026-09-10）に沿う。
"""
from __future__ import annotations

from . import paths as _lp
import argparse
import hashlib
import os
import subprocess
import sys
import tarfile
from pathlib import Path

_STASH = "bw-undo"


def _run(args: list[str], cwd: str) -> tuple[int, str]:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _is_git(cwd: str) -> bool:
    rc, _ = _run(["git", "rev-parse", "--is-inside-work-tree"], cwd)
    return rc == 0


def _tar_path(ws: str) -> Path:
    d = (_lp.dot_dir() / "undo")
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha256(os.path.abspath(ws).encode()).hexdigest()[:12] + ".tar")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bw-undo",
        description="いまの作業場に印をつけ、あとで戻す。",
        epilog="例: bw-undo mark → 危ない編集 → うまくいかなければ bw-undo revert",
    )
    p.add_argument("action", choices=["mark", "revert", "status"],
                   help="mark=印をつける／revert=印まで戻す／status=印の有無と変更")
    a = p.parse_args(argv)
    ws = os.getcwd()

    if _is_git(ws):
        if a.action == "mark":
            # ★作業木は変えずに控えだけ取る（stash push だと手元が消えて驚く）。
            rc, out = _run(["git", "stash", "create"], ws)
            if rc != 0:
                print(f"印をつけられませんでした: {out}", file=sys.stderr)
                return 1
            sha = out.strip()
            if not sha:                     # 変更が無いときは HEAD を印にする
                rc, sha = _run(["git", "rev-parse", "HEAD"], ws)
                if rc != 0:
                    print("印をつけられませんでした（コミットがありません）", file=sys.stderr)
                    return 1
                sha = sha.strip()
            _run(["git", "update-ref", f"refs/{_STASH}", sha], ws)
            print(f"印をつけました（git {sha[:12]}）")
            return 0

        rc, sha = _run(["git", "rev-parse", f"refs/{_STASH}"], ws)
        has = rc == 0 and sha.strip()
        if a.action == "status":
            if not has:
                print("印はまだありません（bw-undo mark で付けます）")
                return 0
            _, diff = _run(["git", "diff", "--stat", sha.strip()], ws)
            print(f"印あり（git {sha.strip()[:12]}）")
            print(diff or "  変更なし")
            return 0

        if not has:
            print("印がありません。戻せません（先に bw-undo mark）", file=sys.stderr)
            return 1
        rc, out = _run(["git", "checkout", "--", "."], ws)
        rc2, out2 = _run(["git", "restore", "--source", sha.strip(), "--worktree", "."], ws)
        if rc2 != 0:
            print(f"戻せませんでした: {out2 or out}", file=sys.stderr)
            return 1
        print(f"印まで戻しました（git {sha.strip()[:12]}）")
        return 0

    # ---- git が無い作業場: tar に退避 ----
    tar = _tar_path(ws)
    if a.action == "mark":
        try:
            with tarfile.open(tar, "w") as tf:
                tf.add(ws, arcname=".")
        except Exception as e:
            print(f"印をつけられませんでした: {e}", file=sys.stderr)
            return 1
        print(f"印をつけました（{tar}）")
        return 0

    if not tar.exists():
        if a.action == "status":
            print("印はまだありません（bw-undo mark で付けます）")
            return 0
        print("印がありません。戻せません（先に bw-undo mark）", file=sys.stderr)
        return 1

    if a.action == "status":
        print(f"印あり（{tar}・{tar.stat().st_size:,} バイト）")
        return 0

    try:
        with tarfile.open(tar, "r") as tf:
            tf.extractall(ws)               # noqa: S202 — 自分で作った控えのみ
    except Exception as e:
        print(f"戻せませんでした: {e}", file=sys.stderr)
        return 1
    print(f"印まで戻しました（{tar}）")
    return 0


if __name__ == "__main__":                  # pragma: no cover
    raise SystemExit(main())
