"""`bw-flow`（開発工程の手引き）の試験。

★オーナー指示 2026-09-10 の後半にあたる機能:
  「開発工程や動作に関するフローを組み込んでおいて**それをモデルが適宜判断する**ように」
  前半（ハーネスが上書きしない＝差し戻しの撤去）と対になる。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from laps import cli_flow

SRC = Path(__file__).resolve().parents[1]
FLOWS = SRC / "laps" / "flows"


def test_工程が出荷物に入っている():
    """★「作ったが出荷物から落ちている」を止める（SKILL.md で実際に踏んだ型）。"""
    import tomllib
    data = tomllib.loads((SRC / "pyproject.toml").read_text())
    pkg = data["tool"]["setuptools"]["package-data"]["laps"]
    assert "flows/*.md" in pkg, "flows/*.md が package-data に無い＝実機だけ 0 本になる"


def test_入口が登録されている():
    import tomllib
    data = tomllib.loads((SRC / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["bw-flow"] == "laps.cli_flow:main"


def test_一覧が名前と説明を出す(capsys):
    assert cli_flow.main([]) == 0
    out = capsys.readouterr().out
    for name in ("fix", "feature", "investigate", "verify", "ship"):
        assert name in out, name
    assert "bw-flow <名前>" in out


@pytest.mark.parametrize("name", ["fix", "feature", "investigate", "verify", "ship"])
def test_本文が出る(name, capsys):
    assert cli_flow.main([name]) == 0
    out = capsys.readouterr().out
    assert len(out) > 200, out
    assert not out.startswith("---"), "頭書きを剥がしていない"


def test_知らない名前は終了コード1で候補を出す(capsys):
    assert cli_flow.main(["そんなの"]) == 1
    err = capsys.readouterr().err
    assert "fix" in err


def test_全部の工程に名前と説明がある():
    for name, desc, body in cli_flow.load_all():
        assert name and desc, name
        assert body.strip(), name


def test_工程は一般論ではなくこの現場の実測を含む():
    """★一般論だけの手引きはモデルが既に知っていて、読ませる意味が無い。

    「この現場で実際に外した／壊れた」節を必ず持たせる（書き手への歯止め）。
    """
    bodies = {n: b for n, _, b in cli_flow.load_all()}
    assert "ping" in bodies["fix"], "実測の型（ping で死活判定しない）が消えている"
    assert "作ったが繋いでいない" in bodies["feature"]
    assert "1 版前" in bodies["ship"] or "1版前" in bodies["ship"]


def test_端末から実際に引ける():
    """★入口の実在（登録漏れは手元では緑のまま実機だけ壊れる）。"""
    r = subprocess.run([sys.executable, "-m", "laps.cli_flow", "verify"],
                       capture_output=True, text=True, cwd=str(SRC))
    assert r.returncode == 0, r.stderr
    assert "確かめる" in r.stdout


def test_既定の中核の人格文も工程を案内している():
    persona = (SRC / "PROMPTS" / "persona.md").read_text()
    assert "bw-flow" in persona
    # ★撤去した仕組みを「ある」と書いたままにしない（モデルに嘘を教えることになる）。
    assert "差し戻しは撤去した" in persona
