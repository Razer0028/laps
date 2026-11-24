"""★全道具が**自分の成否を自分で名乗る**ことを、機械で列挙して確かめる。

なぜ 1 本にまとめるか（オーナー 2026-09-21「ひとつづつ対応してもらちが明かない」）:
  道具ごとに気づいたときだけ直す形では、**残りが何本あるか誰も知らない**。
  実測（2026-09-21・実機 18 走行・0.1.231/232）: 道具の成否の **67.1%（79 回中 53 回）**が
  `as_result` の語彙表からの推測だった。推測が外す実例は同日に出ている——
  `Grep(pattern="denied")` は**探した語が必ず出力に入る**ので 100% 失敗に見えた。

★この試験は「型を名乗ったか」だけを見る。**中身が正しいかは見ていない**（別の試験の仕事）。
★叩けない道具（網・子プロセス・利用者への問い）は下の NOT_MEASURED に名前を並べる。
  ★**黙って総数から外さない。** 数えられなかったものは、数えられなかったと出す。
"""
import os
import tempfile

import pytest

from laps import tools as T
from laps.tools import ToolResult


def _probes(d: str):
    """(道具名, 説明, 入力) の並び。**成功と失敗の両方**を入れる。"""
    a = os.path.join(d, "a.txt")
    return [
        ("Read", "成功", {"file_path": a}),
        ("Read", "無いファイル", {"file_path": os.path.join(d, "nope.txt")}),
        ("Read", "引数が足りない", {}),
        ("Glob", "成功", {"pattern": "*.txt"}),
        ("Glob", "0 件", {"pattern": "*.zzz"}),
        ("Glob", "起点が無い", {"path": os.path.join(d, "nope"), "pattern": "*"}),
        ("Glob", "引数が足りない", {}),
        ("Grep", "成功", {"pattern": "hello"}),
        ("Grep", "0 件", {"pattern": "zzzzz"}),
        ("Grep", "引数が足りない", {}),
        ("Write", "成功", {"file_path": os.path.join(d, "b.txt"), "content": "x"}),
        ("Write", "引数が足りない", {"file_path": os.path.join(d, "c.txt")}),
        ("Edit", "成功", {"file_path": a, "old_string": "hello", "new_string": "HELLO"}),
        ("Edit", "見つからない", {"file_path": a, "old_string": "nothere", "new_string": "x"}),
        ("Edit", "引数が足りない", {"file_path": a}),
        ("Bash", "成功", {"command": "exit 0"}),
        ("Bash", "失敗", {"command": "exit 3"}),
        ("Bash", "引数が足りない", {}),
        ("TodoWrite", "成功", {"todos": [{"content": "t", "status": "pending",
                                          "activeForm": "doing"}]}),
        ("TodoWrite", "引数が足りない", {}),
        ("TaskList", "成功", {}),
        ("BashOutput", "無い id", {"bash_id": "nope"}),
        ("SystemState", "成功", {}),
    ]


#: ★ここで**叩いていない**もの。理由つきで並べる（数えられなかったと言うために）。
NOT_MEASURED = {
    "WebSearch": "外へ出る",
    "WebFetch": "外へ出る",
    "Umbra": "閉域網の書庫へ出る",
    "RemoteShell": "他ホストへ SSH する",
    "Task": "子エージェントを起こす",
    "TaskCreate": "Task に連なる",
    "TaskUpdate": "Task に連なる",
    "TaskGet": "Task に連なる",
    "TaskStop": "Task に連なる",
    "AskUserQuestion": "利用者の返事を待つ",
    "NotebookEdit": "雛形の .ipynb が要る",
    "ApplyDiff": "差分の雛形が要る",
    "Verify": "Bash と同じ経路（Bash で見ている）",
}


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    d = str(tmp_path)
    (tmp_path / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    monkeypatch.chdir(d)
    monkeypatch.setenv("LAPS_WORKSPACE", d)
    return d


def test_全道具が成否を名乗る(ws):
    """契約: **型（ToolResult）** か、**自分で書いた `Error: ` の前置き**。

    ★内容を読ませない。受け手が本文の語から当てる形を 1 つも残さない。
    """
    specs = {s.name: s for s in T.all_specs()}
    bare = []
    for name, why, inp in _probes(ws):
        spec = specs.get(name)
        if spec is None:
            continue
        try:
            r = spec.handler(dict(inp))
        except Exception as e:                      # 例外は別の話（ここでは型だけ見る）
            bare.append(f"{name}／{why}: 例外 {type(e).__name__}: {e}")
            continue
        if isinstance(r, ToolResult):
            continue
        txt = str(r)
        if txt.lstrip().startswith("Error:"):
            continue          # 道具が自分で「失敗だ」と書いている＝当てにいく必要がない
        head = txt.strip().splitlines()[0][:70] if txt.strip() else "(空)"
        bare.append(f"{name}／{why}: 名乗らない素の str → {head!r}")
    assert not bare, (
        "★これらは自分の成否を名乗らず、受け手に本文の語から当てさせています"
        "（ToolResult を返すか、失敗なら `Error: ` で始めてください）:\n  "
        + "\n  ".join(bare)
        + "\n（叩いていないもの: " + "・".join(f"{k}={v}" for k, v in NOT_MEASURED.items()) + "）")


def test_叩けなかった道具を黙って落とさない():
    """★総数の帳尻を合わせる。登録＝叩いた＋叩けなかった、以外があれば教える。"""
    specs = {s.name for s in T.all_specs()}
    probed = {n for n, _, _ in _probes("/tmp")}
    unknown = specs - probed - set(NOT_MEASURED)
    assert not unknown, (
        f"★どちらにも入っていない道具が {len(unknown)} 本あります: {sorted(unknown)}\n"
        "  叩けるなら _probes に足し、叩けないなら理由つきで NOT_MEASURED に足してください。")
