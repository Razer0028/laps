r"""第4回c・**5 回目**の反証で実物が死んだ形を閉じる（2026-09-05）。

反証（`…/r4d/merge-bash4-ref/` の probe1b/2/3/4・保護ポートの自前サーバで再現）:

  A ★**回帰**（4 回目に入れた書き換えが作った穴）:
      `_NAME_KILL_ARG_OPTS` は `-u/-U/-G/-g/-s/-t/-P/-F/--older/--ns…` を
      **読み飛ばすだけ**で判定に反映せず、`_scope_name_kill` はそれを
      `kill <信号> <作業場の中の pid>` に書き換えていた。実物の
      `pkill -9 -u nobody -f X` は「nobody の物」だけ＝root のサーバに当たらないのに、
      書き換え後の `kill` は当たる——**門が、元の命令が撃たない相手を撃った**
      （中と外に同名サーバを置いた 6 形のうち **5 形**で中のサーバが死亡。
      base は 0/6）。しかも注記は絞りの有無に関わらず同一文面で、
      **嘘の説明つき**だった。

  B ★門 3 つとも None で `(exit 0, no output)` になった 13 形:
      `echo x & kill …`（`&` が区切りとして割られていない）／`if`・`while`・`until`・
      `case` のラベル／`\pkill`・`p"kill"`・`pk\ill`（綴りの引用符）／
      `pkill -f $'X'`（`$` が正規表現アンカーになり一致 0＝「巻き添えなし」の
      **誤った成功**）／ヒアドキュメントの後ろの `kill`（`"<<" in cmd` で命令全体の
      改行割りをやめる大鉈）／`sudo -u root pkill`（前置きのオプション引数）。

★ここが守るのは 3 つ:
  ① 撃たせない相手（保護ポートの設備）には**綴りを変えても届かない**。
  ② **元の命令が撃たない相手を、門が撃たない**（絞りを再現できないなら触らない）。
  ③ 書き換えた本文が**実際に走る命令と同じに見える**（空白を潰さない）。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest

from laps import procutil
from laps.tools import bash as B

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="/proc を読む")

WS = "/work/ws1"
#: 100 = 作業場の中の自分のサーバ／101 = 作業場の外の他人のサービス
TABLE = [
    (100, "python3", "python3 myserver.py --port 18100", "/work/ws1",
     ("python3", "myserver.py", "--port", "18100")),
    (101, "python3", "/usr/bin/python3 /srv/shared/scripts/corridor_bridge.py", "/",
     ("/usr/bin/python3", "/srv/shared/scripts/corridor_bridge.py")),
]


@pytest.fixture(autouse=True)
def _fake_proc(monkeypatch):
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE))
    monkeypatch.setattr(B, "_registered_bg_pids", lambda *a, **k: set())
    monkeypatch.setattr(B.sys, "platform", "linux")


# ── A 絞り込みオプション: 読み飛ばしたまま撃たない ──────────────────────
NARROWING = [
    "pkill -9 -u nobody -f corridor_bridge",
    "pkill -9 -U 65534 -f corridor_bridge",
    "pkill -9 -P 1 -f corridor_bridge",
    "pkill -9 -t tty99 -f corridor_bridge",
    "pkill -9 -s 1 -f corridor_bridge",
    "pkill -9 -g 1 -f corridor_bridge",
    "pkill -9 -G 0 -f corridor_bridge",
    "pkill -9 --older 999999 -f corridor_bridge",
    "pkill -9 --ns 1 -f corridor_bridge",
    "pkill -9 -F /tmp/x.pid -f corridor_bridge",
    "pkill -9 -n -f corridor_bridge",
    "killall -u nobody python3",
    "killall -o 999d python3",
    # ★`killall -i` は **interactive**（確認して撃つ）で大小無視ではない。
    #   同じ 1 文字が道具ごとに別物なので、ここは絞り込みのまま。
    "killall -i python3",
]
#: ★ここから移した 3 形（2026-09-05・第4回c の **6 回目**・反証 W15）:
#:     `pkill -v`（反転）／`pkill -i`・`killall -I`（大小無視）
#:   5 回目はこれを「読み飛ばした旗＝触らない」に入れたが、**それが穴だった**。
#:   絞り込み（`-u` 等）は実物の的 ⊆ 門の的なので触らないのが安全側だが、
#:   大小無視と反転は**門の的の外**へ実物が出る＝門が一致 0 を見て
#:   「巻き添えなし」と誤判定し、**黙って素通し**になる。
#:   実測（HEAD 80da2f6・反証の W15）: `pkill -9 -i -f "…​.PY"` で作業場の外の
#:   サーバが注記ひとつ無しに死亡。→ 照合に**反映する**へ変更。
#:   新しい期待は `tests/test_kill_gates_round6.py`。


@pytest.mark.parametrize("cmd", NARROWING)
def test_R5_絞り込みオプションつきは書き換えない(cmd):
    """★元の命令が撃たない相手を撃つ書き換えを作らない（反証 A・5/6 形が死亡）。"""
    r = B._scope_name_kill(cmd, WS)
    assert r is not None, f"注記も出ていない: {cmd}"
    assert r[0] == cmd, f"書き換えてしまった: {r[0]!r}"
    assert "kill -9 100" not in r[0] and "kill -KILL 100" not in r[0]


@pytest.mark.parametrize("cmd", NARROWING[:3])
def test_R5_絞り込みつきの注記は絞りなしと違う文面(cmd):
    """★4 形が同一文面「作業場の中の 1 本だけに絞って撃ちました」＝**嘘の説明**だった。"""
    _new, note = B._scope_name_kill(cmd, WS)
    assert "絞り込みオプション" in note, note
    assert "だけに絞って撃ちました" not in note, note


def test_R5_絞りなしは従来どおり絞って撃つ():
    """★A の直しで「絞る」機能ごと殺していないこと（壊さない物の確認）。"""
    new, note = B._scope_name_kill("pkill -9 -f python3", WS)
    assert new == "kill -9 100", new
    assert "corridor_bridge" in note


def test_R5_絞り込みの検出は_parse_name_kill_が持つ():
    assert B._parse_name_kill("pkill -9 -f x")["unscopable"] is None
    p = B._parse_name_kill("pkill -9 -u nobody -f x")
    assert p is not None and p["unscopable"] == "-u", p
    # 引数は食う（食わないとパターンと読み違える）
    assert p["pats"] == ["x"], p


def test_R5_exact_は部分一致に化けない():
    """★`-x` を旗のまま無視すると `pkill -x python` が `python3` に当たる。"""
    assert B._parse_name_kill("pkill -9 -x python")["exact"] is True
    assert B._scope_name_kill("pkill -9 -x python", WS) is None      # 全長一致しない
    new, _n = B._scope_name_kill("pkill -9 -x python3", WS)
    assert new == "kill -9 100", new


# ── A の床: 絞りつきでも保護ポートの設備には届かない ──────────────────
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def guarded_server(tmp_path, monkeypatch, _fake_proc):
    """作業場の**中**に立てた自前 http.server を保護ポートとして宣言する。"""
    monkeypatch.undo()                       # 偽の proc 表を外して実物を見る
    monkeypatch.setattr(B.sys, "platform", "linux")
    ws = tmp_path / "ws"
    ws.mkdir()
    port = _free_port()
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", str(port))
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(ws), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        if procutil.port_in_use(port):
            break
        time.sleep(0.05)
    else:                                    # pragma: no cover
        proc.kill(); proc.wait(timeout=5)
        pytest.skip("http.server が上がらなかった")
    try:
        yield proc, port, str(ws)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_R5_絞りつきでも保護ポートの設備は断る(guarded_server):
    """★素通しに戻すだけでは丸腰になる。当たる形だけ床で断る（付随の must_fix）。"""
    proc, port, ws = guarded_server
    assert proc.pid in procutil.protected_port_pids()
    cmd = f'pkill -9 -u root -f "http.server {port}"'
    r = B._scope_name_kill(cmd, ws)
    assert r is not None, "絞りつきだからと素通しにしている"
    assert r[0] == "true", r[0]
    assert "保護ポート" in r[1] and "絞り込みオプション" in r[1]
    assert proc.poll() is None


# ── B(2) 単独の `&` は区切り ────────────────────────────────────────
@pytest.mark.parametrize("cmd,want", [
    ("echo x & kill -9 101",                 "echo x & true"),
    ("true & kill -9 101",                   "true & true"),
    ("echo a & echo b & kill -9 101",        "echo a & echo b & true"),
    ("kill -9 101 & echo x",                 "true & echo x"),
])
def test_R5_単独の_amp_の後ろも門が見る(cmd, want):
    r = B._scope_pid_kill(cmd, WS)
    assert r is not None, f"素通し（反証で実物が死んだ形）: {cmd}"
    assert r[0] == want, (cmd, r[0])


def test_R5_amp_は区切りだが_2文字の綴りは違う():
    """`&&` / `2>&1` / `>&2` / `&>` を区切りに読むと命令を壊す。"""
    assert B._split_segments("a && b") == ["a ", "&&", " b"]
    assert B._split_segments("a 2>&1 | b") == ["a 2>&1 | b"]
    assert B._split_segments("a &> f") == ["a &> f"]
    assert B._split_segments("a & b") == ["a ", "&", " b"]


def test_R5_断片の前後の空白を潰さない():
    """★`&` を区切りに足した途端 `true& echo x` に潰れた（走る命令と本文がズレる）。"""
    new, _n = B._scope_pid_kill("kill -9 101 & echo x", WS)
    assert new == "true & echo x", new
    new2, _n2 = B._scope_name_kill("pkill -9 -f corridor_bridge & echo x", WS)
    assert new2 == "true & echo x", new2


def test_R5_括弧の中は区切らない():
    """`$( … )` / `( … )` は `_scope_nested_kill` が別に開く（両方から落とさない）。"""
    assert B._split_segments("( kill 1 & )") == ["( kill 1 & )"]
    assert B._split_segments("$(kill 1; echo x)") == ["$(kill 1; echo x)"]


# ── B(3) if / while / until / case ────────────────────────────────
@pytest.mark.parametrize("cmd", [
    'if pkill -9 -f corridor_bridge; then :; fi',
    'while pkill -9 -f corridor_bridge; do break; done',
    'until pkill -9 -f corridor_bridge; do break; done',
])
def test_R5_条件の側の名前撃ちも門に通す(cmd):
    r = B._scope_name_kill(cmd, WS)
    assert r is not None, f"素通し: {cmd}"
    assert "pkill" not in r[0], r[0]


def test_R5_case_のラベルの後ろも門に通す():
    r = B._scope_pid_kill("case x in x) kill -9 101;; esac", WS)
    assert r is not None, "素通し（実物で保護ポートのサーバが死んだ形）"
    assert "kill -9 101" not in r[0], r[0]


# ── B(4)/B(5) 綴りの引用符・バックスラッシュ・ANSI-C 引用 ─────────────
@pytest.mark.parametrize("cmd", [
    r'\pkill -9 -f corridor_bridge',
    'p"kill" -9 -f corridor_bridge',
    r'pk\ill -9 -f corridor_bridge',
    "'pkill' -9 -f corridor_bridge",
    "$'pkill' -9 -f corridor_bridge",
    'busybox pkill -9 -f corridor_bridge',
])
def test_R5_綴りを割った名前撃ちも門に通す(cmd):
    r = B._scope_name_kill(cmd, WS)
    assert r is not None, f"素通し（実物で保護ポートのサーバが死んだ形）: {cmd}"
    assert "corridor_bridge" in r[1]


def test_R5_ANSI_C_引用のパターンを読む():
    r"""★`$'X'` を丸ごとパターンにすると `$` がアンカーになり**一致 0**。
    「読めなかった」が「巻き添えなし＝安全」に化けていた。"""
    assert B._prog_literal("$'corridor_bridge'") == "corridor_bridge"
    r = B._scope_name_kill("pkill -9 -f $'corridor_bridge'", WS)
    assert r is not None and r[0] == "true", r
    # 読めない綴りは None（触らない）＝「読み飛ばせた」を「読めた」にしない
    assert B._prog_literal(r"$'a\qb'") is None
    assert B._prog_literal("$X") is None


def test_R5_展開が混ざるパターンは触らない():
    """★2026-09-05・第4回c の 7 回目に**期待を書き換えた**（反証 M5/S14）。

    旧: `_parse_name_kill` が `None`＝**黙って素通し**。実測で
        `while read -r l; do pkill -9 -f "$l"; done <<< "X"` が保護ポートの
        サーバを殺したのに、結果本文は `(exit 0, no output)` だった。
    新: 読めたのは「pkill であること」までで**的が分からない**ので、
        `unreadable` を立てて `_name_kill_unreliable` の受け皿へ渡す
        （＝書き換えはしないが、黙ってはいない）。
    """
    parsed = B._parse_name_kill('pkill -9 -f "$NAME"')
    assert parsed is not None and parsed["unreadable"], parsed
    assert B._scope_name_kill('pkill -9 -f "$NAME"', WS) is not None, "黙って素通し"


# ── B(6) ヒアドキュメント ─────────────────────────────────────────
def test_R5_ヒアドキュメントの後ろの_kill_を見る():
    cmd = "cat > /dev/null <<'EOF'\nhello\nEOF\nkill -9 101"
    r = B._scope_pid_kill(cmd, WS)
    assert r is not None, "素通し（実物で保護ポートのサーバが死んだ形）"
    assert r[0].endswith("true"), r[0]
    assert "hello" in r[0], "本文を壊した"


def test_R5_ヒアドキュメントの中身は書き換えない():
    cmd = "cat > /dev/null <<'EOF'\nkill -9 101\nEOF\necho done"
    assert B._scope_pid_kill(cmd, WS) is None, "本文の中の kill を書き換えた"


def test_R5_終端が見つからないヒアドキュメントは触らない():
    """読めない形は**取りこぼす方に倒す**（旧実装と同じ安全側）。"""
    cmd = "cat <<EOF\nkill -9 101\n"
    assert B._scope_pid_kill(cmd, WS) is None


def test_R5_ヒアドキュメントの範囲():
    spans = B._heredoc_spans("cat <<'EOF'\nx\nEOF\nkill 1")
    assert len(spans) == 1
    a, b = spans[0]
    assert "kill 1" not in "cat <<'EOF'\nx\nEOF\nkill 1"[a:b]


# ── B(7) 前置きのオプション引数 ────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    'sudo -u root pkill -9 -f corridor_bridge',
    'sudo --user=root pkill -9 -f corridor_bridge',
    'sudo -u root -g root pkill -9 -f corridor_bridge',
    'env -u LD_PRELOAD pkill -9 -f corridor_bridge',
    'nice -n 5 pkill -9 -f corridor_bridge',
])
def test_R5_引数つき前置きオプションを食う(cmd):
    r = B._scope_name_kill(cmd, WS)
    assert r is not None, f"素通し: {cmd}"
    assert "pkill" not in r[0], r[0]


def test_R5_前置きは1文字も変えずに書き戻す():
    new, _n = B._scope_name_kill("sudo -u root pkill -9 -f corridor_bridge", WS)
    assert new == "sudo -u root true", new


# ── C(9) 保護ポートの設定の書き損じを、拾える所へ出す ───────────────────
class _Ag:
    def __init__(self):
        self.counters: dict[str, int] = {}


@pytest.mark.parametrize("raw", [" ", "\t", "22,abc", "0x4ce0"])
def test_R5_保護ポートの書き損じを状態として残す(monkeypatch, raw):
    """★旧実装は `warnings.warn` **だけ**＝stderr・プロセスに 1 回・戻り値にも
    counters にも出ない。`simplefilter("always")` 無しでは検出すらできなかった。"""
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", raw)
    procutil.protected_ports()
    assert procutil.ports_config_problem(), raw


def test_R5_書き損じが無いときは何も言わない(monkeypatch):
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "22,18301")
    procutil.protected_ports()
    assert procutil.ports_config_problem() is None
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", "")     # 明示の逃げ道
    procutil.protected_ports()
    assert procutil.ports_config_problem() is None


def test_R5_書き損じは結果本文とcountersに出る(monkeypatch, tmp_path):
    from laps import tools as T
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", " ")
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    B._reset_ports_note()
    ag = _Ag()
    tok = T.CURRENT_AGENT.set(ag)
    try:
        out = B._run({"command": "echo hi", "cwd": str(tmp_path), "timeout": 20})
    finally:
        T.CURRENT_AGENT.reset(tok)
    assert "保護ポートの設定に問題があります" in out, out
    assert ag.counters.get("protected_ports_misconfig") == 1, ag.counters
