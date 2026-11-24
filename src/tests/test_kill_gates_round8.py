r"""第4回c・**8 回目**の反証（`wf-merge-bash-ref2`）で見つかった形を閉じる（2026-09-06）。

★M-A（コメント `#`）: `_split_kill_args` が `#` を**語**として扱っていたので、
  3 経路が同時に黙っていた。

      kill -9 <pid> # cleanup      → 対象の白名簿に当たらず `_parse_kill` が None
      pkill -9 -f X # 1            → パターンが 2 個＝「読み違えた」で None
      killall -9 zzz # <name>      → `#` と後続語が**追加の名前**になり的が広がる

  前 2 つは門が**一度も見ない**まま素通し（PID 名前空間の中で実測: 保護ポートの
  サーバが死亡し、結果本文は `(exit 0, no output)`）。3 つ目は逆に、
  **元の命令が撃たない相手を門が撃つ**（5 回目の反証 A と同じ族の別の綴り）。
  → `_comment_start` / `_strip_comment_tail` を新設し、`_parse_kill` /
    `_parse_name_kill` / `_split_segments` / `_split_pipeline` の 4 か所へ配線。

★M-B（対象が置換で来る形）: `kill -9 $(pgrep -f X)` は `pkill -f X` と**同じ相手**を
  撃つのに、名前撃ちの門は綴りが `kill` なので見ず、PID 側は対象が `$( … )` なので
  「解けない＝素通し」に落ちていた（実測 F5/F6 で保護ポートのサーバが死亡）。
  → `_pgrep_match_pids` が **pkill と同じ照合器**（`_name_kill_matcher`）で解く。
    照合を 2 本持たないために `_scope_name_kill` の照合もこの 1 本へ寄せた。
  ⚠ 出る物が変わる綴り（`-c` 件数 / `-n`・`-o` 1 本だけ / `-v` 反転）と
    絞り込み（`-u` 等）は**解かない**＝従来どおり素通し。「読み飛ばせた」を
    「正しく読めた」と扱わない（反証 A の教訓）。
"""
from __future__ import annotations

import sys

import pytest

from laps.tools import bash as B

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="/proc を読む")

WS = "/work/ws8"
MINE, GUARD, OUT = 4101, 4102, 4103
#: MINE = 作業場の中の自分のサーバ／GUARD = 保護ポートの設備／OUT = 作業場の外
TABLE = [
    (MINE, "python3", f"python3 {WS}/mine_srv.py 9001", WS,
     ("python3", f"{WS}/mine_srv.py", "9001")),
    (GUARD, "python3", "python3 /srv/gw/gateway.py 18400", "/srv/gw",
     ("python3", "/srv/gw/gateway.py", "18400")),
    (OUT, "python3", "python3 /opt/other/other_srv.py 9002", "/opt/other",
     ("python3", "/opt/other/other_srv.py", "9002")),
]


@pytest.fixture(autouse=True)
def _fake_proc(monkeypatch):
    monkeypatch.setattr(B, "_proc_table", lambda: list(TABLE))
    monkeypatch.setattr(B, "_registered_bg_pids", lambda *a, **k: set())
    monkeypatch.setattr(B, "_kill_protected_pids", lambda: {1, GUARD})
    monkeypatch.setattr(B.procutil, "protected_port_pids", lambda: {GUARD})
    monkeypatch.setattr(B.procutil, "protected_ports", lambda: [22, 18301, 18400])
    monkeypatch.setattr(B.sys, "platform", "linux")


def _gate(cmd: str):
    """本番と同じ順（入れ子 → 名前撃ち → PID 撃ち）。"""
    return B._gate_fragment(cmd, WS, None, 0)


# ── M-A: コメントを語として読んでいた ───────────────────────────────
@pytest.mark.parametrize("cmd", [
    "kill -9 {p} # cleanup",
    "kill -9 {p} #x",
    "kill -9 {p} 2>/dev/null # c",
    "kill -9 {p} # ; pkill -f sshd",
    "kill -9 {p} # | cat",
    "kill -9 {p} # && echo done",
])
def test_R8_コメントつきの_PID_撃ちも門が見る(cmd):
    got = _gate(cmd.format(p=GUARD))
    assert got is not None, cmd
    assert got[0].startswith("true"), got[0]
    assert "保護ポート" in got[1]


def test_R8_コメントは1文字も変えずに書き戻す():
    got = _gate(f"kill -9 {GUARD} # cleanup")
    assert got[0] == "true # cleanup", got[0]


@pytest.mark.parametrize("cmd", [
    'pkill -9 -f "gateway.py" # 1',
    'pkill -9 -f "gateway.py" #',
])
def test_R8_コメントつきの名前撃ちも門が見る(cmd):
    got = _gate(cmd)
    assert got is not None, cmd
    assert got[0].startswith("true"), got[0]


def test_R8_killall_のコメントが追加の名前にならない():
    """`killall -9 zzz # <name>` の `#` と後続語が**別の名前**になっていた
    ＝門が、元の命令が撃たない相手（`gateway.py`）を撃つ形。"""
    p = B._parse_name_kill("killall -9 zzz # gateway.py")
    assert p is not None
    assert p["pats"] == ["zzz"], p["pats"]
    assert p["tail"] == " # gateway.py", p["tail"]


@pytest.mark.parametrize("cmd,pats", [
    ('pkill -9 -f "a#b"', ["a#b"]),          # 引用符の中の `#` はコメントではない
    ("pkill -9 -f a#b", ["a#b"]),            # 語の**途中**の `#` もコメントではない
    ("pkill -9 -f 'http://x/#frag'", ["http://x/#frag"]),
])
def test_R8_語の途中の_シャープ_はコメントではない(cmd, pats):
    p = B._parse_name_kill(cmd)
    assert p is not None, cmd
    assert p["pats"] == pats, p


def test_R8_コメントの中の区切りで断片を割らない():
    segs = [s for s in B._split_segments("kill -9 1 # ; pkill -f sshd")
            if s not in B._SEG_SEPS]
    assert segs == ["kill -9 1 # ; pkill -f sshd"], segs
    assert B._split_pipeline("kill -9 1 # | cat") == ["kill -9 1 # | cat"]


# ── M-B: `$(pgrep …)` を pkill と同じ照合器で解く ─────────────────────
@pytest.mark.parametrize("cmd", [
    'kill -9 $(pgrep -f "gateway.py")',
    'kill -9 `pgrep -f "gateway.py"`',
    'kill -9 $(pgrep -f "other_srv.py")',
    'kill -9 $(/usr/bin/pgrep -f "gateway.py")',
    'kill -9 $(pgrep -x python3)',           # comm 完全一致＝3 本とも当たる
])
def test_R8_pgrep_の置換を解いて巻き添えを断る(cmd):
    got = _gate(cmd)
    assert got is not None, cmd
    assert got[0] == "true", got[0]


def test_R8_自分のサーバだけなら1文字も変えない():
    assert _gate('kill -9 $(pgrep -f "mine_srv.py")') is None


@pytest.mark.parametrize("args,want", [
    ('-f "gateway.py"', [GUARD]),
    ('-f "_srv[.]py"', [MINE, OUT]),         # gateway.py には当たらない
    ("-x python3", [MINE, GUARD, OUT]),
    ("python3", [MINE, GUARD, OUT]),         # 綴り無し＝comm を部分一致
    ("nosuchthing", None),                   # 当たらない＝解いた結果が空
])
def test_R8_pgrep_の照合は実物と同じ対象(args, want):
    assert B._pgrep_match_pids(args) == want


@pytest.mark.parametrize("args", [
    '-c -f "gateway.py"',                    # 件数だけ出る＝PID ではない
    '-n -f "gateway.py"',                    # 最新 1 本だけ
    '-o -f "gateway.py"',
    '-v -f "gateway.py"',                    # 反転
    '-d, -f "gateway.py"',                   # 区切り文字
    '-u root -f "gateway.py"',               # 絞り込み（意味を再現できない）
    '--newest -f "gateway.py"',
    '-f "$PAT"',                             # パターンが変数
    '-f "a{2,1}b"',                          # 読めない綴り
])
def test_R8_解けない綴りは解かない(args):
    """「読み飛ばせた」を「正しく読めた」と扱わない（反証 A の教訓）。
    解かない＝従来どおり素通し（門が的を**広げる**より安全側）。"""
    assert B._pgrep_match_pids(args) is None


@pytest.mark.parametrize("cmd", [
    'kill -9 $(pgrep -c -f "gateway.py")',
    'kill -9 $(pgrep -u root -f "gateway.py")',
])
def test_R8_解けない_pgrep_は従来どおり素通し(cmd):
    assert _gate(cmd) is None


# ── M-B: `$(echo <数字>)` ────────────────────────────────────────────
def test_R8_echo_の置換は数字のときだけ解く():
    assert _gate(f'kill -9 "$(echo {GUARD})"')[0] == "true"
    assert _gate(f"kill -9 $(echo {MINE})") is None
    assert _gate("kill -9 $(echo abc)") is None


# ── 照合器を 1 本にした結果、名前撃ち側の振る舞いが変わっていない ─────────
def test_R8_名前撃ちは作業場の中だけに絞る():
    got = _gate("pkill -9 -f srv")
    assert got is not None
    assert got[0] == f"kill -9 {MINE}", got[0]
    assert "保護ポート" in got[1]


def test_R8_killall_の絶対パスは実行ファイルで照合する(monkeypatch):
    """`_name_kill_matcher` へ移しても `/` つきは exe 照合のまま。"""
    monkeypatch.setattr(B, "_pid_exe_id", lambda pid: (9, 1) if pid == GUARD else (9, 2))
    monkeypatch.setattr(B.os, "stat", lambda p: type("S", (), {"st_dev": 9, "st_ino": 1})())
    got = _gate("killall -9 /usr/bin/python3")
    assert got is not None and got[0] == "true", got


# ── M-B: `$p` / `${p}` は bash の規則どおり展開して読む ────────────────
def test_R8_未定義の変数は空に展開して読む():
    """`kill -9 ${p}<pid>` は実物では `kill -9 <pid>`。門は素通ししていた（F7）。"""
    got = _gate(f"kill -9 ${{p}}{GUARD}")
    assert got is not None and got[0] == "true", got


def test_R8_同じ命令の中の代入を追う():
    """`p=$(pgrep -f X); kill -9 $p`（F10）。代入の断片は 1 文字も変えない。"""
    got = _gate('p=$(pgrep -f "gateway.py"); kill -9 $p')
    assert got is not None
    assert got[0] == 'p=$(pgrep -f "gateway.py"); true', got[0]


def test_R8_代入が自分のサーバなら触らない():
    assert _gate('p=$(pgrep -f "mine_srv.py"); kill -9 $p') is None


def test_R8_変数に_PID_が複数入る形():
    assert _gate(f'PIDS="{MINE} {GUARD}"; kill -9 $PIDS')[0].endswith("true")
    assert _gate(f'PIDS="{MINE}"; kill -9 $PIDS') is None


def test_R8_ループの中では代入を追わない():
    """同じ変数が何度も別の値になるので、覚えると**元の命令が撃たない相手**を断る。
    `p` を覚えないので `$p` は空＝対象なし＝触らない（従来どおり素通し）。"""
    assert _gate(f"p={GUARD}; for p in 1 2; do kill -9 $p; done") is None


@pytest.mark.parametrize("tok", ["$!", "%1", "${p:-9}", "$1", "$$", "$(jobs -p)"])
def test_R8_展開してよい形でなければ触らない(tok):
    assert B._expand_kill_token(tok, {}) is None


def test_R8_展開は環境変数も見る(monkeypatch):
    monkeypatch.setenv("BWR8PID", str(GUARD))
    assert B._expand_kill_token("$BWR8PID", None) == str(GUARD)
    assert B._expand_kill_token("${BWR8PID}", {"BWR8PID": "7"}) == "7"   # 代入が優先


@pytest.mark.parametrize("seg,want", [
    ('p=$(pgrep -f "x")', ("p", '$(pgrep -f "x")')),
    ('P="12 34"', ("P", "12 34")),
    ("kill -9 1", None),
    ("p=1 kill -9 2", None),          # 代入だけの断片ではない
])
def test_R8_単純な代入だけを読む(seg, want):
    assert B._kill_assignment(seg) == want


# ── M-C: 文字列を別のシェルに流し込む綴り ─────────────────────────────
@pytest.mark.parametrize("cmd", [
    'echo "kill -9 {p}" | bash',
    'echo "kill -9 {p}" | sh',
    'echo "kill -9 {p}" | /bin/bash',
    'printf "kill -9 {p}" | bash',
    'source /dev/stdin <<< "kill -9 {p}"',
    '. /dev/stdin <<< "kill -9 {p}"',
])
def test_R8_文字列を流し込む形も同じ門へ(cmd):
    got = _gate(cmd.format(p=GUARD))
    assert got is not None, cmd
    assert "true" in got[0] and str(GUARD) not in got[0], got[0]


def test_R8_流し込む中身が名前撃ちでも門へ():
    got = _gate("""echo 'pkill -9 -f "gateway.py"' | bash""")
    assert got is not None and got[0] == "echo 'true' | bash", got


def test_R8_自分のサーバなら流し込む形でも触らない():
    assert _gate(f'echo "kill -9 {MINE}" | bash') is None


def test_R8_echo_だけなら撃たないので触らない():
    assert _gate(f'echo "kill -9 {GUARD}"') is None


# ── 実記録に在る形: `$(ps … | grep … | awk '{print $2}')` ──────────────
@pytest.mark.parametrize("cmd", [
    """kill -9 $(ps -ef | grep gateway.py | grep -v grep | awk '{print $2}')""",
    """kill -9 $(ps aux | grep "gateway.py" | awk '{print $2}')""",
    """kill -9 `ps -ef | grep gateway.py | awk '{print $2}'`""",
    """kill -9 $(ps -e | grep python3 | awk '{print $1}')""",   # 列が 1 の綴り
])
def test_R8_ps_grep_awk_を解いて巻き添えを断る(cmd):
    got = _gate(cmd)
    assert got is not None, cmd
    assert got[0] == "true", got[0]


def test_R8_ps_grep_awk_が自分だけなら触らない():
    assert _gate(
        """kill -9 $(ps -ef | grep mine_srv | grep -v grep | awk '{print $2}')"""
    ) is None


@pytest.mark.parametrize("body,want", [
    ("""ps -ef | grep gateway.py | awk '{print $2}'""", [GUARD]),
    ("""ps -ef | grep -v gateway.py | awk '{print $2}'""", [MINE, OUT]),
    ("""ps -ef | grep -i GATEWAY.PY | awk '{print $2}'""", [GUARD]),
    ("""ps aux | grep _srv | grep -v other | awk '{print $2}'""", [MINE]),
])
def test_R8_ps_pipe_の照合(body, want):
    assert B._ps_pipe_match_pids(body) == want


@pytest.mark.parametrize("body", [
    # 列が分からない綴り
    """ps -eo pid,comm | grep gateway | awk '{print $1}'""",
    """ps --sort=pid | grep gateway | awk '{print $1}'""",
    # 列の食い違い（`ps -ef` の 1 列目は UID であって PID ではない）
    """ps -ef | grep gateway.py | awk '{print $1}'""",
    """ps -e | grep gateway.py | awk '{print $2}'""",
    # 取り出し方が読めない
    """ps -ef | grep gateway.py | cut -d' ' -f2""",
    """ps -ef | grep gateway.py | awk -F: '{print $2}'""",
    # grep が読めない（引数を取る綴り／BRE と ERE で意味が変わる綴り）
    """ps -ef | grep -m 1 gateway.py | awk '{print $2}'""",
    """ps -ef | grep 'gate\\(way\\)' | awk '{print $2}'""",
    """ps -ef | grep 'gateway+' | awk '{print $2}'""",
    # grep が無い＝全部の PID が出る形（意図が読めない）
    """ps -ef | awk '{print $2}'""",
])
def test_R8_解けない_ps_pipe_は解かない(body):
    assert B._ps_pipe_match_pids(body) is None


def test_R8_egrep_なら_ERE_として読む():
    assert B._ps_pipe_match_pids(
        """ps -ef | egrep 'gate(way)' | awk '{print $2}'""") == [GUARD]


def test_R8_fgrep_は素の文字列として読む():
    assert B._ps_pipe_match_pids(
        """ps -ef | fgrep 'gateway.py' | awk '{print $2}'""") == [GUARD]
    assert B._ps_pipe_match_pids(
        """ps -ef | grep -F 'gatewayXpy' | awk '{print $2}'""") is None


def test_R8_逆引用符の中のパイプで割らない():
    """`_split_segments` は 5 回目から逆引用符を守っていたのに、`_split_pipeline`
    だけ守っていなかった（片方だけ直る形）。3 段に割れると対象が白名簿に
    当たらず `_parse_kill` が None＝門が一度も見ないまま素通しになる。"""
    c = """kill -9 `ps -ef | grep x | awk '{print $2}'`"""
    assert B._split_pipeline(c) == [c]
    assert B._split_pipeline("a | b") == ["a ", "|", " b"]     # 対照
