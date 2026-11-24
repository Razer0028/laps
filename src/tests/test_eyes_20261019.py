"""★2026-10-19 Blackwall の目（オーナー「Blackwall の目を強化したい。視覚的にブラウザ操作ができるようにしたり」「通常の画像ファイルも
確認できるようにしておいて」）。推論機（KAT-Coder）に Qwen3.6 の画像の部品を付けた（13/13）ので、道具の側で画像を渡す。

- Browser の look: アプリが撮った画面（番号の札つき）を画像としてモデルへ渡し、札の一覧を文字で添える。click/type は番号・位置で指せる。
- Read: 推論機が開けない形式（WebP・TIFF など）は PNG に直し、長い辺が 2048 を超える画像は縮める。
アプリ（Rust の browser.rs）と推論機は使わない。
"""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from laps.oc import browsertool, failclass
from laps.oc import tools as oc_tools
from laps.tools import appcall


def teardown_function(_f):
    appcall.set_caller(None)


def _png(w=40, h=20, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_look_attaches_the_picture_and_lists_the_numbers():
    seen = []

    def fake(op, args, timeout):
        seen.append(args)
        return {"ok": True, "url": "https://example.com/", "title": "Example", "png": base64.b64encode(_png()).decode(),
                "marks": [{"ref": 1, "kind": "link", "label": "Docs"}, {"ref": 2, "kind": "input text", "label": "Search"}],
                "more_below": True}
    appcall.set_caller(fake, caps=["browser"])
    r = browsertool.run({"action": "look"})
    assert r.ok and seen == [{"action": "look"}]
    s = str(r)
    assert "[1] link Docs" in s and "[2] input text Search" in s and "scroll" in s
    img = r.data["image"]
    assert img["mime"] == "image/png" and base64.b64decode(img["b64"]).startswith(b"\x89PNG")


def test_click_and_type_by_number_or_position():
    seen = []

    def fake(op, args, timeout):
        seen.append(args)
        return {"ok": True, "url": "https://example.com/", "title": "Example", "clicked": "Docs"}
    appcall.set_caller(fake, caps=["browser"])
    assert browsertool.run({"action": "click", "ref": 7}).ok
    assert browsertool.run({"action": "click", "x": 512, "y": 300}).ok
    assert browsertool.run({"action": "type", "ref": 3, "text": "llama.cpp", "submit": True}).ok
    assert browsertool.run({"action": "scroll", "direction": "down"}).ok
    assert browsertool.run({"action": "key", "key": "Escape"}).ok
    assert seen[0] == {"action": "click", "ref": 7}
    assert seen[1] == {"action": "click", "x": 512, "y": 300}
    assert seen[2] == {"action": "type", "ref": 3, "text": "llama.cpp", "submit": True}
    assert seen[3] == {"action": "scroll", "direction": "down"} and seen[4] == {"action": "key", "key": "Escape"}


def test_bad_calls_are_explained_without_asking_the_app():
    appcall.set_caller(lambda *a: pytest.fail("should not be called"), caps=["browser"])
    r = browsertool.run({"action": "click"})
    assert not r.ok and "ref" in str(r)
    assert failclass.classify("Browser", False, "error", str(r)) == "missing_arg"
    r = browsertool.run({"action": "key"})
    assert not r.ok and failclass.classify("Browser", False, "error", str(r)) == "missing_arg"


def test_failclass_names_the_new_browser_errors():
    assert failclass.classify("Browser", False, "error",
                              "Error: no element has number 9 (look again; numbers change after the page changes)") \
        == "browser_no_element"
    assert failclass.classify("Browser", False, "error", "Error: the page picture came back empty") == "browser_no_picture"


def _read(path):
    return oc_tools.read_tool({"file_path": str(path)})


def test_webp_is_converted_to_png(tmp_path):
    p = tmp_path / "photo.webp"
    Image.new("RGB", (64, 32), (10, 120, 200)).save(p, "WEBP")
    r = _read(p)
    assert r.ok, str(r)
    assert "converted from WEBP to PNG" in str(r)
    assert r.data["image"]["mime"] == "image/png"
    assert Image.open(io.BytesIO(base64.b64decode(r.data["image"]["b64"]))).size == (64, 32)


def test_tiff_is_converted_and_big_images_are_resized(tmp_path):
    t = tmp_path / "scan.tiff"
    Image.new("RGB", (30, 30), (0, 0, 0)).save(t, "TIFF")
    r = _read(t)
    assert r.ok and r.data["image"]["mime"] == "image/png" and "converted from TIFF" in str(r)
    big = tmp_path / "big.png"
    Image.new("RGB", (3000, 1000), (255, 255, 255)).save(big, "PNG")
    r = _read(big)
    assert r.ok and "resized from 3000x1000 to 2048x683" in str(r)
    assert Image.open(io.BytesIO(base64.b64decode(r.data["image"]["b64"]))).size == (2048, 683)
    jpg = tmp_path / "big.jpg"
    Image.new("RGB", (2500, 2500), (90, 90, 90)).save(jpg, "JPEG")
    r = _read(jpg)
    assert r.ok and r.data["image"]["mime"] == "image/jpeg" and "resized from 2500x2500 to 2048x2048" in str(r)


def test_small_png_is_sent_as_is(tmp_path):
    p = tmp_path / "small.png"
    raw = _png(100, 50)
    p.write_bytes(raw)
    r = _read(p)
    assert r.ok and "resized" not in str(r) and "converted" not in str(r)
    assert base64.b64decode(r.data["image"]["b64"]) == raw
