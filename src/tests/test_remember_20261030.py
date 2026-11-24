"""★2026-10-30 Remember（利用者が「覚えて」と頼んだ事柄を全部の会話で持つ・オーナー「メモリ機能の強化」）。

置き場は ~/.config/blackwall/REMEMBER.md。前置き（prompt.instructions）に入ること・全体の指示の読み方（最初の 1 本）を変えないこと・
0.1.601 が AGENTS.md の区画に書いた分を移すこと・資格情報を覚えないことを見る。
"""
from __future__ import annotations

import json

from laps.oc import prompt, remember


def test_add_list_remove_and_prompt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    claude = tmp_path / "home" / ".claude" / "CLAUDE.md"
    claude.parent.mkdir(parents=True)
    claude.write_text("# 利用者の Claude の決まり\n- テストは pytest で\n", encoding="utf-8")
    assert remember.tool({"action": "add", "text": "返事は日本語で"}).ok
    assert remember.tool({"action": "add", "text": "  返事は日本語で "}).ok          # 同じ物は足さない
    assert remember.tool({"action": "add", "text": "作業フォルダは D:\\work"}).ok
    assert remember.items() == ["返事は日本語で", "作業フォルダは D:\\work"]
    assert not (tmp_path / "blackwall" / "AGENTS.md").exists()          # 全体の指示のファイルは作らない
    # 前置きに入る。★~/.claude/CLAUDE.md（全体の指示の「最初の 1 本」）も今までどおり読まれる
    body = "\n".join(prompt.instructions(str(tmp_path)))
    assert "返事は日本語で" in body and "テストは pytest で" in body
    assert body.index("テストは pytest で") < body.index("返事は日本語で")
    r = remember.tool({"action": "list"})
    assert "1. 返事は日本語で" in str(r)
    assert remember.tool({"action": "remove", "n": 1}).ok
    assert remember.items() == ["作業フォルダは D:\\work"]
    assert remember.tool({"action": "remove", "text": "作業フォルダ"}).ok
    assert remember.items() == [] and not remember.path().exists()
    # Windows の画面の口（JSON 1 行）
    assert remember.main(["add", "朝は", "短く"]) == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["ok"] and out["items"] == ["朝は 短く"]


def test_moves_notes_written_by_0_1_601(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    old = tmp_path / "blackwall" / "AGENTS.md"
    old.parent.mkdir(parents=True)
    old.write_text("\n".join([remember.START, remember.HEAD, "- 返事は短く", remember.END]) + "\n", encoding="utf-8")
    assert remember.items() == ["返事は短く"]
    assert not old.exists()                                           # 空になった＝消して元の読み方へ
    old.write_text("# 自分で書いた決まり\n\n" + "\n".join([remember.START, remember.HEAD, "- 二つ目", remember.END]) + "\n",
                   encoding="utf-8")
    assert remember.items() == ["返事は短く", "二つ目"]
    assert old.read_text(encoding="utf-8") == "# 自分で書いた決まり\n"     # 区画の外は残る


def test_refuses_secrets_and_bad_input(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    r = remember.tool({"action": "add", "text": "GitHub のトークンは ghp_abcdefghijklmnopqrstuvwxyz0123456789"})
    assert not r.ok and "not saved" in str(r)
    assert not remember.tool({"action": "add", "text": ""}).ok
    assert not remember.tool({"action": "add", "text": "あ" * 400}).ok
    assert not remember.tool({"action": "remove", "n": 3}).ok
    assert not remember.tool({"action": "zzz"}).ok
    assert remember.items() == []
