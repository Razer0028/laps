"""wt/ 直下のシンボリックリンク（bench/scripts/android/app/…→live）が何をしているかを
機械で固定する門（2026-09-05・r4b/tests-never-run・critic §2-6）。

★背景（実測。/home/user/project で実際に確かめた）: このリポのレイアウトは `src/` が
  ネストした git リポで、`bench/`・`scripts/`・`packaging/`・`dist/`・`android/`・
  `app/`・`.venv/`・`ACCEPTANCE.md`・`WORKLOG.md` は `src/` の**兄弟**（1つ上の
  階層）にある。この構造のため、多数の試験・道具が
  `Path(__file__).resolve().parents[2]` で1つ上（＝リポ本体の親）へ出て、
  そこから兄弟ディレクトリを掴む——**単独チェックアウト（/home/user/project/src）で
  走らせる分にはこれで正しい**（`parents[2]` は `/home/user/project` を指す）。

  ところが第4回の並列レビューで `wt/r4-<key>/` という **src の git worktree** が
  何本も作られた。worktree はそれ自身の git 管理ファイルしか持たないので、
  worktree の中の試験にとって `parents[2]` は `wt/`（何も無い）を指してしまい、
  下の ALLOWLIST の全ファイルが軒並み FileNotFoundError になる――そこで
  `wt/` 直下に `bench`・`scripts`・`android`・`app`・`packaging`・`dist`・`.venv`・
  `src`・`ACCEPTANCE.md`・`WORKLOG.md` への**シンボリックリンク**（live の実体へ）が
  用意されている。これは事故ではなく、`parents[2]` パターンを worktree でも
  動かすための必然の適応である。

  ★だがこの farm は**全 worktree で共有された 1 つの可変資源**である。ある worker が
  `app/`・`scripts/`・`android/` の共有チェックアウトを別ブランチへ `git checkout`
  する（反証 idx16 が実測: reflog で app が 21 分・scripts が 5 分、他ブランチの
  ままだった）と、**その間 `wt/` 配下のどの worktree で `parents[2]` 経由の試験を
  走らせても、同じ「よそ見中」の状態を読む**。worktree ごとに隔離されているように
  見えて、この兄弟ディレクトリ分だけは隔離されていない――複数 worker が同時に
  `parents[2]` 経由の試験を走らせると、片方の書き換え中の状態をもう片方が読む形が
  構造的に起こりうる。

★2026-09-05 r4c/tests-never-run（反証 verdict10 must_fix#1）: 上の柵は
  `Path(__file__).resolve().parents[N]`（角括弧の書き方）**1つ**しか見ていなかった。
  同じ「farm 越しに live の兄弟階層を読む」形は、書き方を変えるだけで素通りする
  ことが実測で分かっている――(a) `.parent.parent.parent` のような `.parent` を
  N 回チェーンする書き方（`bench/fullcode_cases.py:25`）、(b) `parents[N]` を経由
  せず**絶対パス文字列 `/home/user/project/...` を直書き**する書き方（自リポの
  `/home/user/project/src` 自身を指す場合を除く。`tests/test_outcome_words_parity.py:9`・
  `tests/test_skill_injection.py:20` ほか）。どちらも worktree の外／farm の外の
  live を直接指すので、この柵はこの2つも数え上げる。

この門がすることは2つ:
  1. 【固定】兄弟階層（自分のリポの外）へ出ている箇所を、書き方ごとに
     `tests/`・`bench/`・`blackwall/` 全体から静的に数え上げ、既知の一覧
     （本ファイル末尾の ALLOWLIST）と一致することを確かめる。数え上げる書き方は
     3つ: ①`.parents[N]`（N≥2、角括弧）②`.parent` を3回以上チェーン（`.parent.parent.parent`
     以上＝角括弧の `parents[2]` 以上と同じ深さ）③文字列リテラルの絶対パス
     `/home/user/project/...`（末尾が `src` そのものを指す場合を除く）。**新しく増えたら、
     この試験がまず落ちる**——気づかずに増える形を防ぐ。ALLOWLIST に無いファイルの
     文字列だけを対象にした、この3つの書き方**以外**（例: 環境変数・関数引数経由で
     組み立てたパス）は、この静的走査の対象外＝この試験だけが検出手段ではない
     ことに注意。
  2. 【実証】farm と同じ形（1つ上の階層にシンボリックリンクを置く）を tmp に
     再現し、「farm の実体（live）を書き換えると、farm 経由で読む側は書き換え後を
     読む」ことを実際に動かして確かめる（＝この危険が仮説でなく構造であることの、
     実リポの現在の状態に依存しない再生可能な証拠）。

★この門は farm を撤去しない。撤去すると ALLOWLIST の全ファイルの `parents[N]` が
  軒並み壊れる（bench/scripts/android/app/.venv への到達手段が無くなる）――
  単一 worker の判断で決めてよい範囲を超える、統合時の判断事項。撤去したい場合の
  検討手順は `scratch/r4-proposed/tests-never-run/wt-symlink-removal-procedure.md`
  に書いた（このセッションでは実行していない）。
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]  # このリポ自身の root（常にこの worktree/checkout）

#: 「兄弟階層（parents[N]、N≥2 相当）へ出ている」箇所の一覧
#: （2026-09-05 r4c 時点で3つの書き方すべてを数え上げ済み）。
#: 増えた・消えたときは、まずここが理由付きで更新されること
#: （気づかず増える／消えるのを防ぐのがこの門の目的なので、通すために消すのではなく、
#:  本当に要らなくなったことを確かめてから消すこと）。
ALLOWLIST: dict[str, list[int]] = {
    "bench/fullcode_cases.py": [25],  # ①`.parent.parent.parent`（2026-09-05 r4c 追加・must_fix#1）
    "bench/longtask/check.py": [17],  # ③絶対パス文字列（.venv/bin/python・同上）
    "bench/longtask/check2.py": [13],  # ③同上
    "bench/longtask/check3.py": [28],  # ③同上
    "bench/postedit_ab.py": [39],
    "bench/postedit_demo.py": [34],
    "bench/real_tasks.py": [365],
    "bench/replay_edit_cascade.py": [16],  # ③絶対パス文字列（scratch/vdirtest のトレース）
    "bench/replay_tool_caps.py": [32],  # ③同上（pathlib.Path 経由）
    "bench/replay_kill_gates.py": [27],  # 第4回c 統合で増えた（wt/ farm 経由で live を読む）
    "bench/replay_kill_scope.py": [29],  # 第4回c 統合で増えた（wt/ farm 経由で live を読む）
    "bench/replay_server_lifecycle.py": [33],  # 第4回c 統合で増えた（wt/ farm 経由で live を読む）
    "laps/gateway.py": [59],
    # ★2026-10-18 追加（門が赤のまま増えていた分を確かめて載せた）:
    "laps/oc/prompt.py": [180],  # ①`.parent.parent.parent`＝src/PROMPTS（前置きの本文。配った先は LAPS_PROMPT_DIR で指す）
    "tests/conftest.py": [235],  # ①`.parents[2]`＝リポジトリの根（ラボの作業木かを scratch/ の有無で見る）
    "tests/test_prompt_audit_20261016.py": [23],  # ①`.parents[2]`＝リポジトリの根（AGENTS.md 等を読む）
    "tests/test_serve_audit_20261016.py": [26],  # ①`.parents[2]`＝リポジトリの根（src/ を足す）
    "tests/test_deb_packaging.py": [18],
    "tests/test_outcome_words_parity.py": [9],  # ③絶対パス文字列（live android を直読み・2026-09-05 r4c 追加）
    "tests/test_protocol_parity.py": [38],
    "tests/test_redact.py": [479],
    "tests/test_release_git_state.py": [31],
    "tests/test_release_platform_parity.py": [24],
    "tests/test_release_route_gates.py": [38],
    "tests/test_skill_injection.py": [20],  # ③絶対パス文字列（live scratch/vdirtest・2026-09-05 r4c 追加）
    "tests/test_term_app.py": [748, 768],  # ③同上（fake-workspace 値。実読みではないが文字列としては同形）
    "tests/test_update_chain.py": [33],
    # ★2026-09-09: `bench/` を版管理下（src/bench）へ移したので、走査の視界に入った。
    #   5 本とも `/home/user/project/scratch/labbench/...` の**絶対パス直書き**で、
    #   `__file__` から組み立てていない＝移動の影響を受けない（floorlib の REPO とは別の型）。
    #   labbench の走行記録は版管理外の実データなので、絶対パスのままでよい。
    # ★2026-09-09: S3 の門は scripts/ と src/deploy/ の**両方**を見るので、リポジトリの根の
    #   さらに上（兄弟階層）へ出る。門そのものが兄弟をまたぐ設計なので、これは想定どおり。
    "tests/test_s3_gate_wired.py": [6],
    "bench/adoption_report.py": [37],
    "bench/count_local_as_remote.py": [67],
    "bench/premise_report.py": [28],
}

#: 統合予定だが**このチェックアウトにはまだ存在しないファイル**の事前承認
#: （2026-09-05 r4c・must_fix#2）。`r4/vdir-fidelity`（未統合の別ブランチ）が
#: `tests/test_vdir_phase_fidelity.py:37` に `parents[2]` を持ち込む予定
#: （critic r4b §3 第2群の手順）。ここに載せておけば、統合の瞬間にこの試験が
#: 予期せず赤くなることはない――ただし統合後の行番号がここと違えば
#: （vdir-fidelity 側が指す行が動いた／env・明示パスに直して parents[2] 自体が
#: 消えた等）、それはそれで検出対象になる（下の `_pending_paths` を参照）。
#: vdir-fidelity が env/明示パスに直せばこのエントリは丸ごと不要になる。

_PARENTS_BRACKET = re.compile(r"\.parents\[(\d+)\]")
_PARENT_CHAIN = re.compile(r"\.parent(?:\.parent){2,}")
_ABS_LITERAL = re.compile(r"""["']\s*/home/user/project/(?!src(?:["'/]|$))""")


def _scan(path: Path) -> list[int]:
    """path の中で「兄弟階層」へ出ている行番号（書き方①②③のいずれか）。

    ① `.parents[N]`（N≥2）　② `.parent` を3回以上チェーン
    ③ 文字列リテラルの絶対パス `/home/user/project/...`（`/home/user/project/src` 自身は除く）
    """
    hits: set[int] = set()
    for i, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        for m in _PARENTS_BRACKET.finditer(line):
            if int(m.group(1)) >= 2:
                hits.add(i)
        if _PARENT_CHAIN.search(line):
            hits.add(i)
        if _ABS_LITERAL.search(line):
            hits.add(i)
    return sorted(hits)


#: この試験ファイル自身は除外する。冒頭の説明文に用語として「parents[2]」が
#: 出てくる（散文であって兄弟階層を読む実コードではない）のと、下の実証テストが
#: **完全に隔離された tmp フィクスチャ**の中で `parents[2]` を計算する（実リポの
#: bench/scripts/android 等には一切触れない）ので、どちらも「新たに増えた本物の
#: 兄弟階層アクセス」ではない。
_SELF = Path(__file__).resolve().relative_to(REPO)


def _scan_repo() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for base in ("tests", "bench", "laps"):
        d = REPO / base
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            if f.resolve() == REPO / _SELF:
                continue
            hits = _scan(f)
            if hits:
                found[str(f.relative_to(REPO))] = hits
    return found


def test_兄弟階層へ出る箇所は一覧と一致する():
    """新しく増えた・消えた兄弟階層アクセスを見つける（本ファイル冒頭の理由を参照）。

    数え上げる書き方は3つ（`_scan` 参照）: ①`.parents[N]`（N≥2）②`.parent` を
    3回以上チェーン（`.parent.parent.parent` 以上）③文字列リテラルの絶対パス
    `/home/user/project/...`（`/home/user/project/src` 自身は除く）。

    ★`parents[1]` や `.parent.parent`（2回だけ）はこのリポ自身の root にしか
      ならない（worktree でも安全）ので対象にしない。

    ★ALLOWLIST には、まだこのチェックアウトに存在しないファイル向けの
      **事前承認**（pending）が混じることがある（未統合の別ブランチが持ち込む
      予定の行。例: `tests/test_vdir_phase_fidelity.py`）。そうしたエントリは
      ファイルが実在するようになるまで比較から外す――統合の瞬間に不意打ちで
      赤くならないようにするため。ファイルが実在するのに行番号が食い違う場合は
      通常どおり検出対象になる。
    """
    found = _scan_repo()
    pending = {p: lines for p, lines in ALLOWLIST.items() if not (REPO / p).is_file()}
    expected = {p: lines for p, lines in ALLOWLIST.items() if p not in pending}
    assert found == expected, (
        "兄弟階層（bench/scripts/android/app/.venv 等、worktree では wt/ の"
        "symlink farm 経由で live を読む、または /home/user/project/... の絶対パス"
        "直書きで farm を経由せず live を直接読む）へ出ている箇所——静的走査で"
        "捕まえられる3つの書き方（`.parents[N]` N≥2／`.parent` 3連鎖以上／"
        "絶対パス文字列 `/home/user/project/...`。それ以外の組み立て方はこの試験の"
        "検出範囲外）に限って——が一覧と食い違います。増えた／消えた箇所を確認し、"
        "本ファイルの ALLOWLIST を更新すること。\n"
        f"  一覧（現存分）: {sorted(expected.items())}\n"
        f"  一覧（pending・未存在ファイル）: {sorted(pending.items())}\n"
        f"  実物:           {sorted(found.items())}"
    )


def test_worktree_の症状は再生できる_farmを書き換えると読む側も変わる():
    """★実証: wt/ farm と同じ形を tmp に再現し、危険が構造であることを実際に動かす。

    現実の /home/user/project の状態には依存しない（このリポが将来 farm を撤去しても、
    このテストは symlink 経由の可変共有という**一般的な仕組み**を確かめ続ける）。
    """
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        # ★"live" 側 = 本物の /home/user/project に相当する、siblings を持つ実体チェックアウト。
        live_repo = base / "live"
        (live_repo / "repo" / "tests").mkdir(parents=True)
        (live_repo / "sibling").mkdir(parents=True)
        (live_repo / "sibling" / "marker.txt").write_text("v1", encoding="utf-8")

        # ★farm 側 = wt/ に相当する、シンボリックリンクだけの層。
        farm = base / "farm"
        farm.mkdir()
        os.symlink(live_repo / "sibling", farm / "sibling")

        # ★worktree 側 = wt/r4-<key>/ に相当する、farm の中の実チェックアウト。
        wt_checkout = farm / "r4-fake"
        (wt_checkout / "tests").mkdir(parents=True)
        test_file = wt_checkout / "tests" / "test_x.py"
        test_file.write_text("# 中身は使わない。パスの形だけ必要", encoding="utf-8")

        # 実物の試験ファイルと同じ計算をここでも行う: parents[2] で farm へ出て、
        # sibling を読む。
        resolved = test_file.resolve().parents[2] / "sibling" / "marker.txt"
        assert resolved.read_text(encoding="utf-8") == "v1", (
            "farm 経由の解決そのものが想定と違う＝この再現が壊れている"
        )

        # ★live 側を書き換える（＝他の worker が別ブランチへ checkout した、に相当）。
        (live_repo / "sibling" / "marker.txt").write_text("v2-someone-else-checked-out", encoding="utf-8")

        # worktree 側は自分では何も変えていないのに、読む中身が変わる。
        assert resolved.read_text(encoding="utf-8") == "v2-someone-else-checked-out", (
            "live を書き換えても worktree 側の読み取りが変わらなかった＝"
            "この危険（他 worker の書き換えが自分の試験に混ざる）は実は起きていない"
            "ということになるが、実測ではこの通りに起きている（反証 idx16/17 の"
            "reflog を参照）。この assert が落ちたなら、この再現コード自体が"
            "実物と食い違っている——本文の理由を読み直すこと。"
        )
