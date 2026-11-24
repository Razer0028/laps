r"""第4回c・**6 回目**の反証で見つかった「門の照合が実物とずれる」形を閉じる（2026-09-05）。

反証（`…/r4d/wf-merge-bash-ref1/probe_widen.py`・中と外に同名サーバを置く 18 形）で、
5 回目の HEAD（80da2f6）は 18 形中 17 形を正しく扱ったが **W15 だけが落ちた**:

    pkill -9 -i -f "bwref1w_<port>.PY"      ← 実物の大小無視は `.py` に当たる

  * `_scope_name_kill` は `-i` を「読み飛ばした旗＝絞り込み」に分類し、
  * 自分の照合は**大小を区別したまま**（`re.compile(pat)`）で一致 0 になり、
  * `if not outside: return None` に落ちて**注記ひとつ無く素通し**、
  * 実物の `pkill -i` が作業場の外のサーバを殺した（実測: 中も外も死亡）。

★ここが分けている 2 種類（5 回目が一緒くたにしたのが穴の根）:

  絞り込み（`-u nobody` / `-P 1` / `--older` …）
      実物の的 ⊆ 門の的。門が見た「巻き添え候補」は**多すぎる方**にずれるので、
      触らない＋保護ポートの床、で安全側。5 回目の直しはこれで正しい。

  照合のずれ（`-i` 大小無視 / `[[:digit:]]` / 読めないパターン）
      実物の的 ⊄ 門の的。門が「一致 0」と見た相手が**実際には撃たれる**。
      このとき `not outside` は「巻き添えなし」ではなく「**分からない**」。
      → 再現できる物（`-i`・POSIX の文字クラス）は**照合に反映**し、
        再現できない物だけ `_name_kill_unreliable` の受け皿へ。

★**いったん入れて実物で外した物**（残す価値のある失敗）: `pkill -v`（反転）。
  机上では「反転も再現できる」ので `hit = not hit` を実装したが、PID 名前空間の
  中で実物に当てたら **procps-ng 4.0.4 の pkill は `-v` を受け付けない**
  （`pkill: 無効なオプション -- 'v'` / exit 2 / 1 本も死なない。`-v/--inverse` は
  **pgrep 側**の綴り）。反映すると「実物が 1 本も撃たない命令」を門が
  `kill -9 <中の pid>` に書き換える＝5 回目に閉じた反証 A を自分で作り直す形。
  → `-v` は絞り込み扱いのまま。**机上で「読める」ことは、実物が受け取ることの
  証拠にならない。**

⚠ `killall -i` は **interactive**（確認して撃つ）で大小無視ではない。
   `killall` の大小無視は `-I`。同じ 1 文字が道具ごとに別物なので分けて扱う。
⚠ `pkill --help` と突き合わせて、引数を取るのに食っていなかった綴り
   （`-O` / `-q` / `-r` / `--cgroup` / `--runstates`）を足した。食い損ねると
   値がパターンに化けて `pats` が 2 つ＝`None`＝**素通し**になる。
"""
from __future__ import annotations

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


# ── ① 語割りが `-i` / `-v` を「読み飛ばした旗」にしない ────────────────
@pytest.mark.parametrize("cmd,key", [
    ("pkill -9 -i -f x", "icase"),
    ("pkill -9 --ignore-case -f x", "icase"),
    ("pkill -9 -if x", "icase"),                      # 束ね
    ("killall -9 -I x", "icase"),
    ("killall -9 --ignore-case x", "icase"),
])
def test_R6_照合に反映する綴りは絞り込み扱いにしない(cmd, key):
    p = B._parse_name_kill(cmd)
    assert p is not None, cmd
    assert p[key] is True, (cmd, p)
    assert p["unscopable"] is None, (cmd, p["unscopable"])


@pytest.mark.parametrize("cmd", [
    "killall -9 -i python3",        # interactive（大小無視ではない）
    "pkill -9 -n -f x",             # 最新 1 本だけ
    "pkill -9 -o -f x",             # 最古 1 本だけ
    "pkill -9 -u nobody -f x",      # 持ち主で絞る
    # ★`-v` は**実物（procps-ng 4.0.4）が受け付けない**。反映すると
    #   「実物が 1 本も撃たない命令」を門が kill に書き換える＝反証 A の再来。
    "pkill -9 -v -f x",
])
def test_R6_絞り込みは今までどおり触らない(cmd):
    p = B._parse_name_kill(cmd)
    assert p is not None and p["unscopable"] is not None, (cmd, p)


def test_R6_素の名前撃ちは大小無視でない():
    assert B._parse_name_kill("pkill -9 -f x")["icase"] is False


def test_R6_反転は反映しない():
    """★実測（PID 名前空間の中・実物）: `pkill -9 -v -f X` は
    `pkill: 無効なオプション -- 'v'` / exit 2 で**1 本も死なない**。
    門が反転を「再現できる」と扱うと、撃たない命令を kill に書き換えてしまう。"""
    p = B._parse_name_kill("pkill -9 -v -f corridor_bridge")
    assert "inverse" not in p
    assert p["unscopable"] == "-v", p
    r = B._scope_name_kill("pkill -9 -v -f corridor_bridge", WS)
    assert r is None or r[0] == "pkill -9 -v -f corridor_bridge", r


# ── ③b 引数を取る綴りの書き落としは「素通し」になる ────────────────────
@pytest.mark.parametrize("opt,val", [
    ("-O", "3600"), ("-q", "5"), ("-r", "S"), ("--cgroup", "a/b"),
    ("--runstates", "D"),
])
def test_R6_引数を取る綴りを食わないとパターンに化ける(opt, val):
    """★食い損ねると pats が 2 つ＝`pkill` は 1 つしか取らない → None → 素通し。
    実物（`pkill --help`・procps-ng 4.0.4）と突き合わせて足した分。"""
    p = B._parse_name_kill(f"pkill -9 {opt} {val} -f corridor_bridge")
    assert p is not None, f"{opt} を食えず素通しになる"
    assert p["pats"] == ["corridor_bridge"], p["pats"]
    assert p["unscopable"] is not None                 # 絞りなので触らない


# ── ② 大小無視: 一致 0 →「巻き添えなし」の**誤った成功**にしない ────────
def test_R6_大小無視で外の相手を見落とさない():
    """★反証 W15 の本体。5 回目は None（素通し）を返し、実物が外のサーバを殺した。"""
    cmd = 'pkill -9 -i -f "CORRIDOR_BRIDGE.PY"'
    r = B._scope_name_kill(cmd, WS)
    assert r is not None, "大小無視を反映せず素通しにしている（反証 W15 の形）"
    assert r[0] == "true", r[0]
    assert "corridor_bridge" in r[1]


def test_R6_大小無視でも巻き添えが無ければ触らない():
    """★丸ごと断るのではない。中の 1 本にしか当たらないなら今までどおり通す。"""
    assert B._scope_name_kill('pkill -9 -i -f "MYSERVER.PY"', WS) is None


def test_R6_大小無視は絞って撃つほうにも効く():
    """両方に当たる形なら、外を落として中だけ撃つ（機能を殺していない）。"""
    new, note = B._scope_name_kill('pkill -9 -i -f "PYTHON3"', WS)
    assert new == "kill -9 100", new
    assert "corridor_bridge" in note


def test_R6_killall_の大小無視は名前の突き合わせにも効く():
    """`killall` は comm（名前）の**完全一致**。`-I` はその一致を大小無視にする。"""
    new, _n = B._scope_name_kill('killall -9 -I "PYTHON3"', WS)
    assert new == "kill -9 100", new
    # `-I` が無ければ大小が合わず一致 0＝触らない（実物も当たらない）
    assert B._scope_name_kill('killall -9 "PYTHON3"', WS) is None


def test_R6_killall_の_i_は大小無視ではない():
    """★`killall -i` を大小無視と読むと、確認つきの命令を無断で撃つ形に化ける。"""
    p = B._parse_name_kill('killall -9 -i "PYTHON3"')
    assert p["icase"] is False and p["unscopable"] == "-i", p
    r = B._scope_name_kill('killall -9 -i "PYTHON3"', WS)
    assert r is None or r[0] == 'killall -9 -i "PYTHON3"', r


# ── ③ POSIX の括弧式（`[[:digit:]]`）: Python の `re` とは別物 ──────────
def test_R6_POSIX_文字クラスを直さないと一致0になる():
    """★実物は `regcomp(REG_EXTENDED)`＝`[[:digit:]]` は数字。Python の `re` は
    「`[`,`:`,`d`,`i`,`g`,`t` のどれか＋`]`」と読む＝門だけが一致 0 →
    「巻き添えなし」で素通し（`-i` と同じ**見落とす方向**のずれ）。"""
    import re as _re
    assert _re.search(r"corridor_[[:alpha:]]+", "corridor_bridge") is None   # Python 素
    fixed, why = B._posix_to_python_re(r"corridor_[[:alpha:]]+")
    assert why is None and fixed == "corridor_[a-zA-Z]+"
    assert _re.search(fixed, "corridor_bridge") is not None


def test_R6_POSIX_文字クラスの名前撃ちが外の相手を見落とさない():
    new, note = B._scope_name_kill(r'pkill -9 -f "[[:lower:]]+_bridge"', WS)
    assert new == "true", new
    assert "corridor_bridge" in note


def test_R6_知らないPOSIXクラスは受け皿へ(monkeypatch):
    monkeypatch.setattr(procutil, "protected_port_pids", lambda: {101})
    monkeypatch.setattr(procutil, "protected_ports", lambda: {22})
    new, note = B._scope_name_kill(r'pkill -9 -f "[[:nope:]]+_bridge"', WS)
    assert new == "true", new
    assert "再現できません" in note and "nope" in note


# ── ④ 読めないパターン: literal に落として「読めた」ことにしない ─────────
def test_R6_読めないパターンは保護ポートが居るなら撃たない(monkeypatch):
    monkeypatch.setattr(procutil, "protected_port_pids", lambda: {101})
    monkeypatch.setattr(procutil, "protected_ports", lambda: {22})
    new, note = B._scope_name_kill('pkill -9 -f "a{2,1}corridor"', WS)
    assert new == "true", new
    assert "再現できません" in note and "pid 101" in note


def test_R6_読めないパターンは保護が居なければ通すが黙っては通さない(monkeypatch):
    monkeypatch.setattr(procutil, "protected_port_pids", lambda: set())
    cmd = 'pkill -9 -f "a{2,1}corridor"'
    new, note = B._scope_name_kill(cmd, WS)
    assert new == cmd, new
    assert "再現できません" in note


def test_R6_読める形は受け皿へ行かない(monkeypatch):
    """★受け皿が広すぎると普通の命令まで断る（機能を殺す）。"""
    monkeypatch.setattr(procutil, "protected_port_pids", lambda: {101})
    new, _n = B._scope_name_kill("pkill -9 -f python3", WS)
    assert new == "kill -9 100", new


# ── ⑤ 実物: 保護ポートの設備が大小無視で死なない ──────────────────────
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def guarded_server(tmp_path, monkeypatch, _fake_proc):
    """作業場の**中**に立てた自前 http.server を保護ポートとして宣言する。

    ★撃つのは自分で起こしたこの 1 本だけ（標的は unique な marker）。
      広いパターン（`pkill -f python` 等）は共有ホストの他人に当たるので使わない。
    """
    monkeypatch.undo()                       # 偽の proc 表を外して実物を見る
    monkeypatch.setattr(B.sys, "platform", "linux")
    ws = tmp_path / "ws"
    ws.mkdir()
    port = _free_port()
    marker = f"bwr6_{port}"
    src = ws / f"{marker}.py"
    src.write_text(
        "import http.server,socketserver,sys\n"
        "socketserver.TCPServer(('127.0.0.1',int(sys.argv[1])),"
        "http.server.SimpleHTTPRequestHandler).serve_forever()\n", encoding="utf-8")
    monkeypatch.setenv("LAPS_PROTECTED_PORTS", str(port))
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    proc = subprocess.Popen([sys.executable, str(src), str(port)],
                            cwd=str(ws), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        if procutil.port_in_use(port):
            break
        time.sleep(0.05)
    else:                                    # pragma: no cover
        proc.kill(); proc.wait(timeout=5)
        pytest.skip("http.server が上がらなかった")
    try:
        yield proc, marker, str(ws)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


def test_R6_実物_大小無視でも保護ポートの設備は死なない(guarded_server, monkeypatch):
    """★反証 W15 を実物で閉じる（5 回目の HEAD では中も外も死んだ）。"""
    # ★政策は自分で trusted にする（全体スイートでは既定 workspace＝pkill が許可リストに無く
    #   「policy denied」で何も測れない。2026-09-06 preflight 実測。他の試験が漏らした env に頼らない）
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    proc, marker, ws = guarded_server
    assert proc.pid in procutil.protected_port_pids()
    cmd = f'pkill -9 -i -f "{marker.upper()}.PY"'
    r = B._scope_name_kill(cmd, ws)
    assert r is not None, "大小無視で一致 0 に見えて素通しにしている"
    assert r[0] == "true", r[0]
    out = B._run({"command": cmd, "cwd": ws, "timeout": 15})
    time.sleep(0.5)
    assert proc.poll() is None, f"保護ポートの設備が死んだ: {out}"
    # ★政策で弾かれて「たまたま生きていた」を通さない（この試験が何も測って
    #   いない形＝この現場が何度も踏んだ偽の緑を作らない）。
    assert "policy denied" not in out, out
    assert "保護ポート" in out, out
