"""`bwtool wait` — 別の機械や列に渡した仕事が終わるのを、走行を閉じずに待つ（2026-11-11 オーナー「自動的にタスクを再開する機能とかって
実装されてるの？」→「任せます」）。

★それまでの穴: 走行は自分の Bash が背景に起こした仕事が動いている間だけ閉じずに待つ（engine/loop.py の _park）。ssh の先・GPU の貸し出し・
  labrun の列に投げた仕事は手元のコマンドがすぐ終わるので、待つ物が無くなって走行が閉じ、「終わったら報告します」は果たされなかった。
  モデルが自分で `while …; do sleep 60; done` を背景に起こしても、黙って待つので 5 分で「止まって見える」と判定され、待つ対象から
  外れて走行の終わりに止められていた。
★これは背景の仕事として起こす（Bash の run_in_background）。決まった間隔で確かめの命令を走らせ、1 分ごとに経過を 1 行出す
  （出力があるので止まって見えない）。確かめが通ったら終わる＝走行の待機が「仕事が終わった」の知らせで起き、続きを始める。

  bwtool wait --until '<終わったら成功する命令>' [--every 60] [--max 86400]
  bwtool wait --while '<動いている間は成功する命令>' [--every 60] [--max 86400]
終了コード: 0 待ち終わった・3 --max を過ぎた・2 使い方の誤り。
"""
from __future__ import annotations

import subprocess
import sys
import time

USAGE = ("usage: bwtool wait --until '<command that succeeds when the work is done>' | --while '<command that succeeds while "
         "it runs>' [--every SECONDS] [--max SECONDS]\n"
         "Run it with Bash run_in_background=true: you are woken when it exits. Exit code 0 done, 3 gave up after --max.")

#: 経過の行を出す間隔の上限（秒）。背景の仕事は出力も CPU も 5 分動かないと「止まって見える」とされる（tools/shell.py）。
BEAT_MAX = 60.0


def _num(v: str, name: str) -> float:
    try:
        x = float(v)
    except ValueError:
        raise SystemExit(f"Error: {name} needs a number of seconds, not {v!r}\n{USAGE}")
    return x


def _check(cmd: str, timeout: float) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, errors="replace")
        last = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
        return p.returncode, (last[-1][:160] if last else "")
    except subprocess.TimeoutExpired:
        return 124, f"the check did not finish in {int(timeout)} s"
    except OSError as e:
        return 127, str(e)[:160]


def main(argv: list[str]) -> int:
    until = whil = ""
    every, cap = 60.0, 86400.0
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            print(USAGE)
            return 0
        if a in ("--until", "--while", "--every", "--max") and i + 1 < len(argv):
            v = argv[i + 1]
            if a == "--until":
                until = v
            elif a == "--while":
                whil = v
            elif a == "--every":
                every = _num(v, a)
            else:
                cap = _num(v, a)
            i += 2
            continue
        print(f"Error: unknown argument {a!r}\n{USAGE}", file=sys.stderr)
        return 2
    if bool(until) == bool(whil):
        print(f"Error: give exactly one of --until or --while\n{USAGE}", file=sys.stderr)
        return 2
    every = min(max(every, 5.0), 3600.0)
    cmd, want_ok = (until, True) if until else (whil, False)
    t0 = time.monotonic()
    n = 0
    print(f"[wait] {'until' if until else 'while'}: {cmd}  (every {int(every)} s, at most {int(cap)} s)", flush=True)
    while True:
        n += 1
        rc, last = _check(cmd, timeout=max(30.0, min(every, 300.0)))
        mins = int((time.monotonic() - t0) // 60)
        if (rc == 0) == want_ok:
            print(f"[wait] done after {mins} min (check {n}, exit {rc}){': ' + last if last else ''}", flush=True)
            return 0
        if cap > 0 and time.monotonic() - t0 >= cap:
            print(f"[wait] gave up after {mins} min (check {n}, exit {rc}){': ' + last if last else ''}", flush=True)
            return 3
        print(f"[wait] check {n}: not yet (exit {rc}){': ' + last if last else ''}", flush=True)
        # 次の確かめまで眠る。間に 1 分ごとに経過を出す（出力が止まると背景の仕事は「止まって見える」とされる）。
        until_next = time.monotonic() + every
        while True:
            left = until_next - time.monotonic()
            if left <= 0:
                break
            time.sleep(min(left, BEAT_MAX))
            if until_next - time.monotonic() > 1:
                print(f"[wait] still waiting ({int((time.monotonic() - t0) // 60)} min)", flush=True)
