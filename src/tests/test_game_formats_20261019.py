"""ゲームの形式を増やした分（2026-10-19 オーナー「あらゆるゲームに対応できるように」）。

formats_more に足した VPK・BSA・BA2・SWF・XNB・RE Engine・CPK・Unity WebGL・asar の構造と、
identify.py のエンジン判定・次の一手を、小さな合成ファイルで確かめる。
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

from laps.oc import formats_more as fm

IDENTIFY = Path(fm.__file__).parent / "skills" / "game-file-analysis" / "scripts" / "identify.py"


def _desc(p: Path) -> str:
    data = p.read_bytes()
    rows = fm.describe_more(str(p), data[:4096], len(data))
    assert rows, p.name
    return "\n".join(rows)


def _vpk_bytes() -> bytes:
    def entry(name: str) -> bytes:
        return name.encode() + b"\x00" + struct.pack("<IHHIIH", 0, 0, 0, 0, 10, 0xFFFF)
    tree = (b"vmt\x00" + b"materials/x\x00" + entry("a") + entry("b") + b"\x00" + b"\x00"
            + b"mdl\x00" + b"models\x00" + entry("hero") + b"\x00" + b"\x00" + b"\x00")
    return struct.pack("<III", 0x55AA1234, 1, len(tree)) + tree


def test_vpk_lists_files(tmp_path: Path) -> None:
    p = tmp_path / "pak01_dir.vpk"
    p.write_bytes(_vpk_bytes())
    out = _desc(p)
    assert "Source エンジンの VPK" in out and "ファイル 3 個" in out
    assert "materials/x/a.vmt" in out and "models/hero.mdl" in out


def test_bethesda_archives(tmp_path: Path) -> None:
    p = tmp_path / "x.bsa"
    p.write_bytes(b"BSA\x00" + struct.pack("<IIIII", 105, 36, 4, 7, 99) + b"\x00" * 20)
    assert "Skyrim SE" in _desc(p) and "ファイル 99" in _desc(p)
    q = tmp_path / "x.ba2"
    q.write_bytes(b"BTDX" + struct.pack("<I", 1) + b"DX10" + struct.pack("<I", 12) + b"\x00" * 16)
    assert "テクスチャ" in _desc(q) and "ファイル 12" in _desc(q)


def test_swf_xnb_re_cpk(tmp_path: Path) -> None:
    (tmp_path / "a.swf").write_bytes(b"CWS\x0a" + struct.pack("<I", 2 << 20) + b"\x78\x9c")
    assert "zlib" in _desc(tmp_path / "a.swf") and "ffdec" in _desc(tmp_path / "a.swf")
    (tmp_path / "a.xnb").write_bytes(b"XNBw\x05\x00" + struct.pack("<I", 1234) + b"\x00" * 8)
    assert "Windows" in _desc(tmp_path / "a.xnb") and "xnbcli" in _desc(tmp_path / "a.xnb")
    (tmp_path / "re_chunk_000.pak").write_bytes(b"KPKA" + struct.pack("<II", 4, 321) + b"\x00" * 8)
    assert "ファイル 321" in _desc(tmp_path / "re_chunk_000.pak")
    (tmp_path / "a.cpk").write_bytes(b"CPK " + b"\x00" * 12 + b"@UTF" + b"\x00" * 12)
    assert "そのまま読める" in _desc(tmp_path / "a.cpk")


def test_unity_webgl_data(tmp_path: Path) -> None:
    files = [("data.unity3d", 100), ("Il2CppData/Metadata/global-metadata.dat", 50)]
    idx = b""
    for n, s in files:
        idx += struct.pack("<III", 0, s, len(n)) + n.encode()
    hdr = 20 + len(idx)
    p = tmp_path / "Build.data"
    p.write_bytes(b"UnityWebData1.0\x00" + struct.pack("<I", hdr) + idx + b"\x00" * 150)
    out = _desc(p)
    assert "中のファイル 2 個" in out and "global-metadata.dat" in out


def test_asar(tmp_path: Path) -> None:
    tree = json.dumps({"files": {"main.js": {"size": 1, "offset": "0"},
                                 "data": {"files": {"scenario": {"files": {"a.ks": {"size": 1, "offset": "1"}}}}}}})
    raw = tree.encode()
    p = tmp_path / "app.asar"
    p.write_bytes(struct.pack("<IIII", 4, len(raw) + 8, len(raw) + 4, len(raw)) + raw + b"ab")
    out = _desc(p)
    assert "ファイル 2 個" in out and "data/scenario/a.ks" in out


def test_identify_engines_and_next_step(tmp_path: Path) -> None:
    (tmp_path / "hl2").mkdir()
    (tmp_path / "hl2" / "pak01_dir.vpk").write_bytes(_vpk_bytes())
    (tmp_path / "Content").mkdir()
    (tmp_path / "Content" / "x.xnb").write_bytes(b"XNBw\x05\x00" + b"\x00" * 10)
    (tmp_path / "game.love").write_bytes(b"PK\x03\x04" + b"\x00" * 10)
    exe = tmp_path / "tool.exe"
    exe.write_bytes(b"MZ" + b"\x00" * 600 + b"MEI\x0c\x0b\x0a\x0b\x0e" + b"\x00" * 80)
    r = subprocess.run([sys.executable, str(IDENTIFY), str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    for eng in ("Source engine", "XNA / FNA / MonoGame", "LOVE (Love2D)"):
        assert eng in out
    assert "next: xnbcli" in out
    assert "PyInstaller executable" in out and "Source engine VPK" in out


def test_identify_does_not_call_every_exe_a_pak(tmp_path: Path) -> None:
    """.exe の尾も読むようにしたので、Unreal の印を .exe に当てない。"""
    (tmp_path / "a.exe").write_bytes(b"MZ" + b"\x00" * 600 + b"\xe1\x12\x6f\x5a" + b"\x00" * 40)
    r = subprocess.run([sys.executable, str(IDENTIFY), str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert "Unreal .pak" not in r.stdout and "Windows executable" in r.stdout
