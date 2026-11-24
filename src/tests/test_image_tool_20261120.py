"""★2026-11-20 道具 Image（engine/tools/image.py）: 手元・ウェブの画像を会話の shots/ に写して画面に出す。推論機は使わない。"""
from __future__ import annotations

import http.server
import os
import socketserver
import threading

import pytest

from laps.engine.tools import Ctx, ext, image, shell

PIL = pytest.importorskip("PIL.Image")


@pytest.fixture
def ctx(tmp_path):
    return Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event(), jobs=shell.Jobs())


def test_local_image_is_copied_into_shots(ctx, tmp_path):
    PIL.new("RGB", (64, 64), (0, 90, 200)).save(tmp_path / "small.jpg")
    r = image.show({"path": "small.jpg", "caption": "青"}, ctx)
    assert r.ok and r.shot and os.path.dirname(r.shot).endswith("shots") and r.head == "青"
    assert f"![青]({r.shot})" in r.output                      # 本文に出す書き方を返す


def test_big_image_is_shrunk(ctx, tmp_path):
    PIL.new("RGB", (4000, 3000), (200, 30, 30)).save(tmp_path / "big.png")
    r = image.show({"path": str(tmp_path / "big.png")}, ctx)
    assert r.ok and r.shot.endswith(".jpg") and "2560x1920" in r.output


def test_refuses_svg_non_images_and_other_schemes(ctx, tmp_path):
    (tmp_path / "x.svg").write_text("<svg/>")
    (tmp_path / "a.txt").write_text("x")
    assert not image.show({"path": "x.svg"}, ctx).ok
    assert not image.show({"path": "a.txt"}, ctx).ok
    assert not image.show({"url": "file:///etc/passwd"}, ctx).ok
    assert not image.show({}, ctx).ok


def test_url_image_and_page(ctx, tmp_path, monkeypatch):
    PIL.new("RGB", (32, 32), (0, 200, 0)).save(tmp_path / "g.png")
    (tmp_path / "page.html").write_text("<html></html>")
    monkeypatch.chdir(tmp_path)
    H = type("H", (http.server.SimpleHTTPRequestHandler,), {"log_message": lambda *a: None})
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        r = image.show({"url": base + "/g.png"}, ctx)
        assert r.ok and r.shot.endswith(".png")
        bad = image.show({"url": base + "/page.html"}, ctx)
        assert not bad.ok and "not an image" in bad.output
    finally:
        srv.shutdown()


def test_listed_and_callable_through_tools(ctx, tmp_path):
    PIL.new("RGB", (16, 16)).save(tmp_path / "t.png")
    assert "Image" in ext.LISTED and "Image" in ext._native()
    r = ext.tools({"name": "Image", "input": {"path": "t.png"}}, ctx)
    assert r.ok and r.shot
