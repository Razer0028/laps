#!/usr/bin/env python3
"""docs/env-switches.md の各行の「最初の参照 (file:line)」を tests/_env_scan.py の scan() が返す
最小の (file, line) に合わせて書き直す（test_documented_file_lines_match_scan と同じ ground truth）。
★2026-09-08: 行番号はコードを触るたびにずれ、直す道具が無く前回（ebefab4）は手で 82 行書き換えていた。
使い方: regen_env_switches_lines.py [--check]"""
import pathlib, re, sys
from collections import defaultdict

def _root() -> pathlib.Path:
    """--root <src の場所> で作業木（wt/*）にも当てられる。既定は live の src。"""
    if "--root" in sys.argv:
        return pathlib.Path(sys.argv[sys.argv.index("--root") + 1]).resolve()
    return pathlib.Path(__file__).resolve().parents[1] / "src"


ROOT = _root()
sys.path.insert(0, str(ROOT / "tests"))
import _env_scan as env_scan  # noqa: E402

DOC = ROOT / "docs" / "env-switches.md"
ROW = re.compile(r"^\| `(LAPS_[A-Z0-9_]+)` \|")
LOC = re.compile(r"`([A-Za-z0-9_/.-]+\.py):(\d+)`")


def main() -> int:
    check = "--check" in sys.argv
    ground = defaultdict(set)
    for r in env_scan.scan():
        ground[r["name"]].add((r["file"], r["line"]))
    out, changed, skipped = [], [], []
    for line in DOC.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m and ground.get(m.group(1)):
            f, n = sorted(ground[m.group(1)])[0]
            # ★2026-09-22: 旧実装は行の**最初の** `path.py:NNN` を書き換えていた。
            #   説明文の中に出典として `path.py:NNN` を書くと**そちらを書き換えてしまい**、
            #   本来の列は古いまま残る。実害: 説明文が
            #   「langgraph `blackwall/agent.py:3984`」という無意味な文に化け、
            #   doc-gate は落ち続け、しかもこの道具は「ずれなし」と言っていた。
            #   ⇒ 書き換えるのは **file:line の列（5 列表の 4 列目）だけ**にする。
            cols = line.split("|")
            if len(cols) < 6:
                skipped.append((m.group(1), "列の数が想定と違う")); out.append(line); continue
            locs = LOC.findall(line)
            if len(locs) > 1 and not LOC.search(cols[4]):
                # 説明文にだけ path:line が在る（列には無い）＝触れない
                skipped.append((m.group(1), "file:line の列が見つからない")); out.append(line); continue
            lm = LOC.search(cols[4])
            if lm and (lm.group(1) != f or int(lm.group(2)) != n):
                changed.append((m.group(1), f"{lm.group(1)}:{lm.group(2)}", f"{f}:{n}"))
                cols[4] = cols[4][:lm.start()] + f"`{f}:{n}`" + cols[4][lm.end():]
                line = "|".join(cols)
            elif not lm:
                skipped.append((m.group(1), "列に file:line が書かれていない"))
        out.append(line)
    for name, old, new in changed:
        print(f"   ・{name}: {old} → {new}")
    # ★**触れなかった行を必ず言う**（2026-09-22）。黙って飛ばすと
    #   「ずれなし」と「直せなかった」が見分けられない——今日それで 2 回はまった。
    for name, why in skipped:
        print(f"   ⚠ {name}: 触れませんでした（{why}）")
    if not changed:
        print("   ・ずれなし" if not skipped else "   ・書き換えは 0 件（上の ⚠ を見てください）")
        # ★止めない（2026-09-22 訂正）。最初 `return 1` にしたら**出荷が止まった**——
        #   触れない行が在るのは**文書の欠け**であって、出荷を止める理由ではない。
        #   本物のずれは `test_documented_file_lines_match_scan` が捕まえる。
        #   ★言うべきは「黙って成功と言わない」であって「失敗にする」ではなかった。
        return 0
    if check:
        return 1
    DOC.write_text("\n".join(out) + "\n", encoding="utf-8"); print(f"   docs/env-switches.md を書き換えました（{len(changed)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
