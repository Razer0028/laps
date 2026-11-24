""".NET の BinaryFormatter（MS-NRBF）で書かれたデータを読む — セーブデータ・設定の中身を木にする。2026-10-10。

オーナー「ゲーム解析系をさらに強化してください。ログを参考に」。走行記録（10-10・オーナー機）: ゲームのセーブデータの
中の Base64 の項目が BinaryFormatter の塊だった。エージェントは Read の map で「構造のあるデータ」としか分からず、
pythonnet を入れて .NET で読もうとして import で失敗し、書き戻しの小さなサーバまで作った（1 走行・約 25 分）。
頭の 17 バイト（00 01 00 00 00 FF FF FF FF 01 00 00 00 00 00 00 00）で分かる形なので、道具が読んで返す。
読むだけ（書き戻しは .NET 側で）。BinaryFormatter の塊を .NET で逆シリアル化するのは危険（任意のコードが走りうる）
なので、中身を見るだけならこちらが安全でもある。
出典: [MS-NRBF] .NET Remoting: Binary Format Data Structure（Microsoft の公開仕様）。
"""
from __future__ import annotations

import datetime as _dt
import re
import struct
import time

MAX_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 500_000
TIME_BUDGET = 20.0
MAX_DEPTH = 7
MAX_ITEMS = 24
MAX_LINES = 400

HEADER = b"\x00\x01\x00\x00\x00\xff\xff\xff\xff\x01\x00\x00\x00\x00\x00\x00\x00"

_PRIM_NAMES = {1: "Boolean", 2: "Byte", 3: "Char", 5: "Decimal", 6: "Double", 7: "Int16", 8: "Int32", 9: "Int64",
               10: "SByte", 11: "Single", 12: "TimeSpan", 13: "DateTime", 14: "UInt16", 15: "UInt32", 16: "UInt64",
               17: "Null", 18: "String"}
_FMT = {2: "<B", 6: "<d", 7: "<h", 8: "<i", 9: "<q", 10: "<b", 11: "<f", 12: "<q", 14: "<H", 15: "<I", 16: "<Q"}


def looks_nrbf(head: bytes) -> bool:
    """頭が SerializedStreamHeader（記録 0・版 1.0）か。"""
    if len(head) < 17 or head[0] != 0:
        return False
    _root, hid, major, minor = struct.unpack_from("<iiii", head, 1)
    return hid == -1 and major == 1 and minor == 0      # ★HeaderId -1 まで見る（TrueType も 00 01 00 00 で始まる）


class Bad(Exception):
    pass


class _Ref:
    __slots__ = ("id",)

    def __init__(self, i: int) -> None:
        self.id = i


class _Nulls:
    __slots__ = ("n",)

    def __init__(self, n: int) -> None:
        self.n = n


class _Obj:
    __slots__ = ("id", "cls", "lib", "members")

    def __init__(self, i: int, cls: str, lib: int | None) -> None:
        self.id, self.cls, self.lib, self.members = i, cls, lib, []


class _Arr:
    __slots__ = ("id", "elem", "items", "dims")

    def __init__(self, i: int, elem: str, dims: list[int]) -> None:
        self.id, self.elem, self.dims, self.items = i, elem, dims, []


class Parser:
    def __init__(self, b: bytes, pos: int = 0) -> None:
        self.b, self.p = b, pos
        self.objs: dict[int, object] = {}
        self.meta: dict[int, tuple] = {}          # 型の情報（ClassWithId が使い回す）
        self.libs: dict[int, str] = {}
        self.records = 0
        self.deadline = time.time() + TIME_BUDGET
        self.root = None

    # ── 低い層 ──
    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.b):
            raise Bad(f"{self.p:,} バイト目で途切れています")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def u8(self) -> int:
        return self.take(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def lps(self) -> str:
        n = shift = 0
        for _ in range(5):
            c = self.u8()
            n |= (c & 0x7F) << shift
            shift += 7
            if not c & 0x80:
                break
        return self.take(n).decode("utf-8", "replace")

    def prim(self, code: int):
        if code == 1:
            return bool(self.u8())
        if code == 3:
            c = self.b[self.p] if self.p < len(self.b) else 0
            n = 1 if c < 0x80 else 2 if c < 0xE0 else 3 if c < 0xF0 else 4
            return self.take(n).decode("utf-8", "replace")
        if code in (5, 18):
            return self.lps()
        if code == 13:
            v = struct.unpack("<Q", self.take(8))[0]
            ticks = v & 0x3FFFFFFFFFFFFFFF
            try:
                return _dt.datetime(1, 1, 1) + _dt.timedelta(microseconds=ticks // 10)
            except OverflowError:
                return f"DateTime(ticks={ticks})"
        if code == 17:
            return None
        f = _FMT.get(code)
        if not f:
            raise Bad(f"知らない基本型 {code}")
        v = struct.unpack(f, self.take(struct.calcsize(f)))[0]
        if code == 12:
            return _dt.timedelta(microseconds=v // 10)
        return v

    # ── 記録 ──
    def parse(self):
        if self.u8() != 0:
            raise Bad("頭の記録（SerializedStreamHeader）がありません")
        self.root, _hid, _maj, _min = struct.unpack("<iiii", self.take(16))
        while True:
            if self.p >= len(self.b):
                raise Bad("終わりの記録（MessageEnd）の前にデータが尽きました")
            if self.b[self.p] == 11:
                self.p += 1
                return
            self.value()

    def value(self):
        """記録を 1 つ読み、値（基本型・文字列・_Obj・_Arr・_Ref・None・_Nulls）を返す。"""
        while True:
            self.records += 1
            if self.records > MAX_RECORDS or (self.records % 4096 == 0 and time.time() > self.deadline):
                raise Bad("記録が多すぎるか時間の上限に達しました（ここまで）")
            t = self.u8()
            if t == 12:                                  # BinaryLibrary（次の記録の前に置かれる）
                lid = self.i32()
                self.libs[lid] = self.lps()
                continue
            break
        if t == 6:
            i = self.i32()
            s = self.lps()
            self.objs[i] = s
            return s
        if t == 9:
            return _Ref(self.i32())
        if t == 10:
            return None
        if t == 13:
            return _Nulls(self.u8())
        if t == 14:
            return _Nulls(self.i32())
        if t == 8:
            return self.prim(self.u8())
        if t in (1, 2, 3, 4, 5):
            return self.klass(t)
        if t in (7, 15, 16, 17):
            return self.array(t)
        raise Bad(f"知らない記録の種類 {t}（{self.p - 1:,} バイト目）")

    def _typeinfo(self, n: int) -> tuple[list[int], list]:
        kinds = [self.u8() for _ in range(n)]
        adds: list = []
        for k in kinds:
            if k in (0, 7):
                adds.append(self.u8())
            elif k == 3:
                adds.append(self.lps())
            elif k == 4:
                adds.append((self.lps(), self.i32()))
            else:
                adds.append(None)
        return kinds, adds

    def klass(self, t: int) -> _Obj:
        if t == 1:                                       # ClassWithId: 前に出た型を使い回す
            i, mid = self.i32(), self.i32()
            if mid not in self.meta:
                raise Bad(f"ClassWithId が知らない型 {mid} を指しています")
            name, names, kinds, adds, lib = self.meta[mid]
        else:
            i = self.i32()
            name = self.lps()
            cnt = self.i32()
            if not 0 <= cnt <= 100_000:
                raise Bad(f"項目の数 {cnt} が合いません")
            names = [self.lps() for _ in range(cnt)]
            kinds = adds = None
            if t in (4, 5):
                kinds, adds = self._typeinfo(cnt)
            lib = self.i32() if t in (3, 5) else None
            self.meta[i] = (name, names, kinds, adds, lib)
        o = _Obj(i, name, lib)
        self.objs[i] = o
        k = 0
        while k < len(names):
            if kinds is not None and kinds[k] == 0:
                v = self.prim(adds[k])
            else:
                v = self.value()
            if isinstance(v, _Nulls):
                for _ in range(v.n):
                    if k < len(names):
                        o.members.append((names[k], None))
                        k += 1
                continue
            o.members.append((names[k], v))
            k += 1
        return o

    def array(self, t: int) -> _Arr:
        if t in (15, 16, 17):
            i, n = self.i32(), self.i32()
            if not 0 <= n <= 50_000_000:
                raise Bad(f"配列の長さ {n} が合いません")
            if t == 15:
                code = self.u8()
                a = _Arr(i, _PRIM_NAMES.get(code, f"基本型{code}"), [n])
                if code == 2:                            # byte[] はまとめて取る
                    a.items = self.take(n)
                else:
                    a.items = [self.prim(code) for _ in range(n)]
                self.objs[i] = a
                return a
            a = _Arr(i, "Object" if t == 16 else "String", [n])
            self.objs[i] = a
            self._items(a, n, None, None)
            return a
        i = self.i32()
        atype = self.u8()
        rank = self.i32()
        if not 1 <= rank <= 32:
            raise Bad(f"配列の次元 {rank} が合いません")
        dims = [self.i32() for _ in range(rank)]
        if atype in (3, 4, 5):
            [self.i32() for _ in range(rank)]
        kinds, adds = self._typeinfo(1)
        elem = _elem_name(kinds[0], adds[0])
        if atype in (1, 4):
            elem += "[]"
        a = _Arr(i, elem, dims)
        self.objs[i] = a
        total = 1
        for d in dims:
            total *= max(0, d)
        if total > 50_000_000:
            raise Bad(f"配列の要素数 {total:,} が大きすぎます")
        if kinds[0] == 0 and adds[0] == 2:
            a.items = self.take(total)
        else:
            self._items(a, total, kinds[0], adds[0])
        return a

    def _items(self, a: _Arr, n: int, kind, add) -> None:
        k = 0
        while k < n:
            v = self.prim(add) if kind == 0 else self.value()
            if isinstance(v, _Nulls):
                a.items.extend([None] * v.n)
                k += v.n
                continue
            a.items.append(v)
            k += 1


def _elem_name(kind: int, add) -> str:
    if kind == 0:
        return _PRIM_NAMES.get(add, f"基本型{add}")
    if kind in (3, 4):
        return short_type(add if kind == 3 else add[0])
    return {1: "String", 2: "Object", 5: "Object[]", 6: "String[]", 7: f"{_PRIM_NAMES.get(add, '?')}[]"}.get(kind, "?")


def short_type(t: str) -> str:
    """「System.Collections.Generic.Dictionary`2[[System.String, mscorlib, …],[…]]」→「Dictionary<String, Single>」。"""
    t = t.strip()
    i = t.find("`")
    j = t.find("[[")
    if i < 0 or j < 0:
        base = t.split(",")[0].strip()
        return re.split(r"[.+]", base)[-1] if "." in base or "+" in base else base
    base = re.split(r"[.]", t[:i])[-1]
    args, depth, cur, k = [], 0, "", j + 1
    while k < len(t):
        c = t[k]
        if c == "[":
            depth += 1
            if depth == 1:
                cur = ""
                k += 1
                continue
        elif c == "]":
            depth -= 1
            if depth == 0:
                args.append(cur)
            elif depth < 0:
                break
        if depth >= 1:
            cur += c
        k += 1
    rest = t[k + 1:] if k < len(t) else ""
    tail = "[]" if rest.startswith("[]") else ""
    inner = []
    for a in args:
        d = 0
        cut = len(a)
        for q, c in enumerate(a):
            d += c == "["
            d -= c == "]"
            if c == "," and d == 0:
                cut = q
                break
        inner.append(short_type(a[:cut]))
    nested = t[i:j].split("+", 1)
    suffix = ("+" + nested[1]) if len(nested) > 1 else ""
    return f"{base}<{', '.join(inner)}>{suffix}{tail}"


# ── 木にする ─────────────────────────────────────────────────────────────

class _Render:
    def __init__(self, p: Parser) -> None:
        self.p = p
        self.lines: list[str] = []
        self.shown: set[int] = set()

    def deref(self, v):
        seen = 0
        while isinstance(v, _Ref) and seen < 8:
            v = self.p.objs.get(v.id, f"（#{v.id} は見つからない）")
            seen += 1
        return v

    def scalar(self, v) -> str | None:
        v = self.deref(v)
        if v is None:
            return "null"
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, float):
            return f"{v:.6g}"
        if isinstance(v, (int, _dt.datetime, _dt.timedelta)):
            return str(v)
        if isinstance(v, str):
            s = v.replace("\n", "\\n")
            return '"' + (s if len(s) <= 160 else s[:160] + f"…（{len(s):,} 字）") + '"'
        return None

    def add(self, depth: int, text: str) -> None:
        if len(self.lines) < MAX_LINES:
            self.lines.append("  " * (depth + 1) + text)

    def node(self, label: str, v, depth: int) -> None:
        v0 = v
        v = self.deref(v)
        s = self.scalar(v)
        if s is not None:
            self.add(depth, f"{label} = {s}")
            return
        oid = getattr(v, "id", None)
        if oid is not None and oid in self.shown:
            self.add(depth, f"{label} → #{oid}（上に出た物）")
            return
        if depth >= MAX_DEPTH:
            self.add(depth, f"{label} → #{oid}（深いので省略）")
            return
        if oid is not None:
            self.shown.add(oid)
        if isinstance(v, _Arr):
            self.array(label, v, depth)
            return
        if isinstance(v, _Obj):
            self.obj(label, v, depth, isinstance(v0, _Ref))

    def obj(self, label: str, o: _Obj, depth: int, via_ref: bool) -> None:
        lib = self.p.libs.get(o.lib, "") if o.lib is not None else ""
        t = short_type(o.cls)
        m = dict(o.members)
        if o.cls.startswith("System.Collections.Generic.Dictionary`2") and "KeyValuePairs" in m:
            pairs = self.deref(m.get("KeyValuePairs"))
            items = pairs.items if isinstance(pairs, _Arr) else []
            self._mark(pairs, self.deref(m.get("Comparer")), *[self.deref(x) for x in items])
            self.add(depth, f"{label} → {t} {len(items):,} 件 (#{o.id})")
            for kv in items[:MAX_ITEMS * 2]:
                kv = self.deref(kv)
                if isinstance(kv, _Obj):
                    km = dict(kv.members)
                    ks = self.scalar(km.get("key")) or "?"
                    self.node(f"[{ks}]", km.get("value"), depth + 1)
            if len(items) > MAX_ITEMS * 2:
                self.add(depth + 1, f"…ほか {len(items) - MAX_ITEMS * 2:,} 件")
            return
        if o.cls.startswith("System.Collections.Generic.List`1") and "_items" in m:
            arr = self.deref(m.get("_items"))
            size = m.get("_size") if isinstance(m.get("_size"), int) else None
            items = list(arr.items[:size] if isinstance(arr, _Arr) and size is not None else
                         (arr.items if isinstance(arr, _Arr) else []))
            self._mark(arr)
            self.add(depth, f"{label} → {t} {len(items):,} 件 (#{o.id})")
            self._list(items, depth + 1)
            return
        self.add(depth, f"{label} → {t}" + (f"（{lib.split(',')[0]}）" if lib else "") + f" #{o.id}")
        for name, v in o.members:
            self.node(name, v, depth + 1)

    def _mark(self, *objs) -> None:
        """辞書・リストの内側（KeyValuePair の配列・比較器・_items）は畳んで出したので「出た物」に数える。"""
        for o in objs:
            if isinstance(o, (_Obj, _Arr)):
                self.shown.add(o.id)

    def array(self, label: str, a: _Arr, depth: int) -> None:
        n = len(a.items)
        dims = "×".join(map(str, a.dims))
        if isinstance(a.items, (bytes, bytearray)):
            b = bytes(a.items)
            hint = ""
            if b.startswith(b"\x1f\x8b"):
                hint = "・★gzip"
            elif looks_nrbf(b[:17]):
                hint = "・★中にも BinaryFormatter"
            elif b[:8] == b"\x89PNG\r\n\x1a\n":
                hint = "・★PNG"
            self.add(depth, f"{label} → Byte[{dims}] (#{a.id}) 頭 {b[:24].hex(' ')}{hint}")
            return
        self.add(depth, f"{label} → {a.elem}[{dims}] (#{a.id})")
        self._list(a.items, depth + 1)

    def _list(self, items, depth: int) -> None:
        sc = [self.scalar(x) for x in items[:MAX_ITEMS * 4]]
        if items and all(s is not None for s in sc) and sum(len(s) for s in sc) < 600:
            self.add(depth, ", ".join(sc) + (f" …ほか {len(items) - len(sc):,}" if len(items) > len(sc) else ""))
            return
        for k, x in enumerate(items[:MAX_ITEMS]):
            self.node(f"[{k}]", x, depth)
        if len(items) > MAX_ITEMS:
            self.add(depth, f"…ほか {len(items) - MAX_ITEMS:,} 件")


def describe(data: bytes, start: int = 0) -> list[str]:
    """BinaryFormatter の塊を木の行にする（読めた所までと理由も）。"""
    p = Parser(data, start)
    err = None
    try:
        p.parse()
    except (Bad, struct.error, UnicodeDecodeError, ValueError, OverflowError) as e:
        err = str(e)
    classes: dict[str, int] = {}
    for o in p.objs.values():
        if isinstance(o, _Obj):
            classes[short_type(o.cls)] = classes.get(short_type(o.cls), 0) + 1
    libs = sorted({v.split(",")[0] for v in p.libs.values()})
    out = ["構造（.NET の BinaryFormatter＝MS-NRBF）:",
           f"  根 #{p.root}・オブジェクト {len(p.objs):,} 個・記録 {p.records:,}・{p.p - start:,} バイト読んだ（全体 {len(data) - start:,}）"
           + (f"・ライブラリ {', '.join(libs[:6])}" if libs else "")]
    if classes:
        top = sorted(classes.items(), key=lambda x: -x[1])
        out.append("  クラス: " + "・".join(f"{k} {v}" for k, v in top[:16]) + (f"・ほか {len(top) - 16}" if len(top) > 16 else ""))
    if err:
        out.append(f"  ★途中で止まりました: {err}（読めた所までを出します）")
    elif p.p < len(data):
        out.append(f"  ★終わりの記録の後ろに {len(data) - p.p:,} バイト残っています（別の塊・付け足し）")
    r = _Render(p)
    root = p.objs.get(p.root)
    if root is not None:
        r.node(f"#{p.root}（根）", root, 0)
    for oid, o in p.objs.items():                       # 根から辿れなかった物（あれば）
        if len(r.lines) >= MAX_LINES:
            break
        if isinstance(o, (_Obj, _Arr)) and oid not in r.shown and oid > 0:
            r.node(f"#{oid}", o, 0)
    out += r.lines
    if len(r.lines) >= MAX_LINES:
        out.append(f"  （{MAX_LINES} 行で打ち切り）")
    out.append("  → 値を書き換えて戻すなら .NET（BinaryFormatter）側で。中身を見るだけなら .NET で逆シリアル化しない"
               "（信頼できないデータからは任意のコードが走りうる）。")
    return out


def describe_file(path: str) -> list[str]:
    with open(path, "rb") as fh:
        data = fh.read(MAX_BYTES)
    return describe(data)


# ── 文字のファイルの中の Base64 の項目（JSON のセーブデータ）──────────────────────
# ★走行記録（10-10）: 復号した JSON の項目の値が Base64 で、その中身が BinaryFormatter だった。モデルは Base64 を
#   手で取り出して .bin に書き、map で「構造のあるデータ」としか分からなかった。Read が JSON を返すときに添える。

_B64_FIELD = re.compile(r'"([^"\\]{1,80})"\s*:\s*"([A-Za-z0-9+/]{24,}={0,2})"')
MAX_B64_FIELDS = 8
MAX_B64_LINES = 120


def base64_fields(text: str) -> list[str]:
    """"項目": "<Base64>" の値を開け、中身が BinaryFormatter・gzip なら木を返す（当たりが無ければ空）。"""
    import base64 as _b64
    import zlib as _zl
    out: list[str] = []
    n = 0
    for m in _B64_FIELD.finditer(text):
        if n >= MAX_B64_FIELDS or len(out) >= MAX_B64_LINES:
            break
        val = m.group(2)
        try:
            raw = _b64.b64decode(val + "=" * (-len(val) % 4), validate=True)
        except (ValueError, TypeError):
            continue
        what = None
        if looks_nrbf(raw[:17]):
            what, body = "BinaryFormatter", raw
        elif raw[:2] == b"\x1f\x8b":
            try:
                body = _zl.decompressobj(31).decompress(raw, MAX_BYTES)
            except Exception:  # noqa: BLE001
                continue
            if looks_nrbf(body[:17]):
                what = "gzip の中に BinaryFormatter"
            else:
                out.append(f"★項目 \"{m.group(1)}\" の値は Base64 → gzip（展開すると {len(body):,} バイト・頭 {body[:24]!r}）")
                n += 1
                continue
        if what is None:
            continue
        n += 1
        rows = describe(body)
        out.append(f"★項目 \"{m.group(1)}\" の値は Base64 → {what}（{len(raw):,} バイト・道具が開いた）:")
        out += ["  " + r for r in rows[1:-1]][: max(10, MAX_B64_LINES - len(out))]
    return out
