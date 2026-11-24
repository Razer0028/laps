"""同梱 README は wheel の METADATA になる＝出荷物の説明書。実体と合わせる。

★2026-08-30 のレビューで見つかった形（P0-6・実体を読んで確認済み）:
  README は **削除済みの REPL** の slash 一覧（`/memory` `/context` `/hooks`
  `/sessions`）を載せ、`blackwall code "..."` という**別の入口の**呼び方を
  書き、道具を 18 と書いていた（実際は 22）。既定の送り先とモデル名も
  `127.0.0.1:8080` / `blackwall-omega-j` で、コードのどこにも無い値だった。

  文書の間違いは動かして確かめられないので、**気づかれずに残る**。
  ここで機械に突き合わせておけば、次に食い違ったとき赤くなる。
"""
from __future__ import annotations
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
README = (SRC / "README.md").read_text(encoding="utf-8")


def test_documented_slash_commands_all_exist():
    from laps.term import slash
    block = re.search(r"### Slash commands \(interactive\)\n+```\n(.*?)```",
                      README, re.DOTALL)
    assert block, "README の slash 一覧の節が見つからない"
    named = set(re.findall(r"/([a-z]+)", block.group(1)))
    unknown = sorted(n for n in named if n not in slash.COMMANDS)
    assert not unknown, f"README にあるが実装に無い slash: {unknown}"


def test_every_visible_slash_command_is_documented():
    from laps.term import slash
    missing = sorted(n for n, c in slash.COMMANDS.items()
                     if not c.hidden and f"/{n}" not in README)
    assert not missing, f"実装にあるが README に無い slash: {missing}"


def test_tool_count_in_readme_matches_the_registry():
    from laps import tools
    n = len(tools.all_specs())
    assert f"## Tools ({n})" in README, \
        f"README の道具数が実体と違う（実体 {n} 個）"


def test_readme_states_the_real_defaults():
    from laps import backend
    from laps.term import config
    assert f"`{config.DEFAULTS['model']}`" in README
    assert f"`{config.DEFAULTS['backend']}`" in README
    assert backend.DEFAULT_MODEL == config.DEFAULTS["model"], \
        "backend.py と term/config.py で既定のモデル名が食い違っている"
    assert backend.DEFAULT_BASE_URL == config.DEFAULTS["backend"], \
        "backend.py と term/config.py で既定の送り先が食い違っている"


def test_both_entry_points_are_documented():
    tomllib = __import__("tomllib")
    meta = tomllib.loads((SRC / "pyproject.toml").read_text(encoding="utf-8"))
    for name in meta["project"]["scripts"]:
        assert f"`{name}`" in README, f"入口 {name} が README に無い"
