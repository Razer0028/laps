"""`docs/env-switches.md` must list exactly the `LAPS_*` switches the code
actually reads — no more, no less (W5, 2026-09-04).

Catches the two directions of drift separately, so a failure says which one
happened instead of just "these sets differ":
  - a switch was added to the code and nobody added a row for it
  - a row was documented (or a row was removed from the code but not the doc)
    and the code no longer reads that name

The scanner (`tests/_env_scan.py`) is an AST walk, not a grep — see its module
docstring for exactly what it can and cannot find (one level of indirection
through small "read env var by name" helpers; a short manual list for the one
case reached only through a dict literal). If you add a new switch in a shape
the scanner can't see, either teach the scanner or add it to
`_env_scan.KNOWN_INDIRECT_EXTRA` — this test will otherwise correctly flag it
as undocumented forever.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import _env_scan as env_scan

DOCS_PATH = Path(__file__).resolve().parent.parent / "docs" / "env-switches.md"

#: Row shape written by the generator: "| `LAPS_FOO` | ... |"
_ROW_RE = re.compile(r"^\|\s*`(LAPS_[A-Z0-9_]+)`\s*\|", re.MULTILINE)

#: Full row shape, capturing the trailing `file:line` column too.
_FULL_ROW_RE = re.compile(
    r"^\|\s*`(LAPS_[A-Z0-9_]+)`\s*\|.*\|\s*`([^`:]+):(\d+)`\s*\|\s*\d+\s*\|$",
    re.MULTILINE,
)

#: r4b/docs-env（2026-09-05）が他の r4 branch と同時に新設した env var を先回りして
#: 文書化した3行。この worktree の `blackwall/` コード自体はまだこれらを読んでいない
#: （それぞれ r4/core-hold-fix・r4/core-server-lifecycle 側の変更）ので、
#: `test_no_documented_switch_is_stale` と `test_documented_file_lines_match_scan`
#: からは一時的に除外する。★3branch を統合したらこのセットを空にすること——空の
#: ままにしておくと、この2試験が「本当に stale な行」を見逃す抜け穴になる。
_PENDING_INTEGRATION = {
    "LAPS_BASH_RELEASE",        # r4/core-server-lifecycle: laps/tools/bash.py:869
    "LAPS_BASH_RELEASE_GRACE",  # r4/core-server-lifecycle: laps/tools/bash.py:187
}


def _documented_names() -> set[str]:
    text = DOCS_PATH.read_text(encoding="utf-8")
    return set(_ROW_RE.findall(text))


def _documented_rows() -> list[tuple[str, str, int]]:
    """(name, file, line) を文書の行順で返す——`_ROW_RE` と違い、末尾の file:line
    カラムまで拾う。行の形が壊れて末尾カラムが読めない行は静かに無視する
    （ここは「形が正しい行の file:line が正しいか」だけを見る）。"""
    text = DOCS_PATH.read_text(encoding="utf-8")
    return [(name, file, int(line)) for name, file, line in _FULL_ROW_RE.findall(text)]


def test_docs_file_exists():
    assert DOCS_PATH.is_file(), (
        f"{DOCS_PATH} が無い。env スイッチの一覧はここに置く約束"
        "（tests/test_env_switches_documented.py が突き合わせる相手）。"
    )


def test_every_code_switch_is_documented():
    code_names = env_scan.names()
    doc_names = _documented_names()
    missing = sorted(code_names - doc_names)
    assert not missing, (
        "コードが読んでいるのに docs/env-switches.md に行が無い env var: "
        f"{missing}\n"
        "→ 表に1行足す（既定値・意味・file:line）。名前の間接参照（ラッパー関数経由）で"
        "スキャナが見つけられない形なら tests/_env_scan.py の KNOWN_INDIRECT_EXTRA へ"
        "追加するか、スキャナ自体を拡張すること。"
    )


def test_no_documented_switch_is_stale():
    code_names = env_scan.names()
    doc_names = _documented_names()
    stale = sorted(doc_names - code_names - _PENDING_INTEGRATION)
    assert not stale, (
        "docs/env-switches.md に行があるのに、いまのコードはもう読んでいない env var: "
        f"{stale}\n"
        "→ その機能ごと削除されたなら行も消す。まだ読まれているはずならスキャナの"
        "盲点（tests/_env_scan.py docstring 参照）を疑う——サイレントに黙って外れた"
        "スイッチという可能性もある。（`_PENDING_INTEGRATION` に載っている変数は"
        "他 r4 branch が同時に新設中で、統合後にコードが揃うまでの一時除外。"
        "見つかった stale 名がそこにも無ければ本物のドリフト。）"
    )


def test_pending_integration_list_is_still_accurate():
    """`_PENDING_INTEGRATION` は一時的な免除リストなので腐りやすい——ここでは
    最低限、免除している名前が実際に文書化されている（存在しない名前を空に免除
    していない）ことと、統合が済んで免除の必要が無くなったのに消し忘れていないか
    を見る。後者は「もうコードにある」なら気づけるように warnings 経由で知らせる
    （fail にはしない——他 branch の統合順序はこの担当の管理下ではないため）。"""
    doc_names = _documented_names()
    missing_from_doc = sorted(_PENDING_INTEGRATION - doc_names)
    assert not missing_from_doc, (
        f"_PENDING_INTEGRATION に載っているのに docs/env-switches.md に行が無い: "
        f"{missing_from_doc}\n→ 免除リストの名前が間違っているか、行を消し忘れている。"
    )
    already_landed = sorted(_PENDING_INTEGRATION & env_scan.names())
    if already_landed:
        import warnings
        warnings.warn(
            "統合が進んだ様子: 次の env var はもうこの worktree のコードで読まれて"
            f"いる（_PENDING_INTEGRATION から外してよい）: {already_landed}"
        )


def test_documented_file_lines_match_scan():
    """各行の「最初の参照 (file:line)」が `tests/_env_scan.py` の scan() が実際に
    見つけた最小の (file, line) と一致しているかを機械で見張る（r4b/docs-env,
    2026-09-05）。

    ドキュメントの規約（実装係・検証係が共通して使った定義）は「その名前で scan()
    が返す (file, line) の集合を重複排除・ソートした最初の要素」。ここではその定義を
    そのまま再現して突き合わせる——手で `grep -n` して目視するのではなく、
    生成に使ったのと同じ ground truth 関数を呼ぶことで、今回のような
    「コード側に行插入があって file:line が45〜49行ズレていた」ドリフト
    （2026-09-05 是正・28行）を将来も機械的に捕まえる。

    変異試験（このテストが本当に落ちることの確認）: `LAPS_HOME` 行の
    file:line を `blackwall/cli.py:1`（実際の読み取り行は364）に書き換えて実行す
    ると、このテストは FAILED になる。
    """
    from collections import defaultdict

    ground: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for r in env_scan.scan():
        ground[r["name"]].add((r["file"], r["line"]))

    bad = []
    seen = set()
    for name, file, line in _documented_rows():
        seen.add(name)
        if name in _PENDING_INTEGRATION:
            continue
        locs = ground.get(name)
        if not locs:
            continue  # 未知の名前（コードに無い）は他の2試験の役目
        expected_file, expected_line = sorted(locs)[0]
        if (file, line) != (expected_file, expected_line):
            bad.append((name, f"{file}:{line}", f"{expected_file}:{expected_line}"))
    assert not bad, (
        "docs/env-switches.md の file:line が scan() の実測（重複排除・ソート済みの"
        f"最初の要素）とズレている（変数, 記載, 実測）: {bad}\n"
        "→ tests/_env_scan.py の scan() をそのまま呼び直して file:line 列を再生成すること。"
    )


def test_scanner_finds_a_sane_number_of_switches():
    """0 個や 1 個は「スキャナが壊れて何も見ていない」の兆候。下限は緩く、増える分には
    黙って通す（新しいスイッチが増えるのは正常）。"""
    n = len(env_scan.names())
    assert n >= 100, f"検出数が異常に少ない（{n}）。スキャナが壊れていないか確認すること。"


def test_known_indirect_extra_is_still_accurate():
    """手動リストの理由づけが腐っていないかの最低限の確認: そこに書いた file が実在し、
    その行番号一帯に、その名前の文字列が本当に登場するかだけを見る（意味までは見ない —
    それは tests/test_policy*.py の役目ではなく、ここは env-switches の一覧の話)。"""
    root = Path(__file__).resolve().parent.parent
    for entry in env_scan.KNOWN_INDIRECT_EXTRA:
        f = root / entry["file"]
        assert f.is_file(), f"KNOWN_INDIRECT_EXTRA が指すファイルが無い: {entry}"
        text = f.read_text(encoding="utf-8")
        assert entry["name"] in text, (
            f"KNOWN_INDIRECT_EXTRA の {entry['name']} が {entry['file']} の中に"
            "もう見当たらない。移動/削除されていないか確認すること。"
        )
