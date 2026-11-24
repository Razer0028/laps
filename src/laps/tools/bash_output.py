"""BashOutput tool: poll output and status of a background process started by Bash."""
from __future__ import annotations
import os
import signal
import json
import sys
from pathlib import Path
from typing import NamedTuple
from .. import procrun  # 固まらない subprocess（2026-09-06）

from . import ToolSpec
from .. import procutil


def _bg_dir() -> Path:
    """★実体は `procutil.bg_dir()`（2026-09-05・第4回b）。bash.py と同じ 2 行を
    別々に持っていた（同じ物が 2 つ＝片方だけ直る形）。"""
    return procutil.bg_dir()


def _live_cmdline(pid: int) -> str:
    """いまその pid で動いているものの綴り（読めなければ空）。注記にだけ使う。"""
    try:
        raw = procutil._read(f"/proc/{pid}/cmdline", binary=True)
        return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except Exception:
        return ""


def _authorize_kill(target: str, cwd: str | None) -> str | None:
    """**止める前に政策ゲートを通す**（2026-09-05・第4回b の反証を受けて新設）。

    ★塞いだ穴（実測）: `LAPS_POLICY=readonly` で `Bash("kill <pid>")` は
      `policy denied: kill は許可リストに無い` と断られるのに、**同じプロセスが
      `BashOutput(port=…, kill=true)` では止まった**（生存 True→False）。
      `bash.py` 自身が「Policy gate first: … it must run before *any* dispatch
      path」と書いている、その dispatch path をこの道具が新設していた。
      監査（`policy.audit`）にも残らないので、撃った記録すら無かった。
    戻り: 断られたときのメッセージ（通ったら None）。
    """
    try:
        from ..policy import authorize_command, PolicyDenied
    except Exception as e:
        # ★7 巡目（must_fix ① の同型・**政策の門そのもの**）: 旧実装は
        #   「政策層が読めない＝従来どおり通す」と書いて撃っていた。だが
        #   `bash.py:18` は `from ..policy import authorize_command` を
        #   **module 直下**でやるので、政策層が読めない機械では `Bash("kill …")`
        #   は import ごと落ちる。同じ行為の 2 つの入口が、片方は落ち、片方は
        #   黙って撃つ——この道具が新設した dispatch path を塞いだ意味が消える。
        _bump("bg_policy_gate_broken")
        return (f"Error: 政策の判定ができません（政策層を読めません: "
                f"{type(e).__name__}: {str(e)[:120]}）。**撃っていません。**\n"
                "  これは道具の欠陥です（環境の問題ではありません）。")
    try:
        authorize_command(target, cwd=cwd)
    except PolicyDenied as e:
        return f"Error: policy denied: {e}"
    except Exception as e:
        # ★7 巡目: 旧 `return None` は**政策が壊れたら撃つ**という意味だった。
        #   `authorize_command` は「通す（返る）」か「`PolicyDenied` を投げる」の
        #   どちらかで、それ以外は欠陥。監査が書けないときに実行を止めない配慮は
        #   **policy 側で済んでいる**（`test_監査に書けなくても実行は止まらない`）
        #   ので、ここで丸める理由はもう無い。撃つ許可は確かめられたときだけ。
        _bump("bg_policy_gate_broken")
        return (f"Error: 政策の判定が失敗しました（{type(e).__name__}: "
                f"{str(e)[:120]}）。**撃っていません。**\n"
                "  同じ命令は Bash(\"kill …\") でも同じ政策を通るので、"
                "こちらだけ通すことはしません。")
    return None


#: `decode_bytes` と同じ優先順（utf-8 を最優先）。ここでは境界のトリム候補として使う。
_TAIL_ENCODINGS = ("utf-8", "cp932", "euc-jp", "latin-1")

#: このうち cp932 だけは「decode が例外を出すか」で境界を判定できない
#: （下の `_sjis_boundary_skip` の docstring 参照）ので別扱いする。
_SJIS_TAIL_ENCODINGS = frozenset({"cp932"})

#: 逆方向に安全点を探すときの上限（バイト）。tail() 側もこの分だけ余分に読む。
_SJIS_ANCHOR_MAX_BACK = 512


def _sjis_lead(b: int) -> bool:
    """cp932 の2バイト文字の先頭バイト範囲（Python の cp932 codec で実測確認済み:
    0x81-0x9F と 0xE0-0xFC が『単独では incomplete multibyte sequence』になる範囲）。"""
    return 0x81 <= b <= 0x9F or 0xE0 <= b <= 0xFC


def _sjis_never_trail(b: int) -> bool:
    """この値は cp932 のどの2バイト文字の2バイト目にもなり得ない
    （実測した2バイト目の許容範囲の外＝0x40-0x7E, 0x80-0xFC のどちらでもない）。
    ＝ここに来たら、正しく解釈されている前提のもとでは**必ず文字の先頭**。"""
    return not ((0x40 <= b <= 0x7E) or (0x80 <= b <= 0xFC))


def _sjis_boundary_skip(full: bytes, idx: int, max_back: int = _SJIS_ANCHOR_MAX_BACK) -> int | None:
    """`full[idx:]`（＝ `tail()` が返そうとしている窓）が cp932 の文字境界で
    始まっているかを、**decode が例外を出すかどうかではなく**構造で判定する。

    ★なぜ要るか（2026-09-05・実測）: cp932 はほぼ全バイト値が単独でも合法
      （半角カナ・私用領域など）なので、`data.decode("cp932")` は境界がずれて
      いても**例外を出さない**。「あいうえお…」を cp932 で書いたファイルの
      末尾を奇数バイト目から seek して読むと、例外は 0 件のまま先頭に半角カナや
      私用領域文字が1つ混入する——`_decode_tail_window` の旧来の「トリムして
      decode が通ったら採用」という判定は、cp932 に対しては原理的に効かない
      （UTF-8 は不正な継続バイト列で確実に例外を出すので効くが、cp932 は
      効かない側）。

    判定の型: `idx` の直前に「2バイト目には絶対になれない値」（`_sjis_never_trail`）
    が見つかれば、そこは必ず文字の先頭——そこから前向きに（lead バイトなら
    2 バイト、それ以外は 1 バイトで）数え直し、`idx` にちょうど乗るか
    1 バイトはみ出すかで境界を確定できる。★Shift_JIS 系は自己同期しない符号化
    （2バイト目の値域が1バイト文字の値域と重なる）なので、直前 `max_back`
    バイト以内に安全点が無ければ（＝英数字を挟まない和文が長く続く）判定を
    諦めて `None` を返す——これは実装の甘さではなく符号化そのものの限界で、
    無限に遡って全ファイルを読み直すことはしない。

    戻り値: `idx` から何バイト前進すれば文字の先頭に揃うか（0 か 1）。
    判定できないときは `None`（呼び出し側は従来の総当りにフォールバックする）。
    """
    lo = max(0, idx - max_back)
    anchor = None
    for p in range(idx, lo - 1, -1):
        if _sjis_never_trail(full[p]):
            anchor = p
            break
    if anchor is None:
        return None
    i = anchor
    while i < idx:
        i += 2 if (_sjis_lead(full[i]) and i + 1 < len(full)) else 1
    if i == idx:
        return 0
    if i == idx + 1:
        return 1
    return None  # 単/2バイトの想定から外れる並び（理論上到達しないはずだが fail-safe）


def _decode_tail_window(data: bytes, *, from_middle: bool, prefix: bytes = b"") -> str:
    """`tail()` が seek で切り出した末尾ウィンドウを復号する。

    ★実測で見つかった実害（2026-09-04）: ファイルの途中から `seek` して読むと、
      先頭がマルチバイト文字の**途中**で切れていることがある。`decode_bytes` は
      「バッファ全体が1つの符号化で書かれている」前提で utf-8→cp932→euc-jp→latin-1
      の順に**例外が出なければ採用**するので、途中で切れた1バイト目が
      たまたま cp932 として例外なく読めてしまうと、**末尾全体が別の符号化で
      丸ごと誤読される**——しかも置換文字は1つも出ない（見た目は「読めた」ので、
      壊れているという合図が無い）。実測: 本物は UTF-8 で末尾が「けこ」なのに、
      `tail=5` で読むと `代％`（cp932 として読んだ結果）になった。

    直し方（utf-8 等）: ファイルの途中から読んだとき（`from_middle=True`）だけ、
    先頭 0〜3 バイトをトリムしながら「その符号化のまま**例外なく**読める」箇所を
    探す。utf-8 を最優先で試すので、ふつうの UTF-8 出力（この現場の主流）は
    トリムだけで正しく復元できる。

    直し方（cp932）★2026-09-05 追記: 上のトリム方式は「例外が出ない＝境界が
    正しい」という前提に立っているが、cp932 はその前提が成り立たない
    （`_sjis_boundary_skip` の docstring 参照）。`prefix`（窓の手前の
    追加読み取り分・`tail()` が渡す）があれば、そちらで構造的に境界を
    決め直す。`prefix` が無い/判定できないときは、cp932 についても従来どおり
    トリムして decode が通った箇所を採る（この経路の挙動は変えない＝
    判定不能な稀なケースを新たに壊さない）。
    """
    from .read import decode_bytes
    if from_middle and data:
        idx = len(prefix)
        full = (prefix + data) if prefix else data
        for enc in _TAIL_ENCODINGS:
            if enc in _SJIS_TAIL_ENCODINGS and prefix:
                skip = _sjis_boundary_skip(full, idx)
                if skip is not None:
                    try:
                        data[skip:].decode(enc)
                    except UnicodeDecodeError:
                        pass  # 構造判定はできたが decode 自体が失敗 → 下の総当りへ
                    else:
                        return decode_bytes(data[skip:] if skip else data)
            for k in range(0, min(4, len(data)) + 1):
                try:
                    data[k:].decode(enc)
                except UnicodeDecodeError:
                    continue
                return decode_bytes(data[k:] if k else data)
    return decode_bytes(data)


def _is_alive(pid: int) -> bool:
    """生きているかを**壊さずに**確かめる。

    ★Windows で `os.kill(pid, 0)` は生存確認ではない。Python は
    CTRL_C_EVENT / CTRL_BREAK_EVENT 以外の値を渡すと **TerminateProcess** を呼ぶので、
    「生きているか見る」つもりの1行が**対象を強制終了する**。
    いまは背景実行そのものが `preexec_fn=os.setsid`（Linux 専用）で失敗するため
    ここへ到達しないが、そちらを直した瞬間に地雷が有効になる。先に塞いでおく。
    （2026-08-16・Claude Code との設計突き合わせで発見）
    """
    if os.name == "nt":
        # Windows は OpenProcess 相当が要る。標準ライブラリだけで安全に見るなら
        # tasklist を読む。**見るだけの操作で相手を殺さない**ことが最優先。
        try:
            import subprocess
            # ★**バイトで受ける**（2026-09-02・`remote_read` / `system_state` と同じ形）。
            #   旧: `text=True`＝その機械の既定（日本語 Windows は cp932）で復号する。
            #   `tasklist` は cp932 で出すので UnicodeDecodeError になり、それを
            #   下の `except Exception` が飲んで **False（＝いない）** を返す。
            #   つまり「動いている背景処理を、動いていないと報告する」——
            #   しかも例外は 1 行も残らない（黙って嘘になる形）。
            #   ⚠ ここは Windows 専用の枝なので、cp932 は例外ではなく既定。
            r = procrun.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                               capture_output=True, timeout=10)
            from .read import decode_bytes
            out = r.stdout if isinstance(r.stdout, str) else decode_bytes(r.stdout or b"")
            return str(pid) in out
        except Exception:
            return False          # 確かめられないなら「不明」ではなく「いない」に倒す
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False                      # 番号が居ない＝本当に居ない
    except PermissionError:
        # ★7 巡目: EPERM は「**居るが自分のものではない**」であって「居ない」ではない。
        #   旧 `except (OSError, ProcessLookupError): return False` は他ユーザの
        #   プロセスを死んだことにしていた。読めないことを居ないと言わない。
        # ★★8 巡目（再反証 M4・記述の訂正）: 7 巡目はここの理由に
        #   「`_survivors` は生き残りを 1 本落とし、`_run` は already exited と言う」と
        #   書いたが、**それは当時まだ直っていなかった**。`_survivors` はこの行を
        #   通ったあと `_pid_listen_ports` の空集合で同じように落としており、
        #   本番の形で `OK: SIGTERM で止まりました` ＋ サーバは LISTEN 中だった
        #   （実測 `probe_m1.py`）＝**散文だけ正しく見える形**（この係が繰り返し
        #   潰している当のもの）。M1 を直した 8 巡目に、この 1 行がようやく
        #   次の 3 か所で効く: `_survivors`（生き残りを読み直す入口）／
        #   `_await_late_bind`（待つ相手が居るか）／`_run`（`alive`）。
        return True
    except OSError:
        return False
    # /proc state: 'Z' = zombie (already exited, just unreaped) — treat as not alive.
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    state = line.split()[1]
                    return state != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def _group_members(pgid, errors: list | None = None) -> list[dict]:
    """プロセスグループに残っているもの。**自分のグループは絶対に返さない。**

    ★`errors`（6 巡目の続き・2026-09-05）: 数えられなかったとき、その理由をここへ
      積む。**`[]` は「もう居ない」ではなく「見えなかった」でもありうる**ので、
      呼ぶ側がそれを区別できるようにする（下の `except` の注記を読むこと）。
    """
    if not pgid or sys.platform == "win32":
        return []
    try:
        pgid = int(pgid)
    except (TypeError, ValueError):
        return []
    try:
        if pgid == os.getpgid(0):
            return []           # 自分（エージェント）のグループ＝撃たない・数えない
    except OSError:
        pass
    try:
        return procutil.group_members(pgid)
    except Exception as e:
        # ★6 巡目の続き（must_fix ① の同型・**この係が見落としていた場所**）:
        #   旧実装は「数えられなかった」を **`[]`＝もう居ない**に丸めていた。
        #   実測（本番の形・`probe_blind.py` の `rows` 形）: `procutil.proc_rows` を
        #   union の改名事故と同じ AttributeError にすると、
        #     ・`_run` の `alive = _is_alive(pid) or bool(_members)` が False になり
        #     ・`kill=true` は**撃たずに** `pid N already exited` を返す
        #   （`Error:` で始まらないので `tools/__init__.py:tool_failed` は**成功**と読む）。
        #   構成員は生きているのに「もう終わっている」と言う形＝この係が潰している
        #   false green の親戚。数えられなかった事実は捨てずに呼ぶ側へ渡す。
        _bump("bg_members_unknown")
        if errors is not None:
            errors.append(f"構成員を数えられません（group_members が失敗: "
                          f"{type(e).__name__}: {str(e)[:120]}）")
        return []


def _meta_port(meta: dict) -> int | None:
    """台帳の `cmd` からポート番号を**推測**する。

    ⛔ **証拠に使わない**（2026-09-05・第4回c 3 巡目・再反証 M1）。
      ここは `bash._port_in_cmd` の正規表現＝命令文字列に `:数字` や `-p 数字` が
      あるだけで数を返す。実測で 4 形が**偽のポート**を掴んだ:
        `bash start.sh >> run-13:45:07.log` → 45
        `DATABASE_URL=postgres://…:5432/db` → 5432
        `--image registry:5000/app`         → 5000
        `ssh -L 19000:127.0.0.1:P`          → 127（IP のオクテット）
      偽のポートは `port_in_use()` が撃つ前も後も False なので、
      「ポートも見て確かめた」という**偽の証拠**で `OK: 止まりました` が出ていた
      （本番経路で再現）。いまは止まったかの判定に**一切**使わない。
      使ってよいのは表示（`_hint_note`＝「この数は推測です」と断る一行）だけ。
    """
    try:
        from .bash import _port_in_cmd
        return _port_in_cmd(str(meta.get("cmd") or ""))
    except Exception:
        return None


# ── 撃つ前に「実際に待ち受けているポート」を観測する（2026-09-05・第4回c 3 巡目）──
def _listen_inode_ports(errors: list | None = None) -> dict[str, int]:
    """LISTEN 中のソケット inode → ポート番号（`/proc/net/tcp{,6}`）。

    ★`procutil._socket_inodes(port)` の**逆引き**。あちらは「このポートの inode は？」で、
      こちらは「この inode はどのポート？」＝**pid から実 LISTEN を引く**のに要る
      （`procutil.port_listeners` と読む物・判定（`st == 0A`）は同じ）。
      procutil は触らない係なので、この 1 本だけをここに置く。
    """
    out: dict[str, int] = {}
    # ★表そのものが読めたかを分ける（6 巡目の続き・2026-09-05）。空の dict は
    #   「誰も待ち受けていない」でも「表が読めなかった」でも同じ形になるので、
    #   呼ぶ側が区別できないと **「見えなかった」を「無い」** と言ってしまう。
    #   実測（本番の形・`probe_blind.py` の `imap` 形）: ここが空だと
    #   `setsid` で逃げたサーバが生きたまま `OK: SIGTERM で止まりました`（★false green）。
    try:
        st_listen = procutil._ST_LISTEN
    except AttributeError as e:
        # union で実際に起きた形（`_boot_time`→`_boot_epoch` の改名）と同型。
        # 飲まずに、**読めなかった事実**として上へ渡す（黙って 0 件にしない）。
        if errors is not None:
            errors.append(f"LISTEN の表を読む定数が引けません（{type(e).__name__}: {e}）")
        return out
    read_any = False
    why: list[str] = []
    for name in ("tcp", "tcp6"):
        try:
            with open(f"/proc/net/{name}") as fh:
                lines = fh.read().splitlines()
        except OSError as e:
            why.append(f"/proc/net/{name}: {e.__class__.__name__}")
            continue
        read_any = True
        for line in lines[1:]:
            f = line.split()
            if len(f) < 10 or f[3] != st_listen:
                continue
            local = f[1]
            if ":" not in local:
                continue
            try:
                out[f[9]] = int(local.rsplit(":", 1)[1], 16)
            except ValueError:
                continue
    if not read_any and errors is not None:
        errors.append("LISTEN の表が 1 つも読めません（" + "／".join(why) + "）")
    return out


def _pid_listen_ports(pid, imap: dict[str, int],
                      errors: list | None = None) -> set[int]:
    """その pid が **LISTEN で握っている**ポート（`/proc/<pid>/fd` の socket inode 突合）。

    空集合は 2 つの意味を持つ——「待ち受けていない」と「**読めなかった**」。
    ★2026-09-05・8 巡目（再反証 M1・本番の形で false green を実測）: 7 巡目までは
      その区別を docstring で「呼ぶ側が扱うこと」と**約束しただけ**で、呼ぶ側
      2 か所（`_survivors` / `_observe_listen` の構成員側）はどちらも空集合を
      「待受なし」に丸めていた。`blind` に一切積まれないので `snap["scanned"]` は
      True のまま＝`_after_kill` の `if blind:` は発火せず `_ok_text` に落ちる。
      実測（`probe_m1.py`・本番経路）: 逃げ子の `/proc/<pid>/fd` を EPERM にすると
      `_is_alive` は True を返す（7 巡目の直しは効いている）のに、その 1 行あとで
      `_survivors` が同じ EPERM で落とし、`OK: SIGTERM で止まりました` ／
      `tool_failed()=False` ／サーバは LISTEN 中。
      → **散文で約束しない。`errors` を受け取り、呼ぶ側の `blind` に積む。**
    ⚠ 積むのは **`PermissionError` だけ**。`ProcessLookupError` / `FileNotFoundError`
      （＝本当に死んだ）は「読めなかった」ではないので積まない——積むと、
      正しく止まった停止まで「確かめられていません」になる（偽の赤を作らない）。
    """
    ports: set[int] = set()
    if not imap:
        return ports
    try:
        entries = os.listdir(f"/proc/{int(pid)}/fd")
    except PermissionError as e:
        if errors is not None:
            errors.append(f"pid {pid} が何を待ち受けているか読めません"
                          f"（/proc/{pid}/fd: PermissionError: {e.strerror or e}）"
                          "——他ユーザのプロセスは中身が見えません")
        return ports
    except (OSError, ValueError, TypeError):
        return ports                      # 本当に居ない（ProcessLookupError/ENOENT）
    denied = 0
    for fd in entries:
        try:
            link = os.readlink(f"/proc/{pid}/fd/{fd}")
        except PermissionError:
            denied += 1                   # ★fd ごとに 1 行積まない（あとで 1 行）
            continue
        except OSError:
            continue
        if link.startswith("socket:[") and link.endswith("]"):
            p = imap.get(link[8:-1])
            if p is not None:
                ports.add(p)
    if denied and errors is not None:
        errors.append(f"pid {pid} の fd を {denied} 本読めません"
                      "（PermissionError）——待受を握っていても見えません")
    return ports


#: `_observe_listen` が控える身内の上限（子孫を先に詰める）。**作業場が広いと
#: 候補は数百になりうる**ので、撃ったあとの読み直し（`_survivors`）が重くならない
#: ところで切る。切った本数は `snap["kin_total"]` に残す（黙って捨てない）。
#: ⚠ これは**弱い候補**（待ち受けてもいないし、命令にポートの手掛かりも無い）に
#:   だけ効く床。強い候補は `_KIN_STRONG_CAP` まで切らない（下記）。
_KIN_CAP = 64

#: 強い候補（**いま待ち受けている**か、これから bind しうる）の上限
#: （2026-09-05・第4回c 9 巡目・再反証 MF-1/MF-3）。
#:
#: ★なぜ別の床を置くか: 8 巡目は強い候補も `_KIN_CAP=64` で切り、**切って生きて
#:   いたら盲**（＝`Error: 止まったかどうかは確かめられていません`）にしていた。
#:   その「生きていたら」は**構造的にほぼ常に真**だった——`kin` は定義上
#:   **撃つ pgid の外**の行だけ（構成員は `pg == pgid` で先に除かれる）なので、
#:   `os.killpg(pgid, …)` は `kin_over` の相手を**一度も殺せない**。
#:   実測（`p_mf1.py`・的は逃げ子も待受も無い普通の停止・水増しは生きている
#:   第三者 70 本）: base 06215bf `OK: SIGTERM で止まりました` ／
#:   8 巡目 tip `Error: …止まったかどうかは確かめられていません`（tool_failed=True）。
#:   ＝**正しく止まった停止が偽の赤になっていた**。
#: ★いまの形: 強い候補は 128 本まで**全部読み直す**（切らない）。切るのは
#:   そこを超えたときだけで、そのときは盲にする（＝観測できていないと言う）。
#:   128 を超える「作業場の中の、待ち受けているか bind しそうな第三者」は
#:   実測では出たことがない（反証役の実物 132 プロセスでも強い候補は 66 本）。
#: ⚠ 残る限界（正直に）: 129 本以上の強い候補が居る作業場では、逃げ子も待受も
#:   無い停止でも `Error:`（偽の赤）になる。false green を作らない側に倒した結果。
_KIN_STRONG_CAP = 128


def _launch_scope(meta: dict) -> str | None:
    """身内を探す範囲＝**作業場**（起動が作業場の中にあるときだけ返す）。

    ★2026-09-05・第4回c **4 巡目**に 2 つ直した（どちらも再反証が実物で再現）:

      ① **`meta["cwd"]` が空なら `procutil.canonical_workspace()` に落とす。**
         実機は `Bash` に `cwd` を渡さない（trace 2,938/2,938・台帳 17/17 が
         `cwd=null`）。旧実装はここで `None` を返していたので、**この現場で
         いちばん多い形では身内の観測が一行も動かなかった**——`exp_nocwd` H1 は
         `setsid` で逃げたサーバが生きたまま `OK: SIGTERM で止まりました`
         （`port_in_use=True`・★FALSE GREEN）。試験 8 本が全部 `cwd` を
         渡していたので、そこだけ緑だった（＝試験が本番の形になっていなかった）。

      ② **起動ディレクトリではなく作業場を返す。** 旧実装は起動 dir に絞って
         いたので、`cd ../data && setsid …`（作業場の中だが起動 dir の外）で
         逃げたサーバを取り逃していた（`exp_cover` (c2) が false green）。
         「広げると無関係のリスナを拾う」という旧注記の懸念は、
         **親子関係（`_descends_from`）で身内かどうかを見分け、辿れなかった側は
         `Error:` の本文で「第三者の可能性」を先に書く**ことで受ける。
    """
    try:
        ws = procutil.canonical_workspace()
    except (OSError, ValueError):
        return None
    cwd = str(meta.get("cwd") or "")
    if not cwd:
        return ws                         # ★① 実機の最頻形（台帳に cwd が無い）
    try:
        base = os.path.realpath(cwd)
    except (OSError, ValueError):
        return ws
    if not procutil.in_workspace({"cwd": base}, ws, bg_pids=set()):
        return None                       # 起動そのものが作業場の外＝身内を名乗らない
    return ws                             # ★② 起動 dir ではなく作業場


def _parent_map() -> dict[int, int]:
    """pid → ppid（`/proc/<pid>/stat` の 4 番目）。読めない物は載せない。

    ★なぜ要るか（2026-09-05・第4回c 4 巡目）: 「同じ作業場に居る」だけでは、
      **自分の起動が枝分かれさせた子**と**たまたま同じ場所に居る第三者**を
      区別できない。`setsid` が切るのはプロセスグループとセッションであって
      **親子関係は切らない**ので、撃つ前ならほぼ必ず親を辿って起動に戻れる。
      これで (d)（作業場の外へ `cd` した子）まで拾えるようになり、逆に
      第三者は「辿れなかった側」として言い回しを弱められる。
    ⚠ 置き場所: 本来 `procutil` に置きたいが、この係は `procutil.py` を
      触らない約束（merge-bash 側が同じファイルで作業中）なのでここに持つ。
    """
    out: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for e in entries:
        if not e.isdigit():
            continue
        raw = procutil._read(f"/proc/{e}/stat")
        if not raw:
            continue
        i = raw.rfind(")")                # comm に空白や ')' が入りうる
        if i < 0:
            continue
        rest = raw[i + 2:].split()
        if len(rest) < 2:
            continue
        try:
            out[int(e)] = int(rest[1])
        except ValueError:
            continue
    return out


def _descends_from(pid: int, pgid: int, pmap: dict[int, int],
                   pg_by_pid: dict) -> bool:
    """`pid` の親を辿ると、そのグループの誰か（または長そのもの）に着くか。

    親が死んで `init` に引き取られていれば辿れない（＝`False`）。**「辿れない」を
    「無関係」と言わない**ために、呼ぶ側は False を「弱い候補」として扱うこと。
    """
    cur, seen = pid, {pid}
    for _ in range(64):                   # 循環しても抜ける
        par = pmap.get(cur)
        if par is None or par <= 1 or par in seen:
            return False
        if par == pgid or pg_by_pid.get(par) == pgid:
            return True
        seen.add(par)
        cur = par
    return False


def _launch_epoch(meta: dict, meta_p=None) -> float | None:
    """その背景起動が始まった時刻（epoch 秒・読めなければ None）。

    ⚠ この値は**関門に使わない**（5 巡目・must_fix ①）。表示と、外向きの
      互換のために残してある。
    ★except を絞る（5 巡目・調停係）: `procutil._boot_time` / `_clock_ticks` の
      **名前の取り違え（AttributeError）を飲まない**。飲むと「時刻が読めない」に
      化けて、関門が黙って素通りする形になる（union で実際に起きた）。
    """
    ticks = meta.get("start_ticks")
    if isinstance(ticks, int):
        # ★統合で名前が変わった（2026-09-05）: `procutil._boot_time()` → `_boot_epoch()`
        #   （`float` から `float | None` へ）。旧名のままだと `AttributeError` を
        #   下の `except` が飲み込み、**tick の精密な起動時刻が黙って使われず**
        #   `started_at`（秒どまりの壁時計）へ落ちていた——赤にはならないので
        #   気づけない型。呼び分けを直し、None も明示して落とす。
        try:
            boot = procutil._boot_epoch()
            if boot is not None:
                return boot + ticks / procutil._clock_ticks()
        except (OSError, ValueError, ZeroDivisionError):
            pass
    raw = str(meta.get("started_at") or "")
    if raw:
        try:
            from datetime import datetime
            return datetime.fromisoformat(raw).timestamp()
        except (ValueError, TypeError):
            pass
    try:
        return float(meta_p.stat().st_mtime) if meta_p is not None else None
    except (OSError, AttributeError, TypeError):
        return None


def _launch_ticks(meta: dict) -> int | None:
    """その起動が始まった時刻を **boot からの tick** で（読めなければ None）。

    ★なぜ epoch でなく tick か（2026-09-05・4 巡目・`exp_more` R1 の偽の赤）:
      壁時計（`_launch_epoch`）と比べるには **±5 秒の余裕**が要る（`started_at` の
      ISO 文字列や meta の mtime に落ちたときに「分からない」を「別人」と言わない
      ため）。ところがその 5 秒は、**起動の 1 秒前から居た無関係の第三者まで
      「後から生まれた」に入れて**しまう——実測: 作業場に先に居た `http.server` が、
      あとから起こした `bash build.sh`（サーバでも何でもない）の停止で
      「残っています」と赤くなった。
      `meta["start_ticks"]` と `proc_start_ticks` は**同じ物差し（boot からの tick）**
      なので、そちらがあるときは余裕なしで比べられる。

    ★★2026-09-05・5 巡目（再反証 must_fix ①・false green）: **無いときが本題**。
      `bash._release_record`（打ち切りで背景に回す経路）は、シェルを刈り取った
      **あとで** `proc_start_ticks(pgid)` を呼ぶので、控えの `start_ticks` は
      **必ず `None`**（実機の台帳 17/17 が該当）。そこで壁時計に落ちると、
      比べる相手が `started_at`＝**打ち切り時刻**になる——サーバが上がってから
      打ち切られるまでが 5 秒より長い形（`timeout` が 30 秒・60 秒の普通の起動）で、
      **本物の逃げ子が「起動より前から居る第三者」に分類されて捨てられていた**
      （実測 4/4・`OK: SIGTERM で止まりました` ＋ `port_in_use=True`）。
      → 物差しが無いときは**落とさない**（下の `_observe_listen` を参照）。
    """
    t = meta.get("start_ticks")
    return t if isinstance(t, int) else None


def _bump(name: str, n: int = 1) -> None:
    """発火の計器（`tools/fold.py` と同じ形）。**本番で 0 なら配線ミスと分かる。**

    ★2026-09-05・5 巡目（調停係の指摘）: 物差しが読めなかった事実は、本文だけでなく
      数でも残す。counters が取れない場（単体試験など）では黙って諦める——
      計器のために本体を落とさない。
    """
    try:
        from . import CURRENT_AGENT
        ag = CURRENT_AGENT.get()
        if ag is not None and hasattr(ag, "counters"):
            ag.counters[name] = ag.counters.get(name, 0) + n
    except Exception:
        pass


def _born_after_ticks(pid, since_ticks: int | None) -> bool | None:
    """`pid` はその tick 以降に生まれたか。**None=読めない（分からない）**。

    ★except を絞る（2026-09-05・5 巡目・調停係の指摘）。旧 `except Exception` は
      **コードの取り違えまで飲む**: union（merge-bash との合流）で `procutil` 側の
      関数名が変わったとき、`AttributeError` がここで `None` に丸められ、
      **起動時刻の関門が黙って全部素通り**した（1 本の試験だけが捕まえた）。
      `/proc` が読めない（OSError）や pid が数でない（ValueError）は「分からない」だが、
      **名前の取り違えは「分からない」ではなく欠陥**なので上げる。
    """
    if since_ticks is None:
        return None
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        st = procutil.proc_start_ticks(pid_i)
    except (OSError, ValueError):
        return None
    if st is None:
        return None
    return st >= since_ticks


def _observe_listen(pgid, *, scope: str | None = None,
                    since_epoch: float | None = None,
                    since_ticks: int | None = None) -> dict:
    """**撃つ前に**、実際に LISTEN しているポートを観測して控える。

    ★これが 3 巡目の直しの本体（2026-09-05・再反証 M1〜M3・M5・M6）。
      いままでの証拠は `_meta_port`＝**命令文字列の正規表現**で、偽のポートを
      掴むと `port_in_use(偽)` は撃つ前も後も False＝**偽の証拠で `OK:`** が出た。
      推測をやめ、`/proc/<pid>/fd` → socket inode → `/proc/net/tcp{,6}` の LISTEN
      突合で**現物**を見る（`procutil.port_listeners` と同じ手を pid 側から引く）。

    戻り: `{"ports": set[int], "kin": [row+ports+kinship], "scanned": bool,
             "kin_total": int}`
      ports … グループ構成員が実際に握っている LISTEN ポート（**強い証拠**）。
      kin   … グループの外に居る**逃げた候補**。2 段階で拾う:
              `kinship="descendant"` … 親を辿るとこのグループに着く（**強い**）。
              `kinship="scope"`      … 親は辿れないが `scope`（作業場）の中に居て、
                                       **この起動より前から居たと証明できなかった**
                                       （**弱い**＝第三者かも）。
              他の背景処理（台帳に載っている別グループ）と、自分・親・祖先は数えない。
      scanned … 観測そのものができたか（/proc が読めない機械では False）。
      kin_total … 上限で切る前の候補数（黙って捨てないため）。

    引数 `since_ticks` … **boot からの tick**。`proc_start_ticks` と同じ物差しなので、
      これがあるときだけ「この起動より前から居た」を証明して落とせる。
    引数 `since_epoch`  … ⚠ **関門には使わない**（2026-09-05・5 巡目・must_fix ①）。
      壁時計は `released` の控えでは打ち切り時刻を指すので、余裕 5 秒を付けても
      本物の逃げ子を捨てる。受け取るだけにしてある（既存の呼び出しと、
      反証の再現スクリプトを壊さないため）。

    ★2026-09-05・第4回c 4 巡目の変更（再反証 (g)）: 候補を「**いま LISTEN して
      いるもの**」で絞るのをやめた（旧 `if not ports: continue`）。`setsid sh -c
      'sleep 1.5; exec …'` のように**撃ったあとに bind する**形では、観測の
      時点ではまだ待ち受けていないので候補にすら入らず、`OK:` が出ていた
      （`exp_window` の掃引で bind 遅延 0.2〜1.5 秒すべて false green）。
      いまはプロセスとして控え、**撃ったあとに `_survivors` が LISTEN を読み直す**。
    """
    #: ★`blind`（6 巡目の続き・2026-09-05）＝**観測そのものが出来なかった理由**。
    #:   空でないときに「何も見つからなかった」を「残っていない」と読んではいけない。
    #:   ここに入れるのは **生き残りを隠しうる**失敗だけ（LISTEN の表・プロセス一覧）。
    #:   身内を**広げる**側の失敗（`ancestor_pgids` / `bg_records`）は偽の赤にしか
    #:   ならないので数だけ残す。
    blind: list[str] = []
    snap: dict = {"ports": set(), "kin": [], "scanned": False, "kin_total": 0,
                  "blind": blind,
                  # ★物差し（起動時刻の tick）が無いまま観測したか（5 巡目・調停係）。
                  #   True のときは「前から居た」を証明できないので候補を落として
                  #   いない＝本文にもそう書く（黙って緩めない）。
                  "no_ruler": since_ticks is None, "kept_unproven": 0}
    if sys.platform == "win32":
        return snap
    try:
        pgid = int(pgid)
    except (TypeError, ValueError):
        return snap
    imap = _listen_inode_ports(blind)
    # ★二次（union-2・2026-09-06）: `in_workspace` は毎回 `os.path.realpath(scope)`
    #   を引き直す。`scope` はこの関数の間ずっと同じ値なのに、下のループは
    #   1 走査で数百行を見るので実測 292 回同じ realpath を引き直していた
    #   （union-1 の反証役の指摘）。1 回だけ正規化して渡す（`in_workspace` 自身も
    #   `realpath` を通すので、既に正規化済みの値を渡しても意味は変わらない）。
    _scope_norm = None
    if scope:
        try:
            _scope_norm = os.path.realpath(scope)
        except (OSError, ValueError):
            _scope_norm = scope
    # ★`scanned` は「観測を試みた」ではなく「**観測できた**」の意味にする。
    #   旧実装は無条件に True で、しかもこの欄は本番のどの判断にも使われて
    #   いなかった（＝書いたが繋いでいない欄）。いま `blind` として繋ぐ。
    snap["scanned"] = not blind
    me, parent = os.getpid(), os.getppid()
    try:
        skip_pgids = procutil.ancestor_pgids()
    except Exception:
        _bump("bg_observe_degraded")
        skip_pgids = set()
    other_bg: set[int] = set()
    try:
        for rec in procutil.bg_records():
            g = rec.get("pgid") or rec.get("pid")
            if isinstance(g, int) and g != pgid:
                other_bg.add(g)           # 別の背景処理のサーバを「身内」に数えない
    except Exception:
        _bump("bg_observe_degraded")      # 身内を**広げる**側＝偽の赤にしかならない
    try:
        rows = procutil.proc_rows(skip_self=False)
    except Exception as e:
        # ★ここを `rows = []` で丸めると、**候補が 1 本も挙がらない**まま
        #   `_after_kill` が `OK: 止まりました` を返す（実測: `probe_blind.py` の
        #   `rows` 形）。生き残りを隠す側の失敗なので `blind` に積む。
        blind.append(f"プロセス一覧を読めません（proc_rows が失敗: "
                     f"{type(e).__name__}: {str(e)[:120]}）")
        rows = []
    pmap = _parent_map()
    pg_by_pid = {r["pid"]: r.get("pgid") for r in rows if isinstance(r.get("pid"), int)}
    kin: list[dict] = []
    for r in rows:
        pid = r.get("pid")
        if not isinstance(pid, int) or pid in (me, parent):
            continue
        pg = r.get("pgid")
        if pg in skip_pgids:
            continue                      # 自分・親・祖先のグループは見ない
        if pg == pgid:                    # 構成員＝強い証拠
            # ★8 巡目（再反証 M1）: 読めなかったら `blind` に積む。構成員の待受は
            #   `observed`＝撃ったあとに `port_in_use` で確かめる**唯一の証拠**なので、
            #   ここが読めないまま「観測できた」と言うと、確かめる相手が消える。
            ports = _pid_listen_ports(pid, imap, blind)
            if ports:
                snap["ports"] |= ports
            continue
        if pg in other_bg:
            continue                      # 別の背景処理の物
        if _descends_from(pid, pgid, pmap, pg_by_pid):
            kinship = "descendant"        # 親を辿ればこの起動に着く（cwd を問わない）
        else:
            # ★`in_workspace` が**壊れた**とき（union の B-1 のように意味が変わる／
            #   引数が変わる）に例外を素通しすると、観測の途中で道具ごと落ちる。
            #   落とす代わりに「この行は分類できなかった」として `blind` に積む
            #   ——身内を**取りこぼす**側なので、`OK:` を言わせない材料にする。
            try:
                in_scope = bool(scope) and procutil.in_workspace(r, _scope_norm, bg_pids=set())
            except Exception as e:
                blind.append(f"作業場の判定ができません（in_workspace が失敗: "
                             f"{type(e).__name__}: {str(e)[:120]}）")
                continue
            if not in_scope:
                continue
            kinship = "scope"             # 作業場の中に居るだけ（第三者かもしれない）
        if kinship == "scope":
            # ★時刻の関門は**弱い側にだけ**掛ける。親を辿って着いた相手
            #   （descendant）は、この起動より前から居ることが原理的にありえない
            #   （祖先がこのグループに居るのだから）。
            # ★★2026-09-05・5 巡目（must_fix ①）: **物差しが無いときは落とさない**
            #   （fail-open）。`_born_after_ticks` が `None` を返すのは
            #     ・控えに `start_ticks` が無い（`released`＝実機の台帳 17/17）
            #     ・相手の `/proc` が読めない
            #   のどちらかで、**どちらも「分からない」であって「前から居た」ではない**。
            #   旧実装はここで壁時計（`started_at`＝**打ち切り時刻**）＋余裕 5 秒に
            #   落ちていたので、5 秒より長く走ってから背景に回された起動では
            #   **本物の逃げ子を捨てて `OK:` を返していた**（実測 4/4）。
            #   落とさない側に倒した分だけ「作業場に居るだけの第三者」が候補に
            #   入りうるが、それは `_survivor_note` が
            #   「無関係の第三者かもしれません」と**先に書いて**引き受ける
            #   （＝偽の赤は言い回しで受け、false green は作らない）。
            _fresh = _born_after_ticks(pid, since_ticks)
            if _fresh is False:
                continue                  # この起動より前から居ると**証明できた**
            if _fresh is None:
                snap["kept_unproven"] += 1   # 物差しが無いまま残した（印を出す）
        # ⚠ ここ（撃つ**前**の身内）は読めなくても `blind` に積まない。撃てば
        #   死ぬ相手まで盲に数えると、正しく止まった停止が全部赤くなる。
        #   読めない身内が**撃ったあとも生きていた**ときにだけ盲にする
        #   ＝それを見るのは `_survivors`（`_is_alive` を通ってから読む）。
        kin.append(dict(r, ports=sorted(_pid_listen_ports(pid, imap)),
                        kinship=kinship))
    # ★残す順（2026-09-05・8 巡目・再反証 M2／**9 巡目・再反証 MF-3 で 3 段に戻した**）。
    #   8 巡目は `(descendant でない, ports が空 かつ bind しそうでもない, pid)` の
    #   **2 段**で、「**いま現に LISTEN している**」と「正規表現で bind しそう」を
    #   同じ段に潰していた。実測（`p_rank.py`・逃げ子は本物で撃つ前から LISTEN 中・
    #   水増しは `python app.py --port 8080` を 70 本）:
    #     base 06215bf … `Error: …待ち受けが残っています` ポート番号を名指し **する**
    #     8 巡目 tip  … `Error: …止まったかどうかは確かめられていません` 名指し **しない**
    #   どちらも赤だが、**唯一の次の一手（`BashOutput(port=<番号>)`）に要る番号が
    #   本文から消えた**（低 pid の推測候補が待受中の逃げ子を押し出した）。
    #   いまは 3 段: ①子孫 ②**現に待ち受けている** ③これから bind しうる（推測）。
    #   ⚠ 並べ替えの鍵は**行ごとに 1 回**だけ計算する（`_may_bind` は正規表現なので、
    #     比較のたびに呼ぶと O(n log n) 回走る）。
    ranked = [(r["kinship"] != "descendant",
               not bool(r.get("ports")),
               not _may_bind(r),
               r["pid"], r) for r in kin]
    ranked.sort(key=lambda t: t[:4])
    snap["kin_total"] = len(ranked)
    # ★上限は**弱い候補にだけ**掛ける（2026-09-05・9 巡目・再反証 MF-1）。
    #   8 巡目は強い候補も 64 本で切り、「切って生きていたら盲」にしていたが、
    #   `kin` は**撃つ pgid の外**の行しか入らない（構成員は上の `pg == pgid` で
    #   除かれる）ので `killpg` はその相手を一度も殺せない＝生存判定はほぼ常に真
    #   ＝**正しく止まった停止まで `Error:`**（`p_mf1.py` で実測・上の定数を参照）。
    #   いまは強い候補を `_KIN_STRONG_CAP` まで**全部読み直す**。上限を置いた理由は
    #   費用なので、費用の要らない相手（弱い＝待受も手掛かりも無い）から捨てる。
    strong = [t for t in ranked if not (t[1] and t[2])]
    weak = [t for t in ranked if t[1] and t[2]]
    keep = strong[:_KIN_STRONG_CAP]
    room = max(0, _KIN_CAP - len(keep))
    keep = sorted(keep + weak[:room], key=lambda t: t[:4])
    snap["kin"] = [t[4] for t in keep]
    # ★切った候補も**捨てない**。ただしここでは `blind` に積まない——撃ったあとに
    #   読み直して（`_survivors`）、そこで初めて盲にする。
    #   控えるのは強い候補だけでよい（弱い候補は待受も手掛かりも無い）。
    snap["kin_over"] = [t[4] for t in strong[_KIN_STRONG_CAP:]]
    # ★弱い候補を捨てた事実は**数で残す**（2026-09-05・9 巡目・MF-4 の副作用）。
    #   命令に数が出てこない遅延 bind（`sh -c 'sleep 3; exec ./server'` の
    #   非子孫）はここで痕跡ゼロのまま消える——本文には出さないが、
    #   counters で数えられるようにしておく（0 なら配線ミスと分かる形）。
    _dropped_weak = len(weak) - min(room, len(weak))
    if _dropped_weak:
        _bump("bg_kin_dropped_weak", _dropped_weak)
    # ★最後にもう一度: `scanned`＝**観測が最後まで通った**か（途中で読めなく
    #   なった／プロセス一覧が引けなかったなら False）。
    snap["scanned"] = not blind
    if blind:
        _bump("bg_observe_blind")
    if snap["no_ruler"]:
        _bump("bg_launch_ticks_missing")
    if snap["kept_unproven"]:
        _bump("bg_kin_kept_unproven", snap["kept_unproven"])
    return snap


def _busy_ports(ports, errors: list | None = None) -> list[int]:
    """観測したポートのうち、**いまも誰かが待ち受けている**もの。

    ★`errors`（6 巡目の続き）: `port_in_use` が落ちたポートは「空いていた」では
      なく「**確かめられなかった**」。丸めると、撃つ前に実際に握られているのを
      見たポートを、撃ったあと黙って忘れることになる。
    """
    out = []
    for p in sorted(ports):
        try:
            if procutil.port_in_use(p):
                out.append(p)
        except Exception as e:
            if errors is not None:
                errors.append(f"ポート {p} を確かめられません（port_in_use が失敗: "
                              f"{type(e).__name__}: {str(e)[:120]}）")
            continue
    return out


def _survivors(snap: dict) -> list[dict]:
    """控えた候補のうち、**いまも生きていて、いま LISTEN しているもの**。

    ★撃ったあとに読み直す（撃つ前の観測をそのまま結論にしない）。ポートが
      埋まっているだけでは足りない: 同じ番号を別プロセスが取り直す形（再 bind の
      競合）を「残骸」と呼ばないため、**その pid が今もそのソケットを握っているか**
      を見る。
    ★2026-09-05・4 巡目（再反証 (g)）: 判定を「控えたポートとの積」から
      「**いま握っているポート**」へ変えた。撃ったあとに bind する形
      （`sleep; exec http.server`）では控えが空なので、積を取ると必ず空になり
      見落としていた。控えの時点で待ち受けていなかったものには `late=True` を
      付けて、本文でそう言う（**いつ bind したかを言い換えない**）。
    """
    kin = snap.get("kin") or []
    blind: list = snap.setdefault("blind", [])
    over = snap.get("kin_over") or []
    # ★切り捨てのうち「**撃つ前から待ち受けていた**」行は、盲にする前に
    #   `kin` と**同じ物差し**で読み直す（2026-09-05・9 巡目・再反証 MF-1）。
    #   数は少ない（現に LISTEN していて、なお `_KIN_STRONG_CAP` から溢れた行だけ）
    #   ので費用は数本ぶん。読み直せば**ポート番号を名指しできる**＝
    #   「確かめられていません」より 1 段強い答えになる。
    watched = [r for r in over if r.get("ports")]
    #: 実際に待受を読み直した本数（本文と `blind` の両方がこの数を使う＝
    #: 「先頭 64 本」のような**実態と違う数**を書かないため）。
    snap["kin_read"] = len(kin) + len(watched)
    if not kin and not watched and not over:
        return []
    # ★読み直しのときに表が読めなくなったら、その事実も `blind` に積む
    #   （6 巡目の続き）。撃つ前は読めていたのに撃ったあと読めない、という形でも
    #   「生き残りは居なかった」とは言えない。
    imap = _listen_inode_ports(blind)
    out = []
    for r in list(kin) + watched:
        pid = r.get("pid")
        if not _is_alive(pid):
            continue                      # 本当に死んだ＝盲にしない（偽の赤を作らない）
        # ★8 巡目（再反証 M1）: **生きているのに待受を読めない**なら、
        #   「待ち受けていない」ではなく「観測できなかった」。ここを空集合に
        #   丸めていたので、他ユーザのプロセス（EPERM）で逃げると `OK:` が出た。
        errs: list[str] = []
        now = _pid_listen_ports(pid, imap, errs)
        for m in errs:
            if m not in blind:            # 窓の中で何度も読み直すので重複は積まない
                blind.append(m)
        if not now:
            continue
        out.append(dict(r, ports=sorted(now), late=not (r.get("ports") or ())))
    # ★読み直していない切り捨て（＝撃つ前は待ち受けておらず、`_KIN_STRONG_CAP` から
    #   溢れた強い候補）が**撃ったあとも生きている**なら、観測は最後まで通っていない
    #   （2026-09-05・8 巡目 M2 → 9 巡目 MF-1 で範囲を絞った）。
    #   ⚠ ここの「生きている」は「撃って死ぬはずの相手が生き残った」という意味では
    #     **ない**。`kin` は定義上この pgid の外の行なので `killpg` では死なない。
    #     意味は「**待受を読み直していない相手が、まだポートを握りうる状態にある**」
    #     ——死んでいれば握れないので、そこだけを安く落とすための判定。
    #   `_is_alive` は `os.kill(pid, 0)` 1 発なので `/proc/<pid>/fd` の読み直し
    #   （上限を置いた当の理由）とは費用が桁違い＝ここは全部見てよい。**1 回だけ**
    #   見る（`_await_late_bind` が窓の中で何度も呼ぶので）。
    rest = [r for r in over if not r.get("ports")]
    if rest and not snap.get("kin_over_seen"):
        snap["kin_over_seen"] = True
        left = [r for r in rest if _is_alive(r.get("pid"))]
        if left:
            # ★言葉を実態に合わせる（2026-09-05・9 巡目・MF-4 と同じ規律）。
            #   「まだ生きています」だけだと**撃ったのに死ななかった**と読める。
            #   `kin` はこの pgid の外の行なので `killpg` の相手ではない
            #   ——ここで言えるのは「まだポートを握りうる状態にある」だけ。
            msg = (f"候補が {snap.get('kin_total')} 本あり、待ち受けを読み直せたのは "
                   f"{snap['kin_read']} 本だけです"
                   f"（読み直していない候補のうち {len(left)} 本が生きています"
                   "——**撃った相手ではありません**。"
                   "この作業場に残骸が無いとは言えない、という意味です）")
            if msg not in blind:
                blind.append(msg)
            _bump("bg_kin_truncated", len(left))
    return out


#: 撃った直後に bind する逃げ子を待つ窓（秒）。**逃げ子が生きているときだけ**払う。
#: 実測（`exp_window` の掃引）: bind 遅延 0.2〜1.5 秒のどれでも、待たなければ
#: `OK: SIGTERM で止まりました` が出て**サーバは上がってくる**（4/4 false green）。
_LATE_BIND_SETTLE = 2.0

#: 弱い候補（`_may_bind` が False＝命令にポートの手掛かりが無い非子孫）に払う窓（秒）。
#: ★2026-09-05・10 巡目（再反証 MF-A）。9 巡目までは**ゼロ**で、弱い候補は
#: `pend` から外れて一度も読み直されなかった＝`OK:` の製造元だった（下の実測表）。
#: 強い窓（2.0 秒）と同じ額を払うと `setsid sleep 300 &` が居るだけの停止が
#: 14 倍になる（5 巡目の実測 151 ms → 2,195 ms）ので、**桁を分ける**。
#:
#: ★額は**測って決めた**（推測で置かない）。反証役が挙げた弱い形
#:   （`setsid python -u srv.py &`／ポートは `port.txt`／bind は 1.2 秒後）を
#:   本番経路で撃ち、窓だけを振った実測:
#:     0（9 巡目）… 156 ms  `OK: SIGTERM で止まりました` ＋サーバは上がる ★false green
#:     0.3        … 481 ms  **やはり false green**（bind は窓の外＝この額では買えない）
#:     0.6        … 798 ms  5/5 捕まえるが、**最後の 1 周**で捕まえている＝余裕ゼロ
#:     0.8        … 792〜798 ms  3/3 捕まえる（deadline まで ~300 ms 余る）
#:   逆側（`setsid sleep 300 &` だけ＝永遠に bind しない）の費用は **1,004 ms**
#:   （窓ゼロのときは 151〜200 ms）。4 巡目の 2,195 ms よりは安いが**タダではない**。
#:   ⚠ この窓は「逃げた非子孫が生きていて、まだ何も握っていない」停止でだけ払う。
_LATE_BIND_WEAK_SETTLE = 0.8

#: 弱い候補を何本まで見続けるか。読み直しは `/proc/<pid>/fd` の走査なので、
#: 作業場に無関係のプロセスが大量に居る形で費用が跳ねないよう頭を切る。
_WEAK_WATCH_CAP = 8


def _may_bind(row: dict, hint_port: int | None = None) -> bool:
    """この候補は「**これから待ち受けを始めるかもしれない**」相手か。

    ★2026-09-05・5 巡目（must_fix ⑤）。4 巡目の `_await_late_bind` は
      **逃げ子が 1 本でも生きていれば必ず 2 秒待って**いた。実測: サーバでも
      何でもない `bash build.sh`（`setsid sleep 300 &` が 1 本逃げただけ）の停止が
      **151 ms → 2,195 ms（14 倍）**。窓は「待てば上がってくる相手」にだけ払う。
    通す条件（どれか 1 つ）:
      ① `kinship="descendant"` … 親を辿ればこの起動に着く＝**自分の子**。何をする
         つもりかは分からないので、生きているうちは待つ。
      ② 候補**自身の命令**からポート番号が読める（`bash._port_in_cmd`）。
         `sh -c 'sleep 1.5; exec python -m http.server 8123'` はここで通る。
      ③ 控えの命令にポートの手掛かりがある（`php -S 0.0.0.0:8000` を起こした走行）。
    ⚠ ②③ は**推測**（正規表現）。**推測が何を動かすかを正確に書く**
      （2026-09-05・9 巡目・再反証 MF-4。8 巡目までここには「判定には一切使わない」と
      書いてあったが、8 巡目で `_observe_listen` の**残す/切る**と `kin_over` の
      **控える/捨てる**の両方に入った＝結論を左右していた＝散文が嘘になっていた）。
      いま推測が動かすのは 2 つだけ:
        ・**誰を見続けるか**（`_await_late_bind` で待つ／`_KIN_STRONG_CAP` まで
          切らずに読み直す）。見続けた結果の結論は、**観測した LISTEN だけ**から出す。
        ・**言い切らない**という結論（`_KIN_STRONG_CAP` を超えた推測候補を切って、
          それが生きているときは「確かめられていません」と断る）。
      推測だけを根拠に「**残っている**」と言うことは無い（ポート番号を出す本文は
      必ず `/proc` で読み直した LISTEN が根拠）。
    ⚠⚠ **推測が False のときは「安全側」ではない**（2026-09-05・10 巡目・再反証 MF-A）。
      9 巡目までここには「逆に『止まった』を**言わない**理由にはする——推測を
      安全側にだけ効かせるため」と書いてあったが、**実測は逆だった**。
      `_await_late_bind` は `_may_bind` が False の候補を `pend` から外すので、
      **命令に番号が出てこない遅延 bind の逃げ子**（`setsid python -u srv.py &`／
      ポートは `port.txt` から読む）は待たれず、`OK: SIGTERM で止まりました` が
      出て**サーバは上がってくる**＝推測の False が `OK:` を作っていた。
      実測（head 5e4f066・本番経路 `bash_output._run({"pid": …, "kill": True})`）:
        強い形（命令に番号がある）… 784 ms / `Error:` ＋ ポートを名指し   false green 無し
        弱い形（命令に番号が無い）… 156 ms / `OK: SIGTERM で止まりました` **false green**
      10 巡目で弱い候補にも**短い窓**（`_LATE_BIND_WEAK_SETTLE`）を付けた。いま推測が
      動かすのは「待つ／待たない」ではなく **どれだけ長く見続けるか**（強い 2.0 秒 /
      弱い 0.8 秒）。それより遅く bind する弱い相手は**いまも取り逃す**
      （`test_命令に番号が無い遅い遅延bindは今も取り逃す` が `xfail(strict=True)` で残す）。
    """
    if row.get("kinship") == "descendant":
        return True
    if hint_port is not None:
        return True
    try:
        from .bash import _port_in_cmd
    except Exception:
        return True                       # 読めないなら安全側（待つ）
    text = str(row.get("cmdline") or "")
    if not text:
        text = " ".join(str(t) for t in (row.get("argv") or ()))
    return _port_in_cmd(text) is not None


def _await_late_bind(snap: dict, window: float = _LATE_BIND_SETTLE,
                     *, hint_port: int | None = None) -> list[dict]:
    """撃った直後に bind する逃げ子を、**窓を切って**待つ（2026-09-05・4 巡目 (g)）。

    ★なぜ待つのか: `setsid sh -c 'sleep 1.5; exec <server>'` の形では、撃つ時点でも
      撃った直後でも、その子はまだ 1 つもポートを握っていない。読み直しを 1 回で
      済ませると「待受は無い」＝`OK:` になり、**1 秒後にサーバが上がってくる**
      （次の起動が `Address already in use` になる、この係が潰している当の形）。
    ★何を払うのか（実測に合わせて書き直した・5 巡目 must_fix ⑤）: 窓は
      「**これから待ち受けを始めるかもしれない逃げ子（`_may_bind`）が、生きていて、
      まだ何も握っていない**」ときだけ払う。
        ・逃げ子が居ない普通の停止 … 0 秒
        ・逃げた子が居ても `sleep 300` のような非サーバだけ … **0.8 秒**（実測 1,004 ms）
          （4 巡目はここで 2 秒払っていた＝`bash build.sh` の停止が 14 倍。
            9 巡目は 0 秒にしたが、それが MF-A の false green を作っていた。
            10 巡目で 0.8 秒に**戻した**——額の根拠は `_LATE_BIND_WEAK_SETTLE` の実測表）
        ・既に待ち受けている … 1 回目の読み直しで見つかるので 0 秒
        ・全員死ねば即座に抜ける
    ★★2026-09-05・10 巡目（再反証 MF-A）: **弱い候補にも短い窓を払う**。
      9 巡目までは `_may_bind` が False の候補を `pend` から**外して**いたので、
      「命令に番号が出てこない遅延 bind」（`setsid python -u srv.py &`／ポートは
      `port.txt` から読む）は一度も読み直されず、**推測の False がそのまま
      `OK: SIGTERM で止まりました` を作っていた**（実測 156 ms・サーバは上がってくる）。
      いまは窓の**額**を分ける: 強い候補が 1 本でも居れば `window`（2.0 秒）、
      居なければ弱い候補だけを `_WEAK_WATCH_CAP` 本まで `_LATE_BIND_WEAK_SETTLE`
      （0.8 秒）見る。`sleep 300` の停止は 2,195 ms → **1,004 ms**。
    ⚠ 残る穴（3 つ・どれも正直に）:
      ・窓（強い 2.0 秒）より遅く bind する子は取り逃す。**待てば必ず捕まるとは
        言わない。**
      ・弱い候補の窓は 0.8 秒しかない。`sh -c 'sleep 3; exec ./server'` のように
        **それより遅く** bind する非子孫は**いまも取り逃して `OK:`** になる
        （実測: bind 2.5 秒の形は 470 ms で `OK:`＝取り逃す）
        （`xfail(strict=True)` の試験で残してある）。子孫（①）なら 2.0 秒待つ。
      ・弱い候補は `_WEAK_WATCH_CAP` 本で頭を切る（切った側は見ていない）。
    """
    idle = [r for r in (snap.get("kin") or []) if not r.get("ports")]
    # ★`_may_bind` は正規表現なので**行ごとに 1 回**だけ（`_observe_listen` の
    #   並べ替えと同じ規律）。
    strong = [r for r in idle if _may_bind(r, hint_port)]
    if strong:
        pend, win, weak_tier = strong, max(0.0, window), False
    else:
        pend, win, weak_tier = idle[:_WEAK_WATCH_CAP], _LATE_BIND_WEAK_SETTLE, True
    if not pend or win <= 0:
        return []
    if weak_tier:
        _bump("bg_late_bind_weak_watch", len(pend))
    import time as _t
    deadline = _t.time() + win
    while _t.time() < deadline:
        if not any(_is_alive(r.get("pid")) for r in pend):
            return []                     # 待つ相手がもう居ない
        _t.sleep(0.1)
        surv = _survivors(snap)
        if surv:
            _bump("bg_late_bind_caught")
            if weak_tier:
                # ★弱い窓が**現に効いた**回数（0 なら 10 巡目の直しは死んでいる）。
                _bump("bg_late_bind_caught_weak")
            return surv
    return []


def _survivors_now(snap: dict, *, hint_port: int | None = None) -> list[dict]:
    """撃った（あるいは撃つまでもなかった）**あとの**生き残り。**呼ぶ側は 1 本だけ。**

    ★2026-09-05・5 巡目（must_fix ②）: 「読み直す」と「遅れて bind する子を待つ」を
      **1 本に畳む**。4 巡目は `_after_kill` にだけ窓を付けたので、
      `_run` の `already exited` 枝（グループが撃つ前から空）と
      `_stop_group` の「構成員が見えない」枝は**読み直し 1 回で結論**していた。
      実測（`exp_g2`）: `setsid sh -c 'sleep 0.8; exec <server>' &` を起こした走行で
      `pid N already exited`（＝`Error:` でないので機械は**成功**と読む）を返し、
      その 0.8 秒後にサーバが上がってきた（bind 遅延 0.8/1.5 秒とも false green）。
      枝ごとに違う結論の作り方を持つと、片方だけ直る。
    """
    return _survivors(snap) or _await_late_bind(snap, hint_port=hint_port)


def _hint_note(hint_port, observed) -> str:
    """控えの命令から読めた数を**推測だと断って**添える（証拠には使わない）。"""
    if hint_port is None or hint_port in observed:
        return ""
    return (f"\n  ⚠ 控えの命令から読める `:{hint_port}` は正規表現の**推測**です"
            "（実際の待受と一致する保証が無いので、判定には使っていません）。")


class _How(NamedTuple):
    """止め方の言い回しを **2 つ**持つ（2026-09-05・第4回c 4 巡目・must_fix ⑤）。

    ★何を直したか: 生き残りが居るときの本文が
      「グループ N は片付きました（**SIGTERM で止まりました**）が、…まだ待ち受けが
      あります」になっていた。**散文だけ正しく見えて、読むと「止まった」に見える**
      （この係が潰している当の欠陥＝文言と実態のずれ）。グループに限定した
      言い回しを別に持ち、生き残りが居る本文ではそちらを使う。
    """
    ok: str                               # 止まったと言い切ってよいときの言い回し
    group: str                            # ★グループに限定した言い回し（生き残りが居る）


_H_TERM = _How("SIGTERM で止まりました",
               "構成員は SIGTERM で居なくなりました")
_H_SENT = _How("SIGTERM を送りました",
               "構成員は SIGTERM のあと居なくなりました")
_H_KILL = _How("SIGTERM では止まらず SIGKILL で止めました",
               "構成員は SIGKILL で居なくなりました")
_H_GONE = _How("killpg が ProcessLookupError（撃つ前に空になっていました）",
               "撃つ前からグループは空でした")


def _survivor_note(pgid: int, survivors: list[dict], *, how: str,
                   hint_port=None, observed=frozenset(),
                   kin_total: int | None = None, snap: dict | None = None) -> str:
    """グループは片付いたのに、待ち受けが生き残っている。

    ★`Error:` で始める（2026-09-05・再反証 M4）。この現場で「失敗」を機械が読む
      根拠は **`Error:` 前置き**（`tools/__init__.py` の `tool_failed`）で、
      「`OK:` で始めない」では**成功として記録される**。2 巡目の
      `_unverified_stop_note` はそこを取り違えていた。
    ★原因は断定しない（2 巡目 must_fix ②と同じ規律）。撃つ助言もここからは出さない。
    ★★4 巡目（must_fix ⑥）: 身内かどうかで**赤の強さを分ける**。
      親を辿って着いた（`kinship="descendant"`）なら「この起動が起こしたプロセス」と
      言ってよい。辿れなかった（`"scope"`）＝作業場に居るだけなら、
      **「無関係の第三者かもしれない」を本文の先に書く**。作業場を範囲にした
      副作用（run50 で `php -S 18300/18301` が並走していたような形）で
      **偽の赤**を出さないため。撃つ導線はどちらでも `BashOutput(port=…)`
      （持ち主と cwd を並べてから）で、ここから直接 `kill=true` は勧めない。
    ★★5 巡目（調停係の指摘②）: **物差しが読めなかった事実を本文に出す**。
      控えに `start_ticks` が無い（＝打ち切りで背景に回された控え）と、
      「この起動より前から居た」を証明できないので候補を落としていない。
      緩めたことを黙っていると、赤の強さを読む側が測れない。
    """
    kin_d = [r for r in survivors if r.get("kinship") == "descendant"]
    kin_s = [r for r in survivors if r.get("kinship") != "descendant"]
    shown = kin_d + kin_s
    no_ruler = bool((snap or {}).get("no_ruler")) and bool(kin_s)
    if kin_d:
        lines = [f"Error: グループ {pgid} は片付きました（{how}）が、"
                 "**この起動から枝分かれしたプロセスが、いまも待ち受けています**"
                 "（撃つ前に親子関係を辿って控え、撃ったあとに /proc を読み直しました）:"]
    else:
        lines = [f"Error: グループ {pgid} は片付きました（{how}）が、"
                 "**同じ作業場に、この起動の残骸かもしれない待ち受けが残っています**。",
                 "  ⚠ 親子関係は辿れませんでした（親が先に死ぬと辿れなくなります）。"
                 "**この起動の残骸（setsid / nohup でグループの外へ出た子）かもしれず、"
                 "無関係の第三者かもしれません**——この道具では区別できません。"
                 "持ち主と cwd を見てから判断してください:"]
    if no_ruler:
        lines.append("  ⚠ この控えには起動時刻（start_ticks）がありません"
                     "（打ち切りで背景に回された控え）。**この起動より前から居たかを"
                     "証明できない**ので、作業場の中の待ち受けを落とさずに並べています。")
    for r in shown[:4]:
        tag = "この起動の子孫" if r.get("kinship") == "descendant" else "作業場の中"
        _r_prot = sorted(set(r.get("ports") or ()) & procutil.protected_ports())
        lines.append(f"  [{tag}] " + procutil.describe(r, 70)
                     + "  → ポート " + ", ".join(str(p) for p in r["ports"])
                     + (f"\n      cwd: {r['cwd']}" if r.get("cwd") else "")
                     + ("\n      ★撃つ前は待ち受けていませんでした"
                        "（撃ったあとに bind しています）" if r.get("late") else "")
                     # ★二次（union-2・2026-09-06）: 保護ポートかどうかは
                     #   `_port_report` 側の別の門でしか言っていなかった。
                     #   ここでも保護ポートなら明示する。
                     + (f"\n      ★ポート {', '.join(str(p) for p in _r_prot)} "
                        "は保護された設備です" if _r_prot else ""))
    if len(shown) > 4:
        lines.append(f"  （ほかに {len(shown) - 4} 本）")
    # ★読み直した本数は**実際に読み直した数**を書く（2026-09-05・9 巡目）。
    #   上限は弱い候補にだけ効くので、`_KIN_CAP` を書くと嘘になる
    #   （強い候補が 64 本を超えても全部読み直している）。
    _read = ((snap or {}).get("kin_read")
             or len(((snap or {}).get("kin") or [])))
    if kin_total and _read and kin_total > _read:
        lines.append(f"  （候補は {kin_total} 本あり、{_read} 本を読み直しました）")
    if kin_d:
        lines.append("  グループの外へ出た起動（setsid / nohup 付きサブシェル等）は"
                     "`killpg` では止まりません。")
    lines.append(f"  持ち主を確かめる: BashOutput(port={shown[0]['ports'][0]})"
                 " ——持ち主と cwd を並べてから、作業場の中の物だけ止められます"
                 "（ここでは何も撃っていません）。")
    return "\n".join(lines) + _hint_note(hint_port, observed)


def _proc_cwd(pid: int) -> str:
    """`/proc/<pid>/cwd` の実体（読めなければ空文字）。**台帳の自己申告より現物を優先する。**"""
    try:
        return os.path.realpath(os.readlink(f"/proc/{int(pid)}/cwd"))
    except (OSError, ValueError, TypeError):
        return ""


def _uptime_ticks() -> int | None:
    """いま**起動から何 tick 経ったか**（読めなければ None）。

    ★なぜ `time.time()` でなく tick か（2026-09-05・第4回c 第2反証）:
      比べたい相手は `procutil.proc_start_ticks(pid)`＝**boot からの tick**。
      これを壁時計に直すには `/proc/stat` の `btime`（**整数秒**）が要り、
      そこで ±1 秒の丸めが入る。同じ物差し（boot からの tick）のまま比べれば
      丸めは要らない。**「分からない」を「別人」と言わない**ための精度。
    """
    try:
        with open("/proc/uptime") as fh:
            secs = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    # ★except を絞る（5 巡目・調停係）。`procutil._clock_ticks` の取り違えを
    #   100.0 に丸めると、tick の比較が**静かに間違う**（誰も気づけない）。
    hz = procutil._clock_ticks()          # 同じ換算を 2 つ持たない（読むだけ）
    return int(secs * hz)


def _started_after(pid, since_ticks: int | None) -> bool | None:
    """`pid` は `since_ticks`（boot からの tick）より**後**に生まれたか。

    戻り: True=後に生まれた／False=それ以前／**None=読めない（分からない）**。
    ★None を False と同じ扱いにしないこと。「分からない」を根拠に断定するのが
      この係が潰している当の欠陥。
    ★except は絞る（5 巡目・調停係）: `AttributeError`/`TypeError`（＝コードの
      取り違え）を飲むと、関門が黙って素通りしていることに誰も気づけない。
    """
    if since_ticks is None:
        return None
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        st = procutil.proc_start_ticks(pid_i)
    except (OSError, ValueError):
        return None
    if st is None:
        return None
    return st > since_ticks


def _gate_broken(pid: int, what: str, exc: Exception) -> str:
    """所有権の判定そのものが**壊れた**ときの断り文（6 巡目）。

    ★`撃つ許可` の門は「分からない」を「よい」に読み替えない（fail-closed）。
      union で実際に起きたのは逆で、`procutil._boot_time` の改名による
      `AttributeError` が `except Exception` に飲まれ、関門が黙って素通りした。
      黙って通すより、**壊れていることが分かる形で止まる**ほうが安い。
    """
    _bump("bg_own_gate_broken")
    return (f"Error: pid {pid} の所有権を確かめられません"
            f"（{what}が失敗: {type(exc).__name__}: {str(exc)[:200]}）。"
            "**撃っていません。**\n"
            "  これは道具の欠陥です（環境の問題ではありません）。"
            "止めたい相手が分かっているなら BashOutput(port=…) で持ち主を出してください。")


def _ownership_note(pid: int, meta: dict, meta_p, members: list) -> str | None:
    """撃つ前に「これは本当に、台帳が言っているその処理か」を確かめる
    （2026-09-05・第4回c。反証④「書いたが繋いでいない」への配線）。

    ★塞いだ穴（実測）: `procutil.bg_record_matches` / `owned_bg_pids`（PID 使い回しの
      関門）は `_port_report` と `bash._scope_name_kill` からしか呼ばれておらず、
      **いちばん撃つ経路である `BashOutput(pid=…, kill=true)` は台帳ファイルの
      存在だけを見て `killpg` していた**。作業場の外（cwd=/tmp）で起こした無関係の
      プロセスに、同じ番号の古い控え（`start_ticks` 不一致・mtime は 8/6）を置くと、
      `owned_bg_pids()` は `set()`・`bg_record_matches` は False と**正しく弾いて
      いるのに**、この経路は `OK: SIGTERM で止まりました` を返してその無関係の
      プロセスを殺した。通行証は塞いだのに、扉が開いたままだった形。
    判定は `procutil` の既存の 1 本に委ねる（同じ規律を 2 つ持たない）。
    通すのは次の**どれか 1 つが言えるとき**だけ（2026-09-05・5 巡目 must_fix ③で狭めた）:
      ① **この控えが、目の前のこのグループの物だと証明できた**
         （`_record_proves_group`＝長が生きていれば `bg_record_matches`、
          打ち切りで背景に回された控えなら**控えに書いてある構成員**との照合）。
         そのときだけ、cwd の手掛かり（台帳の申告・長の現物・構成員のどれか 1 つ）で
         作業場の中と見なしてよい。
      ② **長の現物**（`/proc/<pid>/cwd`）が作業場の中。
      ③ **構成員が全員**作業場の中。
    ★★なぜ狭めたか（4 巡目の穴・`exp_own` が実物で再現）: 4 巡目は「台帳の申告・
      長の現物・**構成員のどれか 1 本**が作業場の中なら通す」だった。すると
      **他人のプロセスグループに、作業場の中に居る構成員が 1 本混じっているだけで
      通ってしまう**——長が死んだ他人のグループ（構成員は `/tmp` と作業場に 1 本ずつ）に
      古い控え（8/6・`start_ticks` 無し）を置いて `kill=true` を押すと、
      3 形すべてで `OK: SIGTERM で止まりました` を返し、**他人のグループを実際に殺した**
      （台帳の cwd が作業場／`/tmp`／無し のいずれでも。陰性対照 cb1a54e は断っていた）。
      「1 本でも中なら」は**撃つ許可**の側なので、そこは fail-closed に戻す。
      自分の起動が止められなくなる形（`cd /tmp && setsid …` で構成員 1 本が外へ出る）は
      ①が引き受ける——控えが自分の物だと**証明できている**から通す、という順序。
    ⚠ 残る形（正直に）: `run_in_background` の控えは**構成員を書いていない**ので、
      その控えで「長が先に死に、構成員の 1 本が作業場の外へ出た」形になると、
      証明の材料が無く断る（＝自分の起動を止められない）。実機の記録ではまだ
      見ていない形なので、出てから塞ぐ——塞ぎ方は `_release_record` と同じで、
      **控えに構成員を書く**（`bash.py` 側）。
    戻り: 断るときの本文（通ってよいなら None）。
    """
    if sys.platform == "win32":
        return None                       # /proc も pgid も無い経路（taskkill 側）
    # ★この門が**本番で何回働いたか**を数として残す（6 巡目・must_fix ②の測り方）。
    #   「呼ばれ 30 回・断り 0 回」が分かったのは union が数えたからで、
    #   数が無ければ「配線してあるが一度も効いていない」に気づけない。
    _bump("bg_own_gate_checked")
    try:
        leader_ticks = procutil.proc_start_ticks(pid)
    except (OSError, ValueError):
        leader_ticks = None
    # ★台帳の記録は**1 回だけ組む**（6 巡目・union の反証役の指摘②「同じ事実を
    #   2 通りに計算しない」）。5 巡目は `{pid, start_ticks, mtime}` の**縮約**を
    #   ここで作り直し、`released` の控え（構成員入り）は別関数で二度読んでいた。
    #   `procutil.bg_records()` が作るのと**同じ形**（本文の全欄＋ファイル名の pid＋
    #   その meta の mtime）を 1 つ組み、判定は `procutil` の 1 本に委ねる。
    rec = _as_bg_record(pid, meta, meta_p)
    proven = False                        # ★この控えが「このグループの物」だと言えたか
    if leader_ticks is not None:
        try:
            ok = procutil.bg_record_matches(rec, live=members)
        except Exception as e:
            # ★6 巡目（must_fix ①の同型を自分の範囲で潰す）: 旧 `ok = True` は
            #   **照合できなかったことを「一致した」に化けさせる** fail-open だった。
            #   union で実際に起きた形（`procutil._boot_time` の改名で
            #   `AttributeError` が飲まれ、起動時刻の関門が全部素通り）と同じ。
            #   ここは**撃つ許可**なので、確かめられないなら撃たない。
            return _gate_broken(pid, "台帳との照合", e)
        if not ok:
            _bump("bg_own_gate_denied")
            return (f"Error: pid {pid} は台帳の記録と**別人**です"
                    "（PID が使い回されています）。**撃っていません。**\n"
                    f"  台帳: {str(meta.get('cmd',''))[:120]}"
                    f"（started {meta.get('started_at','?')}）\n"
                    f"  現物: {_proc_cwd(pid) or '(cwd 不明)'} で走っている別のプロセス\n"
                    "  止めたい相手が分かっているなら BashOutput(port=…) で持ち主を出してください。")
        proven = True                     # 長が生きていて開始時刻も一致した
    else:
        proven = _released_record_proves(meta, meta_p, members, rec=rec)
    # ── cwd の手掛かり（2026-09-05・5 巡目 must_fix ③で狭めた）──────────────
    leader_cwd = _proc_cwd(pid)           # 長がまだ生きていれば /proc の現物
    member_cwds: list[tuple[str, str]] = []
    for m in members:
        v = _proc_cwd(m.get("pid") or 0) or str(m.get("cwd") or "")
        if v:
            member_cwds.append((f"構成員 pid {m.get('pid')}", v))

    broken: list[Exception] = []          # 判定そのものが壊れた（＝道具の欠陥）
    # ★台帳の走査は 1 回だけ（`in_workspace` は既定で毎回 `owned_bg_pids()` を引く）。
    #   構成員が 200 本ある形（`exp_perf2`）で 200 回引き直すのは無駄。
    try:
        bg_pids = procutil.owned_bg_pids()
    except Exception as e:
        bg_pids = set()
        broken.append(e)                  # ★空集合で「外に居る」と言い切らない（6 巡目）

    # ★作業場は**明示して渡す**（2026-09-05・統合）。`procutil.in_workspace` は
    #   `workspace` を省くと **False**（fail-closed。分からない物を通すのは
    #   「撃つ許可」の側だから）。lifecycle 単独枝の `in_workspace` は省略時
    #   `canonical_workspace()` へ落ちる別の既定を持っており、その前提で書かれた
    #   下の `_inside()` は `workspace=` を省いて呼んでいた——union 側の
    #   `in_workspace` に重ねるとそこが **必ず False** になり、`_ownership_note` が
    #   **必ず「作業場の外」を返す**（union-1 で 28 本が赤くなったのと同型）。
    #   正典を自分で引いて、下の呼び出し全部に明示して渡す。
    try:
        ws = procutil.canonical_workspace()
    except Exception:
        ws = "(不明)"                     # 引けない＝断る側へ倒す（従来どおり fail-closed）

    def _inside(c: str) -> bool:
        """作業場の中か。**判定できなければ False**（＝撃たない側）。

        ★6 巡目（must_fix ①の同型）: 5 巡目はここが `None`＝「判定できない」で、
          呼ぶ側は `return None`（＝**撃ってよい**）に倒していた。`in_workspace` が
          例外を上げるのは `/proc` が読めないときではなく**コードの取り違え**の
          ときなので、「分からない」を撃つ許可に読み替えてはいけない。
          壊れた事実は `broken` に集めて、断り文で名指しする。
        """
        if not c:
            return False
        try:
            return bool(procutil.in_workspace({"pid": pid, "cwd": c}, ws, bg_pids=bg_pids))
        except Exception as e:
            broken.append(e)
            return False

    # ② 長の現物が中（証明が無くても通してよい唯一の cwd。現物なので古びない）
    if _inside(leader_cwd):
        return None
    # ③ 構成員が**全員**中（1 本だけを根拠にしない）
    if member_cwds and all(_inside(c) for _l, c in member_cwds):
        return None
    # ① 証明済みの控えなら、申告や構成員 1 本でも自分の物として扱う
    #    （`cd /tmp && setsid …` で構成員が 1 本外へ出る自分の起動を、
    #      ここで永久に止められなくしないため）
    if proven:
        # 台帳一致そのものを見る（`owned_bg_pids` の枝）。cwd が読めない相手でも
        # 「自分が起こした背景処理」なら止められる——`_port_report` と同じ規律。
        try:
            if procutil.in_workspace({"pid": pid, "cwd": ""}, ws, bg_pids=bg_pids):
                return None
        except Exception as e:
            broken.append(e)              # ★通さない（6 巡目・上と同じ理由）
        seen = [("台帳", str(meta.get("cwd") or ""))] + member_cwds
        for _l, c in seen:
            if _inside(c):
                return None
    # ★手掛かりが 1 つも無いとき（長は死に、構成員の cwd も読めず、台帳にも申告が
    #   無い）は**断る**（5 巡目で変えた）。4 巡目は「手掛かりが無い＝断定しない」で
    #   通していたが、それは「何も分からない相手に `killpg` してよい」と同じ意味で、
    #   実機の台帳（17/17 が cwd 無し）では**いちばん起きやすい形**だった。
    looked = "／".join(f"{l}={c}" for l, c in
                      ([("台帳", str(meta.get("cwd") or "(無し)")),
                        ("長の現物", leader_cwd or "(読めない)")] + member_cwds)[:4])
    if broken:
        return _gate_broken(pid, "作業場の判定", broken[0])
    _bump("bg_own_gate_denied")
    return (f"Error: pid {pid} は**作業場の外**にいます（作業場 {ws}）。"
            "**撃っていません。**\n"
            f"  見た cwd: {looked}\n"
            # ★must_fix B-5（union-2・2026-09-06）: `ws` が `NO_WORKSPACE`
            #   （床に届かない・実在し得ないパス）だと、この本文には
            #   幽霊パスだけが出て「なぜ作業場が無いのか」が分からない。
            #   `procutil.workspace_note()` はその理由（`LAPS_WORKSPACE`
            #   が `/`・ホーム・深さ 1 だった、等）を添える。
            + procutil.workspace_note()
            + ("" if proven else
               "  この控えが**このグループの物である証拠がありません**"
               "（長は死んでいて、控えの構成員とも一致しません）。PID の使い回しで"
               "無関係のグループを指している可能性があります。\n")
            + "  この道具が止めてよいのは、自分が起こした背景処理か、作業場の中の"
            "プロセスだけです（cwd 引数では広げられません）。")


#: 控えを書いた時刻より**これ以上あとに**生まれた構成員は、控えの構成員と同じ番号でも別人。
#: ★正本は `procutil._BG_MEMBER_SLACK_SEC`（6 巡目に 1 本化）。同じ数を 2 箇所に
#:   持つと片方だけ直る——この現場が繰り返している形なので、参照だけにする。
_REL_MEMBER_SLACK_SEC = procutil._BG_MEMBER_SLACK_SEC


def _as_bg_record(pid: int, meta: dict, meta_p) -> dict:
    """`procutil.bg_records()` が作るのと**同じ形**の 1 件を、手元の meta から組む。

    ★なぜ関数にするか（6 巡目・union の反証役の指摘②）: 5 巡目はここが 2 通りに
      分かれていた——`_ownership_note` は `{pid, start_ticks, mtime}` の**縮約**を
      作って `bg_record_matches` に渡し、`released` の控え（`members` 入り）は
      別関数が `meta` を**もう一度**読んでいた。同じ事実の 2 通りの計算は、
      片方だけ直る形（この現場が何度も踏んでいる）。1 本にする。

    `bg_records()` との対応: `pid` は**ファイル名**（＝引数の `pid`）、
    `mtime` はその meta ファイルの mtime、残りは本文の全欄。
    `meta_p` は `None` でも落ちない（単体で呼ぶ経路がある・5 巡目 nit ⑥）。
    """
    rec = dict(meta)
    rec["pid"] = pid                      # 本文の pid より**ファイル名**を信じる
    if not isinstance(rec.get("pgid"), int):
        rec["pgid"] = pid
    try:
        rec["mtime"] = meta_p.stat().st_mtime
    except (OSError, AttributeError, TypeError):
        rec["mtime"] = 0.0
    return rec


def _released_record_proves(meta: dict, meta_p, members: list, *,
                            rec: dict | None = None) -> bool:
    """打ち切りで背景に回された控え（`released`）が、**このグループの物**だと言えるか。

    ★6 巡目: 判定の中身は `procutil.bg_group_matches` に移した（`bg_record_matches`
      の「長が死んでいる」枝と**同じ 1 本**）。ここはその呼び口だけ残す——
      5 巡目は同じ規律が `procutil`（長が生きている形）と `bash_output`（長が
      死んだ形）に分かれており、`owned_bg_pids()` は後者を知らないままだった
      ＝**`nohup … &` ＋ `exit 0` という最も普通の形が通行証を貰えない**
      （6 巡目 must_fix ④・実測）。

    条件（`procutil.bg_group_matches` の ①〜④）:
      ① 控えの `members` に載っている pid が、いまも**同じ pgid の**構成員である。
      ② その命令（cmdline の先頭 60 字）が控えと一致する（同じ番号の別人を弾く）。
      ③ **控えを書いた時刻より後に生まれていない**（生まれていれば PID の使い回し。
         古い控え＝この係が塞いだ「8/6 の記録で他人を撃つ」形はここで落ちる）。
    ⚠ これは「自分の物だ」と言える証拠であって、**撃ってよいという許可ではない**
      （作業場の判定は呼ぶ側が続けてやる）。
    """
    return procutil.bg_group_matches(rec if rec is not None
                                     else _as_bg_record(int(meta.get("pid") or 0),
                                                        meta, meta_p),
                                     live=members)


def _escaped_note(pgid: int, port: int | None, *, since_ticks: int | None = None) -> str | None:
    """「グループは空になったが**ポートはまだ待受中**」なら、その事実を返す。

    ★これが `OK:` を名乗らせないための唯一の関門（2026-09-05・第4回c）。
      `_stop_group` の猶予ループを抜けた先で、`killpg(SIGKILL)` が
      `ProcessLookupError`（＝グループはもう空）になったことを
      **「止まった」と読み替えていた**のが false green の機構だった。
      グループが空であることは、ポートが空いたことの証拠ではない
      （`setsid` / `disown` / `nohup` 付きサブシェルで**別グループへ出た**サーバは
      グループを空にしたまま生き続ける）。ポートを渡されているなら見る。

    ★★2026-09-05・第4回c の**2 巡目**（反証が実物で再現した must_fix ②⑤）:

      ② **原因を断言していた。** 旧実装は待受が残っていれば無条件に
         「グループの外へ出た起動（setsid/disown/nohup 付きサブシェル）**です**」
         と言い切り、そのうえ `BashOutput(port=P, kill=true)` を勧めていた。
         この道具が手にしているのは **pgid とポート番号だけ**で、そのポートを
         握っているプロセスが「いま止めた相手の残骸」だという証拠はどこにも無い。
         実測（`scratchpad/r4c/expH.py`）: 自分の背景処理（ポートを握っていない
         ただの sleeper・控えの cmd だけが `php -S 0.0.0.0:P`）を止めたら、
         **たまたま同じポートに居た無関係の第三者**について「逃げた起動です」と
         断言し、勧めたとおり `BashOutput(port=P, kill=true)` を実行すると
         **その第三者が死んだ**（`third.poll() = -15`）。再 bind の競合
         （`expB` B3・止めた直後に別プロセスが同じポートを取り直す）も同じ形。
         → **原因を言い切らない。撃つ助言は `_escaped_note` からは出さない**
            （`BashOutput(port=P)` を出す。あちらは持ち主と cwd を並べたうえで、
            作業場の中の物が居るときだけ `kill=true` を案内する＝**見てから撃つ**
            順序になる）。起動時刻で「別人」と**証明できたときだけ**言い切る。

      ⑤ **自分自身を「別ユーザ」と言っていた。** 自分と親を `owners` から除く
         のに `port_in_use` からは除いていないので、エージェント自身が掴んで
         いるポートで「持ち主は /proc から見えません＝別ユーザの可能性」という
         **嘘**が出る形だった。自分が掴んでいるなら、そう言う。

    `since_ticks`: この停止を始めた時点の `_uptime_ticks()`。渡されていて、かつ
    待ち受けている**全員**がそれより後に生まれていれば、止めた相手の残骸では
    ありえない（その時点では存在していない）＝そこだけは言い切ってよい。
    戻り: 待受が残っているときの本文（そうでなければ None＝呼び出し側の判断に戻す）。
    """
    if port is None:
        return None
    try:
        if not procutil.port_in_use(port):
            return None
    except Exception:
        return None                       # 見えない＝断定しない（従来の返しに戻す）
    me, parent = os.getpid(), os.getppid()
    try:
        my_pgid = os.getpgid(0)
    except OSError:
        my_pgid = None
    try:
        rows = procutil.port_listeners(port)
    except Exception:
        rows = []
    self_hit, others = [], []
    for r in rows:
        try:
            rpid = int(r.get("pid") or 0)
        except (TypeError, ValueError):
            rpid = 0
        if rpid in (me, parent) or (my_pgid is not None and r.get("pgid") == my_pgid):
            self_hit.append(r)            # ★⑤ 自分。別ユーザ扱いしない。
        else:
            others.append(r)

    def _who(rows_) -> str:
        try:
            return "; ".join(procutil.describe(r, 60) for r in rows_[:3])
        except Exception:
            return ", ".join(str(r.get("pid")) for r in rows_[:3])

    head = (f"Error: グループ {pgid} の構成員は居なくなりましたが、"
            f"**ポート {port} はまだ使われています**")
    # ★二次（union-2・2026-09-06）: 保護ポートかどうかは`_port_report`側の
    #   別の門でしか言っていなかった。ここで待ち受けているのが保護ポートなら、
    #   その事実を本文から消さない（次の一手を誤らせないため）。
    if port in procutil.protected_ports():
        head += f"（★ポート {port} は保護された設備です）"
    look = f"持ち主を確かめる: BashOutput(port={port})"

    if not others and self_hit:           # ⑤
        return (head + f"（掴んでいるのは**このエージェント自身**です: {_who(self_hit)}）。"
                "止めた処理の残骸ではないので、撃つ助言はしません。")
    if not others:
        return (head + "（持ち主は /proc から見えません＝別ユーザの可能性）。"
                "止めた処理の残骸かどうかは**確かめられていません**。" + look)

    # ② 起動時刻で「別人」と**証明できるときだけ**言い切る。
    unproven = [r for r in others if _started_after(r.get("pid"), since_ticks) is not True]
    if not unproven:
        return (head + f"（{_who(others)}）。ただし待ち受けているのは"
                "**この停止を始めたあとに起動したプロセス**です"
                "＝止めた処理の残骸ではありません（同じポートを取り直した別の起動）。"
                "撃つ助言はしません。" + look)
    return (head + f"（{_who(others)}）。"
            "**これが止めた処理の残骸（setsid/disown/nohup でグループの外へ出た起動）"
            "なのか、同じポートを使う別のプロセスなのかは、この道具では区別できません**"
            "（控えにあるのはポート番号だけで、待受を握っている pid の控えが無いため）。"
            + look + " ——持ち主と cwd を見てから止めること。")


def _blind_note(pgid: int, reasons: list, observed, *, acted: str) -> str:
    """**観測そのものが出来なかった**ときに、止まったと言わない（6 巡目の続き）。

    ★この道具が「止まった」と言える根拠は 1 つしかない——**撃つ前に実際の LISTEN を
      観測し、撃ったあとに読み直して、居ないことを見た**。その観測が出来ていないなら、
      何も見つからないのは当たり前で、それは「残っていない」の証拠にならない。
      2 巡目〜5 巡目で潰してきた false green は全部この読み替えの別の顔だった
      （グループが空＝止まった／推測ポートが空＝止まった／…）。ここは
      **観測の失敗**という最後の顔。
    ★`Error:` で始める: 機械が失敗を読む根拠は `tools/__init__.py:tool_failed` の
      `Error:` 前置きで、「`OK:` で始めない」では**成功として記録される**（4 巡目 M4）。
    """
    _bump("bg_blind_refused")
    uniq = list(dict.fromkeys(str(r) for r in reasons))[:3]
    seen = (f"撃つ前に観測したポート {', '.join(str(p) for p in sorted(observed))} は"
            if observed else "")
    return (f"Error: {acted}。**止まったかどうかは確かめられていません**"
            f"（グループ {pgid} の観測そのものが失敗しています）。\n"
            + "".join(f"  ・{r}\n" for r in uniq)
            + (f"  {seen}確かめられていません。\n" if seen else "")
            + "  何も見つからなかったことを『残っていない』とは読めません"
            "（観測できていないので当然何も見つかりません）。\n"
            "  確かめる: BashOutput(port=<番号>) で持ち主を出す／"
            "`ss -ltnp` で待受を直接見る。")


def _unproven_note(snap: dict | None) -> str:
    """**物差しが読めないまま落とさずに残した**候補を、成功の本文にも書く。

    ★2026-09-05・10 巡目（再反証 MF-B）。9 巡目までこの印は `_survivor_note` の
      中にしか無かった＝**生き残りが見つかって赤くなったときだけ**出ていた。
      印が要るのは逆で、`kept_unproven` を抱えたまま `OK:` と言う経路のほう
      （実測: `no_ruler=True` / `kept_unproven=2`、counters は
      `bg_launch_ticks_missing=1` / `bg_kin_kept_unproven=2` と正しく立っているのに、
      利用者とモデルが読む文字列は `OK: SIGTERM で止まりました（グループ N）。` だけ）。
      **counters に出ていて本文に出ていないものは、読む側には無いのと同じ。**
    ★言い切る強さを下げるだけで、`OK:`／`already exited` そのものは変えない
      （落とした候補が無いのに赤くすると、正しく止まった停止まで赤くなる）。
    """
    if not snap:
        return ""
    kept = int(snap.get("kept_unproven") or 0)
    no_ruler = bool(snap.get("no_ruler"))
    if not no_ruler and not kept:
        return ""
    if no_ruler:
        note = ("\n  ⚠ この控えには起動時刻（start_ticks）がありません"
                "（打ち切りで背景に回された控え）。"
                "**この起動より前から居たかを証明できません。**")
    else:
        note = ("\n  ⚠ 候補の起動時刻を読めませんでした。"
                "**この起動より前から居たかを証明できません。**")
    if kept:
        note += (f"証明できない候補を {kept} 本落とさずに残しましたが、"
                 "撃ったあとの読み直しでは**待ち受けは見つかりませんでした**"
                 "（見つからなかっただけで、遅れて bind する相手は取り逃しえます）。")
    else:
        note += "作業場の中に、この起動の残骸かもしれない候補は 1 本もありませんでした。"
    _bump("bg_unproven_note")             # 0 なら本文への配線が死んでいる
    return note


def _ok_text(pgid: int, observed, how: str, snap: dict | None = None) -> str:
    """止まったと言い切ってよいときの本文。**言い切る根拠を本文に書く。**

    ★`snap`（2026-09-05・10 巡目・再反証 MF-B）: 物差しが無いまま候補を残して
      いたなら、`OK:` の本文にもそう書く（`_unproven_note`）。
    """
    if observed:
        got = ", ".join(str(p) for p in sorted(observed))
        return (f"OK: {how}（グループ {pgid}）。"
                f"撃つ前に観測した待受ポート {got} は空きました。"
                + _unproven_note(snap))
    return f"OK: {how}（グループ {pgid}）。" + _unproven_note(snap)


def _after_kill(pgid: int, snap: dict, observed, *, since_ticks, how: _How,
                hint_port=None, clean_text: str | None = None) -> str:
    """撃ったあとの言い分を、**観測した事実だけ**から組み立てる（3 巡目の要）。

    順に見る:
      ① 撃つ前に観測した LISTEN ポートが**まだ使われている** → `_escaped_note`
         （残骸か第三者かは断定しない・撃つ助言は出さない）。
      ② 控えた逃げ子が**いま待ち受けている** → `_survivor_note`。まだ待ち受けて
         いない逃げ子が生きているなら、窓を切って待つ（`_await_late_bind`）。
      ③ どちらも無い → 止まったと言ってよい。
         ★**観測できなかったときに待受の話をしない**（再反証 M6）。2 巡目は
           「この控えからはポートが読めないので確かめていません」を成功停止にも
           非サーバ（`bash build.sh`）にも毎回出していた——読めないのは控えの話で、
           いまは控えではなく `/proc` を見ているので、見て何も無ければ黙る。
         ⚠ 残る穴（**5 巡目 must_fix ④で書き直した**・`exp_dvar` で 3 形を実測）:
           不変条件は「**子が自分で `cd` したか**」ではない。4 巡目はそう書いて
           「(d) は閉じた」と言ったが、実物で掃くと**言えるのはもっと弱いこと**だった。
           見えないのは次の 2 つが**撃つ時点で同時に成り立つ**とき:
             ・親を辿ってこのグループに着かない（`ppid` が 1＝起こした親が先に死んだ）
             ・cwd が作業場の外
           誰が `cd` したかは関係ない。実測（どれも `port_in_use=True` のまま
           `OK: SIGTERM で止まりました`＝**いまも false green**）:
             d2 `setsid sh -c 'cd /tmp && exec <server>' &`   … 子が cd
             d5 `cd /tmp` を独立行にしてから `setsid <server> &` … **親が cd**
             d6 `cd /tmp; nohup setsid <server> &`             … **親が cd**＋nohup
           4 巡目に閉じたと言えるのは (d) 全体ではなく **d1 `cd /tmp && setsid …&` だけ**で、
           しかも理由は「親が cd したから」ではなく——`&&` がサブシェルを作り、
           そのサブシェルがグループに残って**親を辿れる**（`descendant`）から。
           `;` や改行で書くとサブシェルは生まれず、同じ `cd` でも見えなくなる。
           根治は控えに**実 LISTEN か子の pid** を書くこと＝`bash.py` 側の仕事。
           試験は d2/d5/d6 の 3 本を `xfail(strict=True)` で残してある
           （直ったら試験が知らせる）。
    """
    # ★`setdefault` で取る（8 巡目）。`snap.get()` だと `blind` が無い snap で
    #   **別の list** を掴み、`_survivors` が積んだ盲がこの関数に届かなかった。
    blind: list = snap.setdefault("blind", [])
    busy = _busy_ports(observed, blind)
    if busy:
        note = _escaped_note(pgid, busy[0], since_ticks=since_ticks)
        if note:
            if len(busy) > 1:
                note += ("\n  （撃つ前に観測した他のポートも残っています: "
                         + ", ".join(str(p) for p in busy[1:]) + "）")
            return note + _hint_note(hint_port, observed)
    surv = _survivors_now(snap, hint_port=hint_port)
    if surv:
        return _survivor_note(pgid, surv, how=how.group, hint_port=hint_port,
                              observed=observed, kin_total=snap.get("kin_total"),
                              snap=snap)
    # ★観測が盲だったなら、ここで見つからなかったことは何の証拠にもならない
    #   （6 巡目の続き・`probe_blind.py` で `OK:`＋サーバ生存を実測した形）。
    if blind:
        # ★助詞を重ねない（8 巡目）。`how.group` は「構成員は…」「撃つ前から…」で
        #   始まるので `は` を前置きすると「グループ N は構成員は…」になる。
        return _blind_note(pgid, blind, observed,
                           acted=f"グループ {pgid}: {how.group}")
    # ★`clean_text`（`pid N already exited`）も**機械は成功と読む**ので、
    #   同じ印を付ける（2026-09-05・10 巡目・再反証 MF-B）。
    if clean_text:
        return clean_text + _unproven_note(snap)
    return _ok_text(pgid, observed, how.ok, snap)


def _protected_group_pids(pgid: int) -> list[int]:
    """グループ `pgid` の中に居る、**保護ポートの持ち主（とその仲間）**の pid。

    ★塞いだ穴（2026-09-05・第4回c の再反証・実物で再現）: 保護ポートの門は
      `_stop_group` の**ポート番号**にしか無かった。番号の出どころは
      ・`_port_report` → 利用者が指定したポート（`port=`）
      ・`_run` の pid 経路 → `_meta_port(meta)` ＝**台帳の `cmd` から正規表現で拾った数**
      の 2 通りで、後者は綴りに番号が出ていなければ `None` になる。
      実測: `cmd="./run-server.sh"`（中で保護ポート 19911 を listen）を
      `BashOutput(pid=…, kill=true)` で撃つと番号が取れず ＝ **保護判定が一度も
      走らないまま**「OK: 止まりました」で自分の設備が死んだ。
      → **番号ではなくプロセスで**引く（`procutil.protected_port_pids()` は
        ポートの持ち主とそのプロセスグループの仲間を返す）。綴りに依らない。
    ⚠ 統合後（第4回c 3 巡目と合流・2026-09-05）は**穴がもう一段深い**: `_run` は
      `_meta_port(meta)` を `port=` ではなく **`hint_port=`（表示だけ・証拠にしない）**
      で渡すようになった（再反証 M1: 推測値は `run-13:45:07.log` → 45 のような
      偽のポートを掴み、それが「確かめた」の顔をしていた）。つまり pid 経路には
      **番号による保護判定がそもそも到達しない**——この関数はその経路を埋める。

    ★★union-2（2026-09-06・must_fix B-3）: 上の「唯一の仕組み・消さないこと」は
      統合後は**正しくない**。実際には**第 1 の門が観測で先に返る**——`_run` は
      この関数を呼ぶ**前**に `observed & procutil.protected_ports()`
      （撃つ前に実際に LISTEN しているポートの観測）で保護ポートを弾いており、
      待受が実際に上がっている普通の形はそこで止まる。この関数が要るのは
      **観測が届かない**ときの受け皿（LISTEN 前の遅延 bind・観測の失敗など）。
      さらに**両門とも同じ `/proc/<pid>/fd` を読む**ので、他ユーザのプロセス
      （fd が読めない）では**どちらも盲**になる——「唯一」どころか、
      権限が無いケースでは保護そのものが働かない。正直にそう書く。
    ⚠ ただし**グループの外へ出た持ち主には及ばない**（`setsid`／別 pgid で起動した設備は、この pgid 検査では拾えない）。
    """
    try:
        prot = procutil.protected_port_pids()
    except Exception:
        return []
    if not prot:
        return []
    cand = {pgid}
    for m in _group_members(pgid):
        q = m.get("pid")
        if isinstance(q, int):
            cand.add(q)
    return sorted(cand & prot)


def _stop_group(pgid: int, *, port: int | None = None, hint_port: int | None = None,
                scope: str | None = None, since_epoch: float | None = None,
                since_ticks: int | None = None, grace: float = 3.0) -> str:
    """SIGTERM → 空かなければ SIGKILL。**「送った」ではなく「止まった」まで見る。**

    引数の役割（2026-09-05・第4回c 3 巡目で分けた）:
      `port`      … **すでに観測で確かめてあるポート**（`_port_report` は
                    `procutil.port_listeners` で持ち主を引き当ててから呼ぶ）。証拠。
      `hint_port` … 控えの命令から**正規表現で推測**した数。表示だけ。**証拠にしない**
                    （再反証 M1: 偽ポートで `port_in_use` が撃つ前後とも False になり、
                    偽の証拠で `OK: 止まりました` が出ていた）。
      `scope`/`since_epoch` … 逃げた候補（身内）を拾う範囲。`_launch_scope` / `_launch_epoch`。

    保護ポートの門は**二段**（2026-09-05・統合）: ①観測値（`port` と実 LISTEN）と
      `procutil.protected_ports()` の突き合わせ ②`_protected_group_pids`＝
      **ポートの持ち主の pid** で引く第 3 の門。②は `hint_port` しか無い経路
      （台帳の綴りに番号が出ない `./run-server.sh` など）を守るので、
      ①だけにしないこと。

    ★この現場の実測（`phases.py` の `_FALSE_GREEN_KINDS` "bind"）: 止めたつもりで
      残ったプロセスがポートを握り、次の起動が `Address already in use` で上がらない。
      文言だけ正しくて実態が違う形をここで潰す（送りっぱなしにしない）。
    ★生死は `/proc` を読み直して見る（`killpg(pgid, 0)` の 1 発で済ませる案を書いて
      戻した——`os.killpg` を差し替えて呼び出しを数える既存の試験
      `test_posixのkillは従来どおりkillpgを使う` が、その**確認用の呼び出しまで
      「送った信号」として数えてしまう**ため。試験を弱めるより読み直すほうがよい。
      費用も問題にならない: 普通は 1〜2 周で抜ける（居残るときだけ最大 3 秒）。
    """
    import time as _time
    # ── 撃つ前の床（2026-09-05・第4回b。どれも実測に基づく）─────────────────
    try:
        pgid = int(pgid)
    except (TypeError, ValueError):
        return f"Error: グループ番号が読めません: {pgid!r}"
    if pgid <= 1:
        # ★`cwd="/"` を渡すと :22 の持ち主（pid 1 / pgid 1 の systemd）が
        #   「作業場の中」に分類され、`killpg(1, …)` の経路に入っていた。
        #   カーネルが init への信号を落とすので実害は出ていないが、床が無かった。
        return (f"Error: グループ {pgid} は撃ちません"
                "（pgid<=1 は init／不定のグループです）。")
    # ★床を 1 本に乗せない（2026-09-05・7 巡目）。ここは「撃つと**走行ごと消える**」
    #   唯一の防波堤なのに、`procutil.ancestor_pgids` が壊れると
    #   `except Exception: pass` で**黙って消えて**いた（union の改名事故と同型）。
    #   ① procutil に依らない直の照合（自分のグループ）
    try:
        _self_pgid = os.getpgid(0)
    except OSError:
        _self_pgid = None
    if _self_pgid is not None and pgid == _self_pgid:
        return (f"Error: グループ {pgid} は**このエージェント自身**のグループです。"
                "撃つと走行ごと消えるので止めません。")
    #   ② 祖先・セッションまで見る（procutil）。**壊れたら撃たない**
    try:
        if pgid in procutil.ancestor_pgids():
            return (f"Error: グループ {pgid} は**このエージェント自身か、その祖先**です。"
                    "撃つと走行ごと消えるので止めません。")
    except Exception as e:
        _bump("bg_self_floor_broken")
        return (f"Error: グループ {pgid} が**自分や祖先でないこと**を確かめられません"
                f"（ancestor_pgids が失敗: {type(e).__name__}: {str(e)[:120]}）。"
                "**撃っていません。**\n"
                "  ここを黙って通すと、自分のグループを撃って走行ごと消えます。"
                "  これは道具の欠陥です（環境の問題ではありません）。")
    # ── ★撃つ前に、実際に待ち受けているものを観測して控える（3 巡目の本体）──
    snap = _observe_listen(pgid, scope=scope, since_epoch=since_epoch,
                           since_ticks=since_ticks)
    observed: set[int] = set(snap["ports"])
    if port is not None:
        try:
            observed.add(int(port))       # 呼び出し側が観測済みで渡した値だけ足す
        except (TypeError, ValueError):
            pass
    # ★保護ポートの床には**観測値だけ**を食わせる（再反証 M5）。旧実装は推測値でも
    #   ここに落ちたので、`-p 22 deploy` のような命令を背景で走らせると
    #   （sshd でも何でもないのに）**永久に止められない**退行が出ていた。
    protected = sorted(observed & procutil.protected_ports())
    if protected:
        return (f"Error: ポート {protected[0]} は保護された設備です"
                f"（既定: {sorted(procutil.protected_ports())}）。"
                "撃ちません。本当に必要なら LAPS_PROTECTED_PORTS で外してください。")
    # ★**第 3 の門**（2026-09-05・第4回c の再反証）: `port` が分からない経路
    #   （台帳の cmd に番号が無い＝`./run-server.sh` 等）でも、構成員が保護ポートを
    #   握っていれば撃たない。番号ではなく**プロセス**で引くので綴りに依らない。
    _hit = _protected_group_pids(pgid)
    if _hit:
        return (f"Error: グループ {pgid} には**保護ポート "
                + "/".join(str(x) for x in sorted(procutil.protected_ports()))
                + " の設備**が居ます（pid "
                + ", ".join(str(x) for x in _hit[:5])
                + "）。撃ちません。\n"
                "  ★命令の綴りにポート番号が出ていなくても、ポートの持ち主で判定します"
                "（sshd／閲覧用の中継／Android ゲートウェイ）。\n"
                "  本当に必要なら LAPS_PROTECTED_PORTS で外してください。")
    # ★観測の失敗はすべて `snap["blind"]` の**同じ 1 本の list** に集める
    #   （6 巡目の続き）。`_after_kill` はこの list をそのまま読むので、
    #   猶予ループの中で起きた失敗も撃ったあとの判断に届く。
    blind: list = snap.setdefault("blind", [])
    if not _group_members(pgid, blind) and not _is_alive(pgid):
        if blind:
            # 数えられないだけかもしれない相手を「もう終わっている」と言わない。
            return _blind_note(pgid, blind, observed,
                               acted=f"グループ {pgid} は**撃っていません**")
        # ★「送ったら成功」をやめる（実測: `already exited` を返しながら
        #   `port_in_use` は True のままだった＝止めたつもりで残る形の製造元）。
        busy = _busy_ports(observed, blind)
        if busy:
            return (f"Error: グループ {pgid} に構成員が見えないので**撃っていません**が、"
                    f"ポート {busy[0]} はまだ使われています"
                    "（グループの外へ出た起動＝setsid/disown の残骸かもしれませんが、"
                    "**この道具では断定できません**）。"
                    f"BashOutput(port={busy[0]}) で持ち主を確かめ、"
                    "PID を名指しで `kill <pid>` してください。"
                    + _hint_note(hint_port, observed))
        surv = _survivors_now(snap, hint_port=hint_port)
        if surv:
            return _survivor_note(pgid, surv, how="構成員が見えないので撃っていません",
                                  hint_port=hint_port, observed=observed,
                                  kin_total=snap.get("kin_total"), snap=snap)
        if blind:                     # 観測が盲なら「見えない＝居ない」と言わない
            return _blind_note(pgid, blind, observed,
                               acted=f"グループ {pgid} は**撃っていません**")
        # ★待受を 1 つも観測していないなら、待受の話をしない（再反証 M6）。
        #   ただし物差しが無いまま候補を残していた事実は書く（10 巡目・MF-B）。
        return (f"グループ {pgid} に構成員が見えません（もう終わっているようです）。"
                "撃っていません。" + _unproven_note(snap))
    _since = _uptime_ticks()              # ★この停止を始めた時点（②の物差し）
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        # ★再反証 M3: ここは port を渡されていても見ずに `already exited` と
        #   言っていた（グループが空 ≠ 待受が空いた）。同じ観測を経由させる。
        return _after_kill(pgid, snap, observed, since_ticks=_since,
                           how=_H_GONE,
                           hint_port=hint_port,
                           clean_text=f"pid {pgid} already exited")
    except Exception as e:
        return f"Error killing pid {pgid}: {e}"
    deadline = _time.time() + max(0.0, grace)
    while _time.time() < deadline:
        _time.sleep(0.1)
        if _group_members(pgid, blind):
            continue
        if _busy_ports(observed, blind):
            continue                      # グループは空でも待受が残っている
        return _after_kill(pgid, snap, observed, since_ticks=_since,
                           how=_H_TERM, hint_port=hint_port)
    # ★猶予を抜けた時点で、**判断材料はもう手の中にある**（2026-09-05・第4回c）。
    #   ここから下の return は「グループが空になった」ことだけを見て `OK:` を
    #   返していた——グループの外へ出た起動（setsid/disown）ではラッパのシェルだけが
    #   グループに残っており、それを撃つと空になるので、3 秒かけて待ったあげく
    #   `killpg(SIGKILL)` が `ProcessLookupError` になり、**サーバは生きたまま
    #   「OK: SIGTERM で止まりました」**を返していた（実測 3/3）。
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return _after_kill(pgid, snap, observed, since_ticks=_since,
                           how=_H_SENT, hint_port=hint_port)
    except Exception as e:
        # ★再反証 M4: ここは `OK:` を名乗っていた。失敗した kill は `Error:` で
        #   始める（機械が失敗を読む根拠は `tools/__init__.py:tool_failed` の
        #   **`Error:` 前置き**で、「`OK:` で始めない」では成功として記録される）。
        return (f"Error: SIGTERM をグループ {pgid} へ送りましたが、まだ残っています"
                f"（SIGKILL も試しましたが失敗: {e}）")
    _time.sleep(0.3)
    left = _group_members(pgid, blind)
    if left:
        return (f"Error: グループ {pgid} は SIGKILL のあとも {len(left)} 本残っています: "
                + ", ".join(procutil.describe(m, 60) for m in left[:4]))
    # SIGKILL が通った側も同じ（グループが空 ≠ ポートが空いた）。**撃ったあとに**
    # 読み直す（SIGKILL で本当に落ちた相手を「まだ居る」と言わないため）。
    return _after_kill(pgid, snap, observed, since_ticks=_since,
                       how=_H_KILL, hint_port=hint_port)


def _port_report(inp: dict) -> str:
    """**ポートの持ち主**を /proc から引き、作業場の中の物だけ止める。

    ★なぜ道具にするか（run50-52 の実測）: Bash 902 件のうち
      `ps aux|grep` / `lsof` / `ss` / `pgrep` によるポート持ち主探しが **30 件**、
      `kill`/`pkill`/`killall` が **29 件**。モデルは毎回 2〜4 手を組み立て直しており、
      しかも `pkill -f <語>` は自分の argv に当たって**自分を撃つ**
      （bash.py の `_spawn_args` の注記・実機 9/9）。1 呼び出しに畳む。
    ★安全規律は `bash.py:_scope_name_kill` と**同じ**（cwd が作業場配下か）。
      これを省くと、名前ではなくポート経由で他人のサービスを撃つ事故が再現する
      （2026-09-02 に corridor-bridge を 6 回 SIGKILL している）。
    """
    try:
        port = int(inp.get("port"))
    except (TypeError, ValueError):
        return f"Error: port は整数で指定してください（受け取った値: {inp.get('port')!r}）"
    if not (0 < port < 65536):
        return f"Error: port の範囲が不正です: {port}"
    if sys.platform == "win32":
        return "Error: port によるプロセス特定は POSIX（/proc）でだけ使えます"
    in_use = procutil.port_in_use(port)
    owners = procutil.port_listeners(port)
    # ★作業場は**正典から**取り、道具引数の cwd は狭める方向にしか効かせない
    #   （2026-09-05・第4回b の反証。実測: 同じ作業場外のリスナが、cwd 省略なら
    #   「何も撃っていません」で生存し、`cwd="/"` を渡すと `[作業場の中]` に
    #   分類されて SIGTERM で死んだ＝**撃つ範囲をモデルが決められる**状態だった）。
    ws, ws_note = procutil.narrow_workspace(inp.get("cwd"))
    try:
        # ★台帳は `LAPS_HOME` を共有する同居セッション全部の物なので、
        #   **いまの作業場のものだけ**通す（2026-09-05・第4回c）。
        bg_pids = procutil.owned_bg_pids(ws)
    except Exception:
        bg_pids = set()
    me, parent = os.getpid(), os.getppid()
    try:
        my_pgid = os.getpgid(0)
    except OSError:
        my_pgid = None
    mine, theirs, self_hit, refs = [], [], [], []
    for r in owners:
        if r["pid"] in (me, parent) or (my_pgid is not None and r.get("pgid") == my_pgid):
            self_hit.append(r)          # ★エージェント自身。絶対に撃たない。
        elif procutil.in_workspace(r, ws, bg_pids=bg_pids):
            mine.append(r)
        else:
            theirs.append(r)
            if procutil.references_workspace(r, ws):
                refs.append(r)
    if not in_use:
        return f"ポート {port} は誰も待ち受けていません（空いています）。"
    lines = [f"ポート {port} を待ち受けています。"]
    if ws_note:
        lines.append("  " + ws_note)
    for label, rows in (("作業場の中", mine), ("作業場の外", theirs),
                        ("このエージェント自身", self_hit)):
        for r in rows:
            lines.append(f"  [{label}] {procutil.describe(r)}"
                         + (f"\n      cwd: {r['cwd']}" if r.get("cwd") else ""))
    if refs:
        lines.append("  ★作業場の外のうち "
                     + ", ".join(f"pid {r['pid']}" for r in refs[:5])
                     + " は作業場のパスを参照していますが、cwd は外です"
                       "（自分の物と決めつけません）。")
    if not owners:
        lines.append("  持ち主の PID は見えません（他ユーザのプロセスです）。"
                     "止めるにはその所有者の権限が要ります。")
    if port in procutil.protected_ports():
        lines.append(f"  ★ポート {port} は保護された設備なので、この道具では止めません"
                     f"（既定: {sorted(procutil.protected_ports())}）。")
    if not inp.get("kill"):
        if mine and port not in procutil.protected_ports():
            lines.append(f"  → 止めるなら BashOutput(port={port}, kill=true)"
                         "（作業場の中の物だけ止めます）。")
        return "\n".join(lines)
    # kill=true
    # ★must_fix B-4（union-2・2026-09-06）: この道具の 3 つの拒否（保護・
    #   作業場に無い・政策）は本文の途中に埋もれ、`_port_report` の返り値**全体**
    #   は常に `f"ポート {port} を待ち受けています。"` から始まっていた。
    #   `tools/__init__.py:tool_failed` は `Error:` **前置き**か決まった英語の
    #   文字列（`_FAILED_BODY`）しか見ないので、拒否した呼び出しが**成功と
    #   記録される**（他の道具は 3〜8 巡目でこの規律を通した。ここが最後に
    #   残っていた）。拒否したときは `Error:` を**先頭**に出し、観測した一覧
    #   （ポートの持ち主など）は本文として後ろに続ける。
    if not mine:
        # ★「PID で撃ち直せ」とは案内しない（2026-09-05・第4回b の統合）。
        #   明示 PID の kill も `bash._scope_pid_kill` が同じ基準で断るので、
        #   ここでそれを勧めると**両ゲートが同時に断つ経路**をモデルに歩かせる
        #   （反証①の must_fix 2・run53 の実記録で断りが 2 回出ている）。
        return ("Error: 作業場の中のプロセスが居ないので、**何も撃っていません**。"
               "PID を名指ししても同じ基準で断ります"
               "（自分の物のはずなら作業場の中で起動し直す／"
               "別の持ち主なら別のポートで立てる／"
               "どうしても必要なら AskUserQuestion で確認してください）。\n"
               + "\n".join(lines))
    # ★政策ゲートは**あらゆる dispatch path の前**（bash.py の宣言どおり）。
    denied = _authorize_kill(f"kill -TERM {' '.join(str(r['pid']) for r in mine)}", ws)
    if denied:
        return (f"Error: {denied}（何も撃っていません）\n" + "\n".join(lines))
    seen: set[int] = set()
    stopped: list[str] = []
    any_denied = False
    for r in mine:
        pg = r.get("pgid") or r["pid"]
        if pg in seen:
            continue
        seen.add(pg)
        note = _stop_group(pg, port=port)
        if note.startswith("Error:"):
            any_denied = True
        stopped.append("  " + note)
    lines.extend(stopped)
    if theirs:
        lines.append(f"  ★作業場の外の {len(theirs)} 本には触っていません。")
    lines.append(f"  ポート {port} は今: "
                 + ("まだ使われています" if procutil.port_in_use(port) else "空きました"))
    body = "\n".join(lines)
    if any_denied:
        # ★構成員が複数（同じポートに複数の持ち主）だと一部だけ止まることが
        #   ありうる。その場合も「全部止まった」と読ませない——保護/観測不能で
        #   撃てなかった行が 1 本でもあれば `Error:` を前に出す。
        return "Error: 一部（または全部）を止められませんでした（詳細は下記）。\n" + body
    return body


def _recent_bg_hint(d) -> str:
    """この作業場で最近起こした背景の命令（新しい順に 5 つ）の一行。無ければ使い方。"""
    try:
        ws = procutil.canonical_workspace()
        rows = []
        for mp in sorted(d.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:40]:
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            mws = meta.get("workspace")
            if mws and ws and mws != ws:
                continue
            p_ = mp.name.split(".", 1)[0]
            try:
                state = " running" if _is_alive(int(p_)) else " finished"
            except (TypeError, ValueError):
                state = ""
            rows.append(f"pid {p_}{state}: `{str(meta.get('cmd') or '')[:60]}`")
            if len(rows) >= 5:
                break
        if rows:
            return " Recent background commands in this folder: " + "; ".join(rows) + ". Use one of those pids."
    except Exception:
        pass
    return (" Start a long command with Bash(..., run_in_background=true): its reply gives the pid to use here. "
            "Jobs the harness moved to the background are named J1, J2, ...")


def _run_body(inp: dict) -> str:
    if inp.get("port") is not None and not inp.get("pid") and not inp.get("bash_id"):
        return _port_report(inp)
    pid_raw = inp.get("pid") or inp.get("bash_id")
    if pid_raw is None:
        return "Error: missing 'pid' (または port)"
    try:
        pid = int(pid_raw)
    except (TypeError, ValueError):
        return f"Error: invalid pid: {pid_raw!r}"

    d = _bg_dir()
    out_p = d / f"{pid}.out"
    err_p = d / f"{pid}.err"
    meta_p = d / f"{pid}.meta.json"
    if not meta_p.exists():
        # ★2026-10-23 オーナー「Blackwallの改善を進めてください」: 開発機 の記録で BashOutput(pid=0) が 3 回。旧は探したファイルの場所しか
        #   言わず、小さいモデルは正しい番号を選び直せなかった。この作業場で最近起こした背景の命令を並べる。
        return (f"Error: pid {pid} is not a background command started by Bash here."
                + _recent_bg_hint(d) + f" (Files looked for: {out_p}, {err_p})")

    try:
        # ★符号化を明示し、壊れた台帳で道具ごと落ちない（2026-09-02）。
        #   旧: `json.loads(meta_p.read_text())`。cmd に日本語が入っていれば
        #   UTF-8 でないと読めない機械（Windows の cp932）で必ず失敗し、
        #   書き込み途中の meta を読めば JSONDecodeError で例外が出る。
        #   どちらも「Error in BashOutput: …」だけが利用者に届き、
        #   **背景処理の出力を見る手段そのものが失われる**（出力は読めるのに）。
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, ValueError, UnicodeDecodeError) as e:
        # 台帳が読めなくても **出力は読める**。台帳のせいで出力まで諦めない。
        meta = {"cmd": f"(meta を読めません: {e})"}
    # ★台帳の記録を `procutil` と同じ形にしておく（照合に使う）。
    _rec = dict(meta)
    _rec["pid"] = pid
    if "mtime" not in _rec:
        try:
            _rec["mtime"] = meta_p.stat().st_mtime
        except OSError:
            _rec["mtime"] = 0.0
    # ★グループでも生死を見る（2026-09-05）。背景に回った起動では**シェルが先に死ぬ**
    #   ので、`_is_alive(pid)`（＝グループの長を見る）だけでは
    #   「動いているサーバを、動いていないと報告する」形になる（実測で再現）。
    _pgid = meta.get("pgid") or pid
    #: ★数えられなかった理由（6 巡目の続き）。`[]` を「もう居ない」と読まないため。
    _m_err: list = []
    _members = _group_members(_pgid, _m_err)
    alive = _is_alive(pid) or bool(_members)
    kill = inp.get("kill")
    if kill:
        if alive:
            # ★**台帳の存在だけを通行証にしない**（2026-09-05・第4回c）。
            #   ここは `<pid>.meta.json` が在るかどうかしか見ていなかった。
            #   PID は使い回されるので、記録した子がとうに死んで番号が別人に
            #   回っていると、**その別人をグループごと撃つ**（反証が隔離した
            #   `LAPS_HOME` で実際に再現した）。`bash.py` 側の台帳判定は
            #   `procutil.bg_record_matches` で開始時刻を照合しているのに、
            #   この道具だけが写し忘れていた＝「同じ物を 2 つ持つと片方だけ直る」。
            #   ★その番号で**いま何かが動いている**ときだけ照合する。番号自体が
            #     空いていて、グループの仲間だけが残っている形（`setsid` した
            #     サーバがシェルより長生きする）は従来どおり止められる。
            # ★union-2（2026-09-06）: この照合には try/except が無く、
            #   `procutil.start_ticks` / `bg_record_matches` が壊れたら例外が
            #   `_ownership_note` の `_gate_broken`（下）を素通りして `_run` の
            #   外側の総受け（`Error: BashOutput が途中で落ちました`）まで飛んでいた
            #   ——`test_所有権の判定が壊れたら撃たない` が実測で捕まえた（この照合の
            #   1 本目・「台帳との照合」）。同じ規律をここにも掛ける。
            try:
                _reuse_ticks = procutil.start_ticks(pid)
                _reuse_matches = (_reuse_ticks is None
                                  or procutil.bg_record_matches(_rec))
            except Exception as e:
                return _gate_broken(pid, "台帳との照合", e)
            if _reuse_ticks is not None:
                # ★この門にも自分の計器を持たせる（union-2・2026-09-06）。
                #   `_ownership_note` の `bg_own_gate_checked` を使い回すと、
                #   ここで断って `_ownership_note` に届かなかった呼び出しまで
                #   「2 回チェックした」ことになり、`_ownership_note` 単体の
                #   発火回数が読めなくなる（別の門は別の計器で数える）。
                _bump("bg_reuse_gate_checked")
            if _reuse_ticks is not None and not _reuse_matches:
                _bump("bg_reuse_gate_denied")
                # ★文言は**両方の言い方を 1 本に統合**（2026-09-05）。同じ規律が
                #   `_ownership_note`（下）にもあり、そちらは「**別人**です」と
                #   言っていた。門を 2 つ残すのは意図（片方は台帳の作業場まで見る）
                #   だが、**同じ事実を 2 通りの言葉で言うのはやめる**——利用者にも
                #   試験にも「どちらが出るか」で意味が変わってしまう。
                return (f"Error: pid {pid} の台帳は在りますが、いまその番号で動いて"
                        "いるのは**台帳とは別人**（別のプロセス）です"
                        "（起動時刻が合いません＝PID の使い回し）。撃っていません。\n"
                        f"  記録: {str(meta.get('cmd', ''))[:80]}"
                        f"（started {meta.get('started_at', '?')}）\n"
                        f"  いま: {procutil.describe({'pid': pid, 'cmdline': _live_cmdline(pid)}, 80)}\n"
                        "  自分が起こした処理なら、その走行の pid は既に終わっています"
                        "（出力は `BashOutput(pid=…)` でまだ読めます）。")
            # ★**台帳は作業場つきの通行証**（2026-09-05・第4回c の再反証）。
            #   台帳は `LAPS_HOME`（既定 `~/.blackwall`）に置かれ、同居する
            #   全セッションで共有される。ここは `bash.py` 側（`owned_bg_pids(ws)`）と
            #   違って作業場を**一度も見ていなかった**ので、兄弟の作業場 B から
            #   `BashOutput(pid=<A が起こした pid>, kill=true)` を撃つと通った
            #   （実測。`_KILL_ADVICE` が断りの直後にこの経路を案内していたので、
            #   モデルはそこへ確実に誘導される）。`bash.py` と同じ基準に揃える。
            _ws_now = procutil.canonical_workspace()
            if not procutil.bg_record_workspace_ok(_rec, _ws_now):
                _old_ledger = not meta.get("workspace")
                return (f"Error: pid {pid} の台帳は**この作業場のものではありません**"
                        "（台帳は LAPS_HOME を共有する全セッションの物です）。"
                        "撃っていません。\n"
                        f"  記録の作業場: {meta.get('workspace') or '(記録なし＝古い台帳)'}\n"
                        f"  いまの作業場: {_ws_now}\n"
                        # ★must_fix B-5（union-2）: `_ws_now` が `NO_WORKSPACE`
                        #   （幽霊パス）だと理由が本文から分からない。
                        + procutil.workspace_note()
                        + "  自分が起こした処理なら、その走行の作業場で止めてください"
                        "（出力は `BashOutput(pid=…)` でこのまま読めます）。"
                        # ★二次（union-2・2026-09-06）: `workspace` 欄が無い**古い台帳**
                        #   は作業場を書き直しても pid 経路では永久に通行証にならない
                        #   （この記録自体に作業場が刻まれていないため）。この経路で
                        #   止まらないことを言うだけで終わらせず、他の経路を案内する。
                        + ("\n  ★この控えには作業場が控えられていません（古い台帳）。"
                           "止めたいなら `Bash(\"kill <pid>\")`（trusted のとき）か、"
                           "待ち受けているポートが分かるなら "
                           "`BashOutput(port=<番号>, kill=true)` を使ってください。"
                           if _old_ledger else ""))
            # ★政策ゲートは**どの経路より先**（2026-09-05・第4回b）。
            #   OS 分岐（taskkill / killpg）の**手前**に置く: 片方だけ通る門は
            #   門ではない。監査（policy.audit）にもここで残る。
            denied = _authorize_kill(f"kill -TERM {_pgid or pid}",
                                     str(meta.get("cwd") or "") or None)
            if denied:
                return denied
            # ★所有権の関門（2026-09-05・第4回c）。政策の次・OS 分岐の手前に置く。
            #   ⚠ ただし**いま実際に判定できるのは POSIX だけ**: win32 には
            #   `/proc` も `start_ticks` の材料も無いので `_ownership_note` は
            #   None を返して素通りする（＝Windows の taskkill 経路には
            #   PID 使い回しの関門がまだ無い。塞いだと書かないでおく）。
            not_mine = _ownership_note(pid, meta, meta_p, _members)
            if not_mine:
                return not_mine
            # ★Windows には `os.getpgid` が無い（2026-09-03）。
            #   旧実装は `os.killpg(os.getpgid(pid), SIGTERM)` 一本だけで、
            #   `bash.py` の `_spawn_background` は win32 でも背景起動できるのに
            #   （comspec を DETACHED_PROCESS で起動）、こちらの kill=true は
            #   Windows では **必ず** `AttributeError: module 'os' has no
            #   attribute 'getpgid'` になり、プロセスは生きたまま
            #   「Error killing pid …」とだけ返っていた——`_spawn_background` の
            #   停止案内が同じファイルで `taskkill /F /T /PID` を教えているのに、
            #   この道具自身はそれをしない、という片方だけ直した形。
            if sys.platform == "win32":
                try:
                    import subprocess
                    r = procrun.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                       capture_output=True, timeout=15)
                except Exception as e:
                    return f"Error killing pid {pid}: {e}"
                if r.returncode == 0:
                    return f"OK: taskkill /F /T を pid {pid} に送りました"
                from .read import decode_bytes
                detail = (decode_bytes(r.stdout or b"") + decode_bytes(r.stderr or b"")).strip()
                return (f"Error killing pid {pid}: taskkill exit={r.returncode}"
                        + (f" {detail[:300]}" if detail else ""))
            # ★グループ番号で撃つ（`os.getpgid(pid)` は**長が死んでいると失敗する**）。
            try:
                pg = int(_pgid)
            except (TypeError, ValueError):
                pg = pid
            # ★止める判断の証拠は**観測**から取る（2026-09-05・3 巡目・再反証 M1）。
            #   `port=` には推測を渡さない（`hint_port` は表示だけ）。
            return _stop_group(pg, hint_port=_meta_port(meta),
                               scope=_launch_scope(meta),
                               since_epoch=_launch_epoch(meta, meta_p),
                               since_ticks=_launch_ticks(meta))
        else:
            # ★「終わっている」と言う前に、**待ち受けが残っていないかを見る**
            #   （2026-09-05・第4回b の反証／3 巡目・再反証 M2）。グループの外へ
            #   出た起動（setsid/disown）ではシェルだけが死に、サーバは生きたまま
            #   ここが `already exited` という**成功の顔**を返していた——
            #   `_FALSE_GREEN_KINDS` の "bind" ループを作る本体。
            #   ⚠ 2 巡目はここも `_port_in_cmd`＝**正規表現の推測**で見ていたので、
            #     偽ポート（`…-13:45:07.log` → 45）では素通りし、逆に本物の
            #     `:5432` を掴んだときは**無関係の postgres**を「残骸」と呼ぶ
            #     偽の赤にもなっていた。ここも観測に替える。
            if _m_err:
                # ★構成員を**数えられなかった**だけで「もう終わっている」と
                #   言わない（6 巡目の続き・実測 `probe_blind.py` の `rows` 形は
                #   ここで `pid N already exited` を返し、`sleep` は生きていた）。
                return _blind_note(_pgid or pid, _m_err, (),
                                   acted=f"pid {pid} は**撃っていません**")
            _snap = _observe_listen(_pgid or pid, scope=_launch_scope(meta),
                                    since_epoch=_launch_epoch(meta, meta_p),
                                    since_ticks=_launch_ticks(meta))
            #   ★★5 巡目（must_fix ②）: ここも `_after_kill` と**同じ 1 本**
            #     （`_survivors_now`）を通す。4 巡目は読み直しが 1 回だけで、
            #     `setsid sh -c 'sleep 0.8; exec <server>' &` を起こした走行が
            #     `pid N already exited`（機械は成功と読む）を返し、その直後に
            #     サーバが上がってきた（`exp_g2`・遅延 0.8/1.5 秒とも false green）。
            _surv = _survivors_now(_snap, hint_port=_meta_port(meta))
            if _surv:
                return _survivor_note(pid, _surv, how="グループの構成員はもう居ません",
                                      hint_port=_meta_port(meta),
                                      kin_total=_snap.get("kin_total"), snap=_snap)
            if _snap.get("blind"):    # 観測が盲＝「見つからない」は証拠にならない
                return _blind_note(_pgid or pid, _snap["blind"], _snap.get("ports") or (),
                                   acted=f"pid {pid} は**撃っていません**")
            # ★待受を 1 つも観測していないなら、待受の話をしない（再反証 M6）。
            #   ただし物差しが無いまま候補を残していた事実は書く（10 巡目・MF-B）。
            return f"pid {pid} already exited" + _unproven_note(_snap)

    # ★数として読めない tail で落ちない（"4k" 等をモデルは渡す）
    try:
        tail_n = int(inp.get("tail", 4000) or 4000)
    except (TypeError, ValueError):
        return f"Error: tail はバイト数（整数）で指定してください（受け取った値: {inp.get('tail')!r}）"
    tail_n = max(1, min(tail_n, 1_000_000))

    def tail(p: Path) -> str:
        """末尾だけを読む。★**全部読まない**（2026-09-02）。

        旧: `p.read_bytes()` でファイル全体をメモリへ載せてから末尾を切っていた。
        背景処理は「長く走って大量に吐く」ものが本命（ビルド・学習・転送）で、
        数 GB の `.out` を毎回の poll で丸ごと読むと、その 1 回で機械が詰まる。
        求められているのは末尾 4KB。**必要な分だけ seek して読む。**
        """
        try:
            with open(p, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                off = max(0, size - tail_n)
                # ★境界判定用に、窓の手前も少し余分に読む（2026-09-05）。
                #   cp932 は窓だけでは「ここが文字の先頭か」を判定できない
                #   （`_decode_tail_window` の cp932 分岐／`_sjis_boundary_skip`
                #   参照）。このぶんは判定専用で、本文には出さない
                #   （`_decode_tail_window` に渡すのは `prefix` として別扱い）。
                pad = min(off, _SJIS_ANCHOR_MAX_BACK) if off > 0 else 0
                fh.seek(off - pad)
                raw = fh.read(pad + tail_n)
        except FileNotFoundError:
            return ""
        except OSError as e:
            return f"(読めません: {e})"
        prefix, data = raw[:pad], raw[pad:]
        # ★符号化を決め打ちしない（2026-09-03）。旧: `data.decode("utf-8", errors="replace")`。
        #   背景処理は Windows の `.exe`／`cmd.exe` 経由でも起動できる（bash.py の
        #   `_spawn_background` は win32 分岐を持つ）。日本語 Windows は cp932 で吐くので、
        #   固定 UTF-8 は「指定されたパスが見つかりません。」を全部 U+FFFD に潰す——
        #   `bash.py` / `system_state.py` / `remote_read.py` が同日直したのと**同じ形の穴**
        #   （そちらは `_decode_out` / `decode_bytes` を通しており、ここだけ写し忘れていた）。
        #   判定は `read.py` の `decode_bytes` を借りる（同じ物を2つ持つと片方だけ直る）。
        return _decode_tail_window(data, from_middle=off > 0, prefix=prefix)

    out = tail(out_p)
    err = tail(err_p)
    _who = ""
    if _members:
        _who = ("running: " + "; ".join(procutil.describe(m, 60) for m in _members[:4])
                + "\n")
    header = (
        f"pid: {pid}\n"
        f"alive: {alive}\n"
        + _who +
        f"cmd: {str(meta.get('cmd',''))[:200]}\n"
        f"started: {meta.get('started_at','')}\n"
        f"--- stdout (last {tail_n}B) ---\n"
    )
    body = out or "(empty)"
    if err.strip():
        body += f"\n--- stderr ---\n{err}"
    # ★成功を型で名乗る（2026-09-21）。素の str で返すと `as_result` が
    #   **背景プロセスの出力**で BashOutput 自身の成否を当てにいく。
    #   ★ここは特に当たる: 本文に**背景プロセスの stderr がそのまま入る**ので、
    #   ビルドや試験が `command not found` を吐いた瞬間に「BashOutput が失敗した」
    #   ことになる。★この道具の仕事は**出力を読んで見せること**で、
    #   読めたなら成功。中の処理の成否は `alive` と本文が語る。
    from . import ToolResult
    return ToolResult(header + body, ok=True, kind="ok")


def _run(inp: dict) -> str:
    """道具の外へ例外を出さない（2026-09-05・8 巡目・再反証 M3）。

    ★実測（`probe_m3.py`・本番経路で `del procutil._boot_time`）:
        RESULT: (例外が道具の外まで出た) NameError: name '_boot_time' is not defined
        tool_failed = False   startswith OK = False
      利用者とモデルが読む文字列は `Error in BashOutput: …` になるが、
      `laps/tools/__init__.py:tool_failed` が失敗と読む根拠は **`Error:` 前置き**
      （`_FAILED_PREFIX`）なので `Error in …` は **False**——この 1 経路だけ、
      3 巡目に自分で立てた規律（機械が失敗を読む根拠は `Error:` 前置き）に
      乗っていなかった。`_gate_broken` / `_blind_note` と同じ書式に揃える。
    ⚠ 「撃っていません」とは書かない: 例外は `killpg` の**あと**にも起きうる。
      ここで言えるのは「途中で落ちた・止まったかは確かめていない」だけ。
    """
    try:
        return _run_body(inp)
    except Exception as e:
        _bump("bg_tool_broken")
        return (f"Error: BashOutput が途中で落ちました"
                f"（{type(e).__name__}: {str(e)[:200]}）。"
                "**止まったかどうかは確かめられていません**"
                "（撃つ前に落ちたのか、撃ったあとに落ちたのかも分かりません）。\n"
                "  これは道具の欠陥です（環境の問題ではありません）。\n"
                "  確かめる: BashOutput(port=<番号>) で持ち主を出す／"
                "`ss -ltnp` で待受を直接見る。")


SPEC = ToolSpec(
    name="BashOutput",
    description=(
        "背景で走っている処理を**見る／止める**。pid か port のどちらかを渡す。\n"
        "pid: Bash が返したグループ番号。出力の末尾と生死を返し、kill=true で止める"
        "（SIGTERM→止まらなければ SIGKILL）。\n"
        "port: そのポートを待ち受けているプロセスを /proc から引き当てて表示する。"
        "kill=true なら**作業場の中のものだけ**止める（外のサービスには触らない）。"
        "★`ps aux | grep` や `lsof` や `pkill -f` でポートの持ち主を探さないこと"
        "——`pkill -f` は自分自身に当たる。\n"
        "Optional: tail（バイト数・既定 4000）, cwd。"
        "★cwd は範囲を**狭める**のにしか使えない（作業場の外を指しても無視される）。"
        "止める範囲は作業場（LAPS_WORKSPACE）が決めるので、渡す必要はふつう無い。"
    ),
    schema={
        "type": "object",
        "properties": {
            "pid": {"type": ["integer", "string"]},
            "port": {"type": ["integer", "string"],
                     "description": "TCP ポート番号（pid の代わりに指定できる）"},
            "tail": {"type": "integer"},
            "kill": {"type": "boolean"},
            "cwd": {"type": "string",
                    "description": "作業場の**部分**を指して範囲を狭めるときだけ使う"
                                   "（作業場の外を指しても無視される）"},
        },
    },
    executes=True,
    handler=_run,
)
