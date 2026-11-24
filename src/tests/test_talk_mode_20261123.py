"""★2026-11-23 チャットモード（オーナー「チャットモードがほしい（作業フォルダとかいらないやつ）」）。

新しい中核（laps/engine）の toolset="talk"。作業フォルダーを持たない会話で、道具はウェブと Umbra だけ。
★"chat" は画面の Ask モード（読む道具つき・tests/test_ask_mode_20261112.py）。名前を取り違えない。
"""
from __future__ import annotations

import os

from laps.engine import prompt, server
from laps.engine.tools import base_tools


def test_talk_tools_cannot_touch_files_or_machines():
    tools = set(base_tools("talk"))
    assert tools == {"WebSearch", "WebFetch", "Image"}     # Image はウェブの画像だけ（2026-11-24）
    # Ask の組（"chat"）は今までどおり読む道具を持つ＝取り違えていない
    assert {"Read", "Bash"} <= set(base_tools("chat"))


def test_server_accepts_talk_and_keeps_hands_off():
    assert "talk" in server.TOOLSETS
    assert "talk" in server.HANDS_OFF            # 取り消しの控え・記憶・TODO を行わない
    spec = server.parse({"prompt": "こんにちは", "tools": "talk"}) if hasattr(server, "parse") else None
    if spec is not None:
        assert spec.toolset == "talk"


def test_talk_prompt_has_no_workspace_sections(tmp_path):
    (tmp_path / "AGENTS.md").write_text("THIS-IS-A-WORKSPACE-INSTRUCTION\n", encoding="utf-8")
    s = prompt.system(str(tmp_path), "m", "bash", toolset="talk", language="Japanese")
    assert s.startswith(prompt.TALK)
    assert "Working directory" not in s and str(tmp_path) not in s
    assert "THIS-IS-A-WORKSPACE-INSTRUCTION" not in s     # 作業場の指示ファイルを読まない
    assert "Scratch folder" not in s and "Today's date" in s and "Japanese" in s
    # Agent の前置きは変わらない
    full = prompt.system(os.getcwd(), "m", "bash", toolset="full")
    assert full.startswith(prompt.BASE.replace("{PARALLEL}", prompt._parallel()).replace("{BG}", prompt._bg()))
