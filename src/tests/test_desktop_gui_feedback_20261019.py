"""★2026-10-19 GUI 操作の強化（オーナー「GUI操作がどのモデルも苦手みたいです…ローカルLLMでもうまく操作できるように」）。

実走行（0.1.550・Gemma 4・部品の木が無いゲーム）で見えた失敗を、補助を偽物に差し替えて確かめる（Windows・推論機は使わない）:
- look に screen と window を両方渡すと画面全体が撮られていた → 窓を撮る
- OCR の @x,y を書き写し違えて外れた所を押し続けた → click の text= で文字を探して押す
- 押した結果が「Clicked at … on Pane」だけで、効いたか分からないまま同じ所を押し続けた → 前後の写真で変化を言葉で返す
"""
from __future__ import annotations

import io

import pytest
from PIL import Image

from laps.oc import desktop

OCR_TEXT = ('OCR of Window w1 "Game" - game on s2 (positions are screen pixels: click with x and y)\n'
            '3 rows of text ("text" @x,y = its center):\n'
            '"胸を揉む" @3540,45   "ＳＴＡＲＴ" @2880,700\n'
            '"何もしない" @3359,165\n'
            '"Options menu" @2000,900')


def _png(color, size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class Helper:
    """change=True なら click の後の写真を変える。"""

    def __init__(self, change=False):
        self.seen = []
        self.change = change
        self.clicked = False

    def call(self, req, timeout):
        self.seen.append(dict(req))
        a = req["action"]
        if a == "ocr":
            return {"ok": True, "text": OCR_TEXT}
        if a == "look":
            color = (200, 30, 30) if (self.change and self.clicked) else (240, 240, 240)
            with open(req["out"], "wb") as f:
                f.write(_png(color))
            x0, y0 = req.get("x0", 0), req.get("y0", 0)
            return {"ok": True, "text": f"picture {x0} {y0} 64 48 0\nZoomed"}
        if a == "click":
            self.clicked = True
            return {"ok": True, "text": f"Clicked at {req.get('x')},{req.get('y')} on Pane \"Game\"."}
        return {"ok": True, "text": "ok"}

    def clicks(self):
        return [s for s in self.seen if s["action"] == "click"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("LAPS_DESKTOP_DIR", str(tmp_path))
    monkeypatch.setattr(desktop, "_guard_pids", lambda: [])
    desktop._LAST_LOOK.clear()
    desktop._RECENT_CLICKS.clear()

    def make(change=False):
        h = Helper(change)
        monkeypatch.setattr(desktop, "_get_helper", lambda: h)
        return h
    yield make
    desktop._LAST_LOOK.clear()
    desktop._RECENT_CLICKS.clear()


def test_look_with_window_and_screen_takes_the_window(env):
    h = env()
    desktop.desktop_tool({"action": "look", "screen": 2, "window": "w1"})
    sent = h.seen[0]
    assert sent["window"] == "w1" and "screen" not in sent
    desktop.desktop_tool({"action": "look", "screen": 2})
    assert h.seen[-1]["screen"] == 2                                   # 窓が無ければ今までどおり画面全体


def test_click_by_text_uses_the_ocr_center(env):
    h = env()
    r = desktop.desktop_tool({"action": "click", "text": "胸を揉む", "window": "w1"})
    assert r.ok, str(r)
    c = h.clicks()[-1]
    assert (c["x"], c["y"]) == (3540, 45) and c["window"] == "w1" and "text" not in c
    assert "Found \"胸を揉む\" by OCR at 3540,45" in str(r)
    ocr = [s for s in h.seen if s["action"] == "ocr"][0]
    assert ocr["window"] == "w1"


def test_click_by_text_is_width_and_case_insensitive_and_partial(env):
    h = env()
    assert desktop.desktop_tool({"action": "click", "text": "start", "window": "w1"}).ok
    assert (h.clicks()[-1]["x"], h.clicks()[-1]["y"]) == (2880, 700)
    assert desktop.desktop_tool({"action": "click", "text": "Options", "window": "w1"}).ok
    assert (h.clicks()[-1]["x"], h.clicks()[-1]["y"]) == (2000, 900)


def test_click_by_text_not_found_lists_what_ocr_read_and_does_not_click(env):
    h = env()
    r = desktop.desktop_tool({"action": "click", "text": "Continue", "window": "w1"})
    assert not r.ok
    assert "was not found" in str(r) and "何もしない" in str(r)
    assert h.clicks() == []


def test_click_by_id_ignores_text_and_takes_no_pictures(env):
    h = env()
    desktop.desktop_tool({"action": "click", "id": 5, "text": "whatever"})
    assert [s["action"] for s in h.seen] == ["click"]


def test_click_with_no_change_says_so_and_warns_on_repeat(env):
    h = env(change=False)
    r1 = desktop.desktop_tool({"action": "click", "x": 3540, "y": 85, "window": "w1"})
    assert r1.ok and "Nothing changed on screen" in str(r1)
    assert "same click as the last one" not in str(r1)
    acts = [s["action"] for s in h.seen]
    assert acts == ["focus", "look", "click", "look"]                   # 前へ出す → 撮る → 押す → 撮る
    r2 = desktop.desktop_tool({"action": "click", "x": 3541, "y": 86, "window": "w1"})
    assert "same click as the last one" in str(r2)


def test_click_that_changes_the_screen_says_so(env):
    env(change=True)
    r = desktop.desktop_tool({"action": "click", "x": 100, "y": 100})
    assert r.ok and "The screen changed after the click" in str(r)
    assert "Nothing changed" not in str(r)


def test_click_region_is_the_last_look_when_the_point_is_inside(env):
    h = env()
    desktop._LAST_LOOK.update(left=1920, top=0, w=1920, h=1080)
    desktop.desktop_tool({"action": "click", "x": 2000, "y": 500})
    shot = [s for s in h.seen if s["action"] == "look"][0]
    assert (shot["x0"], shot["y0"], shot["x1"], shot["y1"]) == (1920, 0, 3840, 1080)
    assert desktop._LAST_LOOK == {"left": 1920, "top": 0, "w": 1920, "h": 1080}   # 確かめの写真で rx・ry の基準を変えない


def test_failed_pictures_only_skip_the_check(env, monkeypatch):
    class NoLook(Helper):
        def call(self, req, timeout):
            if req["action"] == "look":
                self.seen.append(dict(req))
                return {"ok": False, "text": "Error: look is not available"}
            return super().call(req, timeout)
    h = NoLook()
    monkeypatch.setattr(desktop, "_get_helper", lambda: h)
    r = desktop.desktop_tool({"action": "click", "x": 10, "y": 10})
    assert r.ok and "Clicked at 10,10" in str(r) and "changed" not in str(r)


def test_description_teaches_games_for_text_only_models():
    for vision in (True, False):
        d = desktop.description(vision)
        assert "Games and drawn windows" in d and "text=" in d and "alt+f4" in d
    assert "`text`: words drawn on screen" in desktop.description(False)
    assert "OCR" in desktop.schema(False)["properties"]["text"]["description"]
