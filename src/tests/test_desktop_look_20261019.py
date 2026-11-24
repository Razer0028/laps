"""★2026-10-19 デスクトップ操作の強化（オーナー「デスクトップ操作機能も強化してほしいです」→「B で」）。

Desktop の look: 補助（desktop_helper.cs）が窓を撮り、snapshot と同じ番号の赤い枠を描いて PNG で保存する。道具はその写真を
画像としてモデルへ渡し、枠の無い所は rx・ry（写真の上の 0〜1000）で押せるようにする。MCP（Claude Code）にも画像で返す。
補助は偽物に差し替える（Windows・csc.exe・推論機は使わない）。
"""
from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from laps import mcpserver, remotectl
from laps.oc import desktop


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (240, 240, 240)).save(buf, "PNG")
    return buf.getvalue()


class FakeHelper:
    def __init__(self, picture=(100, 50, 400, 300, 2), size=None):
        self.seen = []
        self.picture = picture
        self.size = size or picture[2:4]

    def call(self, req, timeout):
        self.seen.append(dict(req))
        if req["action"] == "look":
            with open(req["out"], "wb") as f:
                f.write(_png(*self.size))
            head = "picture " + " ".join(str(n) for n in self.picture)
            return {"ok": True, "text": head + '\nWindow w1 "Untitled - Notepad" - notepad (foreground)\n'
                                               '[1] Button "Save"\n[2] Edit "Text editor"'}
        return {"ok": True, "text": "Clicked."}


@pytest.fixture
def helper(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_DESKTOP_DIR", str(tmp_path))
    desktop._LAST_LOOK.clear()
    h = FakeHelper()
    monkeypatch.setattr(desktop, "_get_helper", lambda: h)
    yield h
    desktop._LAST_LOOK.clear()


def test_look_attaches_the_picture_and_keeps_the_ids(helper, tmp_path):
    r = desktop.desktop_tool({"action": "look", "window": "notepad"})
    assert r.ok, str(r)
    req = helper.seen[0]
    assert req["action"] == "look" and req["window"] == "notepad"
    assert req["out"] == str(tmp_path / desktop.LOOK_IMAGE_NAME)
    text = str(r)
    assert "picture 100" not in text                                   # 1 行目は道具あて＝モデルには見せない
    assert "400x300 pixels" in text and "(2 marked)" in text
    assert '[1] Button "Save"' in text and "rx and ry" in text
    img = r.data["image"]
    assert img["path"] == desktop.LOOK_IMAGE_NAME and img["mime"] == "image/png"
    assert Image.open(io.BytesIO(base64.b64decode(img["b64"]))).size == (400, 300)
    assert desktop._LAST_LOOK == {"left": 100, "top": 50, "w": 400, "h": 300}


def test_aliases_and_old_picture_is_not_reused(helper, tmp_path):
    stale = tmp_path / desktop.LOOK_IMAGE_NAME
    stale.write_bytes(b"old")
    r = desktop.desktop_tool({"action": "screenshot"})
    assert r.ok and helper.seen[0]["action"] == "look"
    assert stale.read_bytes() != b"old"


def test_big_window_picture_is_resized(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_DESKTOP_DIR", str(tmp_path))
    h = FakeHelper(picture=(0, 0, 3840, 2160, 0))
    monkeypatch.setattr(desktop, "_get_helper", lambda: h)
    r = desktop.desktop_tool({"action": "look"})
    # ★2026-11-15 から読み手の上限（約 80 万画素）まで縮め、何 % で見ているかを添える（oc/lookaid.py）
    size = Image.open(io.BytesIO(base64.b64decode(r.data["image"]["b64"]))).size
    assert r.ok and f"of 3840x2160 pixels" in str(r) and size[0] * size[1] <= 1024 * 28 * 28 and abs(size[0] / size[1] - 16 / 9) < .01
    desktop._LAST_LOOK.clear()


def test_click_at_rx_ry_uses_the_last_look(helper):
    desktop.desktop_tool({"action": "look"})
    r = desktop.desktop_tool({"action": "click", "rx": 500, "ry": 500})
    assert r.ok
    sent = [s for s in helper.seen if s["action"] == "click"][-1]
    assert sent["x"] == 100 + 200 and sent["y"] == 50 + 150
    assert "rx" not in sent and "ry" not in sent
    desktop.desktop_tool({"action": "scroll", "rx": 1000, "ry": 0, "amount": -3})
    sent = [s for s in helper.seen if s["action"] == "scroll"][-1]
    assert sent["x"] == 100 + 399 and sent["y"] == 50                   # 端は写真の内側に収める


def test_rx_ry_errors_do_not_reach_the_helper(helper):
    r = desktop.desktop_tool({"action": "click", "rx": 10, "ry": 10})
    assert not r.ok and "Use look first" in str(r)
    desktop.desktop_tool({"action": "look"})
    n = len(helper.seen)
    assert "0 to 1000" in str(desktop.desktop_tool({"action": "click", "rx": 1500, "ry": 10}))
    assert "both rx and ry" in str(desktop.desktop_tool({"action": "click", "rx": 10}))
    assert len(helper.seen) == n
    r = desktop.desktop_tool({"action": "click", "id": 7, "rx": 10, "ry": 10})   # 番号が先
    assert r.ok and helper.seen[-1]["id"] == 7 and "x" not in helper.seen[-1]


def test_helper_without_a_picture_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_DESKTOP_DIR", str(tmp_path))

    class Old:
        def call(self, req, timeout):
            return {"ok": True, "text": "Window w1 \"x\" - x"}
    monkeypatch.setattr(desktop, "_get_helper", lambda: Old())
    r = desktop.desktop_tool({"action": "look"})
    assert not r.ok and "did not describe the picture" in str(r)


def test_schema_and_description_offer_look():
    props = desktop.schema()["properties"]
    assert "look" in props["action"]["enum"] and "rx" in props and "ry" in props
    assert "- look:" in desktop.DESKTOP_DESCRIPTION


def test_build_tries_look_then_ocr_then_plain():
    src = (desktop.HELPER_SOURCE.parent / "desktop.py").read_text(encoding="utf-8")
    assert src.index("/define:BW_OCR;BW_LOOK") < src.index('["/define:BW_OCR"]')
    cs = desktop.HELPER_SOURCE.read_text(encoding="utf-8")
    assert 'case "look": return Look(r);' in cs and "#if BW_LOOK" in cs


def test_gateway_passes_the_picture_and_shrinks_big_ones(monkeypatch):
    raw = _png(64, 32)
    r = remotectl._desktop_image({"image": {"mime": "image/png", "b64": base64.b64encode(raw).decode()}})
    assert r["mime"] == "image/png" and base64.b64decode(r["b64"]) == raw
    monkeypatch.setattr(remotectl, "DESKTOP_IMAGE_MAX", 200)
    noisy = Image.effect_noise((300, 300), 80).convert("RGB")
    buf = io.BytesIO()
    noisy.save(buf, "PNG")
    assert remotectl._desktop_image({"image": {"b64": base64.b64encode(buf.getvalue()).decode()}}) is None
    assert remotectl._desktop_image(None) is None and remotectl._desktop_image({"image": {}}) is None


def test_mcp_returns_the_picture_as_an_image(monkeypatch):
    from laps import ctlclient as cc
    monkeypatch.setattr(cc, "resolve", lambda name: {"id": "d1", "name": "PC"})
    monkeypatch.setattr(cc, "call", lambda dev, op, a, timeout: {
        "ok": True, "result": {"ok": True, "text": "The picture of the window ...",
                               "image": {"mime": "image/png", "b64": "QUJD"}}})
    ans = mcpserver._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "desktop", "arguments": {"action": "look", "machine": "PC"}}})
    content = ans["result"]["content"]
    assert content[0]["type"] == "text" and content[0]["text"].startswith("[PC] The picture")
    assert content[1] == {"type": "image", "data": "QUJD", "mimeType": "image/png"}
    assert ans["result"]["isError"] is False
    json.dumps(ans)
    # 写真の無い答えは今までどおり文字だけ
    monkeypatch.setattr(cc, "call", lambda dev, op, a, timeout: {"ok": True, "result": {"ok": True, "text": "Clicked."}})
    ans = mcpserver._handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "desktop", "arguments": {"action": "click", "machine": "PC", "id": 3}}})
    assert len(ans["result"]["content"]) == 1
