"""`bw-flow` コマンド — 開発の工程を、**モデルが自分で引く**形で置く。

★なぜこの形か（オーナー指示 2026-09-10）
  「エージェントが勝手に動きすぎ。基本的にモデルのサポート的な立ち位置じゃなきゃ
    いけないのにエージェントが主導権を握り過ぎている。**開発工程や動作に関するフローを
    組み込んでおいてそれをモデルが適宜判断するように**しないと」

  同じ日に差し戻し（強制再ループ）を撤去したのは指示の前半——ハーネスが上書きしない——で、
  こちらが後半にあたる。工程は**置いておく**が、いつ引くかはモデルが決める。

★なぜ道具（tool）ではなく CLI か
  実測（同モデル・同 8 課題）で、道具を 20 本持つ中核が 2〜3/8、道具ゼロの薄い中核が
  6〜7/8。**毎手番「どの道具か」を選ばせること自体が負担**だった。
  CLI なら前置きは 2 行増えるだけで、能力は 1 つも減らない
  （`bw-undo` と同じ型）。

★中身は「一般論」ではない
  この現場が実際に外した診断・実際に壊れた出荷の型を書いている。
  一般論だけの手引きはモデルが既に知っていて、読ませる意味が無い。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "flows"


def _front(text: str) -> tuple[dict, str]:
    """`---` で挟んだ頭書きと本文に分ける（skills と同じ形）。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[end + 4:].lstrip("\n")


def load_all() -> list[tuple[str, str, str]]:
    """(name, description, body) を名前順で返す。"""
    out = []
    for p in sorted(_DIR.glob("*.md")):
        meta, body = _front(p.read_text(encoding="utf-8"))
        out.append((meta.get("name", p.stem), meta.get("description", ""), body))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bw-flow",
        description="開発の工程の手引きを引く（引数なしで一覧）。",
        epilog="例: bw-flow fix ／ bw-flow verify",
        allow_abbrev=False,
    )
    p.add_argument("name", nargs="?", default="", help="工程の名前（省略で一覧）")
    a = p.parse_args(argv)

    flows = load_all()
    if not flows:
        print("工程の手引きが入っていません（出荷物から落ちている可能性）", file=sys.stderr)
        return 2

    if not a.name:
        print("使える工程:")
        for name, desc, _ in flows:
            print(f"  {name:<12} {desc}")
        print("\n本文を読むには: bw-flow <名前>")
        return 0

    for name, _, body in flows:
        if name == a.name:
            sys.stdout.write(body.rstrip() + "\n")
            return 0

    print(f"そんな工程はありません: {a.name}", file=sys.stderr)
    print("  → " + " / ".join(n for n, _, _ in flows), file=sys.stderr)
    return 1


if __name__ == "__main__":                      # pragma: no cover
    raise SystemExit(main())
