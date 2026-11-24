r"""第4回c・**7 回目**の反証（`wf-merge-bash-ref1`）で見つかった 19 形を閉じる（2026-09-05）。

★M1（この回の本体）: 6 回目は「照合のずれを閉じた」と看板を出したが、閉じていたのは
  `-i` と `[[:digit:]]` だけだった。実物は **glibc の `regcomp(REG_EXTENDED)`**
  （GNU 拡張つき）で、門は Python の `re`。ctypes で libc を直に叩いた oracle の実測:

      pattern            glibc(実物)                Python re
      bwere_\d+          bwere_ddd                  bwere_123        ← 交わらない
      \<bwere            語頭アンカー（当たる）        素の `<`（当たらない）
      docker-ini\t       素の t（当たる）             タブ（当たらない）
      corri[\d]or        `\` か `d`（当たる）         数字（当たらない）
      killall /abs/path  **実行ファイル**で照合        門は comm と文字列比較

  どれも門の一致が 0 になり、`if not outside: return None` が「巻き添えなし」と
  読んで**黙って素通し**する（結果本文は `(exit 0, no output)`）。実測で
  保護ポートのサーバが死んだ（R01 / R03 / Q05 / R06 / Q07）。
  → **綴りの翻訳をやめ、pkill / killall が呼ぶのと同じ engine を呼ぶ**
    （`_compile_kill_re`）。翻訳表は「表に無い綴りが出るたびに穴が開く」形で、
    5 回目・6 回目・7 回目と 3 回同じ穴を開けた。
  ⚠ **一致 0 は「巻き添えなし」ではなく「分からない」**——これは 6 回目に
    `_name_kill_unreliable` の docstring へ自分で書いた条件そのもので、
    条件は書いたのに**適用する側が狭かった**。

★M2〜M5: 前置き（`function` / `f()` / `coproc`）・末尾の**入力**リダイレクト・
  `trap` / `bash <<<` / `find -exec` / `awk system` / 変数のパターン。
  どれも「門が一度も見ない」形で、実測で保護ポートのサーバが死んだ。
"""
from __future__ import annotations

import os
import sys

import pytest

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


def _gate(cmd: str):
    """本番と同じ順（入れ子 → 名前撃ち → PID 撃ち）。`_run` はこの 3 つを順に当てる。"""
    return B._gate_fragment(cmd, WS, None, 0)


# ── M1 ① 実物と同じ照合器（glibc ERE）────────────────────────────────
@pytest.mark.parametrize("pat,hay,want", [
    (r"bwere_\d+", "bwere_ddd", True),     # ERE の `\d` は素の d
    (r"bwere_\d+", "bwere_123", False),
    (r"\<bwere", "x bwere_1", True),       # GNU の語頭アンカー
    (r"a\tb", "atb", True),                # `\t` は素の t（タブではない）
    (r"a\tb", "a\tb", False),
    (r"c[\d]d", "cdd", True),              # 括弧式の中の `\` は素の文字
    (r"c[\d]d", "c1d", False),
    (r"x[[:digit:]]y", "x7y", True),       # POSIX の文字クラス
])
def test_R7_照合は実物の_ERE_と同じ(pat, hay, want):
    fn, why = B._compile_kill_re(pat, icase=False, exact=False)
    assert why is None, (pat, why)
    assert bool(fn(hay)) is want, (pat, hay)


def test_R7_読めない綴りは理由つきで断る():
    fn, why = B._compile_kill_re("a{2,1}b", icase=False, exact=False)
    assert fn is None and why, why


def test_R7_exact_は_procps_と同じ包み方():
    """`pkill -x` は pgrep.c が `^( … )$` で包んでから regcomp する。"""
    fn, _ = B._compile_kill_re("py|sh", icase=False, exact=True)
    assert fn("py") and fn("sh")
    assert not fn("pyx") and not fn("xsh")


@pytest.mark.parametrize("cmd", [
    r'pkill -9 -f "corri\dor_bridge"',       # 実物は当たる／Python は当たらない
    r'pkill -9 -f "scrip\ts"',
    r'pkill -9 -f "\<corridor_bridge"',
    r'pkill -9 -f "corri[\d]or_bridge"',
])
def test_R7_照合のずれで外の相手を黙って見落とさない(cmd):
    """★6 回目の HEAD はここで `None`（注記ゼロ）を返し、実物が外のサーバを殺した。"""
    r = B._scope_name_kill(cmd, WS)
    assert r is not None, f"素通し（実物は当たる）: {cmd}"
    assert r[0].strip().endswith("true"), r[0]
    assert "corridor_bridge" in r[1]


def test_R7_中にしか当たらない形は今までどおり通す():
    """広げただけではない: 巻き添えが無ければ 1 文字も変えない。"""
    assert B._scope_name_kill(r'pkill -9 -f "myserve\r\.py"', WS) is None


# ── M1 ② killall の `/` つきは**実行ファイル**で照合 ──────────────────
def test_R7_killall_のパスは_exe_で照合する(monkeypatch):
    me = os.stat(sys.executable)
    monkeypatch.setattr(B, "_pid_exe_id",
                        lambda pid: (me.st_dev, me.st_ino) if pid == 101 else (0, 0))
    r = B._scope_name_kill(f"killall -9 {sys.executable}", WS)
    assert r is not None, "comm と文字列比較して一致 0＝素通しになっている"
    assert r[0].strip().endswith("true"), r[0]
    assert "corridor_bridge" in r[1]


def test_R7_exe_を読めないなら分からないとして扱う():
    """FAKE の pid は `/proc/<pid>/exe` を持たない＝当たるか分からない。"""
    r = B._scope_name_kill(f"killall -9 {sys.executable}", WS)
    assert r is not None and "exe" in r[1]


def test_R7_killall_の名前は正規表現として読まない():
    """`killall "a{2,1}b"` は literal 比較。regex として compile して
    「読めない」と断ると、実物が普通に動く形を門が止めることになる。"""
    assert B._scope_name_kill('killall -9 "a{2,1}b"', WS) is None


# ── M2 シェル関数の定義＋呼び出し ─────────────────────────────────
@pytest.mark.parametrize("cmd", [
    'function f { pkill -9 -f "corridor_bridge"; }; f',
    'f() { pkill -9 -f "corridor_bridge"; }; f',
    'g(){ pkill -9 -f "corridor_bridge"; }; g',
    'declare -f 2>/dev/null; function f() { pkill -9 -f "corridor_bridge"; }; f',
    'if true; then function g { pkill -9 -f "corridor_bridge"; }; g; fi',
    'coproc C { pkill -9 -f "corridor_bridge"; }',
])
def test_R7_関数定義の頭を剥がして門に通す(cmd):
    r = _gate(cmd)
    assert r is not None, f"門が一度も見ていない: {cmd}"
    assert "corridor_bridge" in r[1]
    assert "pkill" not in r[0], r[0]


def test_R7_coproc_の名前なしは食い過ぎない():
    """`coproc pkill …`（名前なしの単純命令）で `pkill` を前置きに食うと、
    残りが読めず**素通しに戻る**。"""
    r = B._scope_name_kill('coproc pkill -9 -f "corridor_bridge"', WS)
    assert r is not None and "pkill" not in r[0], r


# ── M3 末尾の**入力**リダイレクト ────────────────────────────────
def test_R7_入力リダイレクトを末尾として剥がす():
    r = B._scope_name_kill('pkill -9 -f "corridor_bridge" 0<&-', WS)
    assert r is not None, "語割りで pats が 2 つになり素通しになっている"
    assert r[0].endswith(" 0<&-"), r[0]


def test_R7_引用符の中の不等号を末尾と読まない():
    """`pkill -f "a <b"` の ` <b"` を切ると引用符が閉じず、`_split_kill_args` が
    None＝**素通し**に戻る（`>` 側は入力側を足す前から在った穴）。"""
    for cmd in ('pkill -9 -f "corridor <bridge"', 'pkill -9 -f "corridor >bridge"'):
        body, tail = B._strip_redirect_tail(cmd)
        assert (body, tail) == (cmd, ""), (cmd, body, tail)
        assert B._parse_name_kill(cmd) is not None


# ── M4 trap / coproc ────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    """trap 'pkill -9 -f "corridor_bridge"' EXIT; true""",
    '''trap "pkill -9 -f corridor_bridge" USR1; true''',
])
def test_R7_trap_の本体を門に通す(cmd):
    r = _gate(cmd)
    assert r is not None, f"門が一度も見ていない: {cmd}"
    assert "pkill" not in r[0], r[0]


# ── M5 別のシェル / find -exec / awk system / 変数のパターン ────────────
@pytest.mark.parametrize("cmd", [
    """bash <<< 'pkill -9 -f "corridor_bridge"'""",
    r'find . -maxdepth 0 -exec pkill -9 -f "corridor_bridge" \;',
    """awk 'BEGIN{system("pkill -9 -f corridor_bridge")}' </dev/null""",
])
def test_R7_別の入口の中身も同じ門へ(cmd):
    r = _gate(cmd)
    assert r is not None, f"門が一度も見ていない: {cmd}"
    assert "pkill" not in r[0], r[0]


def test_R7_変数のパターンは黙って通さない():
    """読めたのは「pkill であること」まで＝**的が分からない**。書き換えないが、
    `(exit 0, no output)` で終わらせない（反証 M5/S14）。"""
    cmd = 'while read -r l; do pkill -9 -f "$l"; done <<< "corridor_bridge"'
    r = _gate(cmd)
    assert r is not None, "黙って素通し"
    assert "分からない" in r[1] or "再現できません" in r[1], r[1]


# ── 計器（標本を足さずに率だけ見ない） ──────────────────────────────
# ── libc が使えない環境の受け皿（**一度も走らない分岐**を作らない） ────────
@pytest.mark.parametrize("pat,gap", [
    (r"a\db", True),          # ERE は素の d・Python は数字
    (r"a\nb", True),          # ERE は素の n・Python は改行
    (r"a[\d]b", True),        # 括弧式の中の `\` は POSIX では素の文字
    (r"a[[.a.]]b", True),     # 照合要素（Python に無い）
    (r"a\<b", True),          # 語頭アンカー（Python は素の `<`）
    (r"a\sb", False),         # GNU 拡張・Python も同じ
    (r"a\.b", False),         # 記号の脱出はどちらも素の文字
    (r"a[[:digit:]]b", False),
])
def test_R7_libc_が無いときの受け皿は意味の違いを見つける(pat, gap):
    assert (B._ere_python_gap(pat) is not None) is gap, (pat, B._ere_python_gap(pat))


def test_R7_libc_が無いときは読めない綴りを断る(monkeypatch):
    monkeypatch.setattr(B, "_ere_lib", lambda: None)
    fn, why = B._compile_kill_re(r"a\db", icase=False, exact=False)
    assert fn is None and why, why
    # 直せる範囲（POSIX の文字クラス）は今までどおり読む
    fn2, why2 = B._compile_kill_re(r"x[[:digit:]]y", icase=False, exact=False)
    assert why2 is None and fn2("x7y") and not fn2("xzy")


def test_R7_libc_が使えている(monkeypatch):
    """★この試験が落ちるなら、上の受け皿ではなく**本番が**受け皿で動いている
    （＝実物と同じ照合器を使えていない）。黙って劣化させないための番人。"""
    assert B._ere_lib() is not None


# ── 自分の監査で見つけた追加の穴: `env -S`（反証には無い） ─────────────
@pytest.mark.parametrize("cmd", [
    'env -S "pkill -9 -f corridor_bridge"',
    "env -S 'pkill -9 -f corridor_bridge'",
    'env -u LANG -S "pkill -9 -f corridor_bridge"',
    'env -S pkill -9 -f corridor_bridge',
])
def test_R7_env_S_の中身も門に通す(cmd):
    """★`env -S` は 1 つの文字列を argv に割って**その先頭を起動する**。
    `-S` を「引数を 1 つ取るオプション」として食っていたので `pkill` が前置きに
    消え、残りが読めず**黙って素通し**していた（実物の env は pkill を起こす）。"""
    r = _gate(cmd)
    assert r is not None, f"門が一度も見ていない: {cmd}"
    assert "pkill" not in r[0], r[0]
