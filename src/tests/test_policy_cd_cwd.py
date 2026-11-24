"""`cd` を跨いだ cwd 追従 — ゲートが自分で検査した前提を、実行時と一致させる。

★2026-09-02 の測定（監査 56,028 件・実物の Bash 道具で最後まで実行して確認）:

    workspace  `cd /外 && touch pwned`      → 外にファイルが**実際に**出来た
    workspace  `cd /外 && rm -rf victim`    → 外のディレクトリを**実際に**消した
    readonly   `cd /srv/notes/secret && ls | head -3 && pwd`
               → 機微ツリーの中身を列挙できた。**同じことを直接やると断られる**
                 （`grep -rl SSHPASS /srv/notes/secret` は約束どおり拒否）。

原因は `cd` が `_READ_ONLY` に入っていること（2026-08-11 に Windows 用として
`dir/findstr/where/tree/more/cd/ver` をまとめて足したとき、cwd の論理と
突き合わせていない）。`check_command(cmd, cwd=…)` の `cwd` は
  ①書き込み先の封じ込め ②機微ツリーの cwd 判定 ③相対パスの解決
の3つの土台で、ゲートは**最初の cwd** で検査し、シェルは **cd 後の場所**で走る。
＝denylist の抜けではなく、**検査した前提が実行時に成立していない**という誤り。
だから readonly を含む全モードに効く。

⚠ この試験は判定だけを見る（本当に走らせない）。実際に走らせて確かめた記録は
   報告に残した——上の 3 形は修正後、実物の道具でも `pwned` が出来ず
   `victim` が残ることを確認している。
"""
from __future__ import annotations

import pytest

import laps.policy as policy


def gate(monkeypatch, mode: str, ws: str | None = None):
    monkeypatch.setenv("LAPS_POLICY", mode)
    if ws:
        monkeypatch.setenv("LAPS_WORKSPACE", ws)
    return policy


def test_cd_で作業フォルダの外に書けない(monkeypatch, tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    outside = tmp_path / "outside"; outside.mkdir()
    p = gate(monkeypatch, "workspace", str(ws))
    for cmd in (f"cd {outside} && touch pwned",
                f"cd {outside} && rm -rf victim",
                f"cd {outside} && mkdir sub"):
        v = p.check_command(cmd, cwd=str(ws))
        assert not v.allowed, f"cd で workspace を抜けられる: {cmd}"


def test_cd_で機微ツリーを走査できない(monkeypatch, tmp_path):
    """readonly でも効く（書き込みの話ではないので）。"""
    p = gate(monkeypatch, "readonly")
    tree = str(tmp_path / "secret-notes")
    monkeypatch.setattr(policy, "_SECRET_TREES", (tree,))
    v = p.check_command(f"cd {tree} && ls | head -3 && pwd", cwd=str(tmp_path))
    assert not v.allowed
    assert "機微" in v.reason


def test_workspace_内の_cd_は通る(monkeypatch, tmp_path):
    """★誤爆したら何が起きるかを固定する。止めてよいのは『定義上、作業フォルダの
    外へ出る cd』だけで、中を歩くのは普通の作業。"""
    ws = tmp_path / "ws"; (ws / "sub").mkdir(parents=True)
    p = gate(monkeypatch, "workspace", str(ws))
    for cmd in (f"cd {ws} && touch ok.txt",
                "cd sub && touch ok.txt",
                f"cd {ws}/sub && rm -f a.txt",
                "ls -la"):
        v = p.check_command(cmd, cwd=str(ws))
        assert v.allowed, f"普通の作業が止まった: {cmd} — {v.reason}"


@pytest.mark.parametrize("cmd", ["cd", "cd -", "cd $HOME && ls", "cd a b"])
def test_解決できない_cd_は_fail_closed(monkeypatch, tmp_path, cmd):
    """行き先が解けないなら、**以降の段をどの cwd で検査してよいか分からない**。

    実測での誤爆見込み: 許可済み 20,198 件のうち `cd` を含む段は 128 件、
    その **128 件すべてが trusted**（＝この枝に来ない）。解決できない形は 0 件
    （絶対パス 113・相対 15 のみ）。⚠ ただし記録の workspace/readonly は
    ほぼ pytest の一時ディレクトリと試験の作り物なので、「実利用者で 0」ではない。
    """
    p = gate(monkeypatch, "workspace", str(tmp_path))
    v = p.check_command(cmd, cwd=str(tmp_path))
    assert not v.allowed
    assert "cd" in v.reason and v.alternative


def test_trusted_の_cd_は止めない(monkeypatch, tmp_path):
    """trusted は「何も止めない」がオーナー決定。追従はするが拒否はしない
    （実測 128/128 の `cd` がこちら側）。"""
    p = gate(monkeypatch, "trusted")
    for cmd in ("cd", "cd -", f"cd {tmp_path} && rm -rf x"):
        assert p.check_command(cmd, cwd=str(tmp_path)).allowed


def test_trusted_でも_cd_後の自己保護は効く(monkeypatch, tmp_path):
    """追従の副産物。`cd ~/.blackwall && rm -f audit.jsonl` を名前で書かれても、
    解決先が実体に一致すれば止まる。"""
    home = tmp_path / "home"; home.mkdir()
    (home / "audit.jsonl").write_text("x")
    monkeypatch.setenv("LAPS_HOME", str(home))
    p = gate(monkeypatch, "trusted")
    v = p.check_command(f"cd {home} && rm -f audit.jsonl", cwd=str(tmp_path))
    assert not v.allowed, "cd 後の相対パスで監査ログを消せる"
