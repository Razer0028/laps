"""W1（Windows デスクトップアプリ・tauri）: 本体が出す事象と、Windows 版が扱う事象の
食い違いを**機械で**突き合わせる。

★なぜ（この現場の実績）: `scripts/check_protocol.py` の冒頭にあるとおり、両側を別々に
  書いたことによる食い違いを**目視で 3 回**外している（握手・道具・依頼）。同じ形が
  2026-09-03〜04 にも起きていた——本体側に増えた

    ・門の印       `[invent ` `[contradiction ` `[search_limit `
    ・notice kind  `ws_guard` / `answer_guard` / `user_stop` / `cancel_applied` /
                   `cancel_error` / `queue_cleared` / `run_start` / `heartbeat`
    ・outcome      `user_stop`
    ・ask の欄     `descriptions`

  のうち、Windows 版が扱えていたのは `heartbeat` だけだった（W1 着手時に実測）。
  端末版（`term/render.py`）と Android（`android/`）には同種の parity 試験があるのに、
  **Windows だけ機械の突き合わせが無かった**＝落ちても誰も気づかない。

構成:
  A. 表の突き合わせ（静的）… Rust の実ソースから表を読み、Python 側の正本と比べる。
  B. 配線の突き合わせ（静的）… 表が「あるだけ」で使われていない、を止める。
     （この現場の最頻の失敗が「書いたが繋いでいない」）
  C. 線の突き合わせ（**動かす**）… 本物の `serve._RunState` に agent の事象を流し、
     線に出た行が Rust の表でどう分類されるかを確かめる。

★Rust を実行しない理由: この Linux 機では `app/src-tauri` が GTK（pango）を要求して
  リンクできない＝`cargo test` が走らない。だから**表と判定そのものは**
  `app/src-tauri/blackwall-core/src/wire.rs` に置いてあり、そちらの 20 本は
  `cargo test --manifest-path blackwall-core/Cargo.toml` で**実際に走る**。
  ここが見るのは「Python 側の正本と、その Rust の表が一致しているか」。
"""
from __future__ import annotations

import io
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from laps import serve as servemod
from laps.term import render as R

SRC = Path(__file__).resolve().parents[1]
# ★2026-09-05（r4c/app-term-clients）: `ROOT = SRC.parent` は素の `/home/user/project/src`
#   で実行すれば `/home/user/project` になり `APP` は本物の `/home/user/project/app`
#   （生きている master）を指す。だが **git worktree（`wt/r4-*`）で実行すると
#   `parents[1]` は worktree のルート（例: `wt/r4-app-term-clients`）なので、
#   `ROOT` は `/home/user/project/wt`、`APP` は `wt/app`——これは `/home/user/project/app`
#   への **symlink**（各 worktree 作成時に張られる。tests-never-run 領域の
#   symlink farm 機構）で、実体は同じ「今の app/master」を指す。つまり
#   worktree で走らせても、この試験が読むのは**このブランチのローカル差分では
#   なく live な app リポジトリ**のまま——`WIRE_RS`/`TRANSLATE_RS` 等が
#   ブランチの変更を一切反映しないのは意図どおり（`_KNOWN_PENDING_MERGE` の
#   前にある注記、および REPORTS/app-term-clients-merge-order.md §1 も参照）。
ROOT = SRC.parent
AGENT_PY = SRC / "laps" / "agent.py"
SERVE_PY = SRC / "laps" / "serve.py"
APP = ROOT / "app"
SIDECAR_RS = APP / "src-tauri" / "src" / "sidecar.rs"
WIRE_RS = APP / "src-tauri" / "blackwall-core" / "src" / "wire.rs"
# ★翻訳器（interpret / Signal / Fold）は 2026-09-04 に blackwall-core へ移した
#   （sidecar.rs に置いたままでは試験が**一度も走らない**ため。translate.rs 冒頭を参照）。
TRANSLATE_RS = APP / "src-tauri" / "blackwall-core" / "src" / "translate.rs"
APP_JS = APP / "ui" / "app.js"
STYLES_CSS = APP / "ui" / "styles.css"


# ---------------------------------------------------------------------------
# Rust の実ソースから表を読む小道具
# ---------------------------------------------------------------------------

def _wire() -> str:
    return WIRE_RS.read_text(encoding="utf-8")


def _sidecar() -> str:
    return SIDECAR_RS.read_text(encoding="utf-8")


def _translate() -> str:
    return TRANSLATE_RS.read_text(encoding="utf-8")


def _windows_side() -> str:
    """Windows 版の受け口ぜんぶ（配線は sidecar、翻訳は translate に分かれている）。"""
    return _sidecar() + "\n" + _translate()


def _rust_str_list(name: str) -> list[str]:
    """`pub const NAME: &[&str] = &[ "a", "b" ];` を読む。"""
    src = _wire()
    # ★`];` までを非貪欲に。1 行で書かれた表（CANCEL_KINDS 等）を取りこぼさない
    #   ——最初これを `\n];` で書いて、**別の表の中身を丸ごと読んで**「一致しない」と
    #   騒いだ（試験の抽出が壊れると、直っているものが赤く出る）。
    m = re.search(rf'const {name}: &\[&str\] = &\[(.*?)\];', src, re.S)
    assert m, f"{name} が {WIRE_RS} に無い（表の名前が変わった？）"
    body = re.sub(r"//[^\n]*", "", m.group(1))          # 行コメントを落とす
    return re.findall(r'"((?:[^"\\]|\\.)*)"', body)


def _rust_pairs(name: str) -> dict[str, str]:
    """`pub const NAME: &[(&str, &str)] = &[ ("k", "v"), ];` を読む。"""
    src = _wire()
    m = re.search(rf'const {name}: &\[\(&str, &str\)\] = &\[(.*?)\n\];', src, re.S)
    assert m, f"{name} が {WIRE_RS} に無い（表の名前が変わった？）"
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return dict(re.findall(r'\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)', body))


# ---------------------------------------------------------------------------
# A. 表の突き合わせ
# ---------------------------------------------------------------------------

def _agent_gate_marker_prefixes() -> dict[str, str]:
    """agent.py の `_push_back_on_*`（完了の主張を差し戻す門）が出す `[<name> ` 印。

    ★`test_protocol_parity.py` と同じ抽出を意図的に**もう一度**書いている。
      あちらは Android 用で、共有すると片方の都合で抽出条件が変わったとき
      両方の意味が同時に変わる（この現場は「検知器を直したのに本番は直っていない」を
      踏んでいるので、ここは**独立に**壊れてほしい）。
    """
    src = AGENT_PY.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for m in re.finditer(r'\n    def (_push_back_on_\w+)\(self', src):
        start = m.end()
        nxt = re.search(r'\n    def ', src[start:])
        body = src[start: start + nxt.start()] if nxt else src[start:]
        fm = re.search(r'f"\[(\w+)[ }]', body)
        if fm:
            out[fm.group(1)] = f"[{fm.group(1)} "
    return out






def test_restart_marks_match_the_terminal_exactly():
    """「下書きを捨てる」印の表は端末版と 1 文字も違わないこと。

    ★ここがずれると壊れ方が静か: 捨て**すぎる**と正しい答えが消え、
      捨て**なさすぎる**と差し戻し前の下書きが残って同じ塊が 2 回出る
      （後者を 2026-09-03 に端末版で実機報告している）。
    """
    assert set(_rust_str_list("RESTART_MARKS")) == set(R.Renderer._RESTART_MARKS)


def test_restart_kinds_match_the_terminal_exactly():
    """種類で「下書きを捨てる」と決まる通知（崩れのやり直し・強制割り込み・答えの書き直し）も端末版と同じ表であること（2026-10-13）。"""
    assert set(_rust_str_list("RESTART_KINDS")) == set(R.Renderer._RESTART_KINDS)


def test_gate_marks_match_the_terminal_exactly():
    assert set(_rust_str_list("GATE_MARKS")) == set(R.Renderer._GATE_MARKS)


def test_restart_marks_are_a_subset_of_gate_marks():
    """捨てるほど重い印が、目立たない側にあってはいけない。"""
    assert set(_rust_str_list("RESTART_MARKS")) <= set(_rust_str_list("GATE_MARKS"))


#: Windows 版**だけ**が作る outcome（本体は出さない）。表の差はここに書いた分だけ許す。
_APP_ONLY_OUTCOMES = {
    # 本体が STOP_GRACE 内に done を返さず、アプリ側で閉じたとき（sidecar.rs run_loop）。
    "cancelled_grace",
}


def test_outcome_table_matches_the_terminal():
    """`done` の outcome → 画面の言葉。端末・Android と**同じ表**（W1 の指示）。

    ★着手時の実測: sidecar.rs は「正常な 5 種」の一覧しか持たず、それ以外を
      **内部識別子のまま**「終わり方: max_iters」と出していた。同日足された
      `user_stop`（利用者が終了）はその一覧に無く、**利用者の意思どおりの終了が
      「変わった終わり方」として赤く出る**状態だった。
    """
    rust = _rust_pairs("OUTCOME_JA")
    term = dict(R._OUTCOME_JA)
    extra = set(rust) - set(term)
    assert extra <= _APP_ONLY_OUTCOMES, f"Windows だけにある outcome: {sorted(extra - _APP_ONLY_OUTCOMES)}"
    missing = sorted(set(term) - set(rust))
    assert not missing, f"Windows の表に無い outcome: {missing}"
    for k, v in term.items():
        assert rust[k] == v, (k, "端末=" + v, "Windows=" + rust[k])


def test_accepted_outcomes_match_the_terminal():
    assert set(_rust_str_list("ACCEPTED")) == set(R._ACCEPTED)
    assert "user_stop" in _rust_str_list("ACCEPTED"), "利用者の終了が失敗扱いになる"


def test_no_outcome_word_leaks_an_internal_identifier():
    """画面へ出る語が英語の識別子のままになっていないこと（項目 17）。"""
    for k, v in _rust_pairs("OUTCOME_JA").items():
        assert v == "" or not v.isascii(), f"outcome {k} の語が識別子のまま: {v!r}"


# ---------------------------------------------------------------------------
# serve が出す notice kind をすべて知っているか
# ---------------------------------------------------------------------------

# ★2026-09-05（r4/app-term-clients）: 抽出ロジック本体は `_protocol_kinds.py` へ
#   切り出した（term 側の同種試験 `test_term_render.py` と共有するため）。
#   ここでは元の名前 `_serve_notice_kinds` のまま使えるよう再輸出する。
from ._protocol_kinds import serve_notice_kinds as _serve_notice_kinds


def _windows_known_kinds() -> set[str]:
    return (set(_rust_str_list("QUIET_KINDS"))
            | set(_rust_str_list("GATE_KINDS"))
            | set(_rust_str_list("PLAIN_KINDS"))
            | set(_rust_str_list("CANCEL_KINDS")))


# ★r4b/app-term-clients（第4回反証13・15への是正）: この試験は `WIRE_RS`
#   （`APP / "src-tauri" / …` = `wt/app` = **各リポジトリの現在の master**、
#   このワークツリーのローカル変更ではない）を読む。app リポジトリで
#   `r4/app-term-clients`（wire.rs の PLAIN_KINDS に "subagent" を足すコミット）が
#   master へ入るまでは、この試験は**正しく赤い**——「バグ」ではなく
#   「まだマージされていないクロスリポジトリの依存」。
#
#   第4回の申告は、これを「worktree 構造上 sibling dir が無く実行不可」という
#   **事実に反する理由**で除外し、実際には走らせれば分かるこの1件の赤を隠していた
#   （反証13・15が指摘・症状は実物で再現確認済み）。
#
#   ★ここでの是正は「skip して隠す」でも「_KNOWN_UNCLASSIFIED_GAP のような
#   バッファへ subagent を放り込んで恒久的に緑にする」でもない
#   （後者は反証13 must_fix (b) が名指しした「一時退避」の恒久化リスクそのもの）。
#   代わりに: 差分が**この既知の1件だけ**なら、依存関係を名指しした
#   `pytest.fail` で赤のまま止める（CI 上「実行していない」ことにしない）。
#   差分にこれ以外の kind が1つでも混ざれば、それは新しい・未知の回帰なので
#   従来どおりの一般的な失敗文言に切り替わる（バッファが growing すり抜け穴に
#   ならない）。詳細・マージ順序は REPORTS/app-term-clients-merge-order.md。
_KNOWN_PENDING_MERGE: dict[str, str] = {
    "subagent": (
        "app リポジトリの r4/app-term-clients（wire.rs の PLAIN_KINDS へ "
        '"subagent" を足すコミット。この worktree では commit ff0a504）が、'
        "その親 r4/tests-never-run（workspace.rs の blackwall-core 移設。"
        "commit d0b1ce5）ごと app/master へマージされるまで、この kind は"
        f" {WIRE_RS} に無いまま——CI で毎回赤くなるのは意図どおり。"
        "release chain は r4/tests-never-run → r4/app-term-clients の順で"
        "app リポジトリへマージすること（tests-never-run 側の反証16も同じ"
        "依存を指摘している）。term 側（laps/term/render.py）は既に "
        "subagent の ✗ 判定を持っており、Windows 側だけがこの依存待ち。"
    ),
}


def test_windows_knows_every_notice_kind_the_core_emits():
    """本体が出す `kind` に、Windows 側が知らないものが無いこと。

    ★知らなくても**捨てはしない**（`Level::Unknown` として「不明な事象」で出す）が、
      それは最後の網であって設計ではない。増えた kind をここで気づけるようにする。
    """
    unknown = sorted(_serve_notice_kinds() - _windows_known_kinds())
    if unknown and set(unknown) <= set(_KNOWN_PENDING_MERGE):
        detail = "\n".join(f"  - {k}: {_KNOWN_PENDING_MERGE[k]}" for k in unknown)
        pytest.fail(
            "Windows がまだ知らない notice kind がありますが、これは新規の回帰では"
            "なく**マージ順序待ちの既知の依存**です（skip はしません——依存が解消"
            "されて緑になったことも、これ以外の kind が新たに紛れ込んだことも、"
            "この試験自身で機械に確かめさせるため）:\n" + detail,
            pytrace=False,
        )
    assert not unknown, (
        f"Windows が知らない notice kind: {unknown} — "
        f"{WIRE_RS} の QUIET_KINDS / GATE_KINDS / PLAIN_KINDS のどれかに足す"
        "（_KNOWN_PENDING_MERGE に無い＝これは新規の回帰の可能性が高い）"
    )


# ★r4b/app-term-clients: 上の分岐そのものが空虚でないことを確かめる（現在の
#   git 状態に依存せず、両方向をモンキーパッチで再現する）。
def test_known_pending_merge_produces_an_explicit_fail_not_a_skip(monkeypatch):
    """既知の依存（subagent だけ）が残っているあいだ: fail する。skip はしない。"""
    monkeypatch.setattr(
        "tests.test_windows_parity._serve_notice_kinds", lambda: {"subagent", "harness"}
    )
    monkeypatch.setattr(
        "tests.test_windows_parity._windows_known_kinds", lambda: {"harness"}
    )
    with pytest.raises(pytest.fail.Exception) as exc_info:
        test_windows_knows_every_notice_kind_the_core_emits()
    msg = str(exc_info.value)
    assert "マージ順序待ちの既知の依存" in msg
    assert "subagent" in msg
    # ★outcome が「fail」であって「skip」でないことも確かめる（別の例外型）。
    assert exc_info.typename != "Skipped", "skip で隠してはいけない（明示的な失敗にする）"


def test_a_second_unknown_kind_falls_back_to_the_generic_failure(monkeypatch):
    """subagent 以外に**もう1つ**未知の kind が混ざったら、バッファ扱いにしない
    （_KNOWN_PENDING_MERGE がなんでも飲み込む恒久バッファへ育たないことの証拠）。"""
    monkeypatch.setattr(
        "tests.test_windows_parity._serve_notice_kinds",
        lambda: {"subagent", "brand_new_kind_nobody_classified_yet"},
    )
    monkeypatch.setattr("tests.test_windows_parity._windows_known_kinds", lambda: set())
    with pytest.raises(AssertionError) as exc_info:
        test_windows_knows_every_notice_kind_the_core_emits()
    msg = str(exc_info.value)
    assert "brand_new_kind_nobody_classified_yet" in msg
    assert "新規の回帰の可能性が高い" in msg


def test_when_the_dependency_lands_the_gate_is_fully_green(monkeypatch):
    """app リポジトリが r4/tests-never-run → r4/app-term-clients を取り込んだ後
    （＝wire.rs が subagent を知っている）は、他に unknown が無ければ普通に緑。"""
    monkeypatch.setattr(
        "tests.test_windows_parity._serve_notice_kinds", lambda: {"subagent", "harness"}
    )
    monkeypatch.setattr(
        "tests.test_windows_parity._windows_known_kinds", lambda: {"subagent", "harness"}
    )
    test_windows_knows_every_notice_kind_the_core_emits()      # 例外を投げなければ合格


def test_the_gates_the_core_pushes_back_with_are_prominent_kinds():
    """`ws_guard` / `answer_guard` / `verifier` / `red_tests` は失敗の報せ＝目立つ側。"""
    gate = set(_rust_str_list("GATE_KINDS"))
    for k in ("ws_guard", "answer_guard", "verifier", "red_tests",
              "cancel_error", "hook_error", "handoff_gap", "phase_failed"):
        assert k in gate, f"{k} が薄い注記に埋もれる"


def test_kind_tables_do_not_overlap():
    """同じ kind が 2 つの表にあると、直した側と直していない側が生まれる。"""
    q = set(_rust_str_list("QUIET_KINDS"))
    g = set(_rust_str_list("GATE_KINDS"))
    p = set(_rust_str_list("PLAIN_KINDS"))
    c = set(_rust_str_list("CANCEL_KINDS"))
    assert not (q & g) and not (q & p) and not (g & p)
    assert not (c & (q | g | p))


# ---------------------------------------------------------------------------
# B. 配線（表があるだけで使われていない、を止める）
# ---------------------------------------------------------------------------

def test_sidecar_uses_the_shared_tables_instead_of_its_own_list():
    """★この現場の最頻の失敗「書いたが繋いでいない」を機械で止める。

    着手時の sidecar.rs は印の一覧を `m.starts_with("[fabrication ") || ...` と
    **その場に直書き**していた。だから本体側に門が増えても誰も気づかない。
    """
    assert '#[path = "../blackwall-core/src/wire.rs"]' in _sidecar(), "共有の表を取り込んでいない"
    assert '#[path = "../blackwall-core/src/translate.rs"]' in _sidecar(), "翻訳器を取り込んでいない"
    s = _windows_side()
    # ★境界つきで見る。素の `in` は `wire::notice_viewX` のような**別物の一部**にも
    #   当たって素通りする（変異試験で実際に素通りした）。
    def _calls(name: str) -> bool:
        return re.search(rf"\bwire::{name}\b", s) is not None
    assert _calls("discards_draft"), "下書きを捨てる判定が共有の表を通っていない"
    assert _calls("notice_view"), "notice の分類が共有の表を通っていない"
    assert _calls("outcome_line"), "outcome の語が共有の表を通っていない"
    assert _calls("CANCEL_KINDS"), "中断の連鎖が 1 本にまとまっていない"
    # 直書きの一覧が復活していないこと（これが戻ると表は飾りになる）。
    assert 'm.starts_with("[fabrication ")' not in s, "印の直書きが復活している"
    assert "const 正常:" not in s, "outcome の直書き一覧が復活している"


def test_no_message_notice_is_not_silently_dropped():
    """★着手時の実測（sidecar.rs:535）: `message`/`text` が無い notice を
    `None => vec![]` で落としていた。本体は `cancel` / `cancel_applied` /
    `cancelled` / `run_start` / `shutdown` を**文言なしで**出すので、
    「止める」を押した後の画面には本体からの合図が 1 行も出ていなかった。
    """
    s = _translate()
    # ★`notice` の分岐だけを見る。`token` / `think` の `None => vec![]` は正しい
    #   （文字が無い断片は出しようがない）——最初これを全文検索で書いて、
    #   関係のない分岐に当たって赤くなった。**探す範囲を間違えると、直っていても落ちる。**
    i = s.find('        "notice" => {')
    assert i > 0, "notice の分岐が見つからない（sidecar.rs の書式が変わった？）"
    arm = s[i: s.find("\n        }\n", i)]
    assert "vec![]" not in arm, "文言の無い notice をまた黙って捨てている"
    assert re.search(r"\bwire::notice_view\b", arm), "分類が共有の表を通っていない"
    assert "Signal::Unknown" in _windows_side(), "不明な事象を出す口が無い"
    # 未知の type（`Signal::Ignore`）も数えるだけで終わらせない。
    assert "unknown_note(app" in _sidecar(), "不明な事象が画面に出ていない（stderr だけ）"


def test_unknown_events_reach_the_screen_not_only_stderr():
    """未知の type / kind / outcome が**画面**に出ること（表示・計数の両方）。"""
    s = _windows_side()
    # 数える（journal / 診断）
    assert "unknown.entry" in s
    # 出す（画面）
    assert 'app.emit(\n        "chat-notice"' in s or '"chat-notice"' in s
    js = APP_JS.read_text(encoding="utf-8")
    assert 'is-unknown' in js, "画面に不明な事象の描き分けが無い"
    css = STYLES_CSS.read_text(encoding="utf-8")
    # ★規則の**本体**があること。`::before` だけ残っていて基の規則が消えていても
    #   素の `in` は通ってしまう（変異試験で素通りした）。
    m = re.search(r"\.notice\.is-unknown\{(.*?)\}", css, re.S)
    assert m, "不明な事象の見た目が定義されていない"
    assert "border-left-style" in m.group(1) or "background" in m.group(1), m.group(1)


def test_gate_notices_are_visually_prominent():
    """門の印は左線と色で立てる（薄い灰色に混ぜない）。

    ★この現場は「失敗の文字が AA 未満」「失敗の左線が CSS 未定義で存在しない」を
      実機で踏んでいる（2026-09-03 の 128 体監査）。定義があることを機械で見る。
    """
    js = APP_JS.read_text(encoding="utf-8")
    assert "is-gate" in js, "画面が門を描き分けていない"
    assert 'ev.payload?.level' in js, "Rust が送る level を画面が読んでいない"
    css = STYLES_CSS.read_text(encoding="utf-8")
    m = re.search(r"\.notice\.is-gate\{(.*?)\}", css, re.S)
    assert m, ".notice.is-gate が CSS に無い（画面には class だけ付いて何も変わらない）"
    body = m.group(1)
    assert "border-left-color" in body and "--red" in body, body
    assert re.search(r"\.notice\.is-gate::before\{[^}]*content", css), "門に印が無い"






def test_cancel_chain_is_collapsed_into_one_line():
    """中断は 1 本にまとめ、10 秒で「まだ止まっていない」を言うこと（W1 項目 3）。"""
    w = _wire()
    assert "pub struct CancelTrack" in w
    assert "CANCEL_STALL_SECS" in w
    # 端末版と同じ物差し（別々の秒数で同じ出来事を測らない）。
    from laps.term import app as termapp
    m = re.search(r"pub const CANCEL_STALL_SECS: u64 = (\d+);", w)
    assert m, w[:200]
    assert int(m.group(1)) == int(termapp.CANCEL_STALL_SECS), (
        f"Windows={m.group(1)}s / 端末={termapp.CANCEL_STALL_SECS}s — "
        "同じ出来事を別の物差しで測っている"
    )
    s = _sidecar()
    assert "fold.cancel.stall(" in s, "「まだ止まっていない」が配線されていない"
    assert "fold.cancel.announce_locally()" in s, "アプリ自身の 1 行が二重表示を作る"


# ---------------------------------------------------------------------------
# C. 線の突き合わせ（本物の serve._RunState を動かす）
# ---------------------------------------------------------------------------

@dataclass
class _FakeSession:
    messages: list


@dataclass
class _FakeAgent:
    session: _FakeSession


class _FakeSrv:
    """`_RunState` が触る欄だけを持つ殻。`out` は**本物の** `serve.Emitter`。"""

    def __init__(self, stream: io.StringIO) -> None:
        self._lock = threading.Lock()
        self._agent: _FakeAgent | None = None
        self.out = servemod.Emitter(stream)


def _run_state(stream: io.StringIO):
    srv = _FakeSrv(stream)
    spec = servemod.SendSpec(id="r1", prompt="x")
    return servemod._RunState(srv, spec), srv


def _wire_lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(l) for l in stream.getvalue().splitlines() if l.strip()]


def _fire_gate_marker(marker_text: str) -> list[dict]:
    """`_push_back_on_*` が実際にする順番を再現する（agent.py:3561-3570 と同じ）。"""
    stream = io.StringIO()
    state, srv = _run_state(stream)
    srv._agent = _FakeAgent(session=_FakeSession(messages=[]))
    real = "作業は完了しました。"
    srv._agent.session.messages.append({"role": "assistant", "content": real})
    state.on_agent_event("assistant", {"text": real, "iter": 1})
    state.on_agent_event("assistant", {"text": marker_text, "iter": 1})
    state.flush()
    return _wire_lines(stream)


_LIVE_GATES = {
    "verifier": "[verifier 1] 完了の主張と作業場の実体が食い違う",
    "red_tests": "[red_tests 1] 最後のテストが 2 件失敗のまま完了にしない",
    "rerun": "[rerun 1] 合格を 1 回しか見ていない試験（証拠 [3]）を再現してから締める",
    "host_scope": "[host_scope 1] 依頼は 192.0.2.160 の話だが、触れた道具呼び出しが 0 回",
    "invent": "[invent 1] 作業場に無い対象を作った",
    "contradiction": "[contradiction 1] 前の発言と食い違う",
}




@pytest.mark.parametrize("gate", ["verifier", "red_tests", "rerun", "invent", "contradiction"])
def test_completion_gates_discard_the_draft_on_windows(gate):
    """完了の主張を差し戻す門は、Windows 側でも前の下書きを捨てること。
    ★捨てないと「完了しました」が差し戻し後の答えと並んで 2 回見える。"""
    text = _LIVE_GATES[gate]
    assert any(text.startswith(m) for m in _rust_str_list("RESTART_MARKS")), gate


def test_host_scope_is_prominent_but_does_not_discard_the_draft():
    """host_scope は「見落とすと痛い」が、下書きを捨てる門ではない（端末版と同じ切り分け）。"""
    text = _LIVE_GATES["host_scope"]
    assert any(text.startswith(m) for m in _rust_str_list("GATE_MARKS"))
    assert not any(text.startswith(m) for m in _rust_str_list("RESTART_MARKS"))






def test_every_type_serve_emits_is_handled_by_the_sidecar():
    """`serve.py` が出す **type** に、sidecar.rs の `interpret` が知らないものが無いこと。

    ★`scripts/check_protocol.py` が同じことをするが、あちらは手で走らせる台本で
      preflight にも pytest にも入っていない（＝走らせなければ気づけない）。
      静的な半分だけ、ここへ写して**必ず走る側**に置く。
    """
    src = SERVE_PY.read_text(encoding="utf-8")
    emits = set(re.findall(r'_emit\(\s*"([a-z_]+)"', src)) | set(re.findall(r'emit\(\s*"([a-z_]+)"', src))
    s = _translate()
    start = s.find("pub fn interpret(")
    assert start > 0
    body = s[start: s.find("\n}\n", start)]
    handled: set[str] = set()
    for line in body.splitlines():
        m = re.match(r'^\s*((?:"[a-z_]+"\s*\|\s*)*"[a-z_]+")\s*(?:if .*?)?=>', line)
        if m:
            handled |= set(re.findall(r'"([a-z_]+)"', m.group(1)))
    missing = sorted(emits - handled)
    assert not missing, f"本体が出す type を Windows が知らない（黙って捨てる）: {missing}"
