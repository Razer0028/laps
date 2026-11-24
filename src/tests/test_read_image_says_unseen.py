"""★2026-09-21 友人の実機: Read が画像の素性しか返していないのに、モデルは「成績（画像より）…（目視）」と
見たように書いた。返す文が**仕組みの話**（base64 の予算）で、「あなたには見えていない」を言っていなかった。"""
import struct
import zlib

from laps.tools.read import _run


def _png(path, w, h):
    raw = b"".join(b"\x00" + b"\x7f" * (w * 3) for _ in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 0)) + chunk(b"IEND", b""))


def test_大きい画像は見えていないと平たく言う(tmp_path):
    p = tmp_path / "backtest_result.png"
    _png(p, 400, 300)
    out = str(_run({"file_path": str(p)}))
    assert "見えていません" in out and "見たように書かないこと" in out
    assert "pixels: 400x300" in out
    assert "data:image" not in out
    assert "identify" not in out and "file " not in out      # Windows に無い道具を案内しない


def test_小さい画像でも見えていないと言う(tmp_path):
    p = tmp_path / "dot.png"
    _png(p, 2, 2)
    out = str(_run({"file_path": str(p)}))
    assert "見えていません" in out and "pixels: 2x2" in out
