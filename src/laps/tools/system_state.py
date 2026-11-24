"""Read this host's state through a fixed vocabulary, with nothing to pipe into.

The dominant fabrication in every measurement so far was not invention. It was
the agent destroying its own evidence and then reporting the wreckage:

    systemctl --failed --no-legend | awk '{print $1}'    ->  ●\n●\n●\n●
    systemctl --failed --no-pager  | wc -l               ->  11

and then, having nothing left it could read, writing "systemd failed ユニット:
なし". Eleven of fourteen fabrications took that shape. The model was not lying
about the units; it could no longer see them.

Guards do not reach this. A citation check compares a claim against the cited
output, and the cited output genuinely was four bullets — the loss happened
before anything was there to check. The fix has to be earlier: a way to ask for
the state that has no pipe in it.

So this tool takes a verb, not a command. Each verb is a fixed argv that returns
the whole answer, unsummarised, and there is no argument that could add `| awk`.
Bash remains available for everything else; nothing here forbids the old way, it
only offers a better one for the handful of questions that matter most.

★Local only, read-only, and short-lived. Its remote counterpart is
tools/remote_read.py, which has the same shape for the same reasons.
"""
from __future__ import annotations
import subprocess
import sys
from .. import procrun  # 固まらない subprocess（2026-09-06）

from . import ToolSpec, ToolResult
from . import fold
from ..policy import audit

# verb -> (argv, one-line description shown to the model)
#
# Every entry returns its answer whole. `--no-legend` and `--plain` are here so
# the output is stable and machine-readable *without* the agent reaching for
# awk; `-h` on du/df because a human-readable size is what a report wants.
_VERBS: dict[str, tuple[list[str], str]] = {
    "failed_units": (["systemctl", "--failed", "--no-pager", "--no-legend", "--plain"],
                     "failed 状態の systemd ユニット一覧（全行・名前つき）"),
    # ★2026-09-07 実用評価 s_sweep: 自ホストでも「enabled なのに inactive」を掃く列挙が無かった（RemoteRead と対）。
    "unit_files": (["systemctl", "list-unit-files", "--type=service", "--no-pager", "--no-legend", "--plain"],
                   "service の unit file 一覧（enabled/disabled/static 等の状態つき・全行）"),
    "units": (["systemctl", "list-units", "--all", "--type=service", "--no-pager", "--no-legend", "--plain"],
              "service unit の稼働状態一覧（inactive/failed を含む全行・LOAD/ACTIVE/SUB つき）"),
    # ★「各ファイルシステムの使用率」とだけ書いてあった版では、「df -i で i ノードの
    #   使用率を調べて」まで SystemState に来た（DEV 8/8）。df -h に i ノードは出ない
    #   ＝正解を潰す誤誘導なので、この項目が答えられる範囲を項目名の側に書く。
    "disk":         (["df", "-h"], "各ファイルシステムの容量使用率（df -h 相当。i ノードは出ない）"),
    "memory":       (["free", "-m"], "メモリとスワップの使用量（MB）"),
    "load":         (["cat", "/proc/loadavg"], "ロードアベレージ"),
    "uptime":       (["uptime"], "稼働時間"),
    "top_memory":   (["ps", "-eo", "rss,pid,comm", "--sort=-rss"],
                     "メモリ使用量の多いプロセス（先頭が最大）"),
    # ★`-p`（持ち主の PID/名前）を足した（2026-09-05・第4回）。同じ道具の**遠隔版**
    #   `remote_read.py:84` は最初から `ss -ltnp` で、ローカル版だけ持ち主が出ていなかった
    #   ——この現場が繰り返す「同じ物を2つ持つと片方だけ直る」形。
    #   持ち主が分かれば、居残りサーバを止める次の一手（BashOutput(port=…, kill=true)）に
    #   そのまま繋がる。他ユーザのプロセスは名前が出ない（権限）が、出力は壊れない。
    "listening":    (["ss", "-ltnp"], "待ち受けている TCP ポートと持ち主（PID/名前）"),
    "os":           (["uname", "-a"], "カーネルとホスト名"),
}

_MAX_OUT = 8000
_TIMEOUT = 30

# ★Windows の同等命令（2026-09-07・owner 実機 0.1.199 の走行で df/free/uptime が WinError 2 で全滅）。
#   PowerShell は -NoProfile -NonInteractive（プロファイルの副作用と対話待ちを断つ）。出力は
#   Format-Table/List の文字列＝モデルが読む前提の生出力（加工しない）。
_PS = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
_VERBS_WIN: dict[str, tuple[list[str], str]] = {
    "failed_units": (_PS + ["Get-Service | Where-Object { $_.StartType -eq 'Automatic' -and $_.Status -ne 'Running' } | Select-Object Status,Name,DisplayName | Format-Table -AutoSize | Out-String -Width 200"],
                     "自動起動なのに動いていないサービス（Windows には systemd の failed は無い）"),
    "disk":         (_PS + ["Get-PSDrive -PSProvider FileSystem | Select-Object Name,@{n='UsedGB';e={[math]::Round($_.Used/1GB,1)}},@{n='FreeGB';e={[math]::Round($_.Free/1GB,1)}},Root | Format-Table -AutoSize | Out-String -Width 200"],
                     "各ドライブの使用量（GB）"),
    "unit_files": (_PS + ["Get-Service | Select-Object Name,StartType,Status | Format-Table -AutoSize | Out-String -Width 200"],
                   "サービス一覧（StartType=自動/手動/無効・全行）"),
    "units": (_PS + ["Get-Service | Select-Object Name,Status,DisplayName | Format-Table -AutoSize | Out-String -Width 200"],
              "サービスの稼働状態一覧（Running/Stopped・全行）"),
    "memory":       (_PS + ["$o=Get-CimInstance Win32_OperatingSystem; [pscustomobject]@{TotalMB=[math]::Round($o.TotalVisibleMemorySize/1KB);FreeMB=[math]::Round($o.FreePhysicalMemory/1KB);UsedMB=[math]::Round(($o.TotalVisibleMemorySize-$o.FreePhysicalMemory)/1KB)} | Format-List | Out-String"],
                     "物理メモリの使用量（MB）"),
    "load":         (_PS + ["Get-CimInstance Win32_Processor | Select-Object Name,LoadPercentage,NumberOfLogicalProcessors | Format-List | Out-String"],
                     "CPU 負荷（%）と論理コア数"),
    "uptime":       (_PS + ["$b=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime; $u=(Get-Date)-$b; 'boot: {0}  uptime: {1}d {2}h {3}m' -f $b,$u.Days,$u.Hours,$u.Minutes"],
                     "起動時刻と稼働時間"),
    "top_memory":   (_PS + ["Get-Process | Sort-Object WS -Descending | Select-Object -First 15 @{n='WS_MB';e={[math]::Round($_.WS/1MB)}},Id,ProcessName | Format-Table -AutoSize | Out-String -Width 200"],
                     "メモリ使用量の多いプロセス（先頭が最大）"),
    "listening":    (_PS + ["Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess | Sort-Object LocalPort | Format-Table -AutoSize | Out-String -Width 200"],
                     "待ち受けている TCP ポートと PID"),
    "os":           (_PS + ["[System.Environment]::OSVersion.VersionString + '  host=' + $env:COMPUTERNAME + '  arch=' + $env:PROCESSOR_ARCHITECTURE"],
                     "OS の版とホスト名"),
}


def _verbs() -> dict[str, tuple[list[str], str]]:
    return _VERBS_WIN if sys.platform == "win32" else _VERBS


def _listening_from_proc(paths=(("/proc/net/tcp", False),
                                     ("/proc/net/tcp6", True))) -> str | None:
    """`ss` も `netstat` も無い環境で、`/proc/net/tcp` から待ち受けを組む。

    ★2026-09-15 の dogfood で見つけた欠陥。最小のコンテナ（`ubuntu:22.04` 素のまま等)
      には **iproute2 が入っていない**。すると `listening` は実行不能で終わり、
      エージェントは「この環境ではポート情報を取得できません」と答えるしかなかった
      ——**`/proc/net/tcp` は Linux なら必ずある**のに。
    ★持ち主（PID/名前）までは出せない。`ss -ltnp` は /proc/<pid>/fd を総なめして
      inode を突き合わせている。**出せないものは出さないと書く**（推測しない）。
    """
    rows: list[tuple[str, int]] = []
    for path, v6 in paths:
        try:
            lines = open(path, encoding="ascii", errors="replace").read().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            f = ln.split()
            if len(f) < 4 or f[3] != "0A":          # 0A = TCP_LISTEN
                continue
            addr, _, port = f[1].partition(":")
            try:
                pnum = int(port, 16)
            except ValueError:
                continue
            if v6:
                shown = "[::]" if set(addr) <= {"0"} else "[v6]"
            else:
                try:
                    b = bytes.fromhex(addr)
                    shown = ".".join(str(x) for x in reversed(b)) if len(b) == 4 else addr
                except ValueError:
                    shown = addr
            rows.append((shown, pnum))
    if not rows:
        return None
    rows = sorted(set(rows), key=lambda r: (r[1], r[0]))
    head = ("待ち受けている TCP ポート（/proc/net/tcp から。"
            "★`ss` も `netstat` も無いため**持ち主の PID/名前は分かりません**）")
    body = "\n".join(f"  {a}:{pt}" for a, pt in rows)
    return head + "\n" + body


def _run(inp: dict) -> str:
    verb = (inp.get("verb") or "").strip()
    table = _verbs()
    if verb not in table:
        audit("system_state", {"verb": verb}, "deny", "verb not allowlisted")
        return (f"Error: `{verb}` は点検項目ではありません。"
                f"使えるのは: {', '.join(sorted(table))}")

    argv, _desc = table[verb]
    audit("system_state", {"verb": verb}, "allow", "read-only fixed argv")
    try:
        # ★**バイトで受ける**（2026-09-02・`remote_read` と同じ欠陥がここにもあった）。
        #   旧: `text=True`＝既定の UTF-8 で解釈。日本語 Windows（cp932）の
        #   `systeminfo` 等の応答で **UnicodeDecodeError** になり、しかもそれは
        #   下の `except OSError` でも `TimeoutExpired` でも捕まらない。
        #   実機で `RemoteRead` が同じ形で 5 動詞すべて落ちている。
        #   ★これで「片方で直して片方に残す」は今日 4 件目。原因の形
        #   （`text=True`）で grep して見つけた。
        p = procrun.run(argv, capture_output=True,
                           timeout=_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        # ★**成否を名乗る**（2026-09-02）。旧: 素の文字列。`Error:` で始まらないので
        #   `tool_failed` の語彙表に当たらず、**何も観測できていないのに ok=True**
        #   （画面に緑）として記録されていた。
        #   ★これは `remote_read` が 2026-09-01 に直したのと**同じ形**で、
        #     そちらだけ `ToolResult(..., ok=False)` になっていた——また片側だけ。
        #     この道具は実機で 197 回使われている（Bash/Read/TodoWrite に次ぐ常用）。
        return ToolResult(f"Error: [SystemState {verb}] タイムアウト（{_TIMEOUT}秒）",
                          ok=False, kind="error")
    except OSError as e:
        # ★道具が無いだけなら、別の取り方を試す（2026-09-15）。
        #   最小コンテナに iproute2 が無く `listening` が丸ごと answerless になっていた。
        if verb == "listening":
            for alt in (["netstat", "-ltnp"], ["netstat", "-ltn"]):
                try:
                    p = procrun.run(alt, capture_output=True, timeout=_TIMEOUT,
                                    stdin=subprocess.DEVNULL)
                    break
                except OSError:
                    continue
            else:
                proc = _listening_from_proc()
                if proc:
                    return proc
                return ToolResult(
                    f"Error: [SystemState {verb}] ss も netstat も無く、"
                    "/proc/net/tcp も読めませんでした", ok=False, kind="error")
        else:
            # 例: Windows に `df` が無い。ここも「観測できなかった」であって成功ではない。
            return ToolResult(f"Error: [SystemState {verb}] 実行できません: {e}",
                              ok=False, kind="error")

    from .read import decode_bytes

    def _txt(raw) -> str:
        if raw is None:
            return ""
        return raw if isinstance(raw, str) else decode_bytes(raw)

    _o, _e = _txt(p.stdout).strip(), _txt(p.stderr).strip()
    # ★**stderr を切り落とさない**（同日・`remote_read` と同じ形）。
    #   `(stdout + stderr)[:上限]` は stderr が**後ろ**にあるので、stdout が長いと
    #   失敗の理由だけが消える。上限に収まらないときは stdout を削る。
    if len(_o) + len(_e) <= _MAX_OUT:
        out = (_o + ("\n" if _o and _e else "") + _e).strip()
    else:
        _keep_e = _e[:min(len(_e), max(_MAX_OUT // 2, _MAX_OUT - len(_o)))]
        _room = max(0, _MAX_OUT - len(_keep_e) - 40)
        _cut = len(_o) - _room
        out = ((_o[:_room] + f"\n…（stdout を {_cut} 字略）…\n" if _cut > 0 else _o)
               + ("\n" if _keep_e else "") + _keep_e).strip()
    if verb == "top_memory":
        out = "\n".join(out.splitlines()[:16])
    body = out[:_MAX_OUT]

    # The harness counts. Handing back four raw lines moved the failure rather
    # than removing it: without this tool the model pipes through `wc -l` and
    # gets the number right; with it, the model has to count the lines itself
    # and 44 runs out of 100 reported three units against an output of four.
    # Every part of this project that works follows the same rule — the harness
    # computes and the model reports. Asking a model to count lines is asking it
    # to compute.
    if verb == "failed_units" and body:
        n = len([l for l in body.splitlines() if l.strip()])
        body = f"合計 {n} 件\n{body}"

    if not body:
        # An empty result is an answer, and saying so plainly matters more here
        # than anywhere else: "no failed units" is the claim the measurements
        # caught being made against four bullets of output.
        body = "(出力なし＝該当ゼロ件)"
    text = f"[SystemState {verb}] exit={p.returncode}\n{body}"
    # ★同じ点検の 2 回目以降は畳む（2026-09-02）。実体は Bash / Glob と**同じ 1 本**
    #   （tools/fold.py: fold_repeat）。実測 8 走行: SystemState は 192 回のうち
    #   **132 回（68.8%）**が道具＋引数＋本文まで完全に同じ反復——全道具で最も高い。
    #   本番のこの関数を掛け直すと 70 件・21,146 バイトが畳めた。
    #   畳みは Bash にしか無く、いちばん反復するこの道具が素通りしていた。
    # ★失敗の理由（`_ERRISH`）は畳んでも必ず残る。run44 の「畳みが理由を隠して
    #   197 回ループした」を繰り返さないため。
    # ★成功も型で名乗る（2026-09-21・read.py / glob.py と同じ理由）。素の str で
    #   返すと `as_result` が**出力の中身**で成否を当てにいく。
    #   ★この道具は自分の終了コードを**本文に書いている**（`exit={p.returncode}`）のに
    #   型では名乗っていなかった。点検の出力には `listening` の一覧や
    #   `failed_units` の内容が入るので、語彙表の語を踏みやすい。
    return ToolResult(
        fold.fold_repeat("SystemState", verb, None, text, text, what="この点検"),
        ok=(p.returncode == 0), kind="ok" if p.returncode == 0 else "error")


SPEC = ToolSpec(
    name="SystemState",
    description=(
        "L.A.P.S 自身が動いている**このマシン**の状態を読み取る（読み取り専用・項目を選ぶ形式）。"
        "★別のホスト（IP やホスト名で名指しされた相手。例: 192.0.2.160）の状態はこれでは読めない＝Bash で `ssh <host> '<コマンド>'` を使う。"
        "依頼が別ホストの話なら、このマシンの load/memory/disk を先に読まない。"
        # ★measured 2026-09-03 dogfood: 「推論機 の GPU を確認して」に SystemState×3 を先に呼び、答えにも自ホストの数字が混ざった。
        "出力は要約せず全部返すので、集計や整形のためにパイプを使う必要がない。"
        "systemd の failed ユニットやディスク使用率を調べるときは、"
        "Bash でコマンドを組み立てるより**こちらを使うこと**。"
        f"項目: {', '.join(f'{k}={v[1]}' for k, v in sorted(_verbs().items()))}"
        # ★項目は Linux の argv で固定されている（下の _VERBS）。Windows では
        #   どれも実行できないので、そこで粘られるより先に逃げ道を書いておく。
        #   ここを書かないと、Windows で `df` や `free` を Bash に渡す方向へ戻る。
        "。★ここに挙がっていない項目は扱えない。また Windows では動かない"
        "（Windows では Bash で systeminfo / tasklist / ipconfig 等を使うこと）"
    ),
    schema={
        "type": "object",
        "properties": {
            "verb": {"type": "string", "enum": sorted(_VERBS),
                     "description": "調べる項目"},
        },
        "required": ["verb"],
    },
    readonly=True,
    handler=_run,
)
