"""倫理・慎重さの刻み。

★この試験が守っているいちばん大事な性質:
  **既定（standard）では system prompt が 1 バイトも変わらない。**
  刻みを足すために既定の文面を書き換えると、測定済みの基準線が別物になり
  「刻みを変えたから変わった」のか「既定を触ったから変わった」のか
  区別できなくなる。比較の基準そのものを壊す形。
"""
from __future__ import annotations

import os

import pytest

from laps import ethics


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LAPS_ETHICS", raising=False)
    monkeypatch.delenv("LAPS_HONESTY_PINS", raising=False)


def _prompts():
    from pathlib import Path
    return Path(ethics.__file__).resolve().parent.parent / "PROMPTS"


def test_default_is_raw():
    assert ethics.level() == "raw"


def test_unknown_level_falls_back_to_default(monkeypatch):
    """★黙って落とさない。存在しない刻みは既定へ倒す。

    設定ファイルの打ち間違いで「慎重さが丸ごと消えた」に気づけない形が
    いちばん危ない。
    """
    monkeypatch.setenv("LAPS_ETHICS", "yolo")
    assert ethics.level() == "raw"
    assert ethics.block(_prompts()).strip() != ""


@pytest.mark.parametrize("lv", list(ethics.LEVELS))
def test_every_level_resolves(monkeypatch, lv):
    monkeypatch.setenv("LAPS_ETHICS", lv)
    assert ethics.level() == lv
    ethics.block(_prompts())  # 例外を出さない


def test_standard_is_byte_identical_to_guardrails(monkeypatch):
    """★既定は guardrails.md **そのもの**。1 バイトも足さない・引かない。"""
    monkeypatch.setenv("LAPS_ETHICS", "standard")
    want = ethics._read(_prompts(), "guardrails.md", "ops-safety.md")
    assert ethics.block(_prompts()) == want


def test_system_prompt_at_default_is_honesty_plus_ops_safety(monkeypatch):
    """既定の system prompt の中身を固定する。

    ★2026-08-29 に契約を変えた（旧名 test_system_prompt_unchanged_at_default）:
      刻み導入前は persona + guardrails だった。そこから「現場の安全」
      （破壊操作の確認・エスカレーション）を ops-safety.md へ切り出し、
      **刻みに関わらず常に載る**ようにした。分ける前は raw にするとラボの保護まで
      消え、「無検閲にすると危険になる」という誤った取引を強いていた。
      既定の**内容の合計は変わらない**（置き場所が変わっただけ）ことをここで守る。
    """
    base = _prompts()
    # 既定は raw（オーナー指示 2026-08-29）＝正直さ + 現場の安全
    want = ethics._read(base, "honesty.md", "ops-safety.md")
    got = ethics.block(base)
    assert got == want


def test_raw_drops_caution_but_keeps_honesty(monkeypatch):
    monkeypatch.setenv("LAPS_ETHICS", "raw")
    b = ethics.block(_prompts())
    # 慎重さ由来の語が消えていること
    # ★2026-08-29 に契約を変えた: 「現場の安全」（破壊操作の確認・エスカレーション）は
    #   PROMPTS/ops-safety.md へ切り出し、**刻みに関わらず常に載る**ようにした。
    #   分ける前は raw にするとラボの保護まで消え、「無検閲にすると危険になる」という
    #   誤った取引を強いていた。倫理と安全は別のもの（test_ethics_split.py が新契約を守る）。
    assert "破壊的操作" in b          # 安全は残る（新契約）
    assert "スコープの確認" not in b   # 慎重さの側は落ちる
    # 正直さの固定は残っていること
    assert "でっち上げない" in b
    # ★2026-09-14 訂正: ここは以前「未実行」という語が**説明文の中に残ること**を
    #   固定していた。その説明文（設計メモ）こそが system prompt に載って
    #   「走らせていないことには触れなくてよい」という許可になっていた——
    #   実機で「動作確認が完了しました」と嘘をつく走行を生んだ（0.1.223・09-13 23:59）。
    #   メモは laps/ethics.py へ移した。固定するのは**要求のほう**。
    assert "実際に走らせて出力を見る" in b, "検証の定義が system prompt に無い"
    assert "ファイルを書いただけでは検証ではない" in b


@pytest.mark.parametrize("lv", list(ethics.LEVELS))
def test_未実行を書けという逃げ道の指示は全ての刻みから消えている(monkeypatch, lv):
    """2026-09-03: honesty.md と guardrails.md の両方に、persona.md が 2026-08-08 に
    「モデルがツールを1つも呼ばずに『未実行』とだけ返す逃げ道として使った(0/5→2/5)」
    という実測で外したのと**同じ文言**が生き残っていた。honesty.md は raw/direct
    （既定は raw）で常に載るので、persona.md 側の直しだけでは system prompt から
    実際には消えていなかった。この試験はその具体的な文言がどの刻みでも二度と
    紛れ込まないことを固定する（語の「未実行」自体は消えない。上のテストが守る）。
    """
    monkeypatch.setenv("LAPS_ETHICS", lv)
    b = ethics.block(_prompts())
    assert "「未実行」と明記する" not in b, f"{lv}: 逃げ道の指示が生き残っている"


def test_direct_keeps_disclosure_without_waiting(monkeypatch):
    monkeypatch.setenv("LAPS_ETHICS", "direct")
    b = ethics.block(_prompts())
    # ★否定形（「確認は待たない」）で書かない。否定形は、外したはずの
    #   「確認を待つ」という選択肢をモデルに思い出させる。肯定形で書く。
    assert "そのまま実行する" in b
    assert "でっち上げない" in b          # 正直さは残る
    # ★2026-08-29 に契約を変えた: 「現場の安全」（破壊操作の確認・エスカレーション）は
    #   PROMPTS/ops-safety.md へ切り出し、**刻みに関わらず常に載る**ようにした。
    #   分ける前は raw にするとラボの保護まで消え、「無検閲にすると危険になる」という
    #   誤った取引を強いていた。倫理と安全は別のもの（test_ethics_split.py が新契約を守る）。
    assert "破壊的操作" in b          # 安全は残る（新契約）
    assert "スコープの確認" not in b   # 慎重さの側は落ちる


def test_strict_is_a_superset_of_standard(monkeypatch):
    monkeypatch.setenv("LAPS_ETHICS", "standard")
    std = ethics.block(_prompts())
    monkeypatch.setenv("LAPS_ETHICS", "strict")
    strict = ethics.block(_prompts())
    assert std in strict
    assert len(strict) > len(std)


def test_honesty_pins_can_be_disabled_but_only_explicitly(monkeypatch):
    """外せること自体は守る（オーナーの権限）。既定で外れないことも守る。"""
    monkeypatch.setenv("LAPS_ETHICS", "raw")
    assert "でっち上げない" in ethics.block(_prompts())
    monkeypatch.setenv("LAPS_HONESTY_PINS", "0")
    # ★新契約: 正直さの固定を切っても**現場の安全は残る**（倫理と安全は別）。
    _b = ethics.block(_prompts())
    assert "でっち上げ" not in _b and "破壊的操作" in _b


@pytest.mark.parametrize("lv", list(ethics.LEVELS))
def test_block_never_leaks_the_dial_to_the_model(monkeypatch, lv):
    """★プロンプトに「刻みの話」を書かない。

    実際に踏んだ形: direct.md の注釈に「エスカレーション一覧を外した」と
    書いたら、**外したはずの規則名がそのままモデルに届いた**。
    プロンプト内の `#` 行も system prompt に載る（guardrails.md 自身が
    冒頭でそう警告している）。設計意図は ethics.py に置く。
    """
    monkeypatch.setenv("LAPS_ETHICS", lv)
    b = ethics.block(_prompts())
    for word in ("刻み", "LAPS_ETHICS", "ethics =", "外したもの", "残したもの"):
        assert word not in b, f"{lv}: プロンプトに刻みの話が混ざっている（{word}）"


def test_describe_always_says_policy_is_separate(monkeypatch):
    for lv in ethics.LEVELS:
        monkeypatch.setenv("LAPS_ETHICS", lv)
        assert "policy" in ethics.describe()


def test_describe_warns_when_pins_are_off(monkeypatch):
    monkeypatch.setenv("LAPS_HONESTY_PINS", "0")
    assert "信用できない" in ethics.describe()


def test_level_does_not_touch_execution_policy(monkeypatch):
    """★刻みを最大に緩めても、実行ゲートは 1mm も緩まないこと。

    ここが崩れると「倫理を緩める」が「何でも実行できる」に化ける。
    """
    from laps import policy
    monkeypatch.setenv("LAPS_POLICY", "readonly")
    monkeypatch.setenv("LAPS_ETHICS", "raw")
    monkeypatch.setenv("LAPS_HONESTY_PINS", "0")
    # 宣言ではなく**振る舞い**で確かめる。読むだけモードで破壊的な命令が
    # 通らないことを、刻みを最大に緩めた状態で見る。
    v = policy.check_command("rm -rf /tmp/blackwall-ethics-probe")
    assert not v.allowed, f"ethics=raw が実行ゲートを緩めた: {v}"


# ---------------------------------------------------------------- 測定用の腕

def test_sham_is_byte_identical_to_standard(monkeypatch):
    """★sham は札だけ違う。中身が1バイトでも違うと「床」の意味が消える。"""
    monkeypatch.setenv("LAPS_ETHICS", "standard")
    std = ethics.block(_prompts())
    monkeypatch.setenv("LAPS_ETHICS", "sham")
    assert ethics.block(_prompts()) == std


def test_sham_is_not_offered_to_users():
    """アプリの設定に出す刻みは4つ。sham は測定専用。"""
    assert "sham" not in ethics.LEVELS
    assert "sham" in ethics.MEASUREMENT_LEVELS


def test_padding_is_off_by_default(monkeypatch):
    """★詰め物は測定の道具であって製品ではない。既定を1バイトも動かさない。"""
    monkeypatch.delenv("LAPS_ETHICS_PAD", raising=False)
    monkeypatch.setenv("LAPS_ETHICS", "standard")
    want = ethics._read(_prompts(), "guardrails.md", "ops-safety.md")
    assert ethics.block(_prompts()) == want


def test_padding_equalises_every_arm(monkeypatch):
    """刻みが計器を動かさないよう、全腕の長さを揃える。"""
    monkeypatch.setenv("LAPS_ETHICS_PAD", "1")
    target = ethics.pad_target(_prompts())
    lens = {}
    for lv in ethics.MEASUREMENT_LEVELS:
        monkeypatch.setenv("LAPS_ETHICS", lv)
        lens[lv] = len(ethics.block(_prompts()))
    assert set(lens.values()) == {target}, lens


def test_pad_target_is_measured_not_hardcoded(monkeypatch):
    """★長さを定数で持つと、プロンプトを1文字直したとき静かにズレる。"""
    monkeypatch.setenv("LAPS_ETHICS", "strict")
    monkeypatch.delenv("LAPS_ETHICS_PAD", raising=False)
    assert ethics.pad_target(_prompts()) >= len(ethics.block(_prompts()))


def test_filler_carries_no_instructions(monkeypatch):
    """詰め物に指示を入れると、それ自体が腕の内容になってしまう。"""
    monkeypatch.setenv("LAPS_ETHICS_PAD", "1")
    monkeypatch.setenv("LAPS_ETHICS", "raw")
    added = ethics.block(_prompts())[len(ethics._raw_block(_prompts(), "raw")):]
    for word in ("してください", "しないこと", "禁止", "必ず", "確認"):
        assert word not in added, f"詰め物に指示が混ざっている: {word}"
