"""shot.get が会話の作業フォルダの中の画像も返す（2026-11-27 オーナー「リモートセッションの場合モデルが提示した画像ファイルとかが
見えない」）。根は会話の meta.json の workspace だけ。外のパス・画像でない物・会話の名指しなしは断る。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from laps import remotectl
from laps.remotectl import CtlError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _setup(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    sid = "20261127_010203_abcdef"
    (home / "sessions" / sid).mkdir(parents=True)
    ws.mkdir()
    (home / "sessions" / sid / "meta.json").write_text(json.dumps({"workspace": str(ws)}), encoding="utf-8")
    return SimpleNamespace(home=str(home), workspace=str(ws)), ws, sid


def test_image_in_the_session_workspace(tmp_path):
    gw, ws, sid = _setup(tmp_path)
    (ws / "out").mkdir()
    (ws / "out" / "chart.png").write_bytes(PNG)
    r = remotectl.shot_get(gw, {"path": str(ws / "out" / "chart.png"), "session": sid})
    assert r["mime"] == "image/png" and r["done"]


def test_refuses_without_session_or_outside_or_not_image(tmp_path):
    gw, ws, sid = _setup(tmp_path)
    (ws / "a.png").write_bytes(PNG)
    with pytest.raises(CtlError):
        remotectl.shot_get(gw, {"path": str(ws / "a.png")})                       # 会話の名指しなし
    other = tmp_path / "other.png"
    other.write_bytes(PNG)
    with pytest.raises(CtlError):
        remotectl.shot_get(gw, {"path": str(other), "session": sid})              # 作業フォルダの外
    (ws / "fake.png").write_bytes(b"not an image at all")
    with pytest.raises(CtlError):
        remotectl.shot_get(gw, {"path": str(ws / "fake.png"), "session": sid})    # 中身が画像でない
    with pytest.raises(CtlError):
        remotectl.shot_get(gw, {"path": str(ws / "a.png"), "session": "../x"})    # 名札の形が違う
