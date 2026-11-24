r"""同梱の道具の部品をあらかじめそろえる（2026-10-07 オーナー「C ドライブから削除してしまったので、
インストール時にインストール先に自動で用意されるようにできませんか？」）。

アプリ（Windows）が起動のたびに裏で `python -m laps.oc.parts` を 1 回走らせる（sidecar.rs）。
端末版（Linux）も起動のたびに同じ物を走らせる（term/client.py の prepare_parts）。
置き場は LAPS_LISTEN_DIR / LAPS_DESKTOP_DIR（アプリは <データ根>\listen・\desktop を渡す）。
そろっていれば何もしないので、インストール・更新の後の最初の起動で一度だけ取り寄せ・組み立てが走る。
  ・Listen  … whisper.cpp の本体（NVIDIA なら CUDA 版 約 0.67GB）とモデル（約 0.87GB）。sha256 照合つき。
  ・Desktop … .NET Framework 4 の csc で補助プログラムを組み立てる（数秒）。
  ・ffmpeg  … PATH に無ければ道具の置き場（LAPS_TOOLS_DIR）へ 9.0.2 を取り寄せる（約 0.11GB・Windows）。
  ・定番の道具 … rg・fd・jq・yq・sd・7z・exiftool・MediaInfo・DetectItEasy を 知識サーバー の道具の棚から（無い物だけ・約 60MB・
    2026-10-15・toolenv.prepare_toolbox・LAPS_TOOLBOX=0 で止める）。
失敗しても困らない（使うときにもう一度そろえに行く）。誤りは記録（<データ根>\parts.log）に残す。
"""
from __future__ import annotations

import os
import sys
import time


def _one_at_a_time():
    """同じ機械で 2 本が同時に取り寄せないように（端末版を 2 つ同時に開いたとき）。取れなければ None。"""
    if os.name != "posix":
        return True                                   # Windows はアプリが起動のたびに 1 本だけ走らせる
    import fcntl
    import tempfile
    f = open(os.path.join(tempfile.gettempdir(), f"blackwall-parts-{os.getuid()}.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def main() -> int:
    t0 = time.time()
    lock = _one_at_a_time()
    if lock is None:
        print("parts: ほかの 1 本がそろえている最中なので何もしない", flush=True)
        return 0
    rc = 0
    for name, fn in (("listen", "prepare"), ("desktop", "prepare"), ("toolenv", "prepare_ffmpeg"),
                     ("toolenv", "prepare_toolbox")):
        try:
            mod = __import__(f"laps.oc.{name}", fromlist=[fn])
            print(getattr(mod, fn)(), flush=True)
        except Exception as e:                       # 片方が失敗しても、もう片方はそろえる
            print(f"{name}: そろえられなかった: {type(e).__name__}: {e}", flush=True)
            rc = 1
    print(f"parts: {time.strftime('%Y-%m-%d %H:%M:%S')} {time.time() - t0:.1f} 秒", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
