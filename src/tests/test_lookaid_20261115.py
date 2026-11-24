"""画面写真の支度（読み手の上限に縮める・目盛り・寄せ）2026-11-15。"""
from __future__ import annotations

import io

from PIL import Image

from laps.oc import lookaid


def _png(w, h, color=(30, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_a_large_screen_is_shrunk_to_the_reader_budget_and_says_so():
    out, s = lookaid.prepare(_png(2560, 1440))
    im = Image.open(io.BytesIO(out))
    assert im.size == (s.w, s.h) and s.w * s.h <= lookaid.DEFAULT_PIXELS and (s.w0, s.h0) == (2560, 1440)
    n = lookaid.note(s)
    assert "shown at" in n and "region=" in n and "every 100" in n


def test_a_small_picture_keeps_its_size_and_gets_the_grid(monkeypatch):
    out, s = lookaid.prepare(_png(800, 600))
    assert (s.w, s.h) == (800, 600) and s.grid
    im = Image.open(io.BytesIO(out)).convert("RGB")
    assert im.getpixel((80, 300)) != (30, 30, 30)          # x=100 の線（800 の 1/10）
    assert "shown at" not in lookaid.note(s)
    monkeypatch.setenv("LAPS_LOOK_GRID", "0")
    out, s = lookaid.prepare(_png(800, 600))
    assert not s.grid and Image.open(io.BytesIO(out)).convert("RGB").getpixel((80, 300)) == (30, 30, 30)


def test_a_point_just_outside_a_box_snaps_to_its_center():
    box = {"x": 100, "y": 40, "w": 50, "h": 20, "n": 3}
    assert lookaid.snap(95, 50, [box])[:2] == (125.0, 50.0)       # 5 px 左
    assert lookaid.snap(130, 50, [box])[2] is None                  # 枠の中はそのまま
    assert lookaid.snap(300, 300, [box])[2] is None                 # 遠ければ寄せない


def test_a_broken_picture_is_left_alone():
    assert lookaid.prepare(b"not an image") is None
