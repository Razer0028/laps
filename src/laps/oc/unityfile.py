"""Unity のアセット（SerializedFile）の目録を読む — バンドル（UnityFS）の中の CAB も、単体の .assets も。

2026-10-09 オーナー「Unity 関連も頼みます」。走行記録（0.1.341〜0.1.343）でエージェントが詰まった所:
  ・MonoBehaviour 12,557 個がすべて UnityPy で ValueError ＝**型の木（type tree）が入っていない**ことが原因のはずだが、
    それを確かめる手段が無く、中身を推測で探し続けた。
  ・どの台本（スクリプト）の MonoBehaviour が何個あるか（CubismMoc がいくつか）が分からなかった。
  ・圧縮されたバンドルの生のバイト列を探していた。
⇒ Read が、目録（版・Unity の版・型の木の有無・種類ごとの数・MonoBehaviour の台本ごとの数）を返す。

読むのは目録と台本（MonoScript）の名前だけ。中身（テクスチャ・音・台本の本体）は展開しない。
バンドルは必要なブロックだけを展開する（LZ4 は自前の小さな展開・LZMA は標準の lzma）。上限を超えたら、そこまでの事実と理由を返す。
書式の出典: UnityPy（MIT）の SerializedFile / BundleFile の読み方。UnityPy のソースにある。
"""
from __future__ import annotations

import struct
import time

# よく出る Unity の型番号（classID → 名前）。無い番号は「classID N」と出す。
CLASS_NAMES = {
    1: "GameObject", 2: "Component", 4: "Transform", 8: "Behaviour", 12: "ParticleAnimator", 18: "EditorExtension",
    20: "Camera", 21: "Material", 23: "MeshRenderer", 25: "Renderer", 27: "Texture", 28: "Texture2D",
    29: "OcclusionCullingSettings", 30: "GraphicsSettings", 33: "MeshFilter", 41: "OcclusionPortal", 43: "Mesh",
    45: "Skybox", 47: "QualitySettings", 48: "Shader", 49: "TextAsset", 50: "Rigidbody2D", 54: "Rigidbody",
    55: "PhysicsManager", 56: "Collider", 58: "CircleCollider2D", 61: "BoxCollider2D", 64: "MeshCollider",
    65: "BoxCollider", 68: "EdgeCollider2D", 72: "ComputeShader", 74: "AnimationClip", 78: "TagManager",
    81: "AudioListener", 82: "AudioSource", 83: "AudioClip", 84: "RenderTexture", 89: "Cubemap", 90: "Avatar",
    91: "AnimatorController", 92: "GUILayer", 93: "RuntimeAnimatorController", 95: "Animator", 104: "RenderSettings",
    108: "Light", 111: "Animation", 114: "MonoBehaviour", 115: "MonoScript", 117: "Texture3D", 119: "NewAnimationTrack",
    120: "Projector", 121: "LineRenderer", 124: "Flare", 128: "Font", 129: "PlayerSettings", 131: "NamedObject",
    137: "SkinnedMeshRenderer", 141: "BuildSettings", 142: "AssetBundle", 147: "ResourceManager", 150: "PreloadData",
    152: "MovieTexture", 156: "TerrainData", 157: "LightmapSettings", 171: "SpriteRenderer", 187: "Tree",
    196: "NavMeshSettings", 198: "ParticleSystem", 199: "ParticleSystemRenderer", 212: "SpriteRenderer",
    213: "Sprite", 221: "AnimatorOverrideController", 222: "CanvasRenderer", 223: "Canvas", 224: "RectTransform",
    225: "CanvasGroup", 226: "BillboardAsset", 238: "NavMeshData", 258: "LightProbes", 290: "AssetBundleManifest",
    319: "AvatarMask", 328: "VideoPlayer", 329: "VideoClip", 1001: "Prefab", 1101: "AnimatorStateTransition",
    1102: "AnimatorState", 1107: "AnimatorStateMachine", 1109: "AnimatorTransition", 1111: "AnimatorTransitionBase",
    687078895: "SpriteAtlas", 19719996: "TilemapCollider2D", 156049354: "Grid", 483693784: "TilemapRenderer",
    1839735485: "Tilemap", 1953259897: "TerrainLayer", 2083052967: "VisualEffect",
}

MAX_META = 32 * 1024 * 1024          # 目録として読む上限
MAX_SCRIPT_READS = 3000
TIME_BUDGET = 25.0


class Short(Exception):
    """読む範囲が足りない・上限に達した。"""


class Reader:
    """位置を持つ読み手。src.read(off, n) で元のバイト列を取る（ファイルでもバンドルの中でも同じ形）。"""

    def __init__(self, src, base: int = 0, endian: str = ">") -> None:
        self.src, self.pos, self.e = src, base, endian

    def bytes(self, n: int) -> bytes:
        b = self.src.read(self.pos, n)
        if len(b) < n:
            raise Short(f"{self.pos:,} バイト目で読み切れません")
        self.pos += n
        return b

    def u8(self) -> int:
        return self.bytes(1)[0]

    def i16(self) -> int:
        return struct.unpack(self.e + "h", self.bytes(2))[0]

    def u16(self) -> int:
        return struct.unpack(self.e + "H", self.bytes(2))[0]

    def i32(self) -> int:
        return struct.unpack(self.e + "i", self.bytes(4))[0]

    def u32(self) -> int:
        return struct.unpack(self.e + "I", self.bytes(4))[0]

    def i64(self) -> int:
        return struct.unpack(self.e + "q", self.bytes(8))[0]

    def cstr(self, limit: int = 4096) -> str:
        out = bytearray()
        while len(out) < limit:
            c = self.bytes(1)
            if c == b"\x00":
                break
            out += c
        return out.decode("utf-8", "replace")

    def align(self, base: int = 0) -> None:
        r = (self.pos - base) % 4
        if r:
            self.pos += 4 - r


class FileSource:
    def __init__(self, path: str) -> None:
        self.fh = open(path, "rb")

    def read(self, off: int, n: int) -> bytes:
        self.fh.seek(off)
        return self.fh.read(n)

    def close(self) -> None:
        self.fh.close()


class BundleSource:
    """UnityFS の展開後の並びを、必要なブロックだけ展開して読む。"""

    def __init__(self, path: str, blocks: list[tuple[int, int, int, int]], decode) -> None:
        # blocks: (ファイル上の位置, 圧縮後, 展開後, 圧縮の種類)
        self.fh = open(path, "rb")
        self.blocks, self.decode = blocks, decode
        self.ustart = []
        u = 0
        for b in blocks:
            self.ustart.append(u)
            u += b[2]
        self.total = u
        self.cache: dict[int, bytes] = {}
        self.decoded_bytes = 0

    def _block(self, i: int) -> bytes:
        if i not in self.cache:
            off, csize, usize, comp = self.blocks[i]
            self.fh.seek(off)
            raw = self.fh.read(csize)
            data = self.decode(raw, usize, comp)
            self.decoded_bytes += usize
            if len(self.cache) > 256:
                self.cache.clear()
            self.cache[i] = data
        return self.cache[i]

    def read(self, off: int, n: int) -> bytes:
        import bisect
        out = bytearray()
        i = bisect.bisect_right(self.ustart, off) - 1
        while n > 0 and 0 <= i < len(self.blocks):
            data = self._block(i)
            s = off - self.ustart[i]
            piece = data[s:s + n]
            if not piece:
                break
            out += piece
            off += len(piece)
            n -= len(piece)
            i += 1
        return bytes(out)

    def close(self) -> None:
        self.fh.close()


def _read_type(r: Reader, ver: int, tree: bool, is_ref: bool = False) -> dict:
    t = {"class": r.i32(), "script": -1}
    if ver >= 16:
        r.u8()                                          # stripped
    if ver >= 17:
        t["script"] = r.i16()
    if ver >= 13:
        if (is_ref and t["script"] >= 0) or (ver < 16 and t["class"] < 0) or (ver >= 16 and t["class"] == 114):
            r.bytes(16)                                 # script の hash
        r.bytes(16)                                     # 型の hash
    if tree:
        if ver >= 12 or ver == 10:
            nodes, strbuf = r.i32(), r.i32()
            if nodes < 0 or strbuf < 0 or nodes > 1_000_000:
                raise Short("型の木の大きさが壊れています")
            r.pos += nodes * (32 if ver >= 19 else 24) + strbuf
        else:
            raise Short("古い形式（版 11 以前）の型の木は読みません")
        if ver >= 21:
            if is_ref:
                r.cstr(), r.cstr(), r.cstr()
            else:
                n = r.i32()
                r.pos += 4 * max(0, n)
    return t


def read_serialized(src, base: int, size: int | None, *, deadline: float) -> dict:
    """base から始まる SerializedFile の目録。"""
    r = Reader(src, base, ">")
    meta_size, file_size, ver, data_off = r.u32(), r.u32(), r.u32(), r.u32()
    if not (5 <= ver <= 40):
        raise Short(f"Unity のアセットの形に見えません（版 {ver}）")
    endian = "<"
    if ver >= 9:
        endian = "<" if r.u8() == 0 else ">"
        r.bytes(3)
    if ver >= 22:
        meta_size, file_size, data_off = r.u32(), r.i64(), r.i64()
        r.i64()
    if meta_size > MAX_META:
        raise Short(f"目録が大きすぎます（{meta_size:,} バイト）")
    r.e = endian
    info: dict = {"version": ver, "file_size": file_size, "data_offset": data_off, "unity": "", "platform": None,
                  "type_tree": None, "endian": endian}
    if ver >= 7:
        info["unity"] = r.cstr()
    if ver >= 8:
        info["platform"] = r.i32()
    tree = True
    if ver >= 13:
        tree = bool(r.u8())
    info["type_tree"] = tree
    types = [_read_type(r, ver, tree) for _ in range(r.i32())]
    big_id = False
    if 7 <= ver < 14:
        big_id = bool(r.i32())
    n = r.i32()
    if not (0 <= n <= 5_000_000):
        raise Short("オブジェクトの数が壊れています")
    objs = []
    for k in range(n):
        if k % 2000 == 0 and time.time() > deadline:
            raise Short("時間の上限に達しました（目録の途中）")
        if big_id:
            pid = r.i64()
        elif ver < 14:
            pid = r.i32()
        else:
            r.align(base)
            pid = r.i64()
        start = r.i64() if ver >= 22 else r.u32()
        bsize = r.u32()
        tid = r.i32()
        if ver < 16:
            cls = r.u16()
            ti = next((i for i, t in enumerate(types) if t["class"] == cls), -1)
        else:
            ti = tid
            cls = types[tid]["class"] if 0 <= tid < len(types) else -1
        if ver < 11:
            r.u16()
        if 11 <= ver < 17:
            r.i16()
        if ver in (15, 16):
            r.u8()
        objs.append((pid, start + data_off, bsize, cls, ti))
    scripts = []
    if ver >= 11:
        for _ in range(r.i32()):
            fi = r.i32()
            if ver < 14:
                lid = r.i32()
            else:
                r.align(base)
                lid = r.i64()
            scripts.append((fi, lid))
    externals = []
    if ver >= 6:
        for _ in range(r.i32()):
            if ver >= 6:
                r.cstr()
            if ver >= 5:
                r.bytes(16)
                r.i32()
            externals.append(r.cstr())
    info.update(types=types, objects=objs, scripts=scripts, externals=externals)
    return info


def _aligned_string(src, off: int, endian: str) -> str | None:
    b = src.read(off, 4)
    if len(b) < 4:
        return None
    n = struct.unpack(endian + "i", b)[0]
    if not (0 < n <= 256):
        return None
    s = src.read(off + 4, n)
    try:
        return s.decode("utf-8")
    except UnicodeDecodeError:
        return None


def summarize(src, base: int, label: str, *, deadline: float, endian_hint: str = "<", info: dict | None = None,
              resolve=None) -> list[str]:
    """1 つのアセットの目録を、判断に要る行にする。

    info: 読んである目録（無ければここで読む）。resolve(fi, lid) → 別のファイルにある台本の名前（無ければ None）。
    ★2026-10-10: 走行記録で台本が隣の globalgamemanagers.assets にあり「（台本は … の 43）」としか出なかった。
    """
    if info is None:
        try:
            info = read_serialized(src, base, None, deadline=deadline)
        except Short as e:
            return [f"  {label}: 目録を読めません — {e}"]
        except (struct.error, IndexError, ValueError) as e:
            return [f"  {label}: 目録を読めません — 形が合いません（{e}）"]
    objs, types = info["objects"], info["types"]
    out = [f"  {label}（アセット・形式の版 {info['version']}・Unity {info['unity'] or '?'}・オブジェクト {len(objs):,} 個）"]
    counts: dict[int, int] = {}
    for _pid, _st, _sz, cls, _ti in objs:
        counts[cls] = counts.get(cls, 0) + 1
    tops = sorted(counts.items(), key=lambda x: -x[1])
    out.append("    種類ごと: " + "・".join(f"{CLASS_NAMES.get(c, f'classID {c}')} {n:,}" for c, n in tops[:14])
               + (f"・ほか {len(tops) - 14} 種類" if len(tops) > 14 else ""))
    mb = counts.get(114, 0)
    if info["type_tree"] is False:
        out.append("    ★型の木（type tree）: なし。MonoBehaviour などは型の情報が無いので、UnityPy の read_typetree() では"
                   " 中身を読めません（ValueError になる）。Read に path_id=<番号> なら、ゲームの Managed/*.dll のクラスの"
                   "宣言から項目の値を読みます（IL2CPP は Il2CppDumper の DummyDll を Managed に置けば同じ）。")
    elif info["type_tree"]:
        out.append("    型の木（type tree）: あり（UnityPy の read_typetree() で中身を読めます）")
    if mb:
        # MonoBehaviour を台本ごとに数える（型の並び → 台本の参照 → MonoScript の名前）
        per_type: dict[int, int] = {}
        for _pid, _st, _sz, cls, ti in objs:
            if cls == 114:
                per_type[ti] = per_type.get(ti, 0) + 1
        by_pid = {pid: (st, sz) for pid, st, sz, cls, _ti in objs if cls == 115}
        names: dict[int, str] = {}
        reads = 0
        for ti in per_type:
            si = types[ti]["script"] if 0 <= ti < len(types) else -1
            if not (0 <= si < len(info["scripts"])):
                continue
            fi, lid = info["scripts"][si]
            if resolve is not None and reads < MAX_SCRIPT_READS and time.time() < deadline:
                reads += 1                              # 名前空間つきのクラス名（同じファイルでも隣のファイルでも）
                try:
                    nm = resolve(fi, lid)
                except Exception:
                    nm = None
                if nm:
                    names[ti] = nm
            if ti not in names and fi == 0 and lid in by_pid and reads < MAX_SCRIPT_READS and time.time() < deadline:
                reads += 1
                nm = _aligned_string(src, base + by_pid[lid][0], info["endian"])
                if nm:
                    names[ti] = nm
            if ti not in names:
                ext = info["externals"][fi - 1] if 0 < fi <= len(info["externals"]) else ""
                names[ti] = f"（台本は {ext or f'ファイル {fi}'} の {lid}）" if fi else f"（台本 {lid}）"
        rows = sorted(per_type.items(), key=lambda x: -x[1])
        out.append("    MonoBehaviour の台本ごと: " + "・".join(f"{names.get(ti, '?')} {n:,}" for ti, n in rows[:20])
                   + (f"・ほか {len(rows) - 20} 種類" if len(rows) > 20 else ""))
    if info["externals"]:
        out.append("    参照する外のファイル: " + ", ".join(info["externals"][:8])
                   + (f" …ほか {len(info['externals']) - 8}" if len(info["externals"]) > 8 else ""))
    return out


def looks_serialized(head: bytes, size: int) -> bool:
    """単体の .assets らしいか（印が無いので、頭の数値が大きさと矛盾しないかで見る）。"""
    if len(head) < 48:
        return False
    meta, fsize, ver, doff = struct.unpack_from(">IIII", head)
    if not (9 <= ver <= 40):
        return False
    if ver >= 22:
        meta = struct.unpack_from(">I", head, 20)[0]
        fsize, doff = struct.unpack_from(">qq", head, 24)
    return 0 < meta < size and fsize == size and 0 < doff <= size
