#!/usr/bin/env python3
"""Extract assets from a Unity game with UnityPy.

  python unity_extract.py "<Game>_Data" --out extracted --list
  python unity_extract.py "<Game>_Data" --out extracted --types AudioClip,Texture2D,Sprite,TextAsset
  python unity_extract.py "<Game>_Data" --out extracted --types AudioClip --name "^voice_" --raw

Point it at the whole `<Game>_Data` folder (or a folder of bundles) so streamed data in .resS/.resource
files resolves. Exports: AudioClip -> wav/ogg/m4a (FSB with --raw when decoding fails), Texture2D/Sprite
-> png, TextAsset -> raw bytes, Mesh -> obj (geometry only), Font -> ttf/otf, MonoBehaviour -> json (when type info exists).
Writes manifest.csv (type, name, container path, output file, status).
Setup: pip install UnityPy
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import re
import sys

DEFAULT_TYPES = "AudioClip,Texture2D,Sprite,TextAsset"
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name: str, fallback: str) -> str:
    name = _BAD.sub("_", (name or "").strip()).strip(". ") or fallback
    return name[:150]


def unique(path: pathlib.Path, used: set) -> pathlib.Path:
    p, i = path, 1
    while str(p).lower() in used or p.exists():
        p = path.with_name(f"{path.stem}_{i}{path.suffix}")
        i += 1
    used.add(str(p).lower())
    return p


def subdir_for(obj, tname: str) -> str:
    try:
        c = obj.container
    except Exception:  # noqa: BLE001
        c = None
    if c:
        parent = str(pathlib.PurePosixPath(c).parent)
        parts = [safe(x, "_") for x in parent.split("/") if x and x != "."]
        return "/".join(parts) or tname
    return tname


def export(obj, data, tname: str, outdir: pathlib.Path, base: str, used: set, raw: bool) -> list[pathlib.Path]:
    written: list[pathlib.Path] = []

    def put(fname: str, payload: bytes) -> None:
        p = unique(outdir / safe(fname, base), used)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)
        written.append(p)

    if tname == "AudioClip":
        try:
            for fname, payload in data.samples.items():
                put(fname, payload)
        except Exception:
            if not raw:
                raise
            from UnityPy.export.AudioClipConverter import get_resource_data
            if data.m_AudioData:
                payload = bytes(data.m_AudioData)
            else:
                r = data.m_Resource
                payload = get_resource_data(r.m_Source, obj.assets_file, r.m_Offset, r.m_Size)
            put(base + (".fsb" if payload[:4] == b"FSB5" else ".bin"), payload)
    elif tname in ("Texture2D", "Sprite"):
        img = data.image
        if img is None or img.width == 0 or img.height == 0:
            raise ValueError("empty image")
        p = unique(outdir / f"{safe(base, 'image')}.png", used)
        p.parent.mkdir(parents=True, exist_ok=True)
        img.save(p)
        written.append(p)
    elif tname == "TextAsset":
        script = data.m_Script
        payload = script.encode("utf-8", "surrogateescape") if isinstance(script, str) else bytes(script)
        ext = pathlib.PurePath(base).suffix
        if not ext:
            try:
                payload.decode("utf-8")
                ext = ".txt"
            except UnicodeDecodeError:
                ext = ".bytes"
        put(base if pathlib.PurePath(base).suffix else base + ext, payload)
    elif tname == "Mesh":
        put(base + ".obj", data.export().encode("utf-8"))
    elif tname == "Font":
        payload = bytes(data.m_FontData or b"")
        if not payload:
            raise ValueError("font has no embedded data")
        put(base + (".otf" if payload[:4] == b"OTTO" else ".ttf"), payload)
    elif tname == "MonoBehaviour":
        tree = obj.read_typetree()
        put(base + ".json", json.dumps(tree, ensure_ascii=False, indent=1, default=str).encode("utf-8"))
    else:
        put(base + ".dat", obj.get_raw_data())
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract assets from a Unity game with UnityPy")
    ap.add_argument("src", help="<Game>_Data folder, a bundle folder, or a single file")
    ap.add_argument("--out", default="extracted")
    ap.add_argument("--types", default=DEFAULT_TYPES, help=f"comma-separated Unity types (default {DEFAULT_TYPES})")
    ap.add_argument("--name", help="only objects whose name matches this regex")
    ap.add_argument("--list", action="store_true", help="only count objects per type (and show sample names)")
    ap.add_argument("--raw", action="store_true", help="AudioClip: write the raw stream when decoding fails")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many exported objects")
    ap.add_argument("--unity-version", help="fallback Unity version for stripped files, e.g. 2021.3.16f1")
    a = ap.parse_args()

    try:
        import UnityPy
    except ImportError:
        sys.exit("UnityPy is not installed. Run: pip install UnityPy")
    if a.unity_version:
        UnityPy.config.FALLBACK_UNITY_VERSION = a.unity_version

    src = pathlib.Path(a.src)
    if not src.exists():
        sys.exit(f"Not found: {src}")
    print(f"loading {src} ...", flush=True)
    env = UnityPy.load(str(src))
    types = {t.strip() for t in a.types.split(",") if t.strip()}
    rx = re.compile(a.name) if a.name else None
    out = pathlib.Path(a.out)

    if a.list:
        counts: collections.Counter = collections.Counter()
        samples: dict[str, list[str]] = collections.defaultdict(list)
        for obj in env.objects:
            t = obj.type.name
            counts[t] += 1
            if len(samples[t]) < 5:
                try:
                    samples[t].append(obj.peek_name() or "")
                except Exception:  # noqa: BLE001
                    pass
        for t, n in counts.most_common():
            print(f"{n:8d}  {t:24s} {', '.join(s for s in samples[t] if s)[:100]}")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    used: set = set()
    stats: collections.Counter = collections.Counter()
    first_error: dict[str, str] = {}
    exported = 0
    with open(out / "manifest.csv", "w", newline="", encoding="utf-8-sig") as mf:
        w = csv.writer(mf)
        w.writerow(["type", "name", "container", "output", "status"])
        for obj in env.objects:
            t = obj.type.name
            if t not in types:
                continue
            try:
                name = obj.peek_name() or ""
            except Exception:  # noqa: BLE001
                name = ""
            if rx and not rx.search(name):
                continue
            base = safe(name, f"{t}_{obj.path_id}")
            sub = subdir_for(obj, t)
            try:
                data = obj.read()
                files = export(obj, data, t, out / sub, base, used, a.raw)
                for f in files:
                    w.writerow([t, name, getattr(obj, "container", "") or "", str(f), "ok"])
                stats[f"{t} ok"] += 1
                exported += 1
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"[:200]
                first_error.setdefault(t, f"{name}: {msg}")
                w.writerow([t, name, "", "", "error: " + msg])
                stats[f"{t} failed"] += 1
            if a.limit and exported >= a.limit:
                break

    for k, v in sorted(stats.items()):
        print(f"{v:8d}  {k}")
    for t, e in first_error.items():
        print(f"first {t} error: {e}")
    if not stats:
        print("nothing matched; run with --list to see which types exist")
    if "Mesh" in types:
        print("note: Mesh -> OBJ keeps static geometry only (no bones, skin weights, blend shapes or hierarchy). "
              "For characters and outfits export FBX with AssetStudioModCLI -m animator (see SKILL.md).")
    print(f"output: {out.resolve()}  (manifest.csv lists every file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
