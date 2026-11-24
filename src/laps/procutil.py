"""プロセス表・プロセスグループ・ポートの持ち主を **/proc から直接**読む。

★なぜ 1 本にまとめるか（2026-09-05・第4回b の統合）:
  同じ「プロセスを見る」処理が `tools/bash.py`（名前撃ちの絞り込み）にだけあり、
  `tools/bash_output.py`（背景処理の停止）と `phases.py` は同じ情報を持たない。
  この現場が繰り返し踏んでいるのは「同じ物を 2 つ持つと片方だけ直る」形なので、
  最初から 1 本にする。第4回では実際に `core-server-lifecycle` と `tools-process`
  の 2 本が**同じ表を別々に**作り、唯一の衝突になった。

★`ps` / `pgrep` / `lsof` を**呼ばない**。理由は 2 つとも実測:
  ① `pgrep -f <語>` は自分の argv に一致して**自分を撃つ**（COMMON.md・1 日 5 回）。
  ② `ss` / `lsof` はこの機械に在る保証が無く、無いときに黙って「持ち主なし」に
     倒れる（＝居残りサーバを「いない」と報告する）。/proc は Linux なら必ず在る。
"""
from __future__ import annotations

from . import paths as _lp
import os
import re
import sys
from pathlib import Path

#: /proc が読める OS でだけ働く（Windows では全部「空」を返す）。
POSIX = sys.platform != "win32"


def _read(path: str, binary: bool = False):
    try:
        if binary:
            with open(path, "rb") as fh:
                return fh.read()
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return b"" if binary else ""


def pids() -> list[int]:
    if not POSIX:
        return []
    try:
        return sorted(int(d) for d in os.listdir("/proc") if d.isdigit())
    except OSError:
        return []


def _stat_fields(pid: int) -> list[str]:
    """`/proc/<pid>/stat` を「最後の `)` から後ろ」で割った残り。

    ★comm（2 番目）は括弧に囲まれ、**中に空白も括弧も入りうる**（`(php -S)` 等）。
      素朴な `split()` は必ずいつか壊れるので、**最後の `)` から後ろ**を割る。
    rest[0]=state, rest[1]=ppid, rest[2]=pgrp … rest[19]=starttime（stat の 22 番目）。
    """
    raw = _read(f"/proc/{pid}/stat")
    if not raw:
        return []
    i = raw.rfind(")")
    if i < 0:
        return []
    return raw[i + 2:].split()


def pgid_of(pid: int) -> int | None:
    """`/proc/<pid>/stat` の 5 番目 = プロセスグループ ID。"""
    rest = _stat_fields(pid)
    if len(rest) < 3 or not rest[2].lstrip("-").isdigit():
        return None
    return int(rest[2])


def ppid_of(pid: int) -> int | None:
    """`/proc/<pid>/stat` の 4 番目 = 親 PID。"""
    rest = _stat_fields(pid)
    if len(rest) < 2 or not rest[1].lstrip("-").isdigit():
        return None
    return int(rest[1])


def start_ticks(pid: int) -> int | None:
    """`/proc/<pid>/stat` の 22 番目 = そのプロセスが起きた時刻（起動からの tick）。

    ★何に使うか: PID は使い回される。`~/.blackwall/bg` の台帳は「この PID は自分が
      起こした子」の通行証になっているので、**番号だけで同一視すると別人に通行証を
      渡す**（この機械の台帳には 8/6 以来の記録が 23 件、全部死亡済みで残っていた）。
      台帳の `start_ticks` / `started_at` とこの値を突き合わせる。
    """
    rest = _stat_fields(pid)
    if len(rest) < 20 or not rest[19].isdigit():
        return None
    return int(rest[19])


#: 旧名（`core-server-lifecycle` 側が使っていた綴り）。同じ物を 2 つ持たない。
proc_start_ticks = start_ticks


def _boot_epoch() -> float | None:
    """`/proc/stat` の `btime`（起動時刻・epoch 秒）。"""
    for line in _read("/proc/stat").splitlines():
        if line.startswith("btime "):
            v = line.split()[1]
            if v.isdigit():
                return float(v)
    return None


def _clock_ticks() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK")) or 100.0
    except (ValueError, OSError, AttributeError):
        return 100.0


def start_epoch(pid: int) -> float | None:
    """プロセスが起きた壁時計（epoch 秒）。取れなければ None。"""
    t = start_ticks(pid)
    b = _boot_epoch()
    if t is None or b is None:
        return None
    return b + t / _clock_ticks()


def ancestors(pid: int, limit: int = 64) -> list[int]:
    """`pid` から根まで遡った祖先の PID（`pid` 自身は含まない）。

    ★なぜ要るか: 道具を実行しているのは `laps.serve` の子で、その親は `term`。
      **自分と祖先を撃つのは自殺**なので、所有権判定より先に落とす必要がある。
      `proc_rows` は自分と親を飛ばすので、飛ばされた分は「不明＝素通し」に化ける
      （第4回の反証で実測）。ここで明示的に数える。
    """
    out: list[int] = []
    if not POSIX:
        return out
    cur = pid
    for _ in range(limit):
        nxt = ppid_of(cur)
        if not nxt or nxt in out or nxt == cur:
            break
        out.append(nxt)
        if nxt <= 1:
            break
        cur = nxt
    return out


def proc_rows(skip_self: bool = True) -> list[dict]:
    """全プロセスを dict で返す（pid / comm / cmdline / cwd / pgid / ppid / argv）。

    ★`argv`（2026-09-05・第4回b）: `cmdline` はスペースで結合済みの 1 本の文字列で、
      **トークン境界が失われている**。「作業場の中か」をトークン単位で見るために
      別に持つ（`in_workspace` が使う）。表示・PATTERN 検索は従来どおり `cmdline`。
    """
    out: list[dict] = []
    if not POSIX:
        return out
    me, pp = os.getpid(), os.getppid()
    for pid in pids():
        if skip_self and pid in (me, pp):
            continue
        raw = _read(f"/proc/{pid}/cmdline", binary=True)
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if not cmd:
            continue                      # カーネルスレッド
        argv = tuple(a.decode("utf-8", "replace") for a in raw.split(b"\0") if a)
        comm = _read(f"/proc/{pid}/comm").strip()
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = ""
        out.append({"pid": pid, "comm": comm, "cmdline": cmd, "cwd": cwd,
                    "pgid": pgid_of(pid), "ppid": ppid_of(pid), "argv": argv})
    return out


def proc_table() -> list[tuple[int, str, str, str, tuple[str, ...]]]:
    """`(pid, comm, cmdline, cwd, argv)` の一覧（`tools/bash.py` が使ってきた形＋argv）。

    ⚠ 2026-09-05 に **4-tuple から 5-tuple へ広げた**（所有権判定がトークンを要る）。
      呼び出し側は `for pid, comm, cmdline, cwd, argv in proc_table()`。
      古い 4 要素で差し替えている試験が残っていても壊れないよう、tuple を受ける側は
      `bash._row()` / `bash._proc_rows()` を通すこと。
    """
    return [(r["pid"], r["comm"], r["cmdline"], r["cwd"], r["argv"]) for r in proc_rows()]


def group_members(pgid: int, *, skip_self: bool = True) -> list[dict]:
    """プロセスグループ `pgid` に残っているプロセス。

    ★`start_new_session=True` で起動した Bash は**自分が session/group の長**なので、
      その pgid はこの 1 回の実行に固有＝ここに入るのは自分の子孫だけ。
    """
    if not POSIX:
        return []
    return [r for r in proc_rows(skip_self=skip_self) if r["pgid"] == pgid]


# ── ポートの持ち主 ──────────────────────────────────────────────────────
#: /proc/net/tcp の `st` 列。0A = TCP_LISTEN。
_ST_LISTEN = "0A"


def _socket_inodes(port: int, *, listen_only: bool = True) -> set[str]:
    """そのポートで待ち受けているソケットの inode 番号。"""
    inodes: set[str] = set()
    for name in ("tcp", "tcp6"):
        raw = _read(f"/proc/net/{name}")
        for line in raw.splitlines()[1:]:
            f = line.split()
            if len(f) < 10:
                continue
            local = f[1]
            if ":" not in local:
                continue
            try:
                lport = int(local.rsplit(":", 1)[1], 16)
            except ValueError:
                continue
            if lport != port:
                continue
            if listen_only and f[3] != _ST_LISTEN:
                continue
            inodes.add(f[9])
    return inodes


def _procs_holding_sockets(inodes: set[str]) -> list[dict]:
    """そのソケット inode を fd に持っているプロセス。**/proc を 1 回だけ走る**。

    ★2026-09-05（第4回c）: ポートごとに `port_listeners` を呼ぶと、保護ポートの
      数だけ全プロセスの `fd/` を舐め直すことになる（保護は既定 3 ポート）。
      kill 系の命令が来るたびに 3 周するのは無駄なので、inode の集合で 1 回にする。
    """
    if not POSIX or not inodes:
        return []
    want = {f"socket:[{i}]" for i in inodes}
    out: list[dict] = []
    for r in proc_rows(skip_self=False):
        fd_dir = f"/proc/{r['pid']}/fd"
        try:
            entries = os.listdir(fd_dir)
        except OSError:
            continue                       # 権限が無い＝見えないだけ
        for fd in entries:
            try:
                if os.readlink(os.path.join(fd_dir, fd)) in want:
                    out.append(r)
                    break
            except OSError:
                continue
    return out


def port_listeners(port: int) -> list[dict]:
    """ポート `port` を **LISTEN で掴んでいる**プロセス（pid/comm/cmdline/cwd/pgid）。

    ★見えない場合がある（他ユーザのプロセスは `/proc/<pid>/fd` を読めない）。
      「見えない」を「いない」と言わないため、呼び出し側は
      `port_in_use()` と併せて使うこと。
    ★**自分自身も含めて返す**（`skip_self=False`）。自分が掴んでいるポートを
      「持ち主なし」と報告するのは嘘で、その嘘のまま撃たせるほうが危ない。
      撃ってよいかの判断は呼び出し側（自分と親は必ず除外する）。
    """
    if not POSIX:
        return []
    return _procs_holding_sockets(_socket_inodes(port))


def port_in_use(port: int) -> bool:
    """誰かが LISTEN しているか（持ち主が見えなくても分かる）。"""
    return bool(_socket_inodes(port))


# ── 撃たないポート ──────────────────────────────────────────────────────
#: 「エージェント自身の設備」として既定で撃たないポート。
#: ★18300 は**入れない**。あれは走行の作業場が自分で建てるサーバで、居残りを
#:   片付けられないと run50 の工程4 の壁（`Address already in use` で建て直せない）が
#:   そのまま再来する。ここに載せるのは「作業場の中に見えても、実際は基盤側の物」
#:   だけ: 22=sshd／18301=閲覧用の socat 中継／18400=Android ゲートウェイ
#:   （2026-09-03 の dogfood で、実際に gateway の unit を撃って本番を壊している）。
_DEFAULT_PROTECTED_PORTS = (22, 18301, 18400)


#: 区切りとして受ける文字（`,` `;` と**あらゆる空白＝空白/タブ/改行**）。
_PORT_SEP_RE = re.compile(r"[,;\s]+")

#: ★`LAPS_PROTECTED_PORTS` の書き損じを**呼び出し元が拾える形**で残す
#:   （2026-09-05・第4回c の 5 回目・反証 C(9)）。旧実装は `warnings.warn` だけで、
#:   (a) 出先は stderr、(b) 既定の `once` 規則で**プロセスに 1 回**しか出ず、
#:   (c) Bash 道具の結果本文にも counters にも一切現れなかった。
#:   ＝「設定を書き損じて保護が思ったとおりでない」ことを、モデルも人も
#:   **その場では絶対に知れない**。警告は残したうえで、状態としても持つ。
_PORTS_CONFIG_PROBLEM: str | None = None


def ports_config_problem() -> str | None:
    """直近の `protected_ports()` が見つけた設定の書き損じ（無ければ None）。"""
    return _PORTS_CONFIG_PROBLEM


def protected_ports() -> set[int]:
    """撃たないポート。`LAPS_PROTECTED_PORTS` があれば**置き換える**
    （空文字を渡せば保護なしにできる＝逃げ道を塞がない）。

    ★塞いだ穴（2026-09-05・第4回c の再反証・実測）: 旧実装は区切りを `,`/`;` に
      しか対応せず、数値でない語を**黙って捨てて**いた。だから

        LAPS_PROTECTED_PORTS="22 18301 18400"   （空白区切り）
        LAPS_PROTECTED_PORTS="22,abc,18400"     （綴り間違い）
        LAPS_PROTECTED_PORTS="0x4ce0"           （16 進のつもり）

      のどれもが **保護ゼロ（または一部だけ）** に落ちる——`docker run -e` や
      `.env` の書き方ひとつで、看板だけ残して床が消える形。設定の書き損じが
      **静かに保護を外す**のは、この現場が何度も踏んだ「効いているのか壊れて
      いるのか区別できない」形そのもの。
      → 区切りは `,`/`;`/空白/改行を全部受け、**読めない語が 1 つでもあれば
        警告して既定を保つ**（fail-closed。読めた番号は既定に足す）。
    ★`""`（明示の空文字）だけは従来どおり「保護なし」＝意図した逃げ道として残す。
      試験 `test_保護を外せば自分のサーバは撃てる` が守っている道。
    """
    global _PORTS_CONFIG_PROBLEM
    _PORTS_CONFIG_PROBLEM = None
    raw = os.environ.get("LAPS_PROTECTED_PORTS")
    if raw is None:
        return set(_DEFAULT_PROTECTED_PORTS)
    if raw == "":
        return set()                      # ★明示の「保護なし」（逃げ道）
    if not raw.strip():
        # ★空白だけ（2026-09-05・第4回c の 4 回目）: `LAPS_PROTECTED_PORTS=" "` は
        #   **警告も無く保護ゼロ**に落ちていた。`docker run -e X=" "` や `.env` の
        #   行末の空白ひとつで床が消える形で、`"22,abc"` を fail-closed にした §M4 の
        #   直しと同じ穴が「空白だけ」に残っていた。意図した逃げ道は**明示の空文字**
        #   だけにして、非空白が 1 つも無いなら既定を保って知らせる。
        msg = ("LAPS_PROTECTED_PORTS が空白だけです（保護を外すなら空文字 \"\" を"
               f"指定してください）。既定 {sorted(_DEFAULT_PROTECTED_PORTS)} を保ちます。")
        _PORTS_CONFIG_PROBLEM = msg
        import warnings
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        return set(_DEFAULT_PROTECTED_PORTS)
    out: set[int] = set()
    bad: list[str] = []
    for tok in _PORT_SEP_RE.split(raw.strip()):
        if not tok:
            continue
        try:
            v = int(tok, 10)              # ★10 進のみ（`0x4ce0` は読めない語）
        except ValueError:
            bad.append(tok)
            continue
        if 0 < v < 65536:
            out.add(v)
        else:
            bad.append(tok)
    if bad:
        msg = ("LAPS_PROTECTED_PORTS に読めない語があります: "
               + ", ".join(repr(b) for b in bad[:5])
               + "（10 進の 1〜65535 を `,`/`;`/空白で区切ってください）。"
               f"保護を黙って外さないため、既定 {sorted(_DEFAULT_PROTECTED_PORTS)} を"
               "残したうえで、読めた分を足します。")
        _PORTS_CONFIG_PROBLEM = msg
        import warnings
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        out |= set(_DEFAULT_PROTECTED_PORTS)
    return out


def protected_port_pids() -> set[int]:
    """保護ポート（`protected_ports()`）を LISTEN しているプロセスと、その
    **プロセスグループの仲間**の pid。＝`kill`／`pkill` から守る相手。

    ★塞いだ穴（2026-09-05・第4回c・実物で再現した）: 保護ポートは
      `bash_output._stop_group` の**ポート経由の停止にしか**効いていなかった。
      作業場が `/home/user/project` のとき、本番の Android ゲートウェイ
      （`python -m laps.gateway`・cwd が `/home/user/project/src`）は
      `in_workspace` で堂々と「中」に入るので、
        * `pkill -f gateway` → 名前撃ちゲートは inside=3 / outside=0 ＝**無変更で通す**
        * `kill <gateway の pid>` → PID ゲートも「中」＝**そのまま通す**
      となり、2026-09-03 の dogfood で実際に壊した相手を**もう一度**撃てた。
      pid は再起動のたびに変わるので、番号ではなく**ポートの持ち主**で引く。
    ★グループの仲間まで含めるのは、`sh -c "… python -m gateway"` のように
      ソケットを握るのが子で、撃たれるのが親（グループの長）になる形があるため。
      グループ番号が 1 以下（init／不定）のときは広げない。
    """
    out: set[int] = set()
    if not POSIX:
        return out
    ports = protected_ports()
    if not ports:
        return out
    inodes: set[str] = set()
    for port in ports:
        inodes |= _socket_inodes(port)
    if not inodes:
        return out
    pgids: set[int] = set()
    for r in _procs_holding_sockets(inodes):
        pid = r.get("pid")
        if isinstance(pid, int) and pid > 0:
            out.add(pid)
        g = r.get("pgid")
        if isinstance(g, int) and g > 1:
            pgids.add(g)
    if pgids:
        for r in proc_rows(skip_self=False):
            if r.get("pgid") in pgids:
                out.add(r["pid"])
    return out


# ── 作業場の内か外か（**所有権判定はこの 1 本だけ**）────────────────────
def _within(path: str, root: str) -> bool:
    """`path` が `root` の中か。**区切りつきの前方一致**（`agent.py:_workspace_guard`
    と同じ形）。どちらも正規化済みの前提。

    ★裸の `startswith` をやめた理由（2026-09-05・第4回b の反証で 4 例とも実測）:
      `/root/vdirwork/run5` が `/root/vdirwork/run53` に、`/home/user/project` が
      **保護対象の 13GB ツリー** `/home/user/project-restore` に、
      `/home/user/project/wt/r4-core` が並走 worktree `…/r4-core-server-lifecycle` に
      当たっていた。名前撃ちでは「絞る」だけだったこの緩さが、ポート経由の停止では
      **撃つ許可**になっていた。
    """
    if not path or not root:
        return False
    root = root.rstrip(os.sep) or os.sep
    return path == root or path.startswith(root + os.sep if root != os.sep else os.sep)


def path_within(path: str, ws: str) -> bool:
    """`path`（realpath して）が `ws` の中かを**境界つき**で判定する。"""
    if not path or not ws:
        return False
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    return _within(real, ws.rstrip(os.sep) or os.sep)


#: 作業場のパスの**直後**に来てよい文字（ここで切れていれば別のパスではない）。
#: ★改行（2026-09-05・第4回c の 4 回目）: `cd <ws>\nphp -S …` のように
#:   **改行で続く**形が「別のパス」と読まれて自分のサーバを所有と判定できなかった。
#:   改行はシェルでは `;` と同じ区切り＝ここで切れていれば作業場そのもの。
_WS_END_OK = set("/ \t\r\n\"'`:;,)&|>=")
#: `-t/path` `--docroot=/path` のような**オプションに貼り付いた**書き方。
_OPT_PREFIX_RE = re.compile(r"^-{1,2}[A-Za-z0-9][A-Za-z0-9-]*=?$")
#: `sh -c "cd /path && …"` のように**そこへ移って動く**形。
#: ★これは**安い前置きの網**でしかない（2026-09-05・第4回c の再反証）。
#:   引用符を許すのは `sh -c "cd /ws && …"` を flatten した `cmdline` を読むためだが、
#:   そのままだと `grep -rn "cd /ws" /etc` の**引用符の中**まで拾う。
#:   本当に実行される `cd` かどうかは `_shell_cd_positions()` が語の位置で決める。
_CD_PREFIX_RE = re.compile(r"""(?:^|[;&|("'`]|\s)\s*(?P<kw>cd|pushd)\s+$""")

#: 語の区切りになるシェルの記号（引用符は別扱い）。
_SH_BREAK = ";&|\n()<>"
#: 「そのまま次の語がコマンド」になる前置き。`cd` の直前に来ても実行されるので通す。
_CMD_PASSTHROUGH = frozenset({
    "then", "do", "else", "elif", "!", "exec", "command", "builtin", "eval",
    "env", "nohup", "setsid", "stdbuf", "nice", "ionice", "time", "unbuffer",
})
#: 変数代入の前置き（`FOO=1 cd /ws`）。
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: `sh -c` / `bash -lc` のような「次の語がスクリプト本体」になる短オプション。
_DASH_C_RE = re.compile(r"^-[A-Za-z]*c$")


def _shell_cd_positions(text: str) -> set[int]:
    """`text` をシェルの語として読み、`cd`/`pushd` が**コマンド位置**に現れる開始位置。

    ★なぜ位置で見るか（2026-09-05・第4回c の再反証が 4 形で実測）: 綴りだけで
      判定すると、**引用符の中**の `cd <ws>` まで「作業場を土台にしている」に化ける。

        grep -rn "cd /home/user/project" /etc      ← ただの検索語
        echo 'cd /home/user/project'               ← ただの文字列
        sed -i "s|cd /home/user/project|…|" f      ← ただの置換パターン
        printf "cd /home/user/project"             ← ただの書式

      いずれも `cd` は**実行されない**のに所有の証拠になり、その行のプロセスが
      「自分の物＝撃ってよい」に化けていた（`pkill -f` が自分の argv に一致する
      のと同じ、字面で意味を決める穴）。
    ★引用の中は丸ごと飛ばす（中で `cd` が動くことはない）。`$( … )` の中は
      **飛ばさない**（あそこは本当にシェルが走る）。
    ★`-c` の直後を「コマンド位置」に戻すのは、`/proc/<pid>/cmdline` を空白で
      継いだ形（`sh -c cd /ws && php -S …`＝引用符が消えている）を読むため。
    """
    out: set[int] = set()
    i, n = 0, len(text)
    at_cmd = True                          # 直前が区切り＝次の語はコマンド
    while i < n:
        ch = text[i]
        if ch in ("'", '"', "`"):
            q = ch
            i += 1
            while i < n:
                if text[i] == "\\" and q == '"':
                    i += 2
                    continue
                if text[i] == q:
                    i += 1
                    break
                i += 1
            at_cmd = False                 # 引用の語はコマンド名になり得るが cd ではない
            continue
        if ch.isspace():
            i += 1
            continue
        if ch in _SH_BREAK:
            at_cmd = True
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace() and text[j] not in _SH_BREAK \
                and text[j] not in "'\"`":
            j += 1
        word = text[i:j]
        if at_cmd:
            if word in ("cd", "pushd"):
                out.add(i)
                at_cmd = False
            elif word in _CMD_PASSTHROUGH or _ASSIGN_RE.match(word) \
                    or os.path.basename(word) in _CMD_PASSTHROUGH:
                at_cmd = True              # 前置き＝次の語がまだコマンド
            else:
                at_cmd = False
        elif _DASH_C_RE.match(word) or word == "--":
            at_cmd = True                  # `sh -c <script>` の <script> は本体
        else:
            at_cmd = False
        i = j if j > i else i + 1
    return out
#: 作業場パスの直前に来てよい区切り（`mentions_workspace` の従来の許容範囲）。
_WS_HEAD_OK = set(" \t\"'`=:;,(")


def _ws_hits(text: str, ws: str) -> list[str]:
    """`text` の中に `ws` が**パスとして**現れる箇所の種類を並べる。

    種類は 3 つ:
      * ``"cd"``   … `cd <ws>` / `pushd <ws>`（**そこへ移って動く**＝土台にしている）
      * ``"opt"``  … `-t<ws>/site` / `--docroot=<ws>`（**貼り付いた**オプションの値）
      * ``"bare"`` … それ以外（`tail -f <ws>/x.log` のような**ただの参照**）

    ★境界を見る理由（実測・両反証が同じ形を指した）: 素の部分文字列一致は
      `/work/ws1` が `/work/ws10-other-project/...` の途中に当たっても『中』に
      化けた（`r4` と `r42` と同じ穴）。両端で切れているかを確かめる。
    ★逆に「絶対パスのトークンだけ」に寄せると、`_spawn_background` が実際に作る
      argv（`sh -c "cd <ws> && php -S …"`・1 トークン）や `-t<ws>/site` を
      取りこぼす（第4回b の A/B で旧＝中／新＝外と出た）。だから種類を分けて返し、
      **どれを所有の根拠にするかは呼ぶ側が決める**。
    """
    out: list[str] = []
    if not text or not ws:
        return out
    n = len(ws)
    i = 0
    cds: set[int] | None = None            # `cd` の**実行される**位置（遅延計算）
    while True:
        i = text.find(ws, i)
        if i < 0:
            return out
        end = i + n
        if end == len(text) or text[end] in _WS_END_OK:
            head = text[:i]
            m = _CD_PREFIX_RE.search(head)
            if m is not None:
                # ★綴りだけでは足りない（第4回c の再反証）。その `cd` が
                #   **シェルの語としてコマンド位置に在る**ときだけ所有の証拠。
                #   引用符の中（`grep -rn "cd <ws>" /etc`・`echo 'cd <ws>'`・
                #   `sed -i "s|cd <ws>|…|"`・`printf "cd <ws>"`）は数えない。
                if cds is None:
                    cds = _shell_cd_positions(text)
                if m.start("kw") in cds:
                    out.append("cd")
                    i = end
                    continue
            j = max(head.rfind(" "), head.rfind("\t"),
                    head.rfind('"'), head.rfind("'"), head.rfind("`"))
            prefix = head[j + 1:]
            if not prefix:
                out.append("bare")
            elif _OPT_PREFIX_RE.match(prefix):
                out.append("opt")
            elif text[i - 1] in _WS_HEAD_OK:
                out.append("bare")
        i = end


def mentions_workspace(text: str, ws: str) -> bool:
    """命令行の文字列 `text` の中に、作業場 `ws` のパスが**パスとして**出てくるか。
    ★**列挙・注記の材料**。撃つ根拠にするのは `roots_at_workspace` だけ。"""
    return bool(_ws_hits(text, ws))


#: `-c` にスクリプトを取るシェル（`argv[0]` の basename で見る）。
_SHELL_PROGS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh", "busybox"})
#: シェルの前に挟まる「そのまま次を起こす」道具。`timeout` だけ引数を 1 つ食う。
_EXEC_WRAPPERS = frozenset({"env", "nohup", "setsid", "stdbuf", "nice", "ionice",
                            "timeout", "time", "chrt", "unbuffer"})
_WRAPPERS_TAKING_ONE_ARG = frozenset({"timeout", "chrt"})


def shell_script_args(argv) -> list[str]:
    """`argv` のうち、**シェルにスクリプトとして渡されている語**だけを返す。

    ★なぜ要るか（2026-09-05・第4回c の反証が実物で示した）: `cd <ws> …` という
      綴りを **argv のどのトークンでも**「作業場を土台にしている」と読むと、
      `grep -rn "cd /home/user/project" /etc` や `echo 'cd /home/user/project'` の
      **ただの引数**まで所有の証拠になる（`pkill -f` が自分の argv に当たる族と
      同じ形——語の意味ではなく字面で判定してしまう穴）。
      `cd` が本当に実行されるのは「シェルが `-c` で受け取った語」の中だけなので、
      **位置で**決める。
    ★取りこぼしを増やさないため、`env`/`nohup`/`setsid`/`timeout` 等の
      「そのまま次を起こす」道具は読み飛ばす。それ以外の入れ子（`sudo`・
      `su -c`・自作ラッパ）は見ない（＝所有と読まない側に倒れる）。
    """
    argv = list(argv or ())
    if not argv:
        return []
    i = 0
    while i < len(argv) and os.path.basename(argv[i]) in _EXEC_WRAPPERS:
        w = os.path.basename(argv[i])
        i += 1
        while i < len(argv) and (argv[i].startswith("-") or "=" in argv[i]):
            i += 1
        if w in _WRAPPERS_TAKING_ONE_ARG and i < len(argv):
            i += 1                        # `timeout 30 sh -c …` の 30
    if i >= len(argv) or os.path.basename(argv[i]) not in _SHELL_PROGS:
        return []
    out: list[str] = []
    for j in range(i + 1, len(argv)):
        a = argv[j]
        # `-c` / `-lc` / `-ec` のように **c で終わる短オプション束**が対象。
        if a == "-c" or (len(a) <= 5 and a.startswith("-") and not a.startswith("--")
                         and a.endswith("c")):
            if j + 1 < len(argv):
                out.append(argv[j + 1])
            break
        if not a.startswith("-"):
            break                         # `sh script.sh` はスクリプトを -c で受けない
    return out


def roots_at_workspace(text: str, ws: str, *, kinds: tuple[str, ...] = ("cd", "opt")) -> bool:
    """`text` が「作業場を**土台にして**動いている」形か（`cd <ws>` / `-t<ws>`）。

    ★`kinds`（2026-09-05・第4回c）: `cd` の形は**シェルが実行する語**でだけ
      所有の証拠になる（`shell_script_args` 参照）。素の argv トークンに対しては
      `kinds=("opt",)` で呼ぶこと。

    ★なぜ `mentions_workspace` と分けるか（2026-09-05・第4回b の統合で決めた）:
      反証②（core-server-lifecycle）の must_fix は
      「`ws in cmdline` の部分一致は**列挙にだけ使い、kill の根拠にしない**
      （cwd が外なら撃たない）」——`/tmp` に居る `tail -f <ws>/x.log` まで
      「自分の物」にしていたのが実害だった。
      反証①（tools-process）の must_fix 5 は
      「**非絶対トークン中**の作業場パス（`-t/path`・`--docroot=/path`・
      `sh -c "cd /path && …"`）も所有シグナルとして拾い直す」——こちらを落とすと
      自分のサーバを止められない run50 工程4 の壁が戻る。
      両方を同時に満たす線が「**土台にしている形だけ所有、裸の参照は列挙だけ**」。
    """
    return any(k in kinds for k in _ws_hits(text, ws))


#: 作業場が**床に届かない**ときの値。実在し得ないパスなので、`path_within` /
#: `in_workspace` はここを基準にすると何ひとつ「中」にしない（＝全部断る）。
#: ★空文字にしてはいけない: `bash._scope_*_kill` は `not workspace` で
#:   **何も見ずに素通し**するので、空にすると床が fail-open になる（逆効果）。
NO_WORKSPACE = "/nonexistent/blackwall-no-workspace"

#: 作業場として受ける最小の深さ。`/home/user/project` は 2・`/root` は 1。
_WS_MIN_DEPTH = 2

#: 深さ 1 でも作業場として受けない所（システムと共用の場所）。★2026-10-23 Terminal-Bench の結果の確認: 課題の作業場は `/app`
#: （コンテナの定番）で、深さ 1 なので作業場と見なされず NO_WORKSPACE になり、モデルが自分で背景に起こしたサーバーまで
#: BashOutput が「この作業場のものではありません」と断った（ベンチ 8 回）。浅い所を一律に断るのをやめ、危ない所だけを名指しする。
_WS_SHALLOW_DENY = frozenset({
    "bin", "boot", "dev", "etc", "home", "lib", "lib32", "lib64", "libx32", "lost+found", "media", "mnt", "opt", "proc",
    "root", "run", "sbin", "snap", "srv", "sys", "tmp", "usr", "var", "users", "applications", "library", "system",
    "volumes", "private", "cores", "nix", "gnu",
})


def workspace_floor_ok(path: str) -> bool:
    """その場所を「作業場」として受けてよいか（`/`・`$HOME`・深さ 1 は受けない）。

    ★塞いだ穴（2026-09-05・第4回c・実測）: `serve._apply_env` は依頼の
      `spec.workspace` を**検証せずに** `LAPS_WORKSPACE` へ入れて chdir する。
      `/` を渡されると `in_workspace` はホストのほぼ全プロセスを「中」と判定し
      （反証の実測 288 本中 278 本／私の再測 **260 本中 260 本＝全部**）、
      kill 系ゲートは**撃つ許可**を出す側に化ける
      （ゲートが在ることが、かえって「確認済み」の顔をする）。
      浅すぎる場所は作業場として受け取らない。
    """
    if not path:
        return False
    try:
        p = os.path.realpath(path)
    except (OSError, ValueError):
        return False
    if p == os.sep:
        return False
    parts = [x for x in p.split(os.sep) if x]
    if len(parts) < _WS_MIN_DEPTH:
        # 深さ 1 は、システムと共用の場所でなければ受ける（`/app`・`/work`・`/workspace` はコンテナの作業場）。
        if len(parts) != 1 or parts[0].lower() in _WS_SHALLOW_DENY or sys.platform == "win32":
            return False
    try:
        home = os.path.realpath(os.path.expanduser("~"))
    except (OSError, ValueError, RuntimeError):
        home = ""
    if home and p == home:
        return False
    return True


def canonical_workspace() -> str:
    """正典の作業場 = `policy._workspace()`（`LAPS_WORKSPACE` 既定 cwd）。

    ★**道具の引数から取らない**。`cwd` は Bash / BashOutput がモデルに開いている
      引数なので、そこを基準にすると「何を撃ってよいか」をモデル自身が決められる
      （`cwd="/"` を渡せば全ホストが作業場になり／逆に `cwd="<ws>/site"` を渡すと
      自分のサーバが『外』に化けて撃てなくなる）。
      ラボの研究知見 `ai-agent-research-notes.md`「実行可否はモデルの外側に置く／
      権限定義をエージェント自身に書かせない」に従う。
    ★**床がある**（2026-09-05・第4回c）: 環境変数のほうも無検証ではない。
      `/`・`$HOME`・深さ 1 の場所は作業場として受けず `NO_WORKSPACE` を返す
      ＝何も「自分の物」にならない（撃たない側へ倒す）。
    """
    try:
        from . import policy
        ws = os.path.realpath(str(policy._workspace()))
    except Exception:
        try:
            ws = os.path.realpath(os.getcwd())
        except (OSError, ValueError):
            return NO_WORKSPACE
    return ws if workspace_floor_ok(ws) else NO_WORKSPACE


def bg_workspace_field() -> str:
    """台帳（`~/.blackwall/bg/<pid>.meta.json`）の `workspace` 欄に書く値。

    ★must_fix B-1（union-2・2026-09-06）: `canonical_workspace()` が床に届かず
      `NO_WORKSPACE` を返すとき、それを**そのまま**書くと `bg_record_workspace_ok`
      の自己一致バグ（`NO_WORKSPACE == NO_WORKSPACE`）で「作業場が無い」セッション
      どうしが互いの記録を通行証にしてしまう（`bg_record_workspace_ok` 側は塞いだが、
      書く側が `NO_WORKSPACE` という**特定の文字列**を渡さなければ根本から起きない）。
      空文字にする（欄はあるが値が空＝`bg_record_workspace_ok` は
      `not isinstance(rec_ws, str) or not rec_ws` で fail-closed に断る）。
    """
    ws = canonical_workspace()
    return "" if ws == NO_WORKSPACE else ws


def workspace_note() -> str:
    """作業場が床に届いていないときに、注記へ添える 1 行（普段は空）。
    ★黙って全部断ると「なぜ撃てないのか」が分からない（この現場が何度も踏んだ
      『効いているのか壊れているのか区別できない』形）ので、理由を出す。"""
    if canonical_workspace() != NO_WORKSPACE:
        return ""
    raw = os.environ.get("LAPS_WORKSPACE") or os.getcwd()
    return ("\n   ★作業場が設定されていません（LAPS_WORKSPACE="
            f"{raw!r} は `/`・ホーム・深さ 1 で、作業場として受けません）。"
            "この状態では**どのプロセスも『自分の物』になりません**。")


#: 別名（tools-process 側が使っていた綴り）。同じ物を 2 つ持たない。
owner_workspace = canonical_workspace


def narrow_workspace(hint=None) -> tuple[str, str]:
    """道具引数の `cwd` を **狭める方向にしか効かせない**。

    戻り: `(実効の作業場, 添える注記)`。`hint` が作業場の外なら無視して理由を返す。
    """
    ws = canonical_workspace()
    if ws == NO_WORKSPACE:
        return ws, workspace_note().strip()
    if not hint:
        return ws, ""
    try:
        h = os.path.realpath(str(hint))
    except (OSError, ValueError):
        return ws, ""
    if _within(h, ws):
        return h, ""
    return ws, (f"（cwd={hint} は作業場 {ws} の外なので**無視しました**。"
                "cwd は範囲を狭めるのにだけ使えます。）")


# ── 自分が起こした背景処理の台帳 ────────────────────────────────────────
def bg_dir(create: bool = False):
    """`~/.blackwall/bg`（`LAPS_HOME` で移せる）。★同じ 2 行を
    `tools/bash.py` と `tools/bash_output.py` が別々に持っていたのでここへ寄せた。"""
    home = str(_lp.home_dir())
    d = Path(home) / "bg"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def bg_records() -> list[dict]:
    """台帳の生の中身（`<pid>.meta.json`）。読めない物は飛ばす。"""
    out: list[dict] = []
    try:
        files = sorted(bg_dir().glob("*.meta.json"))
    except OSError:
        return out
    for f in files:
        # ★`Path.stem` は**最後の拡張子しか外さない**: `1828868.meta.json` の stem は
        #   `1828868.meta` で `.isdigit()` は False。この 1 行のせいで台帳は
        #   **一度も引けていなかった**（実測: この機械の 23 件すべてが素通り）。
        #   ＝「台帳に載っていれば自分の物」という通行証は、そもそも発行されて
        #   いなかったし、自分のサーバを守る側の効き目も無かった。
        name = f.name.split(".", 1)[0]
        if not name.isdigit():
            continue
        rec = {"pid": int(name), "path": f}
        try:
            rec["mtime"] = f.stat().st_mtime
        except OSError:
            rec["mtime"] = 0.0
        try:
            import json as _json
            data = _json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rec.update({k: v for k, v in data.items() if k != "path"})
        except Exception:
            pass
        rec["pid"] = int(name)            # 本文の pid より**ファイル名**を信じる
        out.append(rec)
    return out


#: 台帳の記録時刻とプロセスの起動時刻がこれ以上ずれていたら「別人」と見なす（秒）。
_BG_START_SLACK_SEC = 300.0
#: 死んだ控えを片付けるまでの既定（時間）。`LAPS_BG_KEEP_HOURS` で変えられる。
_BG_KEEP_HOURS_DEFAULT = 72.0

#: 控えを書いた時刻より**これ以上あとに**生まれた構成員は、控えと同じ番号でも別人。
#: （`bash_output._REL_MEMBER_SLACK_SEC` をここへ 1 本化した・6 巡目）
_BG_MEMBER_SLACK_SEC = 5.0


def _proc_born_epoch(pid: int) -> float | None:
    """`pid` が生まれた壁時計時刻（epoch 秒・読めなければ None）。

    ★`AttributeError` は飲まない（5 巡目・調停係）: `_boot_epoch` / `_clock_ticks`
      の取り違えを「読めない」に丸めると、下の関門が黙って素通りする。
    ★2026-09-06（union-2 の再点検）: lifecycle 単独枝は `_boot_time()`（常に
      `float` を返す旧 API）を直接呼んでいた。union で `_boot_time` は
      `_boot_epoch()`（`float | None`）に改名・置換されており、旧名のまま重ねると
      `NameError` になる（3 点再点検の型・実測で踏んだ）。呼び名を直す。
    """
    st = proc_start_ticks(pid)
    if st is None:
        return None
    boot = _boot_epoch()
    if not boot:
        return None
    return boot + st / _clock_ticks()


def _record_written_epoch(rec: dict) -> float | None:
    """その控えが書かれた時刻（epoch 秒・読めなければ None）。

    ★`bash_output._launch_epoch` と**同じ順**（start_ticks → started_at → mtime）。
      同じ事実を 2 通りに計算しないための 1 本（6 巡目・union の反証役の指摘②）。
    """
    ticks = rec.get("start_ticks")
    if isinstance(ticks, int):
        try:
            boot = _boot_epoch()
            if boot is not None:
                return boot + ticks / _clock_ticks()
        except (OSError, ValueError, ZeroDivisionError):
            pass
    raw = str(rec.get("started_at") or "")
    if raw:
        try:
            from datetime import datetime
            return datetime.fromisoformat(raw).timestamp()
        except (ValueError, TypeError):
            pass
    try:
        m = float(rec.get("mtime") or 0.0)
    except (TypeError, ValueError):
        return None
    return m or None


def _member_now(mpid: int, live_by_pid: dict, pgid: int) -> tuple[int | None, str]:
    """その構成員の**いまの** `(pgid, cmdline)`。

    `live`（＝呼ぶ側が既に取った `group_members(pgid)`）に居ればそれを使う——
    `/proc` の全走査をもう一度やらないため。その行が `pgid` 欄を持たない
    （呼ぶ側が手で組んだ行）ときは、**`group_members(pgid)` の結果である**という
    呼ぶ側の保証をそのまま採る。居なければ `/proc/<pid>` を 2 つだけ読む。
    """
    row = live_by_pid.get(mpid)
    if row is not None:
        pg = row.get("pgid")
        return (pg if isinstance(pg, int) else pgid), str(row.get("cmdline") or "")
    raw = _read(f"/proc/{mpid}/cmdline", binary=True)
    cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    return pgid_of(mpid), cmd


def bg_group_matches(rec: dict, live: list[dict] | None = None) -> bool:
    """**長が死んだ**控えが、いま目の前のそのグループの物だと言えるか。

    ★なぜ要るか（2026-09-05・6 巡目・merge-bash の反証役の指摘④・実測つき）:
      `bg_record_matches` は先頭で「`proc_start_ticks(長)` が None なら False」だった。
      ところが**長が先に死ぬのがいちばん普通の形**である——
      `nohup sleep 40 &` ＋ `exit 0` はシェルが即座に終わり、`bash._release_record`
      はそのあとで控えを書くので `start_ticks` は**必ず None**、長の `/proc` はもう無い。
      つまり `_release_record` が控えた欄は**一度も参照されず**、
      `owned_bg_pids()` には載らなかった＝自分で起こした背景サーバが
      「自分の物ではない」ことになり、`_port_report` は止めてくれない
      （run50 工程4 の「自分のサーバを止められず建て直せない」壁と同じ形）。

    ★ただし「長が死んでいる」と「番号が別人に回った」を**区別する**こと。
      通行証は**撃つ許可**なので、区別できないなら False に倒す（fail-closed）。
      通すのは次が**全部**言えるときだけ:
        ① 控えに `members`（`_release_record` が書いたそのときの構成員）がある。
        ② その pid が**いまも同じ pgid に居る**（死んでいれば別の枝で拾えない）。
        ③ 命令の頭 60 字が控えと一致する（同じ番号の別人を弾く）。
        ④ 控えを書いた時刻より**あとに生まれていない**（生まれていれば使い回し）。

    `live` … 呼ぶ側が既に持っている `group_members(pgid)`。渡さなければ
      構成員の `/proc/<pid>` だけを直接読む（全走査はしない＝`owned_bg_pids()` が
      台帳 1 件ごとにプロセス表を舐めないため）。
    """
    if not POSIX:
        return False
    members = rec.get("members")
    if not isinstance(members, list) or not members:
        return False                      # 控えに構成員が無い＝照合材料が無い
    pgid = rec.get("pgid") or rec.get("pid")
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    live_by_pid = {}
    for row in (live or []):
        if isinstance(row, dict) and isinstance(row.get("pid"), int):
            live_by_pid[row["pid"]] = row
    written = _record_written_epoch(rec)
    for row in members:
        if not (isinstance(row, (list, tuple)) and row and isinstance(row[0], int)):
            continue
        mpid = row[0]
        want_cmd = str(row[1] or "") if len(row) > 1 else ""
        now_pgid, now_cmd = _member_now(mpid, live_by_pid, pgid)
        if now_pgid != pgid:
            continue                      # ② 死んだ／別のグループに移った
        if want_cmd and now_cmd[:60] != want_cmd[:60]:
            continue                      # ③ 同じ番号の別人
        if written is not None:
            born = _proc_born_epoch(mpid)
            if born is None:
                continue                  # 生まれを確かめられない＝証拠にしない
            if born > written + _BG_MEMBER_SLACK_SEC:
                continue                  # ④ 控えより**あと**に生まれた＝使い回し
        return True
    return False


def bg_record_matches(rec: dict, live: list[dict] | None = None) -> bool:
    """その pid で**いま走っているのが、台帳が言っているそのプロセスか**。

    ⓪ **長がもう居ない**（`proc_start_ticks` が None）ときは、控えに書いてある
       構成員でグループを照合する（`bg_group_matches`）。★これが無いと
       「長が先に死ぬいちばん普通の形」が丸ごと通行証を貰えない（6 巡目 must_fix ④）。
    長が生きているときは 3 段（上から順に、強い証拠を先に見る）:
      ① `start_ticks`（`/proc/<pid>/stat` の 22 番目）が記録してあれば厳密一致。
      ② `started_at`（壁時計・`_spawn_background` が書く形）があれば起動時刻と照合。
      ③ どちらも無い古い記録は、起動**時刻**と台帳ファイルの mtime を突き合わせる
         （台帳は起動直後に書くので数秒差に収まる）。
    どれも取れなければ False（＝通行証にしない。分からない物を通すのは
    「撃つ許可」の側なので、ここは fail-closed）。
    """
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0 or not POSIX:
        return False
    now_ticks = start_ticks(pid)
    if now_ticks is None:
        # ★「死んでいる」で終わらせない: 長だけが死んで**グループは生きている**形が
        #   実機のいちばん普通の形。控えの構成員で照合する（別人なら False）。
        return bg_group_matches(rec, live)
    want = rec.get("start_ticks")
    if isinstance(want, int):
        return want == now_ticks
    started = start_epoch(pid)
    if not started:
        return False
    at = rec.get("started_at")
    if isinstance(at, str) and at:
        import time as _time
        try:
            t = _time.mktime(_time.strptime(at, "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, OverflowError):
            t = None
        if t is not None:
            return abs(started - t) <= _BG_START_SLACK_SEC
    mtime = float(rec.get("mtime") or 0.0)
    if not mtime:
        return False
    return abs(started - mtime) <= _BG_START_SLACK_SEC


def bg_record_workspace_ok(rec: dict, workspace: str | None) -> bool:
    """台帳の記録が、**いまの作業場のもの**か。

    ★塞いだ穴（2026-09-05・第4回c）: 台帳は `LAPS_HOME`（既定
      `~/.blackwall`）に置かれ、**同居する全セッションで共有**される。
      作業場を見ないと、`ws=/srv/shared` で走っているセッションにも
      `ws=/root/vdirwork/run53` で起こされた背景処理の通行証が出る
      ＝**ホスト共通の kill 許可証**になっていた。
    ★`workspace` を書いていない**古い記録は通さない**（fail-closed）。
      分からない物を通すのは「撃つ許可」の側。
    ★同じ枝なら通す（記録の作業場が今の中／今の作業場が記録の中）。完全一致に
      すると `narrow_workspace` で `cwd=<ws>/site` と狭めた瞬間に自分のサーバの
      通行証が消え、run50 工程4 の「自分のサーバを止められない壁」が戻る。
    ★★must_fix B-1（union-2・2026-09-06）: `NO_WORKSPACE`（床に届かないときの値・
      実在し得ない 1 本のパス）が**パスとして自己一致していた**。`workspace` も
      `rec_ws` もどちらも `NO_WORKSPACE` なら、`os.path.realpath` を通しても
      同じ文字列のままで `_within(a, b)`（`a == b`）が **True** になる——
      `/`・`$HOME`・深さ 1 の場所で走っている別セッション（自身も `canonical_
      workspace()` が `NO_WORKSPACE` を返す）の記録が、自分にも通行証を出す形。
      「作業場が無い」は「自分の物」の証拠にならないので、どちらかが
      `NO_WORKSPACE` なら無条件に断る。
    """
    if not workspace or workspace == NO_WORKSPACE:
        return False
    rec_ws = rec.get("workspace")
    if not isinstance(rec_ws, str) or not rec_ws or rec_ws == NO_WORKSPACE:
        return False
    try:
        a = os.path.realpath(rec_ws)
        b = os.path.realpath(workspace)
    except (OSError, ValueError):
        return False
    if a == NO_WORKSPACE or b == NO_WORKSPACE:
        return False               # realpath 後の再チェック（念のための保険）
    return _within(a, b) or _within(b, a)


def owned_bg_pids(workspace: str | None = None) -> set[int]:
    """自分（このエージェント）が**この作業場で**起こした背景処理のうち、
    いま生きていて、かつ台帳の記録と同一人物である pid。

    ★2026-09-05（第4回b・総括 §2-12）: 以前は「`<pid>.meta.json` が在る」だけで
      cwd も argv も無視して『自分の物』にしていた。この機械の台帳には
      **23 件すべて死亡済み**の記録が残っており（最古は 8/6）、PID 再利用が
      当たれば無関係のプロセスを撃つ通行証になっていた。開始時刻で照合する。
    ★2026-09-05（第4回c）: それでも台帳は**ホスト共通**だった。
      `workspace` も突き合わせる（`bg_record_workspace_ok`）。
      `workspace` を渡さないときは正典（`canonical_workspace()`）を使う。
    """
    ws = os.path.realpath(workspace) if workspace else canonical_workspace()
    return {r["pid"] for r in bg_records()
            if bg_record_workspace_ok(r, ws) and bg_record_matches(r)}


def prune_bg_records(keep_hours: float | None = None) -> int:
    """**終了した**背景処理の控えを片付ける。消した記録の数を返す。

    ★消しすぎない: 走行中のモデルは終わった処理の出力を `BashOutput(pid=…)` で
      読み返す。既定は「死んでいて、かつ 72 時間より古い」物だけ。
      `LAPS_BG_KEEP_HOURS=0` で掃除を止められる。
    """
    if keep_hours is None:
        try:
            keep_hours = float(os.environ.get("LAPS_BG_KEEP_HOURS", "")
                               or _BG_KEEP_HOURS_DEFAULT)
        except ValueError:
            keep_hours = _BG_KEEP_HOURS_DEFAULT
    if keep_hours <= 0:
        return 0
    import time as _time
    cutoff = _time.time() - keep_hours * 3600.0
    n = 0
    for rec in bg_records():
        if float(rec.get("mtime") or 0.0) > cutoff:
            continue
        if start_ticks(rec["pid"]) is not None:
            continue                      # まだ生きている＝触らない
        d = bg_dir()
        for suffix in (".meta.json", ".out", ".err"):
            try:
                (d / f"{rec['pid']}{suffix}").unlink()
            except OSError:
                pass
        n += 1
    return n


def in_workspace(row: dict, workspace: str | None = None, *,
                 bg_pids: set[int] | None = None) -> bool:
    """**「撃ってよい自分の物か」の唯一の判定**（`bash._scope_name_kill` /
    `bash._scope_pid_kill` / `bash_output._port_report` が同じこれを呼ぶ）。

    真になる条件は 3 つだけ:
      ① 自分が `run_in_background` で起こし、**同じ作業場**の記録で、開始時刻まで
         一致する（`owned_bg_pids`）。
      ② cwd が作業場配下（realpath ＋ 区切りつき前方一致）。
      ③ 作業場を**土台にして**起動された形（`sh -c "cd <ws> && …"` / `-t<ws>/site`）。
         `cd <ws>` は**シェルが `-c` で受け取った語**の中でだけ数える（第4回c）。

    ★`ws in cmdline` の素の部分一致と、**裸の絶対パスのトークン**は所有の根拠に
      しない（2026-09-05・第4回b の統合）。`/tmp` に居る `tail -f <ws>/x.log` まで
      「自分の物」になり、名前撃ちでは「絞る」だけだったこの緩さが、ポート経由の
      停止では**撃つ許可**になっていた。作業場のパスを参照しているだけの行は
      `references_workspace()` で**列挙と注記にだけ**使う。
    ★`workspace` を渡さないと False（分からない物を通すのは「撃つ許可」の側）。
    """
    if not workspace:
        return False
    try:
        ws = os.path.realpath(workspace)
    except (OSError, ValueError):
        return False
    pid = row.get("pid")
    if isinstance(pid, int):
        if bg_pids is None:
            bg_pids = owned_bg_pids(ws)
        if pid in bg_pids:
            return True
    if path_within(row.get("cwd") or "", ws):
        return True
    argv = row.get("argv") or ()
    if argv:
        # ★`cd <ws>` は**シェルが実行する語**のときだけ所有の証拠（第4回c）。
        #   素の argv で見ると `grep -rn "cd <ws>" /etc` や `echo 'cd <ws>'` の
        #   ただの引数まで「自分の物」になる（字面で判定する `pkill -f` の族）。
        for script in shell_script_args(argv):
            if roots_at_workspace(script, ws, kinds=("cd", "opt")):
                return True
        for tok in argv:
            if roots_at_workspace(tok, ws, kinds=("opt",)):
                return True
        return False
    return roots_at_workspace(row.get("cmdline") or "", ws)


def references_workspace(row: dict, workspace: str | None = None) -> bool:
    """その行が作業場のパスを**参照しているだけ**か。**列挙・注記の材料**であって、
    撃つ根拠ではない（相対トークンは呼ぶ側の cwd で解決されて無関係に当たる）。"""
    ws = os.path.realpath(workspace) if workspace else canonical_workspace()
    argv = row.get("argv") or ()
    toks = argv if argv else ((row.get("cmdline") or ""),)
    for tok in toks:
        if os.path.isabs(tok) and path_within(tok, ws):
            return True
        if mentions_workspace(tok, ws):
            return True
    return False


def ancestor_pgids() -> set[int]:
    """自分・親・祖先のプロセスグループ番号（**絶対に撃たない相手**）。

    ★`stopproc`（COMMON.md）が同じ規律を持っている。ここに自分のセッション ID も
      入れる: `setsid` で起こしたグループを撃つつもりで、自分の居るセッションの
      グループを撃つと走行ごと消える。
    """
    out: set[int] = set()
    if not POSIX:
        return out
    for fn in (os.getpid, os.getppid):
        try:
            g = os.getpgid(fn())
            if g:
                out.add(g)
        except OSError:
            pass
    try:
        out.add(os.getsid(0))
    except (OSError, AttributeError):
        pass
    pid = os.getpid()
    for _ in range(32):                    # 祖先を辿る（循環しても抜ける）
        rest = _stat_fields(pid)
        if len(rest) < 3:
            break
        try:
            ppid, pgrp = int(rest[1]), int(rest[2])
        except ValueError:
            break
        out.add(pgrp)
        if ppid <= 1:
            break
        pid = ppid
    out.discard(0)
    return out


def describe(row: dict, width: int = 90) -> str:
    return f"pid {row['pid']}  {(row.get('cmdline') or '')[:width]}"
