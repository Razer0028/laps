#!/usr/bin/env python3
"""Identify a game's engine and data formats by magic bytes and folder markers.

  python identify.py "<game folder>" [--top 30] [--max-files 200000]

Prints: engine verdicts with evidence, counts per detected format (with the tool to use),
and the biggest files with their detected format. No dependencies.
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

# (offset, magic bytes, label, tool hint)
SIGNATURES = [
    (0, b"UnityFS", "Unity AssetBundle (UnityFS)", "scripts/unity_extract.py (UnityPy) / AssetRipper"),
    (0, b"UnityWeb", "Unity AssetBundle (old UnityWeb)", "UnityPy / AssetRipper"),
    (0, b"UnityRaw", "Unity AssetBundle (old UnityRaw)", "UnityPy / AssetRipper"),
    (0, b"FSB5", "FMOD sample bank (FSB5)", "vgmstream-cli"),
    (0, b"BKHD", "Wwise soundbank (.bnk)", "vgmstream-cli"),
    (0, b"AKPK", "Wwise file package (.pck)", "vgmstream-cli (or bnkextr / quickbms wwise script)"),
    (0, b"@UTF", "CRI table (ACB/ACF/CPK header)", "vgmstream-cli for .acb (keep the .awb next to it)"),
    (0, b"AFS2", "CRI AWB wave bank", "vgmstream-cli (give it the matching .acb)"),
    (0, b"CPK ", "CRI CPK archive", "CriPakTools / YACpkTool"),
    (0, b"CRID", "CRI USM video", "WannaCRI / VGMToolbox (demux), then ffmpeg"),
    (0, b"GDPC", "Godot package (.pck)", "GDRE Tools (gdsdecomp)"),
    (0, b"RPA-", "Ren'Py archive (.rpa)", "unrpa"),
    (0, b"UnityWebData1.0", "Unity WebGL data (.data)", "UnityPy (load the .data file) / AssetRipper"),
    (0, b"\x34\x12\xaa\x55", "Source engine VPK", "vpk (pip install vpk) / GCFScape"),
    (0, b"VBSP", "Source engine map (.bsp)", "BSPSource / Crowbar"),
    (0, b"IDST", "Source engine model (.mdl)", "Crowbar (decompile) / Blender Source Tools"),
    (0, b"WAD3", "GoldSrc texture WAD", "GCFScape / Wally"),
    (0, b"IWAD", "Doom WAD", "SLADE"),
    (0, b"PWAD", "Doom WAD (patch)", "SLADE"),
    (0, b"PACK", "Quake PAK", "PakScape / 7z"),
    (0, b"BSA\x00", "Bethesda archive (.bsa)", "BSA Browser / bsarch"),
    (0, b"BTDX", "Bethesda archive (.ba2)", "BSA Browser / Archive2"),
    (0, b"TES4", "Bethesda plugin (.esm/.esp)", "xEdit (SSEEdit/FO4Edit)"),
    (0, b"KPKA", "RE Engine archive (.pak)", "REtool + a file-name list"),
    (0, b"FWS", "Flash SWF (uncompressed)", "JPEXS Free Flash Decompiler (ffdec)"),
    (0, b"CWS", "Flash SWF (zlib)", "JPEXS Free Flash Decompiler (ffdec)"),
    (0, b"ZWS", "Flash SWF (LZMA)", "JPEXS Free Flash Decompiler (ffdec)"),
    (0, b"XNBw", "XNA/MonoGame content (.xnb)", "xnbcli / XNBExtract"),
    (0, b"XNBm", "XNA/MonoGame content (.xnb)", "xnbcli / XNBExtract"),
    (0, b"XNBx", "XNA/MonoGame content (.xnb)", "xnbcli / XNBExtract"),
    (0, b"XNBa", "XNA/MonoGame content (.xnb)", "xnbcli / XNBExtract"),
    (0, b"XNBi", "XNA/MonoGame content (.xnb)", "xnbcli / XNBExtract"),
    (0, b"AFS\x00", "CRI AFS archive", "QuickBMS / AFSPacker / GARbro"),
    (0, b"KIF\x00", "CatSystem2 archive (.int)", "GARbro"),
    (0, b"SARC", "Nintendo SARC archive", "SARC-Tool / Switch Toolbox"),
    (0, b"Yaz0", "Nintendo Yaz0-compressed", "Switch Toolbox / yaz0 decompress"),
    (0, b"NARC", "Nintendo DS NARC archive", "Tinke / ndstool"),
    (0, b"\x1bLua", "Lua bytecode", "unluac / luadec"),
    (0, b"\x1bLJ", "LuaJIT bytecode", "luajit-decompiler"),
    (0, b"glTF", "glTF binary model (.glb/.vrm)", "Read (structure) / Blender"),
    (0, b"Kaydara FBX Binary", "FBX model", "Read (structure) / Blender / assimp"),
    (0, b"PMX ", "MMD model (.pmx)", "Read (structure) / Blender + mmd_tools"),
    (0, b"Vocaloid Motion Data", "MMD motion (.vmd)", "Read / Blender + mmd_tools"),
    (0, b"MOC3", "Live2D Cubism model (.moc3)", "Read (structure) / Cubism Viewer"),
    (0, b"\xabKTX 20\xbb", "KTX2 texture", "ktx (KTX-Software) / texconv"),
    (0, b"\xabKTX 11\xbb", "KTX texture", "PVRTexTool / texconv"),
    (0, b"PVR\x03", "PowerVR texture (.pvr)", "PVRTexTool"),
    (0, b"\x13\xab\xa1\x5c", "ASTC texture", "astcenc -d"),
    (0, b"8BPS", "Photoshop image (.psd)", "Read (layers) / GIMP / psd-tools"),
    (0, b"\x28\xb5\x2f\xfd", "zstd-compressed data", "zstd -d"),
    (0, b"\x04\x22\x4d\x18", "LZ4 frame", "lz4 -d"),
    (0, b"\xfd7zXZ\x00", "xz-compressed data", "xz -d"),
    (0, b"\x1f\x8b", "gzip-compressed data (also .unityweb)", "gzip -d / 7z"),
    (0, b"XP3\r\n", "Kirikiri archive (.xp3)", "GARbro"),
    (0, b"RGSSAD\x00", "RPG Maker XP/VX/Ace archive (RGSS)", "encrypted: only for the user's own project"),
    (0, b"RPGMV\x00\x00\x00", "RPG Maker MV/MZ encrypted asset", "encrypted: only for the user's own project"),
    (0, b"-==--==--==--==-", "Unreal IoStore table of contents (.utoc)", "FModel / retoc"),
    (0, b"PK\x03\x04", "ZIP archive (also .jar/.apk/.pak of some engines)", "7z / unzip"),
    (0, b"7z\xbc\xaf\x27\x1c", "7-Zip archive", "7z"),
    (0, b"Rar!\x1a\x07", "RAR archive", "7z / unrar"),
    (0, b"\x89PNG\r\n\x1a\n", "PNG image", ""),
    (0, b"\xff\xd8\xff", "JPEG image", ""),
    (0, b"DDS ", "DDS texture", "texconv / GIMP / Pillow"),
    (0, b"OggS", "Ogg (Vorbis/Opus) audio", "plays as-is / ffmpeg"),
    (0, b"fLaC", "FLAC audio", "ffmpeg"),
    (0, b"ID3", "MP3 audio", ""),
    (0, b"KB2", "Bink 2 video", "RAD Video Tools / ffmpeg (Bink 1 only)"),
    (0, b"BIK", "Bink video", "ffmpeg"),
    (0, b"\x1aE\xdf\xa3", "Matroska/WebM video", "ffmpeg"),
    (4, b"ftyp", "MP4/M4A media", "ffmpeg"),
    (0, b"SQLite format 3\x00", "SQLite database", "sqlite3"),
    (0, b"MZ", "Windows executable/DLL", "ILSpy for .NET assemblies"),
    (0, b"\x7fELF", "Linux executable", ""),
]
PAK_MAGIC = b"\xe1\x12\x6f\x5a"   # Unreal .pak footer magic (little-endian 0x5A6F12E1)

ENGINE_MARKERS = [
    ("Unity", lambda p, n: n in ("unityplayer.dll", "globalgamemanagers", "globalgamemanagers.assets")
        or n.startswith("sharedassets") and n.endswith(".assets") or n.endswith(".ress")),
    ("Unity IL2CPP", lambda p, n: n in ("gameassembly.dll", "global-metadata.dat")),
    ("Unity Mono", lambda p, n: n == "assembly-csharp.dll"),
    ("Unity Addressables", lambda p, n: "streamingassets/aa/" in p and n == "catalog.json"),
    ("Unreal Engine", lambda p, n: n.endswith((".pak", ".utoc", ".ucas", ".uasset")) or "/content/paks/" in p),
    ("Godot", lambda p, n: n.endswith(".pck")),
    ("Ren'Py", lambda p, n: n.endswith((".rpa", ".rpyc", ".rpy")) or "/renpy/" in p),
    ("RPG Maker MV/MZ", lambda p, n: n in ("rpg_core.js", "rmmz_core.js") or p.endswith("/data/system.json")
        or n.endswith((".rpgmvp", ".rpgmvo", ".png_", ".ogg_"))),
    ("RPG Maker XP/VX/Ace", lambda p, n: n.endswith((".rgssad", ".rgss2a", ".rgss3a"))),
    ("GameMaker", lambda p, n: n in ("data.win", "game.unx", "game.ios")),
    ("Kirikiri", lambda p, n: n.endswith(".xp3")),
    ("Electron / NW.js", lambda p, n: n in ("app.asar", "package.nw")),
    ("TyranoScript", lambda p, n: "/tyrano/" in p),
    ("Wolf RPG Editor", lambda p, n: n.endswith(".wolf") or p.endswith("/data/basicdata/game.dat")),
    ("Unity WebGL", lambda p, n: n.endswith((".unityweb", ".framework.js", ".framework.js.br", ".framework.js.gz",
                                               ".data.br", ".data.gz")) or n == "unityloader.js"),
    ("Source engine", lambda p, n: n.endswith("_dir.vpk") or n == "gameinfo.txt"),
    ("Bethesda (Creation/Gamebryo)", lambda p, n: n.endswith((".bsa", ".ba2", ".esm"))),
    ("RE Engine", lambda p, n: n.startswith("re_chunk_000") and n.endswith(".pak")),
    ("Frostbite", lambda p, n: n == "layout.toc" or n.endswith(".cas") and n.startswith("cas_")),
    ("XNA / FNA / MonoGame", lambda p, n: n.endswith(".xnb") or n in ("fna.dll", "monogame.framework.dll",
                                                                     "microsoft.xna.framework.dll")),
    ("LOVE (Love2D)", lambda p, n: n.endswith(".love") or n == "love.dll"),
    ("Cocos2d-x / Cocos Creator", lambda p, n: n.startswith("libcocos2d") or n.endswith(".jsc")
        or n in ("cocos2d-jsb.js", "cc.js") or "/cocos-js/" in p),
    ("Defold", lambda p, n: n.endswith((".arcd", ".arci", ".dmanifest"))),
    ("Construct 2/3", lambda p, n: n in ("c2runtime.js", "c3runtime.js")),
    ("Flash / AIR", lambda p, n: n.endswith(".swf") or n == "adobe air.dll"),
    ("NScripter", lambda p, n: n == "nscript.dat" or n.endswith((".nsa", ".sar", ".ns2"))),
    ("SiglusEngine", lambda p, n: n in ("scene.pck", "gameexe.dat", "siglusengine.exe")),
    ("CatSystem2", lambda p, n: n in ("cs2.exe", "cs2conf.dll") or n.endswith(".int") and "/" not in p[1:]),
    ("Artemis", lambda p, n: n.endswith(".pfs") or n.startswith("root.pfs")),
    ("Python / PyInstaller", lambda p, n: n.endswith(".pyz") or n.startswith("python3") and n.endswith(".dll")),
    ("Java (LWJGL / libGDX)", lambda p, n: n.startswith("lwjgl") or n.startswith("gdx") and n.endswith((".dll", ".so", ".jar"))),
    ("HTML5 (web game)", lambda p, n: n in ("phaser.js", "phaser.min.js", "pixi.js", "pixi.min.js", "three.module.js")),
]

#: エンジン → 次に使う道具（web: はウェブで探す物）。判定の後に「次に何をするか」を 1 行で示すため。
NEXT_STEP = {
    "Unity": "Read on <Game>_Data files (find=/path_id=), AssetStudioModCLI for export, AssetRipper for a project",
    "Unity IL2CPP": "Il2CppDumper or Cpp2IL (GameAssembly.dll + global-metadata.dat) for type/method names",
    "Unity Mono": "Read on Managed/Assembly-CSharp.dll; ilspycmd for method bodies",
    "Unity WebGL": "decompress .br/.gz first (brotli -d / gzip -d), then UnityPy or AssetRipper on the .data",
    "Unreal Engine": "Read on .pak/.utoc; repak / retoc to extract; FModel (UE5) or UModel (models)",
    "Godot": "GDRE_tools (gdre_tools --headless --recover=<pck or exe>)",
    "Ren'Py": "unrpa for .rpa, unrpyc for .rpyc",
    "RPG Maker MV/MZ": "RPGMakerDecrypter, or the XOR steps in SKILL.md with encryptionKey from data/System.json",
    "RPG Maker XP/VX/Ace": "RPGMakerDecrypter",
    "GameMaker": "UTMT_CLI (UndertaleModCli) on data.win",
    "Kirikiri": "GARbro (Windows) for .xp3",
    "Electron / NW.js": "npx @electron/asar extract app.asar out (package.nw is a zip)",
    "TyranoScript": "plain HTML/JS under data/ (or inside app.asar / package.nw)",
    "Wolf RPG Editor": "WolfDec (Windows) for .wolf",
    "Source engine": "vpk (pip: vpk -x out pak01_dir.vpk); models with Crowbar; maps with BSPSource",
    "Bethesda (Creation/Gamebryo)": "BSA Browser / bsarch for .bsa/.ba2; xEdit for plugins; NifSkope for .nif",
    "RE Engine": "REtool with the game's file-name list; RE-Mesh-Editor (Blender) for .mesh",
    "Frostbite": "FrostyToolsuite (Windows GUI)",
    "XNA / FNA / MonoGame": "xnbcli unpack Content out; code: ILSpy on the game .exe (it is .NET)",
    "LOVE (Love2D)": ".love is a zip of Lua source (7z x); a fused .exe has the zip appended (7z x works too)",
    "Cocos2d-x / Cocos Creator": "assets are plain files; .jsc is XXTEA-encrypted JS (key is in the binary: user's own only)",
    "Defold": "Defold's own bob.jar / unpack scripts (web: defold-unpacker)",
    "Construct 2/3": "plain HTML/JS; data.json holds the event sheets",
    "Flash / AIR": "ffdec (ffdec -export script,image,sound,shape out file.swf); ruffle runs it",
    "NScripter": "GARbro for .nsa/.sar; nscript.dat is XOR 0x84 text",
    "SiglusEngine": "GARbro / SiglusExtract (Windows)",
    "CatSystem2": "GARbro for .int",
    "Artemis": "GARbro for .pfs",
    "Python / PyInstaller": "pyinstxtractor (same Python minor version), then pycdc on the .pyc",
    "Java (LWJGL / libGDX)": "7z x the .jar; cfr or jadx for the classes",
    "HTML5 (web game)": "plain files; Serve the folder to run it, Browser to drive it",
}


def sniff(path: str, size: int) -> tuple[str, str] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(32)
            tail = b""
            if path.lower().endswith((".pak", ".exe")) and size > 512:
                f.seek(-512, os.SEEK_END)
                tail = f.read(512)
    except OSError:
        return None
    if tail and PAK_MAGIC in tail and path.lower().endswith(".pak"):
        return "Unreal .pak archive", "repak (unpack) / FModel"
    if head[:4] == b"\x04\x00\x00\x00" and path.lower().endswith(".asar"):
        return "Electron asar archive", "npx @electron/asar extract"
    if head[:4] == b"RIFF" and len(head) >= 12:
        kind = head[8:12]
        if kind == b"FEV ":
            return "FMOD Studio bank (.bank)", "vgmstream-cli"
        if kind == b"WAVE":
            if b"\xff\xff" in head[20:24] or path.lower().endswith(".wem"):
                return "Wwise audio (.wem)", "vgmstream-cli"
            return "WAV audio", ""
        return f"RIFF {kind.decode('latin-1', 'replace')}", ""
    if head[:4] == b"FORM" and path.lower().endswith((".win", ".unx", ".ios", ".droid")):
        return "GameMaker data file", "UndertaleModTool / UndertaleModCli"
    for off, magic, label, tool in SIGNATURES:
        if head[off:off + len(magic)] == magic:
            if magic == b"MZ" and tail:
                if b"MEI\x0c\x0b\x0a\x0b\x0e" in tail:
                    return "PyInstaller executable", "pyinstxtractor, then pycdc on the .pyc"
                if b"GDPC" in tail[-16:]:
                    return "Godot executable with embedded .pck", "GDRE_tools"
            return label, tool
    return None


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> int:
    ap = argparse.ArgumentParser(description="Identify a game's engine and data formats")
    ap.add_argument("folder")
    ap.add_argument("--top", type=int, default=30, help="how many of the biggest files to list")
    ap.add_argument("--max-files", type=int, default=200000)
    a = ap.parse_args()
    root = os.path.abspath(a.folder)
    if not os.path.isdir(root):
        sys.exit(f"Not a folder: {root}")

    engines: dict[str, list[str]] = collections.defaultdict(list)
    formats: collections.Counter = collections.Counter()
    fmt_bytes: collections.Counter = collections.Counter()
    tools: dict[str, str] = {}
    files: list[tuple[int, str, str]] = []
    exts: collections.Counter = collections.Counter()
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
        for fn in filenames:
            n += 1
            if n > a.max_files:
                break
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            lp, ln = "/" + rel.lower(), fn.lower()
            for eng, test in ENGINE_MARKERS:
                if test(lp, ln) and len(engines[eng]) < 5:
                    engines[eng].append(rel)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            exts[os.path.splitext(ln)[1] or "(none)"] += 1
            got = sniff(full, size) if size >= 4 else None
            label = got[0] if got else "unknown"
            if got:
                formats[label] += 1
                fmt_bytes[label] += size
                if got[1]:
                    tools[label] = got[1]
            files.append((size, rel, label))

    print(f"folder: {root}")
    print(f"files scanned: {min(n, a.max_files)}{' (stopped at --max-files)' if n > a.max_files else ''}")
    print("\n== engine evidence")
    if not engines:
        print("  none of the known engine markers found")
    for eng, ev in sorted(engines.items(), key=lambda kv: -len(kv[1])):
        print(f"  {eng}: {', '.join(ev)}")
        if eng in NEXT_STEP:
            print(f"      next: {NEXT_STEP[eng]}")
    print("\n== formats by content (magic bytes)")
    for label, cnt in formats.most_common():
        hint = f"  -> {tools[label]}" if label in tools else ""
        print(f"  {cnt:7d}  {human(fmt_bytes[label]):>9s}  {label}{hint}")
    unknown = [f for f in files if f[2] == "unknown"]
    if unknown:
        print(f"  {len(unknown):7d}  {human(sum(f[0] for f in unknown)):>9s}  unknown (no known magic; may be Unity "
              ".assets/.resS, raw streams, or custom/encrypted data)")
    print("\n== extensions")
    print("  " + ", ".join(f"{e} x{c}" for e, c in exts.most_common(20)))
    print(f"\n== biggest {a.top} files")
    for size, rel, label in sorted(files, reverse=True)[:a.top]:
        print(f"  {human(size):>9s}  {rel}  [{label}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
