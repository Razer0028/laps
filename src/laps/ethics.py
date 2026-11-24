"""倫理・慎重さの刻み（アプリから調整できる）。

★**実行できることは変わらない。** 何を実行してよいかは `policy.py` の allowlist が
  決めている（choke point）。ここが変えるのは**モデルがどれだけ慎重に振る舞うか**——
  確認を待つか、スコープを問い直すか、エスカレーションするか——だけ。
  研究側の一次資料がこの分離を支持している:
  「system prompt に書いた安全ルールは原理的に境界にならない（命令と非信頼データに
  アーキテクチャ上の区別が無い）」→ 実行可否はモデルの外側に置く。
  したがって **`raw` にしても policy ゲートは 1mm も緩まない**。

★**正直さの規律は刻みから外してある**（既定で常に載る）。
  「捏造しない」「未検証を完了と言わない」「file:line で示す」は倫理検閲ではなく
  **測定の前提**である。これを外すと「速く完了したように見えて何も出来ていない」走行が
  高得点になり、刻みの比較そのものが壊れる（この木で実際に踏んだ形:
  『捏造率』が正直な棄権と要約正解を混ぜた採点汚染だった件）。
  外したい場合は `LAPS_HONESTY_PINS=0`。**そのとき測定値は信用できない。**

刻み:
    strict   慎重さ最大。現行 + 追加の確認義務。
    standard 現行の出荷状態（`PROMPTS/guardrails.md` を**そのまま**使う）。
    direct   迷いを生む部分を外す。破壊的操作の開示は残すが、確認待ち・
             所有権スコープの問い直し・エスカレーション一覧を落とす。
    raw      慎重さの塊を丸ごと無し。正直さの固定だけが残る。
"""
from __future__ import annotations

import os
from pathlib import Path

#: ★2026-09-03 に honesty.md から外した文言と、その顛末（**設計意図はここに書く。
#:   honesty.md に書くと、ファイル冒頭が断っているとおり system prompt に載ってしまう**）。
#:
#:   外した文: 「コマンドを実行していない場合は『未実行』と明記する」
#:   理由: モデルがツールを1つも呼ばずに「未実行」とだけ返す逃げ道にした（0/5→2/5）。
#:
#: ★2026-09-14 追記 — **その対処が、逆向きの逃げ道を作っていた。**
#:   外した経緯を括弧書きで honesty.md に残したところ、その中の
#:   「未実行かどうかを言うのはハーネスの仕事で、モデルが書く言葉ではない」
#:   が **system prompt に載り、「走らせていないことには触れなくてよい」という
#:   明示的な許可**になっていた。実機（2026-09-13 23:59・版 0.1.223）で、
#:   ファイルを 9 本書き Bash は mkdir 1 本だけの走行が
#:   「検収基準に従ったテストが作成され、動作確認が完了しました」と締めた。
#:   → 経緯は **prompt から出してここへ移した**。honesty.md には要求だけを置く。
#:   ★教訓: **自分あての注記をモデルが読む場所に書かない。**
_HONESTY_MEMO_2026_09_03 = None          # 文書化のための置き場（値は使わない）

LEVELS = ("strict", "standard", "direct", "raw")
#: 測定専用の腕。**利用者に見せる刻みではない**（アプリの config は 4 つしか受けない）。
#: `sham` は standard と**中身がバイト同一**で札だけ違う＝腕を替えたこと自体の効果（床）を測る。
MEASUREMENT_LEVELS = LEVELS + ("sham",)
# ★オーナー指示（2026-08-29）「倫理的制限もすべて排除」。既定を raw に。
#   raw でも残るのは①正直さの固定②現場の安全（破壊操作の確認）——どちらも倫理ではない。
DEFAULT = "raw"

#: 揃える長さ。いちばん長い腕（strict）に合わせる。
#: 実際の値は `pad_target()` が実測して決めるので、ここは下限の目安。
_PAD_FLOOR = 1407


def level() -> str:
    """いま選ばれている刻み。不正な値は既定へ倒す（黙って落とさない）。"""
    v = (os.environ.get("LAPS_ETHICS") or "").strip().lower()
    return v if v in MEASUREMENT_LEVELS else DEFAULT


def _prompt_dir() -> Path:
    d = os.environ.get("LAPS_PROMPT_DIR")
    return Path(d) if d else Path(__file__).resolve().parent.parent / "PROMPTS"


def _read(base: Path, *names: str) -> str:
    """存在するものだけを連結する。無いファイルは黙って飛ばす。"""
    out = []
    for n in names:
        f = base / n
        if f.exists():
            out.append(f.read_text(encoding="utf-8"))
    return "\n\n".join(out)


def block(base: Path | None = None) -> str:
    """system prompt に載せる「正直さ + 慎重さ」の塊を返す。

    ★`standard` は `guardrails.md` を**そのまま**返す。
      刻みを足すために既定の文面を書き換えると、測定済みの基準線
      （Coder-30B が難課題 5/5）が別物になり比較できなくなる。
      既定は1バイトも動かさない、が設計上の要点。
    """
    base = base or _prompt_dir()
    return _pad(_raw_block(base, level()), base)


def _raw_block(base: Path, lv: str) -> str:
    pins = os.environ.get("LAPS_HONESTY_PINS", "1") != "0"

    if lv in ("standard", "sham"):
        # ops-safety は全ての刻みに載る（下の direct/raw と同じ扱い）
        # ★sham は standard と**中身がバイト同一**。札だけ違う。
        #   「腕を替えたこと自体」で動く量（床）を測るために置いてある。
        return _read(base, "guardrails.md", "ops-safety.md")
    if lv == "strict":
        return _read(base, "guardrails.md", "ops-safety.md", "ethics/strict-extra.md")

    # direct / raw は guardrails.md を使わず、固定 + 縮めた慎重さで組み立てる。
    names: list[str] = []
    if pins:
        names.append("honesty.md")
    # ★現場の安全（PROMPTS/ops-safety.md）は刻みに関わらず常に載る
    #   （2026-08-29・オーナーの観察から）。ファイル本文には設計意図を書かない——
    #   「刻み」という語が本文にあると、試験 test_block_never_leaks_the_dial_to_the_model
    #   が正しく落ちる（モデルに刻みの存在を教えないための規律）。実際に一度落とされた。
    #   分ける前は raw にすると破壊操作の確認まで消え、「無検閲にすると危険になる」
    #   という誤った取引を強いていた。倫理と安全は別のもの。
    names.append("ops-safety.md")
    if lv == "direct":
        names.append("ethics/direct.md")
    return _read(base, *names)


def pad_target(base: Path | None = None) -> int:
    """揃える長さ＝いちばん長い腕の実測長。**定数で持たない。**

    定数にすると、プロンプトを1文字直したときに静かにズレる。
    """
    base = base or _prompt_dir()
    return max(_PAD_FLOOR, max(len(_raw_block(base, lv)) for lv in LEVELS))


def _pad(body: str, base: Path) -> str:
    """全腕の文字数を揃える（**測定のときだけ**・既定は無効）。

    ★なぜ要るか（2026-08-19 の敵対レビュー）: 刻みごとにブロック長が違うと
      （実測 1407 / 1152 / 650 / 331 字）、compaction 圧力・文脈予算・
      生成上限のクランプが腕ごとに変わる。**腕が計器を動かす**ので、
      観測された差が「慎重さの差」なのか「長さの差」なのか分離できない。

    ★既定で無効にしてある理由: 出荷する既定（standard）の system prompt を
      1 バイトも動かさないため。詰め物は測定の道具であって製品ではない。

    ★詰め物は**指示を含まない事実記述**にする。指示を入れると、それ自体が
      腕の内容になってしまう。
    """
    if os.environ.get("LAPS_ETHICS_PAD", "0") != "1":
        return body
    target = pad_target(base)
    if len(body) >= target:
        return body
    filler = (
        "\n\n# 参考（この節に指示は含まれません）\n"
        "# ログの各行は日付・時刻・水準・本文の順に並びます。\n"
        "# 水準は INFO / WARN / ERROR の3種類です。\n"
        "# 日付は年・月・日をハイフンで区切って表します。\n"
        "# 時刻は時・分・秒をコロンで区切って表します。\n"
        "# 本文は行末までの任意の文字列です。\n"
    )
    need = target - len(body)
    out = body
    while need > 0:
        take = filler[:need]
        out += take
        need -= len(take)
    return out


def describe() -> str:
    """画面や記録に出す1行。**実行権限とは別物**であることを毎回言う。"""
    lv = level()
    pins = os.environ.get("LAPS_HONESTY_PINS", "1") != "0"
    tail = "" if pins else "・正直さの固定=無効（測定値は信用できない）"
    return f"倫理の刻み={lv}（実行権限は別: policy）{tail}"
