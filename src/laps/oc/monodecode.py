"""MonoBehaviour の項目を、ゲームの Managed/*.dll の型の宣言から読む（型の木が無くても）。2026-10-10。

オーナー「ゲーム解析系をさらに強化してください。ログを参考に」。走行記録（10-09〜10-10）の一番の壁: ゲームの
MonoBehaviour 12,782 個のうち UnityPy で読めたのは 225 個だけ（型の木が無いので read_typetree が ValueError）。
エージェントはクラスの項目を推測し、生のバイト列を手で区切ろうとして台本を書き続けた。
Unity が保存する並びはクラスの宣言から決まる（AssetStudio の TypeDefinitionConverter と同じ規則）:
  ・基底クラスの項目が先。static・const・readonly・[NonSerialized] は無い。public か [SerializeField] の物だけ。
  ・bool・byte・short・char は詰めて置き、項目の後ろを 4 バイトに揃える（配列の要素は揃えない・配列の後ろで揃える）。
  ・文字列＝長さ＋UTF-8＋揃え。配列と List<T>＝個数＋要素。UnityEngine.Object の子への参照＝PPtr（4＋8 バイト）。
  ・enum＝int。[Serializable] のクラス・構造体＝その項目を中に並べる。Dictionary などは保存されない（飛ばす）。
分からない型（AnimationCurve・[SerializeReference]・解決できない型）に来たら、そこで止めて読めた所までを返す。
最後に「読んだバイト数＝中身の大きさ」かを確かめて書く（合わなければ推定が外れている）。
"""
from __future__ import annotations

import os
import struct

from . import dotnetmeta as _dm

MAX_DEPTH = 6
MAX_ITEMS_SHOWN = 12
MAX_ELEMS = 200_000
MAX_LINES = 120

# Unity の値の構造体（項目の並びが決まっている）: 名前 → [(項目名, 型)]
_F, _I = "f", "i"
_UNITY_STRUCTS = {
    "UnityEngine.Vector2": [("x", _F), ("y", _F)],
    "UnityEngine.Vector3": [("x", _F), ("y", _F), ("z", _F)],
    "UnityEngine.Vector4": [("x", _F), ("y", _F), ("z", _F), ("w", _F)],
    "UnityEngine.Quaternion": [("x", _F), ("y", _F), ("z", _F), ("w", _F)],
    "UnityEngine.Color": [("r", _F), ("g", _F), ("b", _F), ("a", _F)],
    "UnityEngine.Rect": [("x", _F), ("y", _F), ("width", _F), ("height", _F)],
    "UnityEngine.Bounds": [("cx", _F), ("cy", _F), ("cz", _F), ("ex", _F), ("ey", _F), ("ez", _F)],
    "UnityEngine.Vector2Int": [("x", _I), ("y", _I)],
    "UnityEngine.Vector3Int": [("x", _I), ("y", _I), ("z", _I)],
    "UnityEngine.RectInt": [("x", _I), ("y", _I), ("width", _I), ("height", _I)],
    "UnityEngine.BoundsInt": [("x", _I), ("y", _I), ("z", _I), ("sx", _I), ("sy", _I), ("sz", _I)],
    "UnityEngine.Color32": [("rgba", "I")],
    "UnityEngine.LayerMask": [("m_Bits", "I")],
    "UnityEngine.Matrix4x4": [(f"e{r}{c}", _F) for r in range(4) for c in range(4)],
    "UnityEngine.Hash128": [("b0", "I"), ("b1", "I"), ("b2", "I"), ("b3", "I")],
    "UnityEngine.RectOffset": [("m_Left", _I), ("m_Right", _I), ("m_Top", _I), ("m_Bottom", _I)],
}
# 形が項目の宣言から決まらない（Unity が独自に書く）物。AnimationCurve・Gradient・GUIStyle は下で形を決めて読む
#   （AssetStudio の SerializedTypeHelper と同じ）。残りはここで止める。
_UNITY_SPECIAL = {"UnityEngine.AnimationCurve", "UnityEngine.Gradient", "UnityEngine.GUIStyle"}
_UNITY_STOP = {"UnityEngine.Rendering.SphericalHarmonicsL2", "UnityEngine.PropertyName"}
_OBJECT_ROOTS = {"UnityEngine.Object"}
# 基底の並びで読み飛ばす物（項目を持たない・頭は MonoBehaviour の頭として読み済み）
_ENGINE_BASES = {"System.Object", "System.ValueType", "UnityEngine.Object", "UnityEngine.Component", "UnityEngine.Behaviour",
                 "UnityEngine.MonoBehaviour", "UnityEngine.ScriptableObject", "UnityEngine.StateMachineBehaviour"}
# UnityEngine の DLL が無くても UnityEngine.Object の子と分かる名前
_KNOWN_OBJECTS = _ENGINE_BASES - {"System.Object", "System.ValueType"} | {
    "UnityEngine.GameObject", "UnityEngine.Transform", "UnityEngine.RectTransform", "UnityEngine.Sprite",
    "UnityEngine.Texture", "UnityEngine.Texture2D", "UnityEngine.Material", "UnityEngine.Mesh", "UnityEngine.AudioClip",
    "UnityEngine.TextAsset", "UnityEngine.AnimationClip", "UnityEngine.Shader", "UnityEngine.Font"}
_PRIM_FMT = {"bool": "?", "byte": "B", "sbyte": "b", "short": "h", "ushort": "H", "char": "H", "int": "i",
             "uint": "I", "long": "q", "ulong": "Q", "float": "f", "double": "d"}
_SMALL = {"bool", "byte", "sbyte", "short", "ushort", "char"}
_SYSTEM_NAMES = {"System.Boolean": "bool", "System.Byte": "byte", "System.SByte": "sbyte", "System.Int16": "short",
                 "System.UInt16": "ushort", "System.Char": "char", "System.Int32": "int", "System.UInt32": "uint",
                 "System.Int64": "long", "System.UInt64": "ulong", "System.Single": "float", "System.Double": "double",
                 "System.String": "string"}


class Stop(Exception):
    """ここから先は読めない（理由）。"""


def managed_dir(path: str) -> str | None:
    """<Game>_Data/Managed を探す（ファイルのフォルダから 4 階層上まで）。"""
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(5):
        cand = os.path.join(d, "Managed")
        if os.path.isdir(cand):
            return cand
        if os.path.basename(d).lower().endswith("_data"):
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


class Assemblies:
    """Managed フォルダの DLL を名前で開く（必要になった物だけ・開けない物は覚えておく）。"""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self._files: dict[str, str] = {}
        try:
            for n in os.listdir(folder):
                if n.lower().endswith(".dll"):
                    self._files[n[:-4].lower()] = os.path.join(folder, n)
        except OSError:
            pass
        self._open: dict[str, _dm.Assembly | None] = {}

    def get(self, name: str) -> _dm.Assembly | None:
        key = name.lower()
        if key.endswith(".dll"):
            key = key[:-4]
        if key not in self._open:
            p = self._files.get(key)
            a = None
            if p:
                try:
                    with open(p, "rb") as fh:
                        a = _dm.Assembly(fh.read(256 * 1024 * 1024))
                except (OSError, _dm.Bad, struct.error, IndexError, ValueError):
                    a = None
            self._open[key] = a
        return self._open[key]


class Decoder:
    def __init__(self, asms: Assemblies, data: bytes, endian: str, ver: int, pptr_label, base_off: int = 0,
                 unity: tuple[int, int] = (2019, 4)) -> None:
        self.asms, self.b, self.e, self.ver = asms, data, endian, ver
        self.uv = unity
        self.p = 0
        self.base_off = base_off                # 表示の位置＝オブジェクトの頭から（項目の頭の位置を足す）
        self.pptr_label = pptr_label            # (fid, pid) → 表示
        self.lines: list[str] = []

    # ── 読む ──
    def take(self, n: int) -> bytes:
        if self.p + n > len(self.b):
            raise Stop(f"中身の終わり（{len(self.b):,} バイト）を越えて読もうとした＝型の推定が合っていない")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def num(self, fmt: str):
        return struct.unpack(self.e + fmt, self.take(struct.calcsize(fmt)))[0]

    def align(self) -> None:
        self.p += (-self.p) % 4

    # ── 型の解決 ──
    def resolve(self, asm: _dm.Assembly, tag: int, idx: int, gen: list | None):
        """("def", asm, 行) か ("node", 型の形) か ("name", 名前)。"""
        if tag == 0:
            return ("def", asm, idx)
        if tag == 1:
            r = asm.row(1, idx)
            if r is None:
                return ("name", "?")
            stag, sidx = r["Scope"]
            full = (r["Namespace"] + "." if r["Namespace"] else "") + r["Name"]
            if stag == 3:                                       # 入れ子の参照
                outer = self.resolve(asm, 1, sidx, gen)
                if outer[0] == "def":
                    k = outer[1].nested_type(outer[2], r["Name"])
                    if k:
                        return ("def", outer[1], k)
                return ("name", full)
            target = asm
            if stag == 2:
                ar = asm.row(0x23, sidx)
                target = self.asms.get(ar["Name"]) if ar else None
            if target is None:
                return ("name", full)
            k = target.lookup_type(r["Namespace"], r["Name"])
            if k:
                return ("def", target, k)
            for ex in target.rows(0x27):                       # 型の転送（netstandard など）
                if ex["Name"] == r["Name"] and ex["Namespace"] == r["Namespace"]:
                    itag, iidx = ex["Implementation"]
                    if itag == 1:
                        ar = target.rows(0x23)[iidx - 1]
                        t2 = self.asms.get(ar["Name"])
                        k = t2.lookup_type(r["Namespace"], r["Name"]) if t2 else 0
                        if k:
                            return ("def", t2, k)
            return ("name", full)
        if tag == 2:
            tr = asm.row(27, idx)
            if tr is not None:
                node, _p = asm.sig_node(asm.blob(tr["Signature"]), 0)
                return ("node", node, asm)
        return ("name", "?")

    def full_name(self, asm: _dm.Assembly, k: int) -> str:
        r = asm.row(2, k)
        if k in asm._nested:
            return self.full_name(asm, asm._nested[k]) + "+" + r["Name"]
        return (r["Namespace"] + "." if r["Namespace"] else "") + r["Name"]

    def bases(self, asm: _dm.Assembly, k: int) -> list[tuple]:
        """基底クラスの並び（近い順）: (名前, asm か None, 行か 0)。"""
        out = []
        cur = (asm, k)
        for _ in range(16):
            a, i = cur
            tag, idx = a.row(2, i)["Extends"]
            if tag == 0 and idx == 0:
                break
            res = self.resolve(a, tag, idx, None)
            if res[0] == "def":
                out.append((self.full_name(res[1], res[2]), res[1], res[2]))
                cur = (res[1], res[2])
            elif res[0] == "node" and res[1][0] == "g":
                g = self.resolve(res[2], res[1][1][0], res[1][1][1], None)
                if g[0] == "def":
                    out.append((self.full_name(g[1], g[2]), g[1], g[2]))
                    cur = (g[1], g[2])
                else:
                    out.append((g[1] if g[0] == "name" else "?", None, 0))
                    break
            else:
                out.append((res[1] if res[0] == "name" else "?", None, 0))
                break
        return out

    def kind(self, asm: _dm.Assembly, k: int) -> str:
        name = self.full_name(asm, k)
        if name in _UNITY_STRUCTS:
            return "ustruct"
        if name in _UNITY_SPECIAL:
            return "special"
        if name in _UNITY_STOP:
            return "stop"
        if name in _SYSTEM_NAMES:
            return "prim"
        bases = self.bases(asm, k)
        chain = [n for n, _a, _k in bases]
        if set(chain) & (_OBJECT_ROOTS | _KNOWN_OBJECTS) or name in _OBJECT_ROOTS:
            return "pptr"
        if "System.Enum" in chain:
            return "enum"
        if bases and bases[-1][1] is None and chain[-1] not in ("System.Object", "System.ValueType"):
            return "unknown"
        flags = asm.row(2, k)["Flags"]
        if flags & 0x20:                                       # interface
            return "skip"
        if flags & 0x2000:
            return "inline"
        return "skip"

    # ── 型ごとに読む ──
    def fields_of(self, asm: _dm.Assembly, k: int) -> list[tuple]:
        """Unity が保存する項目（基底クラスの物から）: (名前, 型の形, その型の asm)。"""
        out = []
        chain = self.bases(asm, k)
        for name, a, i in reversed(chain):
            if name in _ENGINE_BASES:
                continue
            if a is None:
                raise Stop(f"基底クラス {name} の DLL が見つからない（その項目が先に並ぶ）")
            out += self._own_fields(a, i)
        out += self._own_fields(asm, k)
        return out

    def _own_fields(self, asm: _dm.Assembly, k: int) -> list[tuple]:
        out = []
        for f in asm.fields_of(k):
            r = asm.row(4, f)
            if r is None:
                continue
            fl = r["Flags"]
            attrs = asm._attrs.get(f, set())
            if fl & (0x10 | 0x20 | 0x40 | 0x80):               # static・readonly・const・NonSerialized
                continue
            if (fl & 7) != 6 and not ({"SerializeField", "SerializeReference"} & attrs):
                continue
            if "SerializeReference" in attrs:
                out.append((r["Name"], ("x", "SerializeReference"), asm))
                continue
            out.append((r["Name"], asm.field_node(asm.blob(r["Signature"])), asm))
        return out

    def type_label(self, node, asm, gen) -> str:
        t = node[0]
        if t == "p":
            return node[1]
        if t == "a":
            return self.type_label(node[1], asm, gen) + "[]"
        if t == "t":
            res = self.resolve(asm, node[1], node[2], gen)
            return (self.full_name(res[1], res[2]) if res[0] == "def" else str(res[1])).rsplit(".", 1)[-1]
        if t == "g":
            res = self.resolve(asm, node[1][0], node[1][1], gen)
            base = (self.full_name(res[1], res[2]) if res[0] == "def" else str(res[1])).rsplit(".", 1)[-1].split("`")[0]
            return base + "<" + ", ".join(self.type_label(a, asm, gen) for a in node[2]) + ">"
        if t == "v" and gen and node[1] < len(gen):
            return self.type_label(gen[node[1]][0], gen[node[1]][1], None)
        return str(node[1])

    def is_collection(self, node, asm) -> tuple | None:
        """配列か List<T> なら要素の形。"""
        if node[0] == "a":
            return node[1]
        if node[0] == "g":
            res = self.resolve(asm, node[1][0], node[1][1], None)
            name = self.full_name(res[1], res[2]) if res[0] == "def" else str(res[1])
            if name == "System.Collections.Generic.List`1" and len(node[2]) == 1:
                return node[2][0]
        return None

    def value(self, node, asm, gen, depth: int, element: bool = False):
        """1 つの値を読む。戻り値＝表示用の文字か、("obj", [(名前, 値)]) か ("list", [値])。保存されない型なら None。"""
        t = node[0]
        if t == "v":
            if gen and node[1] < len(gen):
                n2, a2 = gen[node[1]]
                return self.value(n2, a2, None, depth, element)
            raise Stop("型引数（ジェネリック）の中身が分からない")
        if t == "p":
            name = node[1]
            if name == "string":
                n = self.num("i")
                if not 0 <= n <= len(self.b):
                    raise Stop(f"文字列の長さ {n} が合わない＝型の推定が外れている")
                s = self.take(n).decode("utf-8", "replace")
                self.align()
                return '"' + (s if len(s) <= 80 else s[:80] + f"…（{len(s)} 字）") + '"'
            if name in ("object", "TypedReference", "IntPtr", "UIntPtr", "void"):
                return None
            v = self.num(_PRIM_FMT[name])
            if name in _SMALL and not element:
                self.align()
            if name == "float" or name == "double":
                return f"{v:.6g}"
            return str(v)
        coll = self.is_collection(node, asm)
        if coll is not None:
            if self.is_collection(coll, asm) is not None:
                return None                                     # 入れ子の配列・List は Unity が保存しない
            probe = self._probe(coll, asm, gen)
            if probe == "skip":
                return None
            n = self.num("i")
            if coll[0] == "p" and coll[1] in ("byte", "sbyte"):
                return self._bytes(n)
            if not 0 <= n <= MAX_ELEMS:
                raise Stop(f"配列の個数 {n} が合わない＝型の推定が外れている")
            items = [self.value(coll, asm, gen, depth + 1, element=True) for _ in range(n)]
            if coll[0] == "p" and coll[1] in _SMALL:
                self.align()
            return ("list", items)
        if t == "A":
            return None                                         # 多次元配列は保存されない
        if t == "x":
            raise Stop(f"{node[1]} の項目は道具が読まない")
        if t == "t":
            res = self.resolve(asm, node[1], node[2], gen)
            return self._typed(res, depth, [], node[3])
        if t == "g":
            res = self.resolve(asm, node[1][0], node[1][1], gen)
            args = [gen[a[1]] if a[0] == "v" and gen and a[1] < len(gen) else (a, asm) for a in node[2]]
            return self._typed(res, depth, args, False)
        raise Stop("知らない型の形")

    def _probe(self, node, asm, gen) -> str:
        """配列の要素の型が保存される物か（"skip" なら配列ごと保存されない）。"""
        if node[0] in ("t", "g"):
            ref = node[1:3] if node[0] == "t" else node[1]
            res = self.resolve(asm, ref[0], ref[1], gen)
            if res[0] == "def":
                k = self.kind(res[1], res[2])
                return "skip" if k == "skip" else "ok"
        if node[0] == "p" and node[1] in ("object", "IntPtr", "UIntPtr"):
            return "skip"
        return "ok"

    def _typed(self, res, depth: int, args: list, valuetype: bool):
        if res[0] == "name":
            name = res[1]
            if name in _SYSTEM_NAMES:
                return self.value(("p", _SYSTEM_NAMES[name]), None, None, depth)
            if name in _UNITY_STRUCTS:
                return self._ustruct(name)
            if name in _UNITY_SPECIAL:
                return self._special(name)
            if name in _UNITY_STOP:
                raise Stop(f"{name} は Unity が独自の形で書くので道具は読まない")
            if name.startswith("UnityEngine.") and not valuetype:
                return self._pptr()                               # UnityEngine の物（DLL が無くても参照とみなす）
            raise Stop(f"型 {name} の DLL が Managed に無い")
        if res[0] == "node":
            return self.value(res[1], res[2], None, depth)
        a, k = res[1], res[2]
        kind = self.kind(a, k)
        name = self.full_name(a, k)
        if kind == "prim":
            return self.value(("p", _SYSTEM_NAMES[name]), None, None, depth)
        if kind == "ustruct":
            return self._ustruct(name)
        if kind == "special":
            return self._special(name)
        if kind == "stop":
            raise Stop(f"{name} は Unity が独自の形で書くので道具は読まない")
        if kind == "pptr":
            return self._pptr()
        if kind == "enum":
            return str(self.num("i"))
        if kind == "skip":
            return None
        if kind == "unknown":
            raise Stop(f"{name} の基底クラスの DLL が見つからない")
        if depth >= MAX_DEPTH:
            raise Stop("入れ子が深すぎる")
        gen = args or None
        fields = self.fields_of(a, k)
        out = []
        for fname, fnode, fasm in fields:
            v = self.value(fnode, fasm, gen, depth + 1)
            if v is not None:
                out.append((fname, v))
        if a.row(2, k)["Flags"] & 0x20 == 0 and self._is_struct(a, k):
            self.align()
        return ("obj", out)

    def _bytes(self, n: int) -> str:
        """byte[]: 中身は並べず、位置・頭・埋め込まれた形式（MOC3・PNG など）を言う（取り出しに使う）。"""
        if not 0 <= n <= len(self.b) - self.p:
            raise Stop(f"byte[] の長さ {n:,} が残り {len(self.b) - self.p:,} バイトを越える＝型の推定が外れている")
        at = self.base_off + self.p
        blob = self.take(n)
        self.align()
        what = ""
        try:
            from . import bytesview as _bv
            m = _bv._SIG_RE.match(blob[:32])
            if m and _bv._sig_ok(_bv._SIG_NAME[m.group(0)], blob, 0):
                what = f"・★中身は {_bv._SIG_NAME[m.group(0)]}"
        except Exception:  # noqa: BLE001
            pass
        return f"byte[{n:,}]（オブジェクトの頭から +{at:,}〜+{at + n:,}）頭 {blob[:16].hex(' ')}{what}"

    def _is_struct(self, a, k) -> bool:
        ch = self.bases(a, k)
        return bool(ch) and ch[0][0] == "System.ValueType"

    def _special(self, name: str) -> str:
        """Unity が独自の形で書く 3 つ（版で形が変わる・AssetStudio の SerializedTypeHelper と同じ並び）。"""
        uv = self.uv
        if name == "UnityEngine.AnimationCurve":
            n = self.num("i")
            if not 0 <= n <= MAX_ELEMS:
                raise Stop(f"AnimationCurve のキーの数 {n} が合わない")
            ts, vs = [], []
            for _ in range(n):
                t, v = self.num("f"), self.num("f")
                self.take(8)                                   # inSlope・outSlope
                if uv[0] >= 2018:
                    self.take(12)                              # weightedMode・inWeight・outWeight
                ts.append(t)
                vs.append(v)
            pre, post = self.num("i"), self.num("i")
            if uv >= (5, 3):
                self.num("i")                                  # m_RotationOrder
            if not n:
                return "AnimationCurve（キー 0 個）"
            return (f"AnimationCurve（キー {n} 個・時間 {min(ts):.4g}〜{max(ts):.4g}・値 {min(vs):.4g}〜{max(vs):.4g}"
                    f"・前後の繰り返し {pre}/{post}）")
        if name == "UnityEngine.Gradient":
            cols = [tuple(self.num("f") for _ in range(4)) for _ in range(8)] if uv >= (5, 6) else \
                [(self.num("I"),) for _ in range(8)]
            self.take(32)                                      # ctime0..7・atime0..7（UInt16）
            if uv >= (5, 5):
                self.num("i")                                  # m_Mode
            nc, na = self.num("B"), self.num("B")
            self.align()
            show = ", ".join("(" + ", ".join(f"{x:.3g}" for x in c) + ")" for c in cols[:max(1, min(nc, 8))])
            return f"Gradient（色のキー {nc}・透明のキー {na}・{show}）"
        if name == "UnityEngine.GUIStyle":
            ln = self.num("i")
            if not 0 <= ln <= 4096:
                raise Stop("GUIStyle の名前の長さが合わない")
            nm = self.take(ln).decode("utf-8", "replace")
            self.align()
            for _ in range(8):                                  # GUIStyleState: 背景（PPtr）＋文字の色
                self._pptr()
                self.take(16)
            self.take(16 * 4)                                  # RectOffset × 4
            font = self._pptr()
            if uv[0] >= 4:
                size = self.num("i")
                self.take(8)                                   # m_FontStyle・m_Alignment
                self.take(2)
                self.align()                                   # m_WordWrap・m_RichText
                self.take(8 + 8 + 8)                           # m_TextClipping・m_ImagePosition・m_ContentOffset・幅・高さ
                self.take(2)
                self.align()                                   # m_StretchWidth・m_StretchHeight
            else:
                raise Stop("Unity 4 より前の GUIStyle は道具が読まない")
            return f"GUIStyle {nm!r}（文字の大きさ {size}・フォント {font}）"
        raise Stop(f"{name} は道具が読まない")

    def _ustruct(self, name: str) -> str:
        vals = []
        for fname, f in _UNITY_STRUCTS[name]:
            v = self.num(f)
            vals.append(f"{fname}={v:.6g}" if f == _F else f"{fname}={v}")
        return "(" + ", ".join(vals) + ")"

    def _pptr(self) -> str:
        fid = self.num("i")
        pid = self.num("q") if self.ver >= 14 else self.num("i")
        return self.pptr_label(fid, pid)

    # ── 表示 ──
    def render(self, name: str, v, depth: int) -> None:
        pad = "    " + "  " * depth
        if len(self.lines) >= MAX_LINES:
            return
        if isinstance(v, str):
            self.lines.append(f"{pad}{name} = {v}")
        elif isinstance(v, tuple) and v[0] == "list":
            items = v[1]
            if all(isinstance(x, str) for x in items):
                shown = ", ".join(items[:20])
                self.lines.append((f"{pad}{name} = [{len(items)} 件] {shown}" + (" …" if len(items) > 20 else "")).rstrip())
            else:
                self.lines.append(f"{pad}{name} = [{len(items)} 件]")
                for k, x in enumerate(items[:MAX_ITEMS_SHOWN]):
                    self.render(f"[{k}]", x, depth + 1)
                if len(items) > MAX_ITEMS_SHOWN:
                    self.lines.append(f"{pad}  …ほか {len(items) - MAX_ITEMS_SHOWN} 件")
        elif isinstance(v, tuple) and v[0] == "obj":
            flat = [(n, x) for n, x in v[1]]
            if all(isinstance(x, str) for _n, x in flat) and sum(len(x) for _n, x in flat) < 160:
                self.lines.append(f"{pad}{name} = {{" + ", ".join(f"{n}: {x}" for n, x in flat) + "}")
            else:
                self.lines.append(f"{pad}{name}:")
                for n, x in flat:
                    self.render(n, x, depth + 1)


def decode_behaviour(managed: str | None, asm_name: str, ns: str, cls: str, data: bytes, endian: str, ver: int,
                     pptr_label, asms: Assemblies | None = None, base_off: int = 0,
                     unity: tuple[int, int] = (2019, 4)) -> list[str]:
    """MonoBehaviour の項目（頭より後）を読んだ行。読めなければ理由の 1 行。"""
    if not managed:
        return ["  項目の値: ゲームの Managed フォルダ（<Game>_Data/Managed）が見つからないので読めません"
                "（IL2CPP なら Il2CppDumper の DummyDll を Managed という名前のフォルダに置けば読めます）。"]
    asms = asms or Assemblies(managed)
    a = asms.get(asm_name)
    if a is None:
        return [f"  項目の値: {asm_name} を Managed で開けません。"]
    k = a.lookup_type(ns, cls)
    if not k:                                                   # 型の転送（UnityEngine.dll → 各 Module の DLL）
        for ex in a.rows(0x27):
            if ex["Name"] == cls and ex["Namespace"] == ns and ex["Implementation"][0] == 1:
                ar = a.row(0x23, ex["Implementation"][1])
                a2 = asms.get(ar["Name"]) if ar else None
                if a2 is not None and a2.lookup_type(ns, cls):
                    a, k = a2, a2.lookup_type(ns, cls)
                    asm_name = ar["Name"]
                break
    if not k:
        return [f"  項目の値: {asm_name} に {ns + '.' if ns else ''}{cls} が見つかりません。"]
    d = Decoder(asms, data, endian, ver, pptr_label, base_off, unity)
    stop = None
    fields = []
    try:
        fields = d.fields_of(a, k)
    except Stop as e:
        stop = str(e)
    done = 0
    for fname, fnode, fasm in fields:
        start = d.p
        try:
            v = d.value(fnode, fasm, None, 0)
        except (Stop, struct.error) as e:
            stop = f"項目 {fname}（{d.type_label(fnode, fasm, None)}）で止まった: {e}"
            d.p = start
            break
        done += 1
        if v is not None:
            d.render(fname, v, 0)
    head = [f"  項目の値（{os.path.basename(managed.rstrip(os.sep))}/{asm_name.removesuffix('.dll')}.dll のクラスの宣言から"
            f"道具が読んだ・{done}/{len(fields)} 項目・{d.p:,}/{len(data):,} バイト）:"]
    tail = []
    if stop:
        tail.append(f"  ★{stop}（ここまでは読めた。続きは 16 進で）")
    elif d.p == len(data):
        tail.append("  （読んだ量と中身の大きさが一致＝項目の並びと型の推定が合っている）")
    else:
        tail.append(f"  ★読んだ量 {d.p:,} と中身の大きさ {len(data):,} が合わない＝Unity の版の違いか型の推定の外れ。値は参考に。")
    return head + d.lines + tail
