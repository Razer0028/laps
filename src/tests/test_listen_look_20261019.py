"""★2026-10-19 耳の強化（オーナー「耳も強化して」）: Listen の look=true で、音を目で見る画像（上: 波形・下: スペクトログラム）と
長さ・音量・無音の区間を返す。推論機が画像を読めるようになった（KAT に Qwen3.6 の画像の部品）ので、台詞でない音も確かめられる。
ffmpeg が無い機械では飛ばす。whisper の本体は使わない（書き起こしは偽物に差し替える）。
"""
from __future__ import annotations

import base64
import shutil
import subprocess

import pytest

from laps.oc import listen

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg がない")


def _tone_silence_noise(path):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1",
                    "-f", "lavfi", "-i", "anoisesrc=d=1:c=pink:a=0.3",
                    "-filter_complex", "[0:a]aresample=44100,aformat=channel_layouts=mono[a];"
                    "[1:a]aformat=channel_layouts=mono[b];[2:a]aresample=44100,aformat=channel_layouts=mono[c];"
                    "[a][b][c]concat=n=3:v=0:a=1", str(path)], check=True)


def test_sound_picture_has_length_volume_silence_and_a_color_png(tmp_path):
    w = tmp_path / "t.wav"
    _tone_silence_noise(w)
    png, head = listen.sound_picture(w)
    assert png and png.startswith(b"\x89PNG")
    assert "length 3.00 s" in head and "mean volume" in head and "silence 1.00-2.00 s" in head
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(png))
    assert im.size == (1200, 600) and im.mode == "RGB"           # 白黒に落ちていない


def test_listen_look_attaches_the_picture(tmp_path, monkeypatch):
    w = tmp_path / "bgm.wav"
    _tone_silence_noise(w)
    monkeypatch.setattr(listen, "_transcribe", lambda files, **kw: (
        [{"file": str(files[0]), "lang": "en", "text": "", "segments": []}], {"backend": "fake", "elapsed_s": 0.1}))
    r = listen.listen_tool({"path": str(w), "look": True})
    assert r.ok
    assert str(r).startswith("Sound of bgm.wav: length 3.00 s")
    img = r.data["image"]
    assert img["mime"] == "image/png" and base64.b64decode(img["b64"]).startswith(b"\x89PNG")


def test_without_ffmpeg_the_tool_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(listen, "_ffmpeg", lambda: None)
    png, head = listen.sound_picture(tmp_path / "x.wav")
    assert png is None and "needs ffmpeg" in head
