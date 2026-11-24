""".NET のアセンブリ（Assembly-CSharp.dll など）のメタデータを読む — 型・項目（フィールド）・メソッドの名前と型。2026-10-10。

オーナー「ゲーム解析系をさらに強化してください。ログを参考に」。走行記録（10-09〜10-10・オーナー機・Unity の Mono の
ゲーム）: エージェントは Assembly-CSharp.dll の中の文字列を自作の台本で拾って型の名前を探し、ilspycmd の場所を探して
何度も失敗し、MonoBehaviour の中身（型の木なし）を読むために「どの項目がどの順で並ぶか」を推測し続けた。
⇒ Read が .NET の DLL を開いたら、メタデータの表（ECMA-335 の #~・#Strings・#Blob）を読んで型の一覧を返し、
  find=<名前> で型の項目（宣言順・型・Unity が保存するかの推定）とメソッドの形を返す。逆コンパイルはしない（ilspycmd の役）。

Unity が保存する項目の推定（Unity の SerializationLogic の要点）: static・const・readonly・[NonSerialized] は保存しない。
public か [SerializeField]／[SerializeReference] の物を保存する。Dictionary・HashSet・デリゲートなどは保存しない。
基底クラスの項目が先に並ぶ。bool・byte・short・char とその配列の後ろは 4 バイトに揃える。
出典: ECMA-335 第 II 部（メタデータの表と署名）。
"""
from __future__ import annotations

import struct

MAX_FIND_TYPES = 6
MAX_MEMBER_HITS = 40
MAX_METHODS = 60

# 符号化された索引: (タグのビット数, 指す表)
_CODED = {
    "TypeDefOrRef": (2, [2, 1, 27]),
    "HasConstant": (2, [4, 8, 23]),
    "HasCustomAttribute": (5, [6, 4, 1, 2, 8, 9, 10, 0, 14, 23, 20, 17, 26, 27, 32, 35, 38, 39, 40, 42, 44, 43]),
    "HasFieldMarshal": (1, [4, 8]),
    "HasDeclSecurity": (2, [2, 6, 32]),
    "MemberRefParent": (3, [2, 1, 26, 6, 27]),
    "HasSemantics": (1, [20, 23]),
    "MethodDefOrRef": (1, [6, 10]),
    "MemberForwarded": (1, [4, 6]),
    "Implementation": (2, [38, 35, 39]),
    "CustomAttributeType": (3, [None, None, 6, 10, None]),
    "ResolutionScope": (2, [0, 26, 35, 1]),
    "TypeOrMethodDef": (1, [2, 6]),
}
C = lambda n: ("c", n)  # noqa: E731
T = lambda n: ("t", n)  # noqa: E731
# 表の列（ECMA-335 II.22）。0x2C より後ろ（PDB の表）は読まない。
_SCHEMA = {
    0x00: [("Generation", "u2"), ("Name", "s"), ("Mvid", "g"), ("EncId", "g"), ("EncBaseId", "g")],
    0x01: [("Scope", C("ResolutionScope")), ("Name", "s"), ("Namespace", "s")],
    0x02: [("Flags", "u4"), ("Name", "s"), ("Namespace", "s"), ("Extends", C("TypeDefOrRef")), ("FieldList", T(4)),
           ("MethodList", T(6))],
    0x03: [("Field", T(4))],
    0x04: [("Flags", "u2"), ("Name", "s"), ("Signature", "b")],
    0x05: [("Method", T(6))],
    0x06: [("RVA", "u4"), ("ImplFlags", "u2"), ("Flags", "u2"), ("Name", "s"), ("Signature", "b"), ("ParamList", T(8))],
    0x07: [("Param", T(8))],
    0x08: [("Flags", "u2"), ("Sequence", "u2"), ("Name", "s")],
    0x09: [("Class", T(2)), ("Interface", C("TypeDefOrRef"))],
    0x0A: [("Class", C("MemberRefParent")), ("Name", "s"), ("Signature", "b")],
    0x0B: [("Type", "u2"), ("Parent", C("HasConstant")), ("Value", "b")],
    0x0C: [("Parent", C("HasCustomAttribute")), ("Type", C("CustomAttributeType")), ("Value", "b")],
    0x0D: [("Parent", C("HasFieldMarshal")), ("NativeType", "b")],
    0x0E: [("Action", "u2"), ("Parent", C("HasDeclSecurity")), ("PermissionSet", "b")],
    0x0F: [("PackingSize", "u2"), ("ClassSize", "u4"), ("Parent", T(2))],
    0x10: [("Offset", "u4"), ("Field", T(4))],
    0x11: [("Signature", "b")],
    0x12: [("Parent", T(2)), ("EventList", T(0x14))],
    0x13: [("Event", T(0x14))],
    0x14: [("Flags", "u2"), ("Name", "s"), ("EventType", C("TypeDefOrRef"))],
    0x15: [("Parent", T(2)), ("PropertyList", T(0x17))],
    0x16: [("Property", T(0x17))],
    0x17: [("Flags", "u2"), ("Name", "s"), ("Type", "b")],
    0x18: [("Semantics", "u2"), ("Method", T(6)), ("Association", C("HasSemantics"))],
    0x19: [("Class", T(2)), ("Body", C("MethodDefOrRef")), ("Declaration", C("MethodDefOrRef"))],
    0x1A: [("Name", "s")],
    0x1B: [("Signature", "b")],
    0x1C: [("Flags", "u2"), ("Member", C("MemberForwarded")), ("ImportName", "s"), ("Scope", T(0x1A))],
    0x1D: [("RVA", "u4"), ("Field", T(4))],
    0x1E: [("Token", "u4"), ("FuncCode", "u4")],
    0x1F: [("Token", "u4")],
    0x20: [("HashAlgId", "u4"), ("Major", "u2"), ("Minor", "u2"), ("Build", "u2"), ("Rev", "u2"), ("Flags", "u4"),
           ("PublicKey", "b"), ("Name", "s"), ("Culture", "s")],
    0x21: [("Processor", "u4")],
    0x22: [("OSPlatformID", "u4"), ("OSMajor", "u4"), ("OSMinor", "u4")],
    0x23: [("Major", "u2"), ("Minor", "u2"), ("Build", "u2"), ("Rev", "u2"), ("Flags", "u4"), ("PublicKeyOrToken", "b"),
           ("Name", "s"), ("Culture", "s"), ("HashValue", "b")],
    0x24: [("Processor", "u4"), ("AssemblyRef", T(0x23))],
    0x25: [("OSPlatformId", "u4"), ("OSMajor", "u4"), ("OSMinor", "u4"), ("AssemblyRef", T(0x23))],
    0x26: [("Flags", "u4"), ("Name", "s"), ("HashValue", "b")],
    0x27: [("Flags", "u4"), ("TypeDefId", "u4"), ("Name", "s"), ("Namespace", "s"), ("Implementation", C("Implementation"))],
    0x28: [("Offset", "u4"), ("Flags", "u4"), ("Name", "s"), ("Implementation", C("Implementation"))],
    0x29: [("Nested", T(2)), ("Enclosing", T(2))],
    0x2A: [("Number", "u2"), ("Flags", "u2"), ("Owner", C("TypeOrMethodDef")), ("Name", "s")],
    0x2B: [("Method", C("MethodDefOrRef")), ("Instantiation", "b")],
    0x2C: [("Owner", T(0x2A)), ("Constraint", C("TypeDefOrRef"))],
}

_PRIM = {0x01: "void", 0x02: "bool", 0x03: "char", 0x04: "sbyte", 0x05: "byte", 0x06: "short", 0x07: "ushort",
         0x08: "int", 0x09: "uint", 0x0A: "long", 0x0B: "ulong", 0x0C: "float", 0x0D: "double", 0x0E: "string",
         0x16: "TypedReference", 0x18: "IntPtr", 0x19: "UIntPtr", 0x1C: "object"}
_ALIAS = {"System.Boolean": "bool", "System.Int32": "int", "System.Single": "float", "System.String": "string",
          "System.Object": "object", "System.Byte": "byte", "System.Int64": "long", "System.Double": "double",
          "System.Void": "void", "System.Char": "char", "System.Int16": "short", "System.UInt32": "uint"}
# Unity が保存しない型（型の名前の頭）。推定なので「（推定）」と書く。
_NOT_SERIALIZED = ("Dictionary<", "HashSet<", "Queue<", "Stack<", "LinkedList<", "SortedDictionary<", "Action",
                   "Func<", "Task", "IEnumerator", "Coroutine", "Delegate", "EventHandler", "UnityEvent<",
                   "Nullable<", "ConcurrentDictionary<", "IReadOnly", "IList<", "IDictionary<", "IEnumerable<")
_SMALL = {"bool", "byte", "sbyte", "short", "ushort", "char"}


class Bad(Exception):
    pass


def _cu(b: bytes, p: int) -> tuple[int, int]:
    """圧縮された符号なし整数（ECMA-335 II.23.2）。"""
    x = b[p]
    if x & 0x80 == 0:
        return x, p + 1
    if x & 0xC0 == 0x80:
        return ((x & 0x3F) << 8) | b[p + 1], p + 2
    return ((x & 0x1F) << 24) | (b[p + 1] << 16) | (b[p + 2] << 8) | b[p + 3], p + 4


class Assembly:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self._parse_pe()
        self._parse_tables()
        self._nested = {r["Nested"]: r["Enclosing"] for r in self.rows(0x29)}
        self._gp: dict[tuple[int, int], list[str]] = {}
        for r in self.rows(0x2A):
            tag, idx = r["Owner"]
            self._gp.setdefault((tag, idx), []).append(r["Name"])
        self._attrs = self._field_attrs()

    # ── PE とメタデータの根 ──
    def _rva(self, rva: int) -> int:
        for va, vsize, raw, rsize in self.sections:
            if va <= rva < va + max(vsize, rsize):
                return raw + (rva - va)
        raise Bad(f"RVA 0x{rva:x} がどのセクションにもありません")

    def _parse_pe(self) -> None:
        d = self.d
        if d[:2] != b"MZ":
            raise Bad("PE（MZ）ではありません")
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        if d[pe:pe + 4] != b"PE\x00\x00":
            raise Bad("PE の印がありません")
        nsec, optsize = struct.unpack_from("<H", d, pe + 6)[0], struct.unpack_from("<H", d, pe + 20)[0]
        opt = pe + 24
        plus = struct.unpack_from("<H", d, opt)[0] == 0x20B
        dd = opt + (112 if plus else 96)
        cli_rva, cli_size = struct.unpack_from("<II", d, dd + 14 * 8)
        if not cli_rva:
            raise Bad(".NET のヘッダ（CLI）がありません＝ネイティブの DLL（IL2CPP なら GameAssembly.dll と global-metadata.dat）")
        sec = opt + optsize
        self.sections = []
        for k in range(nsec):
            o = sec + k * 40
            vsize, va, rsize, raw = struct.unpack_from("<IIII", d, o + 8)
            self.sections.append((va, vsize, raw, rsize))
        cli = self._rva(cli_rva)
        md_rva, md_size = struct.unpack_from("<II", d, cli + 8)
        root = self._rva(md_rva)
        if d[root:root + 4] != b"BSJB":
            raise Bad("メタデータの印（BSJB）がありません")
        vlen = struct.unpack_from("<I", d, root + 12)[0]
        self.runtime = d[root + 16:root + 16 + vlen].split(b"\x00")[0].decode("ascii", "replace")
        p = root + 16 + vlen
        nstreams = struct.unpack_from("<H", d, p + 2)[0]
        p += 4
        self.streams: dict[str, tuple[int, int]] = {}
        for _ in range(nstreams):
            off, size = struct.unpack_from("<II", d, p)
            p += 8
            e = d.index(b"\x00", p)
            name = d[p:e].decode("ascii", "replace")
            p = (e + 1 + 3) & ~3
            self.streams[name] = (root + off, size)
        if "#~" not in self.streams and "#-" not in self.streams:
            raise Bad("メタデータの表（#~）がありません（難読化・加工の可能性）")
        self._str = self.streams.get("#Strings", (0, 0))
        self._blob = self.streams.get("#Blob", (0, 0))

    def string(self, i: int) -> str:
        off, size = self._str
        if not 0 <= i < size:
            return ""
        e = self.d.find(b"\x00", off + i, off + size)
        return self.d[off + i:e if e >= 0 else off + size].decode("utf-8", "replace")

    def blob(self, i: int) -> bytes:
        off, size = self._blob
        if not 0 <= i < size:
            return b""
        n, p = _cu(self.d, off + i)
        return self.d[p:p + n]

    # ── 表 ──
    def _parse_tables(self) -> None:
        d = self.d
        off, _size = self.streams.get("#~") or self.streams["#-"]
        heap = d[off + 6]
        valid = struct.unpack_from("<Q", d, off + 8)[0]
        p = off + 24
        self.nrows = [0] * 64
        for t in range(64):
            if valid >> t & 1:
                self.nrows[t] = struct.unpack_from("<I", d, p)[0]
                p += 4
        if heap & 0x40:
            p += 4
        ssz = 4 if heap & 1 else 2
        gsz = 4 if heap & 2 else 2
        bsz = 4 if heap & 4 else 2

        def width(kind) -> int:
            if kind == "u2":
                return 2
            if kind == "u4":
                return 4
            if kind == "s":
                return ssz
            if kind == "g":
                return gsz
            if kind == "b":
                return bsz
            if kind[0] == "t":
                return 2 if self.nrows[kind[1]] < 65536 else 4
            bits, tabs = _CODED[kind[1]]
            mx = max((self.nrows[t] for t in tabs if t is not None), default=0)
            return 2 if mx < (1 << (16 - bits)) else 4

        self.tables: dict[int, tuple[int, int, list]] = {}
        for t in range(0x2D):
            if not self.nrows[t]:
                continue
            cols = _SCHEMA[t]
            ws = [width(k) for _n, k in cols]
            rsize = sum(ws)
            self.tables[t] = (p, rsize, list(zip(cols, ws)))
            p += rsize * self.nrows[t]
        self._cache: dict[int, list[dict]] = {}

    def rows(self, t: int) -> list[dict]:
        if t in self._cache:
            return self._cache[t]
        out: list[dict] = []
        if t in self.tables:
            p, rsize, cols = self.tables[t]
            fmt = "<" + "".join("H" if w == 2 else "I" for _c, w in cols)
            for vals in struct.iter_unpack(fmt, self.d[p:p + rsize * self.nrows[t]]):
                r = {}
                for ((name, kind), _w), v in zip(cols, vals):
                    if kind == "s":
                        v = self.string(v)
                    elif isinstance(kind, tuple) and kind[0] == "c":
                        bits, tabs = _CODED[kind[1]]
                        v = (v & ((1 << bits) - 1), v >> bits)
                    r[name] = v
                out.append(r)
        self._cache[t] = out
        return out

    def row(self, t: int, i: int) -> dict | None:
        rs = self.rows(t)
        return rs[i - 1] if 1 <= i <= len(rs) else None

    # ── 名前 ──
    def typedef_name(self, i: int, full: bool = True, generic: bool = True) -> str:
        r = self.row(2, i)
        if r is None:
            return f"?typedef{i}"
        name = r["Name"].split("`")[0]
        gp = self._gp.get((0, i))
        if gp and generic and i not in self._nested:
            name += "<" + ", ".join(gp) + ">"
        if i in self._nested:
            return self.typedef_name(self._nested[i], full, generic) + "." + name
        return (r["Namespace"] + "." + name) if full and r["Namespace"] else name

    def typeref_name(self, i: int, full: bool = True) -> str:
        r = self.row(1, i)
        if r is None:
            return f"?typeref{i}"
        name = r["Name"].split("`")[0]
        tag, idx = r["Scope"]
        if tag == 3:                                    # 入れ子の型の参照
            return self.typeref_name(idx, full) + "." + name
        n = (r["Namespace"] + "." + name) if r["Namespace"] else name
        return _ALIAS.get(n, n if full else name)

    def tdor(self, tag: int, idx: int, full: bool = False, generic: bool = True) -> str:
        if tag == 0:
            return self.typedef_name(idx, full, generic)
        if tag == 1:
            return self.typeref_name(idx, full)
        if tag == 2:
            r = self.row(27, idx)
            if r is not None:
                try:
                    return self.sig_type(self.blob(r["Signature"]), 0)[0]
                except (IndexError, Bad):
                    pass
        return "?"

    def sig_type(self, b: bytes, p: int, gen: list[str] | None = None) -> tuple[str, int]:
        et = b[p]
        p += 1
        if et in _PRIM:
            return _PRIM[et], p
        if et in (0x1F, 0x20):                          # 修飾子は読み飛ばす
            _v, p = _cu(b, p)
            return self.sig_type(b, p, gen)
        if et in (0x45, 0x41):
            return self.sig_type(b, p, gen)
        if et == 0x0F:
            t, p = self.sig_type(b, p, gen)
            return t + "*", p
        if et == 0x10:
            t, p = self.sig_type(b, p, gen)
            return "ref " + t, p
        if et in (0x11, 0x12):
            v, p = _cu(b, p)
            return self.tdor(v & 3, v >> 2), p
        if et in (0x13, 0x1E):
            n, p = _cu(b, p)
            if et == 0x13 and gen and n < len(gen):
                return gen[n], p
            return ("T" if et == 0x13 else "M") + str(n), p
        if et == 0x1D:
            t, p = self.sig_type(b, p, gen)
            return t + "[]", p
        if et == 0x14:
            t, p = self.sig_type(b, p, gen)
            rank, p = _cu(b, p)
            ns, p = _cu(b, p)
            for _ in range(ns):
                _v, p = _cu(b, p)
            nl, p = _cu(b, p)
            for _ in range(nl):
                _v, p = _cu(b, p)
            return t + "[" + "," * max(0, rank - 1) + "]", p
        if et == 0x15:
            p += 1                                      # CLASS / VALUETYPE
            v, p = _cu(b, p)
            base = self.tdor(v & 3, v >> 2, generic=False)
            n, p = _cu(b, p)
            args = []
            for _ in range(n):
                a, p = self.sig_type(b, p, gen)
                args.append(a)
            return f"{base}<{', '.join(args)}>", p
        if et == 0x1B:
            return "関数ポインタ", len(b)
        raise Bad(f"知らない型の印 0x{et:02x}")

    def sig_node(self, b: bytes, p: int) -> tuple[tuple, int]:
        """型の印を組み立てた形で（oc/monodecode.py が使う）。
        ("p", 名前)・("t", タグ, 行, 値型か)・("a", 要素)・("A", 要素, 次元)・("g", (タグ, 行), [引数])・("v", n)・("m", n)・("x", 文字)"""
        et = b[p]
        p += 1
        if et in _PRIM:
            return ("p", _PRIM[et]), p
        if et in (0x1F, 0x20):
            _v, p = _cu(b, p)
            return self.sig_node(b, p)
        if et in (0x45, 0x41):
            return self.sig_node(b, p)
        if et in (0x11, 0x12):
            v, p = _cu(b, p)
            return ("t", v & 3, v >> 2, et == 0x11), p
        if et in (0x13, 0x1E):
            n, p = _cu(b, p)
            return ("v" if et == 0x13 else "m", n), p
        if et == 0x1D:
            e, p = self.sig_node(b, p)
            return ("a", e), p
        if et == 0x14:
            e, p = self.sig_node(b, p)
            rank, p = _cu(b, p)
            ns, p = _cu(b, p)
            for _ in range(ns):
                _v, p = _cu(b, p)
            nl, p = _cu(b, p)
            for _ in range(nl):
                _v, p = _cu(b, p)
            return ("A", e, rank), p
        if et == 0x15:
            p += 1
            v, p = _cu(b, p)
            n, p = _cu(b, p)
            args = []
            for _ in range(n):
                a, p = self.sig_node(b, p)
                args.append(a)
            return ("g", (v & 3, v >> 2), args), p
        if et in (0x0F, 0x10):
            _e, p = self.sig_node(b, p)
            return ("x", "pointer"), p
        return ("x", f"0x{et:02x}"), len(b)

    def field_node(self, sig: bytes) -> tuple:
        if not sig or sig[0] != 0x06:
            return ("x", "?")
        try:
            return self.sig_node(sig, 1)[0]
        except (IndexError, Bad):
            return ("x", "?")

    def lookup_type(self, ns: str, name: str) -> int:
        """名前空間と名前（`1 つき）から TypeDef の行（入れ子でない型）。無ければ 0。"""
        if not hasattr(self, "_by_name"):
            self._by_name = {}
            for k, r in enumerate(self.rows(2), start=1):
                if k not in self._nested:
                    self._by_name.setdefault((r["Namespace"], r["Name"]), k)
        return self._by_name.get((ns, name), 0)

    def nested_type(self, outer: int, name: str) -> int:
        for inner, enc in self._nested.items():
            if enc == outer and self.row(2, inner)["Name"] == name:
                return inner
        return 0

    def field_type(self, sig: bytes, gen: list[str] | None = None) -> str:
        if not sig or sig[0] != 0x06:
            return "?"
        try:
            return self.sig_type(sig, 1, gen)[0]
        except (IndexError, Bad):
            return "?"

    def method_sig(self, sig: bytes, gen: list[str] | None = None) -> tuple[str, list[str]]:
        try:
            p = 0
            cc = sig[p]
            p += 1
            if cc & 0x10:
                _g, p = _cu(sig, p)
            n, p = _cu(sig, p)
            ret, p = self.sig_type(sig, p, gen)
            params = []
            for _ in range(n):
                t, p = self.sig_type(sig, p, gen)
                params.append(t)
            return ret, params
        except (IndexError, Bad):
            return "?", []

    # ── 型ごとの範囲 ──
    def _span(self, t_list: int, ptr_table: int, target: int, i: int, col: str) -> list[int]:
        types = self.rows(2)
        start = types[i - 1][col]
        end = types[i][col] if i < len(types) else len(self.rows(target)) + 1
        idx = list(range(start, end))
        if self.nrows[ptr_table]:                      # #- の間接の表
            ptr = self.rows(ptr_table)
            key = "Field" if target == 4 else "Method"
            idx = [ptr[k - 1][key] for k in idx if 1 <= k <= len(ptr)]
        return idx

    def fields_of(self, i: int) -> list[int]:
        return self._span(2, 3, 4, i, "FieldList")

    def methods_of(self, i: int) -> list[int]:
        return self._span(2, 5, 6, i, "MethodList")

    def _field_attrs(self) -> dict[int, set[str]]:
        """項目に付いた属性の名前（SerializeField・SerializeReference・NonSerialized など）。"""
        out: dict[int, set[str]] = {}
        for r in self.rows(0x0C):
            tag, idx = r["Parent"]
            if tag != 1:
                continue
            ttag, tidx = r["Type"]
            name = ""
            if ttag == 3:
                mr = self.row(10, tidx)
                if mr:
                    ptag, pidx = mr["Class"]
                    name = self.typeref_name(pidx, False) if ptag == 1 else (self.typedef_name(pidx, False) if ptag == 0 else "")
            elif ttag == 2:                              # 同じアセンブリで定義された属性（コンストラクタ → 型）
                name = self.typedef_name(self.method_owner(tidx), False)
            if name:
                out.setdefault(idx, set()).add(name.rsplit(".", 1)[-1].replace("Attribute", ""))
        return out

    def method_owner(self, m: int) -> int:
        if not hasattr(self, "_mowner"):
            self._mowner = {}
            for ti in range(1, len(self.rows(2)) + 1):
                for mi in self.methods_of(ti):
                    self._mowner[mi] = ti
        return self._mowner.get(m, 0)

    def assembly_name(self) -> str:
        a = self.rows(0x20)
        if not a:
            return "（アセンブリの記録なし＝モジュール）"
        r = a[0]
        return f"{r['Name']} {r['Major']}.{r['Minor']}.{r['Build']}.{r['Rev']}"

    def base_chain(self, i: int) -> list[str]:
        chain = []
        seen = set()
        cur = i
        while len(chain) < 12:
            r = self.row(2, cur)
            if r is None:
                break
            tag, idx = r["Extends"]
            if tag == 0 and idx == 0:
                break
            name = self.tdor(tag, idx, full=False)
            chain.append(name)
            if tag != 0 or idx in seen:
                break
            seen.add(idx)
            cur = idx
        return chain


# ── 表示 ─────────────────────────────────────────────────────────────────

_ACCESS = {1: "private", 2: "private protected", 3: "internal", 4: "protected", 5: "protected internal", 6: "public"}


_PLAIN_BASES = {"object", "Object", "ValueType", "Enum", "Attribute", "Exception", "MulticastDelegate", "Delegate"}
_UNITY_BASES = {"MonoBehaviour", "ScriptableObject", "StateMachineBehaviour", "Behaviour"}


def _container(a: Assembly, i: int) -> str:
    """Unity がこの型の項目を保存するか: "yes"（MonoBehaviour などの子・[Serializable]）・"maybe"（基底が別の
    アセンブリで分からない）・"no"（ふつうのクラス・[Serializable] の無い構造体）。"""
    r = a.row(2, i)
    chain = a.base_chain(i)
    if r["Flags"] & 0x2000 or set(chain) & _UNITY_BASES:
        return "yes"
    if chain and chain[-1] not in _PLAIN_BASES and not chain[-1].startswith("System."):
        return "maybe"
    return "no"


def _field_line(a: Assembly, fi: int, gen: list[str] | None, container: str = "yes") -> tuple[str, bool]:
    r = a.row(4, fi)
    if r is None:
        return "?", False
    fl = r["Flags"]
    t = a.field_type(a.blob(r["Signature"]), gen)
    attrs = a._attrs.get(fi, set())
    static, literal, readonly, nonser = fl & 0x10, fl & 0x40, fl & 0x20, fl & 0x80
    public = (fl & 7) == 6
    marked = "SerializeField" in attrs or "SerializeReference" in attrs
    keep = (not static and not literal and not readonly and not nonser and (public or marked)
            and not t.startswith(_NOT_SERIALIZED))
    mods = [_ACCESS.get(fl & 7, "")]
    if literal:
        mods.append("const")
    elif static:
        mods.append("static")
    if readonly:
        mods.append("readonly")
    if nonser:
        mods.append("[NonSerialized]")
    for at in sorted(attrs & {"SerializeField", "SerializeReference", "HideInInspector", "FormerlySerializedAs"}):
        mods.insert(0, f"[{at}]")
    if container == "no":
        keep = False
    pad = "（後ろを 4 バイトに揃える）" if keep and (t in _SMALL or t.rstrip("[]") in _SMALL and t.endswith("[]")) else ""
    why = ""
    if not keep and not static and not literal and t.startswith(_NOT_SERIALIZED):
        why = "（Unity はこの型を保存しない）"
    mark = (" " if not keep else "☆" if container == "maybe" else "★")
    return f"{mark} {' '.join(m for m in mods if m)} {t} {r['Name']}{pad}{why}", bool(keep)


def _type_block(a: Assembly, i: int) -> list[str]:
    r = a.row(2, i)
    fl = r["Flags"]
    kind = "interface" if fl & 0x20 else "class"
    chain = a.base_chain(i)
    if chain and chain[0] in ("ValueType", "System.ValueType"):
        kind = "struct"
    elif chain and chain[0] in ("Enum", "System.Enum"):
        kind = "enum"
    gen = a._gp.get((0, i))
    head = (f"{kind} {a.typedef_name(i)}" + (f" : {' → '.join(chain)}" if chain and kind == "class" else "")
            + ("・[Serializable]" if fl & 0x2000 else "") + ("・abstract" if fl & 0x80 else "") + ("・sealed" if fl & 0x100 else ""))
    out = [head]
    fis = a.fields_of(i)
    if kind == "enum":
        names = [a.row(4, f)["Name"] for f in fis if a.row(4, f) and a.row(4, f)["Name"] != "value__"]
        out.append(f"  値 {len(names)} 個: " + ", ".join(names[:60]) + (" …" if len(names) > 60 else ""))
        return out
    if fis:
        cont = _container(a, i)
        legend = {"yes": "★＝Unity が保存する推定。基底クラスの項目はその前に並ぶ",
                  "maybe": "☆＝基底クラスが別のアセンブリ。それが MonoBehaviour / ScriptableObject の子なら Unity が保存する",
                  "no": "MonoBehaviour などの子でも [Serializable] でもないので Unity は保存しない"}[cont]
        out.append(f"  項目 {len(fis)} 個（宣言順・{legend}）:")
        for f in fis[:80]:
            out.append("    " + _field_line(a, f, gen, cont)[0])
        if len(fis) > 80:
            out.append(f"    …ほか {len(fis) - 80} 個")
    mis = a.methods_of(i)
    if mis:
        ms = []
        for m in mis[:MAX_METHODS]:
            mr = a.row(6, m)
            if mr is None:
                continue
            ret, params = a.method_sig(a.blob(mr["Signature"]), gen)
            pnames = _param_names(a, m, len(params))
            args = ", ".join(f"{t} {n}".strip() for t, n in zip(params, pnames))
            ms.append(f"{mr['Name']}({args})" + ("" if ret == "void" else f": {ret}"))
        out.append(f"  メソッド {len(mis)} 個: " + " / ".join(ms) + (" …" if len(mis) > MAX_METHODS else ""))
    return out


def _param_names(a: Assembly, m: int, n: int) -> list[str]:
    mr = a.row(6, m)
    rows = a.rows(8)
    meths = a.rows(6)
    start = mr["ParamList"]
    end = meths[m]["ParamList"] if m < len(meths) else len(rows) + 1
    names = [""] * n
    for k in range(start, min(end, len(rows) + 1)):
        pr = rows[k - 1]
        if 1 <= pr["Sequence"] <= n:
            names[pr["Sequence"] - 1] = pr["Name"]
    return names


def summary(path: str) -> list[str]:
    """Read が .NET の DLL を開いたときに pe() の後ろへ足す行。"""
    try:
        a = Assembly(_load(path))
    except (Bad, struct.error, IndexError, ValueError) as e:
        return [f"  .NET のメタデータを読めません: {e}"]
    types = a.rows(2)
    refs = [r["Name"] for r in a.rows(0x23)]
    out = [f"  .NET のメタデータ: アセンブリ {a.assembly_name()}・ランタイム {a.runtime}・型 {len(types) - 1:,}・"
           f"メソッド {len(a.rows(6)):,}・項目 {len(a.rows(4)):,}"]
    if refs:
        out.append("  参照するアセンブリ: " + ", ".join(refs[:16]) + (f" …ほか {len(refs) - 16}" if len(refs) > 16 else ""))
    ns: dict[str, int] = {}
    for k, r in enumerate(types[1:], start=2):
        if k in a._nested:
            continue
        ns[r["Namespace"] or "（なし）"] = ns.get(r["Namespace"] or "（なし）", 0) + 1
    out.append("  名前空間ごとの型の数: " + "・".join(f"{k} {v}" for k, v in sorted(ns.items(), key=lambda x: -x[1])[:12]))
    big = []
    for k in range(2, len(types) + 1):
        if k in a._nested:
            continue
        nf, nm = len(a.fields_of(k)), len(a.methods_of(k))
        chain = a.base_chain(k)
        big.append((nf + nm, k, nf, nm, chain[-1] if chain else ""))
    big.sort(reverse=True)
    out.append("  大きい型（項目＋メソッドの数）: " + "・".join(
        f"{a.typedef_name(k, False)}（{b or '—'}・項目 {nf}・メソッド {nm}）" for _s, k, nf, nm, b in big[:12]))
    unity = [a.typedef_name(k, False) for k in range(2, len(types) + 1)
             if set(a.base_chain(k)) & {"MonoBehaviour", "ScriptableObject"}]
    if unity:
        out.append(f"  MonoBehaviour / ScriptableObject の子 {len(unity)} 個: " + ", ".join(unity[:30])
                   + (" …" if len(unity) > 30 else ""))
    out.append("  → 型の項目（宣言順・型・Unity が保存する推定）とメソッドの形: Read に find=<クラス名か項目名>。"
               "ソースに戻すなら ilspycmd（`ilspycmd <dll> -o <出力フォルダ>`）。")
    return out


def find(path: str, text: str) -> list[str]:
    a = Assembly(_load(path))
    qs = [q.strip().lower() for q in text.replace("｜", "|").split("|") if q.strip()]
    if not qs:
        return ["find に探す名前を入れてください（クラス名・項目名・メソッド名。a|b で複数）。"]
    types = a.rows(2)
    exact, part = [], []
    for k in range(2, len(types) + 1):
        nm = a.typedef_name(k).lower()
        short = a.typedef_name(k, False).lower()
        if any(q == short.split("<")[0] or q == nm.split("<")[0] for q in qs):
            exact.append(k)
        elif any(q in nm for q in qs):
            part.append(k)
    out = [f".NET の型を {text!r} で探した（{a.assembly_name()}・型 {len(types) - 1:,} 個）"]
    shown = (exact + part)[:MAX_FIND_TYPES]
    for k in shown:
        out += _type_block(a, k)
    rest = (exact + part)[MAX_FIND_TYPES:]
    if rest:
        out.append(f"ほかに名前が当たった型 {len(rest)} 個: " + ", ".join(a.typedef_name(k) for k in rest[:40])
                   + (" …" if len(rest) > 40 else ""))
    hits = []
    for k in range(2, len(types) + 1):
        if k in shown:
            continue
        for f in a.fields_of(k):
            r = a.row(4, f)
            if r and any(q in r["Name"].lower() for q in qs):
                hits.append(f"  項目 {a.typedef_name(k, False)}.{r['Name']}（{a.field_type(a.blob(r['Signature']), a._gp.get((0, k)))}）")
        for m in a.methods_of(k):
            r = a.row(6, m)
            if r and any(q in r["Name"].lower() for q in qs):
                ret, params = a.method_sig(a.blob(r["Signature"]), a._gp.get((0, k)))
                hits.append(f"  メソッド {a.typedef_name(k, False)}.{r['Name']}({', '.join(params)})"
                            + ("" if ret == "void" else f": {ret}"))
        if len(hits) > MAX_MEMBER_HITS:
            break
    if hits:
        out.append("項目・メソッドの名前に当たったもの:")
        out += hits[:MAX_MEMBER_HITS] + (["  …（打ち切り）"] if len(hits) > MAX_MEMBER_HITS else [])
    if not shown and not hits:
        out.append("当たりはありません（型・項目・メソッドの名前を大文字小文字を区別せず部分一致で見た）。")
    return out


def is_dotnet(head: bytes) -> bool:
    if head[:2] != b"MZ" or len(head) < 0x40:
        return False
    try:
        pe = struct.unpack_from("<I", head, 0x3C)[0]
        if head[pe:pe + 4] != b"PE\x00\x00":
            return False
        opt = pe + 24
        plus = struct.unpack_from("<H", head, opt)[0] == 0x20B
        dd = opt + (112 if plus else 96)
        return struct.unpack_from("<I", head, dd + 14 * 8)[0] != 0
    except struct.error:
        return False


def _load(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(256 * 1024 * 1024)
