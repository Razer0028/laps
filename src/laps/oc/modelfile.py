"""3D・2D モデルのファイルの中の構造を読む（Read・MCP の inspect_file・2026-10-11）。

オーナー「ゲーム解析の強化を…モデル解析系なども」。それまでは FBX は版だけ、glTF は数だけ、moc3 は版だけだった。
モデルを調べるときに要るのは「何が入っていて、何が足りないか」:
  FBX        … 作ったソフト・単位と上の軸・メッシュ（頂点・面・UV）・骨・スキン・ブレンドシェイプ・マテリアル・
               テクスチャ（ファイル名と埋め込みの有無）・アニメーション（名前と長さ）。バイナリと ASCII。
  glTF/GLB/VRM … メッシュ（頂点・三角形・モーフの名前）・マテリアル・画像（埋め込み／外のファイルの有無）・スキン（骨の数）・
               アニメーション（長さ）・拡張。VRM 0.x / 1.0 は名前・作者・利用の条件（ライセンス）・人型の骨・表情。
  OBJ/MTL    … 頂点・面・グループ・マテリアル・参照しているテクスチャの有無。
  PMX/PMD/VMD … MMD のモデル（名前・頂点・面・材質・テクスチャの有無・ボーン・モーフ・剛体）と動き（フレーム数・長さ）。
  .moc3      … Live2D の数（パーツ・デフォーマ・アートメッシュ・パラメータ）と ID の一覧。
  .model3.json … 参照しているファイル（moc3・テクスチャ・物理・表情・モーション）が揃っているか・モーションの長さ。

★2026-10-15（vrcmodel と合意）: FBX（7 以降のバイナリ）・glTF/GLB/VRM・OBJ は、メッシュごとの網目の点検（三角形・島・縁の辺・
同じ位置の頂点・面の無い頂点・面積ほぼ 0 の面）、骨と重みの位置の食い違い（単位・向き）、VRChat の評価の見積りも出す
（数え方は oc/meshstats.py。Blender の bmesh で数えた値と、試しの FBX・GLB・OBJ で一致を確かめた）。

読むだけ。頂点やテクスチャの中身は展開しない（数を数えるのに要る配列だけ展開する）。読めない所はそこまでと理由を返す。
"""
from __future__ import annotations

import json
import math
import os
import re
import struct
import zlib

_MAX_LIST = 25


def _meshstats():
    from . import meshstats
    return meshstats


def _mb(n: int) -> str:
    return f"{n / 1048576:,.1f} MB" if n >= 1048576 else f"{n:,} バイト"


def _names(xs, n: int = _MAX_LIST) -> str:
    xs = [str(x) for x in xs if str(x)]
    return ", ".join(xs[:n]) + (f" …ほか {len(xs) - n} 個" if len(xs) > n else "")


def _exists_note(base: str, rel: str) -> str:
    """参照しているファイルがあるか。★別の PC の絶対パス（D:\\projects\\… など）は、同じ名前を置き場の近くでも探す。"""
    if not rel:
        return ""
    if re.match(r"^[a-z]+:", rel, re.I) and not re.match(r"^[a-z]:[\\/]", rel, re.I):
        return "（外の URL・データ）"
    norm = rel.replace("\\", "/")
    p = norm if os.path.isabs(norm) else os.path.join(base, norm)
    if os.path.exists(p):
        return ""
    name = os.path.basename(norm)
    for d in (base, os.path.join(base, "textures"), os.path.join(base, "Textures"), os.path.join(base, "tex")):
        if name and os.path.exists(os.path.join(d, name)):
            return f"（書かれた場所には無いが {os.path.relpath(os.path.join(d, name), base)} にある）"
    return "（★見つからない）"


# ── FBX ─────────────────────────────────────────────────────────────────────

_KTIME = 46186158000                                   # FBX の時間の 1 秒


class _Node:
    __slots__ = ("name", "props", "kids")

    def __init__(self, name: str, props: list, kids: list):
        self.name, self.props, self.kids = name, props, kids

    def child(self, name: str):
        for k in self.kids:
            if k.name == name:
                return k
        return None

    def all(self, name: str):
        return [k for k in self.kids if k.name == name]


class _Arr:
    """配列の属性。中身は要るときだけ展開する。"""
    __slots__ = ("t", "n", "enc", "raw")

    def __init__(self, t: str, n: int, enc: int, raw: bytes):
        self.t, self.n, self.enc, self.raw = t, n, enc, raw

    def values(self, limit: int = 30_000_000):
        if self.n > limit:
            raise ValueError("配列が大きすぎます")
        data = zlib.decompress(self.raw) if self.enc == 1 else self.raw
        fmt = {"i": "i", "l": "q", "f": "f", "d": "d", "b": "?", "c": "B"}.get(self.t, "B")
        return struct.unpack(f"<{self.n}{fmt}", data[:self.n * struct.calcsize(fmt)])


def _fbx_props(b: bytes, p: int, n: int) -> tuple[list, int]:
    out = []
    for _ in range(n):
        t = chr(b[p]); p += 1
        if t == "Y":
            out.append(struct.unpack_from("<h", b, p)[0]); p += 2
        elif t == "C":
            out.append(bool(b[p])); p += 1
        elif t == "I":
            out.append(struct.unpack_from("<i", b, p)[0]); p += 4
        elif t == "F":
            out.append(struct.unpack_from("<f", b, p)[0]); p += 4
        elif t == "D":
            out.append(struct.unpack_from("<d", b, p)[0]); p += 8
        elif t == "L":
            out.append(struct.unpack_from("<q", b, p)[0]); p += 8
        elif t.islower():                              # 配列（f d l i b のほか、書き出しによって c など）
            cnt, enc, clen = struct.unpack_from("<III", b, p); p += 12
            out.append(_Arr(t, cnt, enc, b[p:p + clen])); p += clen
        elif t in "SR":
            ln = struct.unpack_from("<I", b, p)[0]; p += 4
            raw = b[p:p + ln]; p += ln
            out.append(raw.decode("utf-8", "replace") if t == "S" else raw)
        else:
            raise ValueError(f"知らない属性の型 {t!r}（{p - 1:,} バイト目）")
    return out, p


def _fbx_nodes(b: bytes, p: int, end: int, ver: int, depth: int = 0) -> list[_Node]:
    wide = ver >= 7500
    hdr = 25 if wide else 13
    out = []
    while p + hdr <= end:
        if wide:
            nend, nprops, _plen = struct.unpack_from("<QQQ", b, p); p += 24
        else:
            nend, nprops, _plen = struct.unpack_from("<III", b, p); p += 12
        nlen = b[p]; p += 1
        if nend == 0:
            break
        name = b[p:p + nlen].decode("ascii", "replace"); p += nlen
        props, p = _fbx_props(b, p, nprops)
        kids = _fbx_nodes(b, p, nend - hdr, ver, depth + 1) if p < nend and depth < 40 else []
        out.append(_Node(name, props, kids))
        p = nend
    return out


def _objname(s) -> str:
    s = str(s or "")
    return s.split("\x00\x01")[0] if "\x00\x01" in s else s.split("::")[-1]


def _p70(node, key: str):
    if not node:
        return None
    p70 = node.child("Properties70")
    for p in (p70.all("P") if p70 else []):
        if p.props and p.props[0] == key:
            return p.props[-1] if len(p.props) > 4 else None
    return None


def _p70v(node, key: str, default):
    """Properties70 の値（3 つ組など）。無ければ default。"""
    p70 = node.child("Properties70") if node else None
    for p in (p70.all("P") if p70 else []):
        if p.props and p.props[0] == key and len(p.props) > 4:
            return list(p.props[4:])
    return default


# 4×4 の行列（列ごとに並べた 16 個・[12:15] が平行移動）。FBX の Transform・TransformLink・glTF の matrix と同じ並び。
_I4 = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _mmul(a, b):
    return [sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4)]


def _mapply(m, p):
    x, y, z = p
    return (m[0] * x + m[4] * y + m[8] * z + m[12], m[1] * x + m[5] * y + m[9] * z + m[13],
            m[2] * x + m[6] * y + m[10] * z + m[14])


def _mT(t):
    m = list(_I4)
    m[12], m[13], m[14] = float(t[0]), float(t[1]), float(t[2])
    return m


def _mS(s):
    m = list(_I4)
    m[0], m[5], m[10] = float(s[0]), float(s[1]), float(s[2])
    return m


def _mR(deg, order: int = 0, inverse: bool = False):
    """オイラー角（度）。order は FBX の RotationOrder（0=XYZ＝X を先に回す…5=ZYX）。"""
    def axis(i, a):
        c, s = math.cos(math.radians(a)), math.sin(math.radians(a))
        m = list(_I4)
        j, k = (i + 1) % 3, (i + 2) % 3
        m[j * 4 + j], m[k * 4 + j], m[j * 4 + k], m[k * 4 + k] = c, -s, s, c
        return m
    seq = ("XYZ", "XZY", "YZX", "YXZ", "ZXY", "ZYX")[order if 0 <= order < 6 else 0]
    m = list(_I4)
    for ch in seq:                                     # 先に回す軸が右（列ベクトル）
        m = _mmul(axis("XYZ".index(ch), float(deg["XYZ".index(ch)])), m)
    if inverse:                                        # 回転だけなので転置が逆
        m = [m[(i % 4) * 4 + i // 4] for i in range(16)]
    return m


def _fbx_local(model):
    """FBX の Model の置かれ方（親から見た行列）。T・回転の基準と前後の回転・拡大の基準を含む（InheritType は見ない）。"""
    g = lambda k, d: _p70v(model, k, d)  # noqa: E731
    order = g("RotationOrder", [0])[0]
    rp, sp = g("RotationPivot", [0, 0, 0]), g("ScalingPivot", [0, 0, 0])
    seq = [_mT(g("Lcl Translation", [0, 0, 0])), _mT(g("RotationOffset", [0, 0, 0])), _mT(rp),
           _mR(g("PreRotation", [0, 0, 0])), _mR(g("Lcl Rotation", [0, 0, 0]), int(order) if isinstance(order, int) else 0),
           _mR(g("PostRotation", [0, 0, 0]), 0, inverse=True), _mT([-float(x) for x in rp]),
           _mT(g("ScalingOffset", [0, 0, 0])), _mT(sp), _mS(g("Lcl Scaling", [1, 1, 1])), _mT([-float(x) for x in sp])]
    m = list(_I4)
    for x in seq:
        m = _mmul(m, x)
    return m


def _fbx_arr(node, name: str):
    c = node.child(name) if node else None
    return c.props[0].values() if c and c.props and isinstance(c.props[0], _Arr) else None


def _fbx_extras(objs_kids: list, conns, unit_cm: float) -> tuple[dict, list[str]]:
    """網目の点検（ジオメトリの id → 数）と、骨と重み・VRChat の見積りの行（oc/meshstats.py・2026-10-15）。"""
    from . import meshstats as _ms
    by_id = {k.props[0]: k for k in objs_kids if k.props and isinstance(k.props[0], int)}
    kids_of: dict[int, list[int]] = {}
    par_of: dict[int, list[int]] = {}
    for c in (conns.kids if conns else []):
        if c.name == "C" and len(c.props) >= 3 and c.props[0] == "OO":
            kids_of.setdefault(c.props[2], []).append(c.props[1])
            par_of.setdefault(c.props[1], []).append(c.props[2])
    cls = lambda i: (by_id[i].props[2] if i in by_id and len(by_id[i].props) > 2 else "")  # noqa: E731
    is_ = lambda i, name, sub=None: i in by_id and by_id[i].name == name and (sub is None or cls(i) == sub)  # noqa: E731
    gcache: dict[int, list] = {}

    def glob(mid: int, depth: int = 0):
        if mid in gcache:
            return gcache[mid]
        parent = next((p for p in par_of.get(mid, []) if is_(p, "Model")), None)
        m = _fbx_local(by_id[mid])
        if parent is not None and depth < 200:
            m = _mmul(glob(parent, depth + 1), m)
        gcache[mid] = m
        return m
    topo: dict[int, dict] = {}
    bind, scene = [], []
    st = {"tris": 0, "skinned": 0, "basic": 0, "slots": 0, "bones": 0}
    bone_ids: set[int] = set()
    for gid, g in by_id.items():
        if not is_(gid, "Geometry", "Mesh"):
            continue
        pos, pvi = _fbx_arr(g, "Vertices"), _fbx_arr(g, "PolygonVertexIndex")
        if pos is None or pvi is None:
            continue
        nv = len(pos) // 3
        polys, cur = [], []
        for x in pvi:
            if x < 0:
                cur.append(~x)
                polys.append(cur)
                cur = []
            else:
                cur.append(x)
        if any(v >= nv for p in polys for v in p):
            topo[gid] = {"skipped": "面が頂点の外を指している（壊れているか知らない形）"}
            continue
        topo[gid] = t = _ms.topology(pos, polys, unit_cm / 100.0)
        st["tris"] += t.get("tris", sum(max(len(p) - 2, 0) for p in polys))
        model = next((p for p in par_of.get(gid, []) if is_(p, "Model")), None)
        skins = [d for d in kids_of.get(gid, []) if is_(d, "Deformer", "Skin")]
        clusters = [c for s in skins for c in kids_of.get(s, []) if is_(c, "Deformer", "Cluster")]
        shapes = [d for d in kids_of.get(gid, []) if is_(d, "Deformer", "BlendShape")]
        st["skinned" if clusters or shapes else "basic"] += 1
        if model is not None:
            st["slots"] += max(1, sum(1 for m in kids_of.get(model, []) if is_(m, "Material")))
        mesh_scene = None
        if model is not None:
            geo = list(_I4)
            for x in (_mT(_p70v(by_id[model], "GeometricTranslation", [0, 0, 0])),
                      _mR(_p70v(by_id[model], "GeometricRotation", [0, 0, 0])),
                      _mS(_p70v(by_id[model], "GeometricScaling", [1, 1, 1]))):
                geo = _mmul(geo, x)
            mesh_scene = _mmul(glob(model), geo)
        for cid in clusters:
            cl = by_id[cid]
            bone = next((b for b in kids_of.get(cid, []) if is_(b, "Model")), None)
            idx, w = _fbx_arr(cl, "Indexes"), _fbx_arr(cl, "Weights")
            if bone is None or not idx or not w:
                continue
            bone_ids.add(bone)
            sx = sy = sz = sw = 0.0
            for i, wt in zip(idx, w):
                if 0 <= i < nv and wt > 0:
                    sx += pos[3 * i] * wt
                    sy += pos[3 * i + 1] * wt
                    sz += pos[3 * i + 2] * wt
                    sw += wt
            if not sw > 0:
                continue
            c = (sx / sw, sy / sw, sz / sw)
            name = _objname(by_id[bone].props[1])
            tm, tl = _fbx_arr(cl, "Transform"), _fbx_arr(cl, "TransformLink")
            if tl and len(tl) == 16:
                # ★ファイルの Transform は骨から見たメッシュ（TransformLink⁻¹ × メッシュの置かれ方）で書かれている
                #   （FBX SDK の GetTransformMatrix は世界の値を返すが、保存は骨の空間・Blender の書き出しのコメントにもある）。
                #   メッシュの置かれ方は TransformLink × Transform で戻す（2026-10-15 Blender 4.5 の書き出しで確かめた）。
                mb = _mmul(list(tl), list(tm)) if tm and len(tm) == 16 else list(tl)
                bind.append((name, _mapply(mb, c), (tl[12], tl[13], tl[14]), sw))
            if mesh_scene is not None:
                gb = glob(bone)
                scene.append((name, _mapply(mesh_scene, c), (gb[12], gb[13], gb[14]), sw))
    st["bones"] = len(bone_ids) or sum(1 for i in by_id if is_(i, "Model", "LimbNode"))
    out = []
    if bind or scene:
        b_rows, b_bad = _ms.bone_check(bind, unit_cm, "結び付けた時の姿勢 bind") if bind else ([], False)
        s_rows, s_bad = _ms.bone_check(scene, unit_cm, "置かれた姿勢 Lcl") if scene else ([], False)
        out += b_rows or s_rows
        if b_rows and s_rows and s_bad != b_bad:
            out += s_rows
            out.append("    ★結び付けた時と置かれた姿勢で結果が違う＝読み込むソフトによって骨と体がずれる形")
        elif b_rows and s_rows:
            out.append("    （置かれた姿勢 Lcl で測っても同じ）")
    if st["skinned"]:
        out += _ms.vrchat_estimate(st)
    return topo, out


def fbx(path: str, size: int) -> list[str]:
    with open(path, "rb") as fh:
        b = fh.read(min(size, 768 << 20))
    if not b.startswith(b"Kaydara FBX Binary"):
        return fbx_ascii(path, b)
    ver = struct.unpack_from("<I", b, 23)[0]
    if b[20:23] != b"\x00\x1a\x00" or not 6000 <= ver <= 8000:
        return ["構造（FBX のバイナリ）:",
                f"  ★頭が壊れています（20〜26 バイト目 {b[20:27].hex(' ')}・本来は 00 1a 00 と版の数）。"
                + ("0 のバイトが空白（20）に置き換わっている＝テキストとして転送・変換されて壊れた形。" if b[20:23] == b"\x20\x1a\x20" else ""),
                "  → 元のファイルを取り直してください（中身の多くも同じように壊れています）。"]
    try:
        top = _fbx_nodes(b, 27, len(b), ver)
    except (ValueError, IndexError, struct.error) as e:
        return ["構造（FBX のバイナリ）:", f"  版 {ver}・{_mb(size)}", f"  ★途中で読めません（壊れているか知らない形）: {e}"]
    root = _Node("", [], top)
    out = ["構造（FBX のバイナリ）:"]
    hx = root.child("FBXHeaderExtension")
    creator = (root.child("Creator").props[0] if root.child("Creator") and root.child("Creator").props else "")
    if hx and hx.child("Creator") and hx.child("Creator").props:
        creator = creator or hx.child("Creator").props[0]
    gs = root.child("GlobalSettings")
    up, unit = _p70(gs, "UpAxis"), _p70(gs, "UnitScaleFactor")
    out.append(f"  版 {ver}（{ver // 1000}.{(ver % 1000) // 100}）・{_mb(size)}"
               + (f"・作成 {creator}" if creator else "")
               + (f"・上の軸 {'XYZ'[up] if isinstance(up, int) and 0 <= up < 3 else up}" if up is not None else "")
               + (f"・単位 {unit:g} cm" if isinstance(unit, float) else ""))
    objs = root.child("Objects")
    kids = objs.kids if objs else []
    by: dict[str, int] = {}
    for k in kids:
        sub = k.props[2] if len(k.props) > 2 and isinstance(k.props[2], str) else ""
        key = k.name + (f"（{sub}）" if sub else "")
        by[key] = by.get(key, 0) + 1
    out.append("  中のオブジェクト: " + "・".join(f"{k} {v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:16]))
    meshes = []
    for g in (k for k in kids if k.name == "Geometry" and len(k.props) > 2 and k.props[2] == "Mesh"):
        v = g.child("Vertices")
        pvi = g.child("PolygonVertexIndex")
        nv = v.props[0].n // 3 if v and v.props and isinstance(v.props[0], _Arr) else 0
        npoly = None
        if pvi and pvi.props and isinstance(pvi.props[0], _Arr):
            try:
                npoly = sum(1 for x in pvi.props[0].values() if x < 0)
            except (ValueError, zlib.error, struct.error):
                npoly = None
        uvs = len(g.all("LayerElementUV"))
        meshes.append((_objname(g.props[1]), nv, npoly, uvs, g.props[0]))
    # ★網目の点検・骨と重み・VRChat の見積り（oc/meshstats.py・2026-10-15 vrcmodel と合意）。FBX 7 以降（id でつながる形）だけ。
    topo, extra = {}, []
    if meshes and ver >= 7000:
        try:
            topo, extra = _fbx_extras(kids, root.child("Connections"), unit if isinstance(unit, float) and unit > 0 else 1.0)
        except (ValueError, IndexError, KeyError, TypeError, struct.error, zlib.error, RecursionError) as e:
            extra = [f"  （網目の点検は途中で止まりました: {type(e).__name__}: {e}）"]
    if meshes:
        tv = sum(m[1] for m in meshes)
        out.append(f"  メッシュ {len(meshes)} 個・頂点 {tv:,}" + (f"・面 {sum(m[2] or 0 for m in meshes):,}" if all(m[2] is not None for m in meshes) else ""))
        for nm, nv, np_, uv, gid in sorted(meshes, key=lambda m: -m[1])[:_MAX_LIST]:
            out.append(f"    {nm or '（名前なし）'}  頂点 {nv:,}" + (f"・面 {np_:,}" if np_ is not None else "") + f"・UV {uv} 組")
            if gid in topo:
                out.append("      " + _meshstats().topo_line(topo[gid]))
        if len(topo) > 1:
            out.append("  " + _meshstats().topo_total(list(topo.values())))
    models = [k for k in kids if k.name == "Model"]
    bones = [_objname(k.props[1]) for k in models if len(k.props) > 2 and k.props[2] == "LimbNode"]
    if bones:
        out.append(f"  骨（LimbNode）{len(bones)} 本: {_names(bones, 30)}")
    defs = [k for k in kids if k.name == "Deformer"]
    skins = sum(1 for d in defs if len(d.props) > 2 and d.props[2] == "Skin")
    clusters = sum(1 for d in defs if len(d.props) > 2 and d.props[2] == "Cluster")
    chans = [_objname(d.props[1]) for d in defs if len(d.props) > 2 and d.props[2] == "BlendShapeChannel"]
    if skins or clusters:
        out.append(f"  スキン {skins} 個（骨との結び付き {clusters} 個）")
    if chans:
        out.append(f"  ブレンドシェイプ {len(chans)} 個: {_names(chans, 40)}")
    mats = [_objname(k.props[1]) for k in kids if k.name == "Material"]
    if mats:
        out.append(f"  マテリアル {len(mats)} 個: {_names(mats)}")
    base = os.path.dirname(path)
    vids = {}
    for k in kids:
        if k.name == "Video":
            c = k.child("Content")
            raw = c.props[0] if c and c.props and isinstance(c.props[0], (bytes, bytearray)) else b""
            fn = (k.child("RelativeFilename") or k.child("Filename"))
            vids[(fn.props[0] if fn and fn.props else "")] = len(raw)
    texs = []
    for k in kids:
        if k.name == "Texture":
            rel = k.child("RelativeFilename")
            full = k.child("FileName")
            f = (rel.props[0] if rel and rel.props else "") or (full.props[0] if full and full.props else "")
            emb = next((sz for vf, sz in vids.items() if vf and os.path.basename(vf.replace("\\", "/")) == os.path.basename(f.replace("\\", "/"))), 0)
            texs.append(f"{f or _objname(k.props[1])}" + (f"（埋め込み {_mb(emb)}）" if emb else _exists_note(base, f.replace("\\", "/"))))
    if texs:
        out.append(f"  テクスチャ {len(texs)} 個:")
        out += [f"    {t}" for t in texs[:_MAX_LIST]]
    emb_total = sum(vids.values())
    if emb_total:
        out.append(f"  埋め込まれた画像など {sum(1 for v in vids.values() if v)} 個・{_mb(emb_total)}")
    anims = []
    for k in kids:
        if k.name == "AnimationStack":
            stop, start = _p70(k, "LocalStop"), _p70(k, "LocalStart")
            dur = ((stop or 0) - (start or 0)) / _KTIME if isinstance(stop, int) else None
            anims.append(_objname(k.props[1]) + (f"（{dur:.2f} 秒）" if dur else ""))
    if anims:
        out.append(f"  アニメーション {len(anims)} 本: {_names(anims)}")
        ncurve = sum(1 for k in kids if k.name == "AnimationCurve")
        out.append(f"    キーの曲線 {ncurve:,} 本")
    out += extra
    conns = root.child("Connections")
    if conns:
        out.append(f"  つながり（Connections）{len(conns.kids):,} 本")
    out.append("  → 開くのは Blender（読み込み）・Assimp。Unity / UE に入れるなら単位（cm と m）と上の軸を確かめてから。")
    return out


def fbx_ascii(path: str, b: bytes) -> list[str]:
    t = b.decode("utf-8", "replace")
    m = re.search(r"FBX (\d+)\.(\d+)\.(\d+)", t[:400])
    cnt = lambda pat: len(re.findall(pat, t))  # noqa: E731
    meshes = re.findall(r'Geometry: *\d+, *"Geometry::([^"]*)", *"Mesh"', t)
    bones = re.findall(r'Model: *\d+, *"Model::([^"]*)", *"LimbNode"', t)
    mats = re.findall(r'Material: *\d+, *"Material::([^"]*)"', t)
    texs = re.findall(r'RelativeFilename: *"([^"]*)"', t)
    anims = re.findall(r'AnimationStack: *\d+, *"AnimStack::([^"]*)"', t)
    shapes = re.findall(r'Deformer: *\d+, *"SubDeformer::([^"]*)", *"BlendShapeChannel"', t)
    verts = sum(int(x) // 3 for x in re.findall(r"Vertices: *\*(\d+)", t))
    n_tex = cnt(r"Texture: *\d+")    # f 文字列の中に \ を書くと 3.11 以前では構文エラー（pyproject は >=3.10）
    out = ["構造（FBX のテキスト形式）:",
           f"  版 {'.'.join(m.groups()) if m else '?'}・メッシュ {len(meshes)} 個・頂点 {verts:,}・骨 {len(bones)} 本・"
           f"マテリアル {len(mats)} 個・テクスチャ {n_tex} 個・アニメーション {len(anims)} 本"]
    if any(meshes):
        out.append(f"  メッシュ: {_names(meshes)}")
    if any(bones):
        out.append(f"  骨: {_names(bones, 30)}")
    if shapes:
        out.append(f"  ブレンドシェイプ {len(shapes)} 個: {_names(shapes, 40)}")
    if mats:
        out.append(f"  マテリアル: {_names(mats)}")
    if texs:
        base = os.path.dirname(path)
        out.append("  テクスチャ: " + _names([x + _exists_note(base, x.replace("\\", "/")) for x in dict.fromkeys(texs)]))
    if anims:
        out.append(f"  アニメーション: {_names(anims)}")
    return out


# ── glTF / GLB / VRM ────────────────────────────────────────────────────────

def _gltf_json(path: str, head: bytes) -> tuple[dict, bytes | None]:
    with open(path, "rb") as fh:
        if head.startswith(b"glTF"):
            h = fh.read(20)
            jlen = struct.unpack_from("<I", h, 12)[0]
            js = fh.read(jlen)
            bin_hdr = fh.read(8)
            binchunk = None
            if len(bin_hdr) == 8 and bin_hdr[4:8] == b"BIN\x00":
                binchunk = b""                         # 中身は読まない（大きさだけ）
            return json.loads(js.decode("utf-8")), binchunk
        return json.loads(fh.read(64 << 20).decode("utf-8-sig")), None


_GL_CT = {5120: ("b", 1, 127.0), 5121: ("B", 1, 255.0), 5122: ("h", 2, 32767.0), 5123: ("H", 2, 65535.0),
          5125: ("I", 4, 4294967295.0), 5126: ("f", 4, 1.0)}
_GL_NC = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


def _gltf_buffers(path: str, head: bytes, j: dict) -> list:
    """buffers の中身（GLB の BIN・外の .bin・data URI）。読めない物は None。1 GB を超える物は読まない。"""
    import base64
    from urllib.parse import unquote
    glb = None
    if head.startswith(b"glTF"):
        with open(path, "rb") as fh:
            fh.seek(12)
            while True:
                ch = fh.read(8)
                if len(ch) < 8:
                    break
                ln, typ = struct.unpack("<II", ch)
                if typ == 0x004E4942:                  # "BIN\0"
                    glb = fh.read(ln) if ln <= 1 << 30 else None
                    break
                fh.seek(ln, 1)
    out = []
    base = os.path.dirname(path)
    for i, b in enumerate(j.get("buffers") or []):
        uri = b.get("uri")
        if uri is None:
            out.append(glb if i == 0 else None)
        elif uri.startswith("data:"):
            try:
                out.append(base64.b64decode(uri.split(",", 1)[1]))
            except (ValueError, IndexError):
                out.append(None)
        else:
            p = os.path.join(base, unquote(uri))
            out.append(open(p, "rb").read() if os.path.isfile(p) and os.path.getsize(p) <= 1 << 30 else None)
    return out


def _gltf_acc(j: dict, bufs: list, i) -> list:
    """accessor の値（平らな列）。normalized は 0〜1 に直す。sparse は読まない（ValueError）。"""
    acc = (j.get("accessors") or [])[i]
    ct, size, mx = _GL_CT[acc["componentType"]]
    nc, cnt = _GL_NC[acc["type"]], acc["count"]
    if "sparse" in acc:
        raise ValueError("sparse の accessor は読まない")
    if acc.get("bufferView") is None:
        return [0.0] * (cnt * nc)
    bv = j["bufferViews"][acc["bufferView"]]
    buf = bufs[bv["buffer"]] if bv["buffer"] < len(bufs) else None
    if buf is None:
        raise ValueError("データ（buffer）が読めない")
    off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    esz = size * nc
    stride = bv.get("byteStride") or esz
    if stride == esz:
        vals = list(struct.unpack_from(f"<{cnt * nc}{ct}", buf, off))
    else:
        vals = []
        for k in range(cnt):
            vals.extend(struct.unpack_from(f"<{nc}{ct}", buf, off + k * stride))
    if acc.get("normalized") and ct != "f":
        vals = [max(v / mx, -1.0) for v in vals]
    return vals


def _minv_affine(m):
    """アフィンの 4×4（列ごと）の逆。平行移動の部分だけ要る所で使う。"""
    a, b, c, d, e, f, g, h, k = m[0], m[4], m[8], m[1], m[5], m[9], m[2], m[6], m[10]
    det = a * (e * k - f * h) - b * (d * k - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-30:
        return None
    inv = [(e * k - f * h) / det, (c * h - b * k) / det, (b * f - c * e) / det,
           (f * g - d * k) / det, (a * k - c * g) / det, (c * d - a * f) / det,
           (d * h - e * g) / det, (b * g - a * h) / det, (a * e - b * d) / det]
    tx, ty, tz = m[12], m[13], m[14]
    return (-(inv[0] * tx + inv[1] * ty + inv[2] * tz), -(inv[3] * tx + inv[4] * ty + inv[5] * tz),
            -(inv[6] * tx + inv[7] * ty + inv[8] * tz))


def _gltf_tris(j: dict, bufs: list, pr: dict, nverts: int) -> list[list[int]]:
    mode = pr.get("mode", 4)
    idx = [int(x) for x in _gltf_acc(j, bufs, pr["indices"])] if pr.get("indices") is not None else list(range(nverts))
    if mode == 4:
        return [idx[k:k + 3] for k in range(0, len(idx) - 2, 3)]
    if mode == 5:                                      # 帯
        return [([idx[k], idx[k + 1], idx[k + 2]] if k % 2 == 0 else [idx[k + 1], idx[k], idx[k + 2]]) for k in range(len(idx) - 2)]
    if mode == 6:                                      # 扇
        return [[idx[0], idx[k], idx[k + 1]] for k in range(1, len(idx) - 1)]
    return []                                          # 点・線は面ではない


def _gltf_extras(path: str, head: bytes, j: dict) -> tuple[list, list[str]]:
    """メッシュごとの網目の点検と、骨と重み・VRChat の見積りの行（oc/meshstats.py・2026-10-15）。単位は m（glTF の決まり）。"""
    ms = _meshstats()
    bufs = _gltf_buffers(path, head, j)
    meshes, nodes = j.get("meshes") or [], j.get("nodes") or []
    rows = []
    for m in meshes:
        pos, polys, offs = [], [], {}
        try:
            for pr in m.get("primitives") or []:
                pi = (pr.get("attributes") or {}).get("POSITION")
                if pi is None or pr.get("mode", 4) not in (4, 5, 6):
                    continue
                if pi not in offs:
                    offs[pi] = len(pos) // 3
                    pos.extend(_gltf_acc(j, bufs, pi))
                o = offs[pi]
                n = j["accessors"][pi]["count"]
                polys.extend([o + a for a in t] for t in _gltf_tris(j, bufs, pr, n) if all(0 <= a < n for a in t))
            rows.append((m.get("name") or "（名前なし）", ms.topology(pos, polys, 1.0)))
        except (ValueError, KeyError, IndexError, TypeError, struct.error) as e:
            rows.append((m.get("name") or "（名前なし）", {"skipped": f"読めない（{type(e).__name__}: {e}）"}))
    out = []
    # 骨と重み（結び付けた時の姿勢＝ inverseBindMatrices の逆と、頂点の重み付きの中心）
    entries, bone_nodes = [], set()
    st = {"tris": 0, "skinned": 0, "basic": 0, "slots": 0, "bones": 0}
    inst = [n for n in nodes if isinstance(n.get("mesh"), int) and n["mesh"] < len(meshes)]
    for n in inst:
        mesh = meshes[n["mesh"]]
        t = rows[n["mesh"]][1]
        prims = mesh.get("primitives") or []
        if "tris" in t:
            st["tris"] += t["tris"]
        else:                                          # 網目を数えられないときは accessor の数から（データは読まない）
            acc = j.get("accessors") or []
            cnt = lambda i: acc[i].get("count", 0) if isinstance(i, int) and 0 <= i < len(acc) else 0  # noqa: E731
            st["tris"] += sum((cnt(pr.get("indices")) if pr.get("indices") is not None
                               else cnt((pr.get("attributes") or {}).get("POSITION"))) // 3
                              for pr in prims if pr.get("mode", 4) == 4)
        morph = any(pr.get("targets") for pr in prims)
        skin = (j.get("skins") or [])[n["skin"]] if isinstance(n.get("skin"), int) and n["skin"] < len(j.get("skins") or []) else None
        st["skinned" if skin or morph else "basic"] += 1
        st["slots"] += max(1, len(prims))
        if not skin:
            continue
        joints = skin.get("joints") or []
        bone_nodes.update(joints)
        try:
            ibm = _gltf_acc(j, bufs, skin["inverseBindMatrices"]) if skin.get("inverseBindMatrices") is not None else None
            sums: dict[int, list[float]] = {}
            for pr in prims:
                at = pr.get("attributes") or {}
                if at.get("POSITION") is None:
                    continue
                pos = _gltf_acc(j, bufs, at["POSITION"])
                for set_ in ("0", "1"):
                    if at.get("JOINTS_" + set_) is None or at.get("WEIGHTS_" + set_) is None:
                        continue
                    jj, ww = _gltf_acc(j, bufs, at["JOINTS_" + set_]), _gltf_acc(j, bufs, at["WEIGHTS_" + set_])
                    for v in range(len(pos) // 3):
                        for k in range(4):
                            w = ww[4 * v + k]
                            if w > 0:
                                s = sums.setdefault(int(jj[4 * v + k]), [0.0, 0.0, 0.0, 0.0])
                                s[0] += pos[3 * v] * w
                                s[1] += pos[3 * v + 1] * w
                                s[2] += pos[3 * v + 2] * w
                                s[3] += w
            for ji, s in sums.items():
                if not 0 <= ji < len(joints) or not s[3] > 0:
                    continue
                bp = _minv_affine(ibm[16 * ji:16 * ji + 16]) if ibm else None
                if bp is None:
                    continue
                name = (nodes[joints[ji]].get("name") if joints[ji] < len(nodes) else None) or f"joint{ji}"
                entries.append((name, (s[0] / s[3], s[1] / s[3], s[2] / s[3]), bp, s[3]))
        except (ValueError, KeyError, IndexError, TypeError, struct.error) as e:
            out.append(f"  （骨と重みは読めない: {type(e).__name__}: {e}）")
    st["bones"] = len(bone_nodes)
    if entries:
        out += ms.bone_check(entries, 100.0, "結び付けた時の姿勢 bind")[0]
    if st["skinned"]:
        out += ms.vrchat_estimate(st)
    return rows, out


def gltf(path: str, head: bytes, size: int) -> list[str]:
    j, _bin = _gltf_json(path, head)
    kind = "VRM" if path.lower().endswith(".vrm") else ("glTF のバイナリ .glb" if head.startswith(b"glTF") else "glTF")
    asset = j.get("asset") or {}
    acc = j.get("accessors") or []
    out = [f"構造（{kind}）:",
           f"  glTF {asset.get('version', '?')}・{_mb(size)}" + (f"・作成 {asset.get('generator')}" if asset.get("generator") else "")]
    exts = list(j.get("extensionsUsed") or [])
    if exts:
        out.append(f"  拡張: {_names(exts)}" + (f"（必須: {_names(j.get('extensionsRequired'))}）" if j.get("extensionsRequired") else ""))

    def acount(i):
        return acc[i].get("count", 0) if isinstance(i, int) and 0 <= i < len(acc) else 0
    meshes = j.get("meshes") or []
    tv = tt = 0
    rows = []
    for m in meshes:
        v = t = 0
        morphs = 0
        for pr in m.get("primitives") or []:
            pv = acount((pr.get("attributes") or {}).get("POSITION"))
            v += pv
            t += (acount(pr.get("indices")) if pr.get("indices") is not None else pv) // 3
            morphs = max(morphs, len(pr.get("targets") or []))
        names = (m.get("extras") or {}).get("targetNames") or []
        tv, tt = tv + v, tt + t
        rows.append((m.get("name") or "（名前なし）", v, t, len(m.get("primitives") or []), morphs, names, len(rows)))
    # ★網目の点検・骨と重み・VRChat の見積り（oc/meshstats.py・2026-10-15 vrcmodel と合意）
    topo, extra = [], []
    if meshes:
        try:
            topo, extra = _gltf_extras(path, head, j)
        except (OSError, ValueError, KeyError, IndexError, TypeError, struct.error) as e:
            extra = [f"  （網目の点検は途中で止まりました: {type(e).__name__}: {e}）"]
    out.append(f"  メッシュ {len(meshes)} 個・頂点 {tv:,}・三角形 {tt:,}・ノード {len(j.get('nodes') or [])} 個・シーン {len(j.get('scenes') or [])} 個")
    for nm, v, t, npr, morphs, names, mi in sorted(rows, key=lambda r: -r[1])[:_MAX_LIST]:
        out.append(f"    {nm}  頂点 {v:,}・三角形 {t:,}・プリミティブ {npr}" + (f"・モーフ {morphs}" + (f"（{_names(names, 12)}）" if names else "") if morphs else ""))
        if mi < len(topo):
            out.append("      " + _meshstats().topo_line(topo[mi][1]))
    if len(topo) > 1:
        out.append("  " + _meshstats().topo_total([t for _, t in topo]))
    if any(t.get("same") and t.get("islands_m", 0) < t.get("islands", 0) for _, t in topo):
        out.append("  （glTF は法線・UV の切れ目で頂点を分けて書くのが決まり。Blender なら読み込みの「Merge Vertices」か、"
                   "読み込んだ後に距離で結合してから布・重みの作業をする）")
    mats = j.get("materials") or []
    if mats:
        out.append(f"  マテリアル {len(mats)} 個: " + _names([(m.get("name") or "?") + (f"〈{m['alphaMode']}〉" if m.get("alphaMode") not in (None, "OPAQUE") else "") for m in mats]))
    imgs = j.get("images") or []
    if imgs:
        base = os.path.dirname(path)
        views = j.get("bufferViews") or []
        lst = []
        for im in imgs:
            if "bufferView" in im:
                bv = views[im["bufferView"]] if isinstance(im["bufferView"], int) and im["bufferView"] < len(views) else {}
                lst.append(f"{im.get('name') or '（埋め込み）'} {im.get('mimeType', '')} {_mb(bv.get('byteLength', 0))}")
            else:
                uri = im.get("uri", "")
                lst.append((uri[:40] + "…（データ URI）") if uri.startswith("data:") else uri + _exists_note(base, uri))
        out.append(f"  画像 {len(imgs)} 枚:")
        out += [f"    {x}" for x in lst[:_MAX_LIST]]
    bufs = j.get("buffers") or []
    if bufs and not head.startswith(b"glTF"):
        base = os.path.dirname(path)
        out.append("  データ: " + _names([(bf.get("uri", "")[:30] + "…" if bf.get("uri", "").startswith("data:") else bf.get("uri", "") + _exists_note(base, bf.get("uri", ""))) + f"（{_mb(bf.get('byteLength', 0))}）" for bf in bufs]))
    skins = j.get("skins") or []
    if skins:
        out.append("  スキン " + "・".join(f"{s.get('name') or '（名前なし）'} 骨 {len(s.get('joints') or [])} 本" for s in skins[:8]))
    anims = j.get("animations") or []
    if anims:
        rows = []
        for a in anims:
            dur = 0.0
            for s in a.get("samplers") or []:
                i = s.get("input")
                if isinstance(i, int) and i < len(acc) and acc[i].get("max"):
                    dur = max(dur, float(acc[i]["max"][0]))
            rows.append(f"{a.get('name') or '?'}（{dur:.2f} 秒・チャンネル {len(a.get('channels') or [])}）")
        out.append(f"  アニメーション {len(anims)} 本: {_names(rows)}")
    out += extra
    out += _vrm(j)
    return out


def _vrm(j: dict) -> list[str]:
    ex = j.get("extensions") or {}
    out = []
    if "VRMC_vrm" in ex:
        v = ex["VRMC_vrm"]
        meta = v.get("meta") or {}
        out.append(f"  VRM 1.0: 名前 {meta.get('name')!r}・版 {meta.get('version')!r}・作者 {_names(meta.get('authors') or [])}")
        lic = [f"{k}={meta[k]}" for k in ("avatarPermission", "commercialUsage", "creditNotation", "allowRedistribution",
                                          "modification", "allowExcessivelyViolentUsage", "allowExcessivelySexualUsage",
                                          "allowPoliticalOrReligiousUsage", "allowAntisocialOrHateUsage") if k in meta]
        out.append(f"    利用の条件: {', '.join(lic)}" + (f"・{meta.get('licenseUrl')}" if meta.get("licenseUrl") else ""))
        hb = ((v.get("humanoid") or {}).get("humanBones") or {})
        out.append(f"    人型の骨 {len(hb)} 本" + ("" if {"hips", "head", "leftHand", "rightHand"} <= set(hb) else "（★必須の骨が欠けている）"))
        exp = v.get("expressions") or {}
        pre, cus = list((exp.get("preset") or {}).keys()), list((exp.get("custom") or {}).keys())
        if pre or cus:
            out.append(f"    表情: 既定 {len(pre)}（{_names(pre, 18)}）・独自 {len(cus)}" + (f"（{_names(cus, 12)}）" if cus else ""))
        sb = ex.get("VRMC_springBone") or {}
        if sb:
            out.append(f"    揺れ物: springs {len(sb.get('springs') or [])}・colliders {len(sb.get('colliders') or [])}")
    elif "VRM" in ex:
        v = ex["VRM"]
        meta = v.get("meta") or {}
        out.append(f"  VRM 0.x: 名前 {meta.get('title')!r}・版 {meta.get('version')!r}・作者 {meta.get('author')!r}")
        lic = [f"{k}={meta[k]}" for k in ("allowedUserName", "violentUssageName", "sexualUssageName", "commercialUssageName",
                                          "licenseName") if k in meta]
        out.append(f"    利用の条件: {', '.join(lic)}" + (f"・{meta.get('otherPermissionUrl')}" if meta.get("otherPermissionUrl") else ""))
        hb = (v.get("humanoid") or {}).get("humanBones") or []
        out.append(f"    人型の骨 {len(hb)} 本")
        groups = ((v.get("blendShapeMaster") or {}).get("blendShapeGroups") or [])
        if groups:
            out.append(f"    表情（BlendShape）{len(groups)} 個: {_names([g.get('presetName') if g.get('presetName') not in (None, 'unknown') else g.get('name') for g in groups], 20)}")
        sa = v.get("secondaryAnimation") or {}
        if sa:
            out.append(f"    揺れ物: boneGroups {len(sa.get('boneGroups') or [])}・colliderGroups {len(sa.get('colliderGroups') or [])}")
    return out


# ── OBJ / MTL ───────────────────────────────────────────────────────────────

def obj(path: str, size: int) -> list[str]:
    cnt: dict[str, int] = {}
    objs, groups, mtls, libs = [], [], [], []
    # ★網目の点検（oc/meshstats.py・2026-10-15）。OBJ の頂点は UV の切れ目でも分かれない（位置の番号を面が共有する）。
    #   単位は書かれていないので 1 = 1 m と見る（Blender の読み込みと同じ）。400 MB を超えるファイルは数えない。
    want = size <= 400 << 20
    pos: list[float] = []
    polys: list[list[int]] = []
    bad = None
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line[:1] == b"#":
                continue
            key = line.split(None, 1)[0].decode("ascii", "replace")
            cnt[key] = cnt.get(key, 0) + 1
            if key in ("o", "g", "usemtl", "mtllib") and len(line.split(None, 1)) > 1:
                val = line.split(None, 1)[1].decode("utf-8", "replace")
                {"o": objs, "g": groups, "usemtl": mtls, "mtllib": libs}[key].append(val)
            elif want and bad is None and key == "v":
                try:
                    x = line.split()
                    pos += (float(x[1]), float(x[2]), float(x[3]))
                except (ValueError, IndexError):
                    bad = "頂点の行が読めない"
            elif want and bad is None and key == "f":
                try:
                    nv = len(pos) // 3
                    f = [int(tok.split(b"/", 1)[0]) for tok in line.split()[1:]]
                    polys.append([i - 1 if i > 0 else nv + i for i in f])
                except ValueError:
                    bad = "面の行が読めない"
    base = os.path.dirname(path)
    out = ["構造（Wavefront OBJ）:",
           f"  頂点 {cnt.get('v', 0):,}・UV {cnt.get('vt', 0):,}・法線 {cnt.get('vn', 0):,}・面 {cnt.get('f', 0):,}・"
           f"オブジェクト {len(objs)}・グループ {len(set(groups))}・使うマテリアル {len(set(mtls))}"]
    if polys and bad is None:
        nv = len(pos) // 3
        if all(0 <= i < nv for p in polys for i in p):
            out.append("  網目（ファイル全体・1 単位 = 1 m と見る）: " + _meshstats().topology_line_of(pos, polys, 1.0))
        else:
            out.append("  網目: ★面が頂点の外を指している（壊れているか、頂点が後に書かれた形）")
    elif bad:
        out.append(f"  網目: 数えない（{bad}）")
    elif not want:
        out.append("  網目: 400 MB を超えるので数えない")
    if objs:
        out.append(f"  オブジェクト: {_names(objs)}")
    for lib in libs[:4]:
        out.append(f"  マテリアルの表: {lib}{_exists_note(base, lib)}")
        p = os.path.join(base, lib)
        if os.path.exists(p):
            out += ["    " + x for x in mtl(p, os.path.getsize(p))[1:]]
    return out


def mtl(path: str, size: int) -> list[str]:
    mats, maps = [], []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("newmtl "):
                mats.append(s[7:])
            elif re.match(r"^(map_\w+|bump|disp|decal|refl)\s", s):
                maps.append(s.split()[-1])
    base = os.path.dirname(path)
    return ["構造（OBJ のマテリアルの表 .mtl）:", f"  マテリアル {len(mats)} 個: {_names(mats)}",
            f"  テクスチャ {len(set(maps))} 個: " + _names([m + _exists_note(base, m) for m in dict.fromkeys(maps)])]


# ── MMD（PMX・PMD・VMD）────────────────────────────────────────────────────

class _B:
    def __init__(self, b: bytes, p: int = 0):
        self.b, self.p = b, p

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.b):
            raise ValueError(f"{self.p:,} バイト目でデータが足りません")
        v = self.b[self.p:self.p + n]; self.p += n; return v

    def u8(self): return self.take(1)[0]
    def i32(self): return struct.unpack("<i", self.take(4))[0]
    def u32(self): return struct.unpack("<I", self.take(4))[0]
    def u16(self): return struct.unpack("<H", self.take(2))[0]

    def skip(self, n: int):
        self.take(n)


def _idx(r: _B, size: int) -> int:
    return {1: lambda: struct.unpack("<b", r.take(1))[0], 2: lambda: struct.unpack("<h", r.take(2))[0],
            4: lambda: r.i32()}[size]()


def pmx(path: str, size: int) -> list[str]:
    with open(path, "rb") as fh:
        r = _B(fh.read(min(size, 512 << 20)))
    r.take(4)
    ver = struct.unpack("<f", r.take(4))[0]
    ng = r.u8()
    g = list(r.take(ng))
    enc, add_uv, vsz, tsz, msz, bsz, mosz, rsz = (g + [0] * 8)[:8]

    def text() -> str:
        n = r.i32()
        raw = r.take(n)
        return raw.decode("utf-16-le" if enc == 0 else "utf-8", "replace")
    name, name_en, _c, _ce = text(), text(), text(), text()
    out = ["構造（MMD のモデル PMX）:", f"  版 {ver:.1f}・名前 {name!r}" + (f"（{name_en!r}）" if name_en else "") + f"・{_mb(size)}"]
    nv = r.i32()
    for _ in range(nv):
        r.skip(32 + 16 * add_uv)
        w = r.u8()
        r.skip({0: bsz, 1: 2 * bsz + 4, 2: 4 * bsz + 16, 3: 2 * bsz + 4 + 36, 4: 4 * bsz + 16}[w])
        r.skip(4)
    nf = r.i32()
    r.skip(nf * vsz)
    texs = [text() for _ in range(r.i32())]
    mats = []
    for _ in range(r.i32()):
        mn = text(); text()
        r.skip(16 + 12 + 4 + 12 + 1 + 16 + 4)
        _idx(r, tsz); _idx(r, tsz)
        r.skip(1)
        shared = r.u8()
        r.skip(1 if shared else tsz)
        text()
        r.skip(4)
        mats.append(mn)
    bones = []
    for _ in range(r.i32()):
        bn = text(); text()
        r.skip(12); _idx(r, bsz); r.skip(4)
        fl = r.u16()
        r.skip(bsz if fl & 0x1 else 12)
        if fl & 0x300:
            r.skip(bsz + 4)
        if fl & 0x400:
            r.skip(12)
        if fl & 0x800:
            r.skip(24)
        if fl & 0x2000:
            r.skip(4)
        if fl & 0x20:
            r.skip(bsz + 8)
            for _ in range(r.i32()):
                r.skip(bsz)
                if r.u8():
                    r.skip(24)
        bones.append(bn)
    morphs: dict[int, list[str]] = {}
    osz = {0: mosz + 4, 1: vsz + 12, 2: bsz + 28, 3: vsz + 16, 4: vsz + 16, 5: vsz + 16, 6: vsz + 16, 7: vsz + 16,
           8: msz + 1 + 112, 9: mosz + 4, 10: rsz + 1 + 24}
    for _ in range(r.i32()):
        mn = text(); text()
        panel = r.u8()
        typ = r.u8()
        r.skip(osz[typ] * r.i32())
        morphs.setdefault(panel, []).append(mn)
    base = os.path.dirname(path)
    out.append(f"  頂点 {nv:,}・面 {nf // 3:,}・材質 {len(mats)}・ボーン {len(bones)}・モーフ {sum(len(v) for v in morphs.values())}")
    out.append(f"  テクスチャ {len(texs)} 個:")
    out += [f"    {t}{_exists_note(base, t.replace(chr(92), '/'))}" for t in texs[:_MAX_LIST]]
    out.append(f"  材質: {_names(mats)}")
    out.append(f"  ボーン（先頭）: {_names(bones, 30)}")
    pn = {1: "眉", 2: "目", 3: "口", 4: "その他", 0: "システム"}
    for k, v in sorted(morphs.items()):
        out.append(f"  モーフ〈{pn.get(k, k)}〉{len(v)}: {_names(v, 20)}")
    try:
        for _ in range(r.i32()):                        # 表示枠
            text(); text(); r.skip(1)
            for _ in range(r.i32()):
                t = r.u8()
                r.skip(bsz if t == 0 else mosz)
        nr = r.i32()
        r.skip(0)
        rb = []
        for _ in range(nr):
            rb.append(text()); text()
            r.skip(rsz if False else 0)
            _idx(r, bsz); r.skip(1 + 2 + 1 + 12 + 12 + 12 + 4 * 5 + 1)
        nj = r.i32()
        out.append(f"  剛体 {nr}・ジョイント {nj}（揺れ物）")
    except (ValueError, struct.error, KeyError):
        pass
    return out


def pmd(path: str, size: int) -> list[str]:
    with open(path, "rb") as fh:
        r = _B(fh.read(min(size, 256 << 20)))
    r.take(3)
    ver = struct.unpack("<f", r.take(4))[0]
    name = r.take(20).split(b"\x00")[0].decode("cp932", "replace")
    r.take(256)
    nv = r.u32(); r.skip(nv * 38)
    nf = r.u32(); r.skip(nf * 2)
    nm = r.u32()
    texs = []
    for _ in range(nm):
        r.skip(50)                                      # 色 16・反射 4・反射色 12・環境色 12・トゥーン 1・輪郭 1・面の数 4
        t = r.take(20).split(b"\x00")[0].decode("cp932", "replace")
        if t:
            texs.append(t)
    nb = r.u16()
    base = os.path.dirname(path)
    return ["構造（MMD のモデル PMD）:", f"  版 {ver:.1f}・名前 {name!r}・頂点 {nv:,}・面 {nf // 3:,}・材質 {nm}・ボーン {nb}",
            "  テクスチャ: " + _names([t + _exists_note(base, t.split('*')[0].replace(chr(92), '/')) for t in dict.fromkeys(texs)])]


def vmd(path: str, size: int) -> list[str]:
    with open(path, "rb") as fh:
        r = _B(fh.read(min(size, 256 << 20)))
    sig = r.take(30)
    nlen = 20 if b"0002" in sig else 10
    model = r.take(nlen).split(b"\x00")[0].decode("cp932", "replace")
    nb = r.u32()
    bones, last = set(), 0
    for _ in range(nb):
        rec = r.take(111)
        bones.add(rec[:15].split(b"\x00")[0].decode("cp932", "replace"))
        last = max(last, struct.unpack_from("<I", rec, 15)[0])
    out = ["構造（MMD の動き VMD）:", f"  対象のモデル {model!r}・ボーンのキー {nb:,}（{len(bones)} 本）"]
    try:
        nm = r.u32()
        morphs = set()
        for _ in range(nm):
            rec = r.take(23)
            morphs.add(rec[:15].split(b"\x00")[0].decode("cp932", "replace"))
            last = max(last, struct.unpack_from("<I", rec, 15)[0])
        out.append(f"  モーフのキー {nm:,}（{len(morphs)} 個: {_names(sorted(morphs), 15)}）")
        nc = r.u32()
        for _ in range(nc):
            rec = r.take(61)
            last = max(last, struct.unpack_from("<I", rec, 0)[0])
        out.append(f"  カメラのキー {nc:,}")
    except ValueError:
        pass
    out.append(f"  長さ {last:,} フレーム（30 fps で {last / 30:.1f} 秒）")
    return out


# ── Live2D（.moc3・.model3.json）────────────────────────────────────────────

_MOC3_VER = {1: "3.0", 2: "3.3", 3: "4.0", 4: "4.2", 5: "5.0", 6: "5.3"}
_COUNT_NAMES = ["parts", "deformers", "warpDeformers", "rotationDeformers", "artMeshes", "parameters"]


def _id_at(b: bytes, o: int) -> str | None:
    if o <= 0 or o + 64 > len(b):
        return None
    raw = b[o:o + 64]
    s = raw.split(b"\x00")[0]
    if not s or any(c < 33 or c > 126 for c in s) or any(raw[len(s):]):
        return None
    return s.decode("ascii")


def moc3_header_problem(b: bytes) -> str:
    """'MOC3' で始まるバイト列が本物の .moc3 の頭か。本物なら ""、違えば理由（英語・道具の結果に入れる）。

    ★2026-10-11（ClaudeCode の知見「ゲーム解析とMOD作成の知見」§5・Windows の VM の実測）: 圧縮されたバンドル（LZ4）の中では
      'MOC3' の 4 バイトが偶然並び、直前の 4 バイトまで長さらしく見える（1 本で 10 件すべて偽物だった）。版や大きさの勘では
      落とせない。仕様で決まっているのは 0x06〜0x3F の予約領域＝58 バイトすべてゼロ（圧縮の偶然はここまで再現しない）。
      版は狭く弾かない（版 1 の実物がある）。同じ日のオーナーの走行で、Blackwall の Read が LZ4 の断片を「版 23・ビッグエンディアン・
      パーツ 12 億」と .moc3 として読んでいた。"""
    if len(b) < 0x40 or not b.startswith(b"MOC3"):
        return "shorter than the 64-byte .moc3 header"
    if not 1 <= b[4] <= 15:
        return f"version byte is {b[4]}"
    if b[5] not in (0, 1):
        return f"endianness byte is {b[5]} (must be 0 or 1)"
    nz = sum(1 for x in b[6:0x40] if x)
    if nz:
        return f"{nz} of the 58 reserved header bytes 0x06-0x3F are not zero (a real .moc3 has all zero)"
    return ""


def moc3(path: str, head: bytes, size: int) -> list[str]:
    with open(path, "rb") as fh:
        b = fh.read(min(size, 128 << 20))
    bad = moc3_header_problem(b)
    if bad:
        return ["構造: 'MOC3' で始まるが、Live2D の .moc3 ではありません。",
                f"  理由: {bad}.",
                "  圧縮されたデータ（UnityFS バンドルの LZ4 など）の中で 'MOC3' の 4 文字が偶然並んだ断片を切り出したものです。",
                "  中の数は意味を持ちません。モデルを取り出すなら、バンドルを Read の find=MOC3（展開した中身で探す）か path_id で読んでください。"]
    v, big = b[4], b[5]
    out = ["構造（Live2D Cubism の .moc3）:",
           f"  版 {v}（Cubism {_MOC3_VER.get(v, '?')} 以降で読める形）・{'ビッグ' if big else 'リトル'}エンディアン・{_mb(size)}"]
    if big:
        return out
    try:
        count_off = struct.unpack_from("<I", b, 0x40)[0]
        counts = struct.unpack_from("<6I", b, count_off)
        out.append("  数: " + "・".join(f"{n} {c:,}" for n, c in zip(
            ["パーツ", "デフォーマ", "ワープ", "回転", "アートメッシュ", "パラメータ"], counts)))
        canvas_off = struct.unpack_from("<I", b, 0x44)[0]
        ppu, ox, oy, cw, ch = struct.unpack_from("<5f", b, canvas_off)
        if 0 < cw < 1e6 and 0 < ch < 1e6:
            out.append(f"  キャンバス {cw:,.0f}×{ch:,.0f}・1 単位 {ppu:g} px")
        # ID の配列（64 バイトの固定長の文字列）を、節の位置の表から探す。並びはパーツ・デフォーマ・アートメッシュ・パラメータ。
        table = struct.unpack_from("<160I", b, 0x40)
        seen = []
        for o in table:
            if o and o not in seen and _id_at(b, o):
                seen.append(o)
        want = [("パーツ", counts[0]), ("デフォーマ", counts[1]), ("アートメッシュ", counts[4]), ("パラメータ", counts[5])]
        for (label, n), o in zip(want, seen):
            ids = [_id_at(b, o + 64 * k) for k in range(n)]
            ok = [x for x in ids if x]
            if len(ok) != n:
                out.append(f"  {label}の ID: 読めた {len(ok)} / {n}（並びが想定と違う可能性）")
                continue
            out.append(f"  {label}の ID {n}: {_names(ok, 40 if label == 'パラメータ' else 20)}")
    except (struct.error, ValueError) as e:
        out.append(f"  数を読めません: {e}")
    out.append("  → 部品の表示名は同じ場所の .cdi3.json、テクスチャ・動き・物理の並びは .model3.json（Read で要約が出ます）。")
    return out


def model3_summary(path: str, text: str) -> list[str]:
    """.model3.json の要約（参照の欠け・モーションの長さ）。Read の本文に添える。"""
    try:
        j = json.loads(text)
    except ValueError:
        return []
    fr = j.get("FileReferences") or {}
    base = os.path.dirname(path)
    out = ["", "<model3>", f"Live2D のモデル定義（版 {j.get('Version')}）:"]
    moc = fr.get("Moc", "")
    out.append(f"  moc3: {moc}{_exists_note(base, moc)}")
    tex = fr.get("Textures") or []
    miss = [t for t in tex if _exists_note(base, t)]
    out.append(f"  テクスチャ {len(tex)} 枚" + (f"（★見つからない {len(miss)}: {_names(miss, 6)}）" if miss else "（全部ある）"))
    for key in ("Physics", "Pose", "DisplayInfo", "UserData"):
        if fr.get(key):
            out.append(f"  {key}: {fr[key]}{_exists_note(base, fr[key])}")
    exps = fr.get("Expressions") or []
    if exps:
        out.append(f"  表情 {len(exps)}: " + _names([e.get("Name", "?") + _exists_note(base, e.get("File", "")) for e in exps]))
    mot = fr.get("Motions") or {}
    total = sum(len(v) for v in mot.values() if isinstance(v, list))
    if total:
        out.append(f"  モーション {total} 本（グループ {len(mot)}）:")
        shown = 0
        for grp, lst in mot.items():
            for m in lst if isinstance(lst, list) else []:
                f = m.get("File", "")
                info = _exists_note(base, f)
                if not info and shown < 20:
                    try:
                        with open(os.path.join(base, f), encoding="utf-8-sig") as fh:
                            meta = (json.load(fh) or {}).get("Meta") or {}
                        info = f"（{meta.get('Duration', '?')} 秒・{meta.get('Fps', '?')} fps・曲線 {meta.get('CurveCount', '?')}" \
                               + ("・繰り返し" if meta.get("Loop") else "") + "）"
                    except (OSError, ValueError):
                        info = "（読めない）"
                    out.append(f"    [{grp or '（無名）'}] {f}{info}" + (f"・音 {m['Sound']}{_exists_note(base, m['Sound'])}" if m.get("Sound") else ""))
                    shown += 1
        if total > shown:
            out.append(f"    …ほか {total - shown} 本")
    for g in j.get("Groups") or []:
        out.append(f"  グループ {g.get('Name')}（{g.get('Target')}）: {_names(g.get('Ids') or [], 12)}")
    if j.get("HitAreas"):
        out.append("  当たり判定: " + _names([h.get("Name") or h.get("Id") for h in j["HitAreas"]]))
    out.append("</model3>")
    return out


# ── 入口 ─────────────────────────────────────────────────────────────────────

def describe(path: str, head: bytes, size: int) -> list[str] | None:
    """モデルの形なら構造の行。違えば None。テキスト形式（.gltf・.obj・.mtl・ASCII FBX）も扱う。"""
    low = path.lower()
    if head.startswith(b"Kaydara FBX Binary") or (low.endswith(".fbx") and b"FBX" in head[:200]):
        return fbx(path, size)
    if head.startswith(b"glTF") or (low.endswith(".gltf") and head.lstrip()[:1] == b"{"):
        return gltf(path, head, size)
    if head.startswith(b"PMX "):
        return pmx(path, size)
    if head.startswith(b"Pmd") and low.endswith(".pmd"):
        return pmd(path, size)
    if head.startswith(b"Vocaloid Motion Data"):
        return vmd(path, size)
    if head.startswith(b"MOC3") and len(head) >= 6:
        return moc3(path, head, size)
    if low.endswith(".obj") and b"\x00" not in head[:1024]:     # COFF の .obj（コンパイラの出力）はバイナリ＝ここに来ない
        return obj(path, size)
    if low.endswith(".mtl") and b"newmtl" in head:
        return mtl(path, size)
    return None


def is_text_model(path: str) -> bool:
    """テキストとしても読めるが、構造の要約を添えたいモデルのファイル。"""
    return path.lower().endswith((".gltf", ".obj", ".mtl", ".fbx"))
