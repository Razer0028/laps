"""Unreal Engine のファイルの中の構造を読む（Read・MCP の inspect_file・2026-10-11）。

オーナー「ゲーム解析の強化を進めてください。Unity や UE も含めて」。
それまでは .pak の末尾の印から版と目録の位置を言うだけだった。UE のゲームを調べる最初の一歩は「何が入っているか」なので、
目録（ファイルの一覧・大きさ・圧縮・暗号）まで道具が読む。

  .pak         … 目録（v1〜v11・UE4.25 以降の path hash 目録を含む）。目録が暗号化されていれば、その事実と要る物（AES の鍵）。
  .utoc/.ucas  … IoStore（UE5・UE4.26 以降）。チャンクの数・圧縮・暗号・目録（ファイルの一覧）。
  .uasset/.umap… パッケージの頭（版・保存したエンジン・名前の一覧・import・export（種類と名前と大きさ））。
                 版なし（unversioned）で保存された物は、ありうる版を順に当てて表が全部つじつまの合う版を選ぶ（推定と書く）。
  .locres      … 翻訳の表（名前空間・キー・文の数と先頭）。
  .uexp/.ubulk … 同じ名前の .uasset の続き（本体はそちら）。

読むだけ。中身（ファイルの本体）は展開しない。読めない・壊れているときは、そこまで読めた事実と理由を返す。
書き方の手本: repak（.pak）・CUE4Parse（IoStore・パッケージの頭・locres）。
"""
from __future__ import annotations

import os
import struct
import time

_MAX_LIST = 30
_TIME = 20.0                     # 大きな目録でも道具を止めない上限（秒）

PAK_MAGIC = 0x5A6F12E1
TOC_MAGIC = b"-==--==--==--==-"
PKG_TAG = 0x9E2A83C1
PKG_TAG_SWAPPED = 0xC1832A9E
LOCRES_MAGIC = bytes.fromhex("0e147475674a03fc4a15909dc3377f1b")


def _mb(n: int) -> str:
    return f"{n / 1048576:,.1f} MB" if n >= 1048576 else f"{n:,} バイト"


class _R:
    """小さな読み手（バイト列の上）。足りなければ ValueError。"""

    def __init__(self, b: bytes, pos: int = 0):
        self.b, self.p = b, pos

    def need(self, n: int) -> None:
        if n < 0 or self.p + n > len(self.b):
            raise ValueError(f"{self.p:,} バイト目から {n:,} バイトを読もうとして、データが足りません")

    def u8(self) -> int:
        self.need(1); v = self.b[self.p]; self.p += 1; return v

    def u16(self) -> int:
        self.need(2); v = struct.unpack_from("<H", self.b, self.p)[0]; self.p += 2; return v

    def u32(self) -> int:
        self.need(4); v = struct.unpack_from("<I", self.b, self.p)[0]; self.p += 4; return v

    def i32(self) -> int:
        self.need(4); v = struct.unpack_from("<i", self.b, self.p)[0]; self.p += 4; return v

    def u64(self) -> int:
        self.need(8); v = struct.unpack_from("<Q", self.b, self.p)[0]; self.p += 8; return v

    def i64(self) -> int:
        self.need(8); v = struct.unpack_from("<q", self.b, self.p)[0]; self.p += 8; return v

    def raw(self, n: int) -> bytes:
        self.need(n); v = self.b[self.p:self.p + n]; self.p += n; return v

    def fstring(self, limit: int = 1 << 16) -> str:
        """UE の FString（長さ i32・正なら Latin-1/UTF-8・負なら UTF-16・終端の 0 を含む）。"""
        n = self.i32()
        if n == 0:
            return ""
        if n > 0:
            if n > limit:
                raise ValueError(f"文字列の長さ {n:,} は大きすぎます（{self.p - 4:,} バイト目）")
            s = self.raw(n)
            return s.rstrip(b"\x00").decode("utf-8", "replace")
        n = -n
        if n > limit:
            raise ValueError(f"文字列の長さ {n:,} は大きすぎます（{self.p - 4:,} バイト目）")
        s = self.raw(n * 2)
        return s.decode("utf-16-le", "replace").rstrip("\x00")


def _ext_table(paths_sizes: list[tuple[str, int]], top: int = 10) -> str:
    agg: dict[str, list[int]] = {}
    for p, sz in paths_sizes:
        e = os.path.splitext(p)[1].lower() or "（拡張子なし）"
        a = agg.setdefault(e, [0, 0])
        a[0] += 1
        a[1] += max(0, sz)
    rows = sorted(agg.items(), key=lambda kv: -kv[1][1])[:top]
    return "・".join(f"{e} {c:,} 個（{_mb(s)}）" for e, (c, s) in rows)


def _dir_table(paths_sizes: list[tuple[str, int]], depth: int = 2, top: int = 12) -> list[str]:
    """フォルダごとの数と大きさ。全部に共通する頭（置き場・Content など）の下を depth 段で数える。"""
    agg: dict[str, list[int]] = {}
    split = [[x for x in p.replace("\\", "/").split("/") if x and x not in ("..",)] for p, _ in paths_sizes]
    common = 0
    if split:
        first = split[0][:-1]
        while common < len(first) and all(len(sp) > common + 1 and sp[common] == first[common] for sp in split):
            common += 1
    depth += common
    for (p, sz), parts in zip(paths_sizes, split):
        key = "/".join(parts[:depth]) if len(parts) > depth else ("/".join(parts[:-1]) or "（直下）")
        a = agg.setdefault(key, [0, 0])
        a[0] += 1
        a[1] += max(0, sz)
    rows = sorted(agg.items(), key=lambda kv: -kv[1][1])[:top]
    return [f"    {d}{'' if d == '（直下）' else '/'}  {c:,} 個・{_mb(s)}" for d, (c, s) in rows]


def _listing(title: str, files: list[tuple[str, int]], find: str = "") -> list[str]:
    out = []
    if find:
        keys = [k.strip().lower() for k in find.split("|") if k.strip()]
        hit = [(p, s) for p, s in files if any(k in p.lower() for k in keys)]
        out.append(f"  {title}のうち「{find}」を含む: {len(hit):,} 個")
        for p, s in hit[:200]:
            out.append(f"    {p}  （{s:,} バイト）")
        if len(hit) > 200:
            out.append(f"    …ほか {len(hit) - 200:,} 個（語を足して絞ってください）")
        return out
    out.append(f"  種類ごと: {_ext_table(files)}")
    out.append("  フォルダごと（大きい順）:")
    out += _dir_table(files)
    out.append(f"  先頭 {min(_MAX_LIST, len(files))} 個:")
    for p, s in files[:_MAX_LIST]:
        out.append(f"    {p}  （{s:,} バイト）")
    if len(files) > _MAX_LIST:
        out.append(f"    …ほか {len(files) - _MAX_LIST:,} 個（Read に find=<語> で名前を含む物だけを出せます）")
    return out


# ── .pak ─────────────────────────────────────────────────────────────────────

_PAK_COMP_OLD = ["Zlib", "Gzip", "Oodle"]          # 版 7 以前の圧縮の番号（1 始まり）


def _pak_footer(path: str, size: int) -> dict | None:
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 400))
        tail = fh.read()
    base = size - len(tail)
    i = tail.rfind(struct.pack("<I", PAK_MAGIC))
    while i >= 0:
        if i + 44 <= len(tail):
            ver, idx_off, idx_size = struct.unpack_from("<IQQ", tail, i + 4)
            if 1 <= ver <= 12 and idx_off + idx_size <= size:
                end_gap = len(tail) - i                 # 印から末尾まで
                names_len = end_gap - 44 - (1 if ver == 9 else 0)
                sub = "8B" if ver == 8 and names_len >= 160 else ("8A" if ver == 8 else "")
                comps: list[str] = []
                if ver >= 8 and names_len > 0:
                    nb = tail[i + 44 + (1 if ver == 9 else 0): i + 44 + (1 if ver == 9 else 0) + names_len]
                    for k in range(0, len(nb) - 31, 32):
                        comps.append(nb[k:k + 32].split(b"\x00")[0].decode("ascii", "replace"))
                else:
                    comps = list(_PAK_COMP_OLD)
                enc = ver >= 4 and i >= 1 and tail[i - 1] == 1
                guid = tail[i - 17:i - 1] if ver >= 7 and i >= 17 else b""
                return {"ver": ver, "sub": sub, "idx_off": idx_off, "idx_size": idx_size, "enc_index": enc,
                        "comps": comps, "key_guid": guid, "magic_at": base + i}
        i = tail.rfind(struct.pack("<I", PAK_MAGIC), 0, i)
    return None


def _pak_entry(r: _R, ver: int, sub: str) -> dict:
    off, csize, usize = r.u64(), r.u64(), r.u64()
    comp = r.u8() if (ver == 8 and sub == "8A") else r.u32()
    if ver == 1:
        r.u64()                                          # 時刻
    r.raw(20)                                            # SHA1
    blocks = 0
    flags = 0
    if ver >= 3:
        if comp:
            blocks = r.u32()
            r.raw(16 * blocks)
        flags = r.u8()
        r.u32()                                          # 圧縮のブロックの大きさ
    return {"usize": usize, "csize": csize, "comp": comp, "enc": bool(flags & 1), "blocks": blocks, "off": off}


def _pak_encoded(r: _R) -> dict:
    bits = r.u32()
    comp = (bits >> 23) & 0x3F
    enc = bool(bits & (1 << 22))
    nblocks = (bits >> 6) & 0xFFFF
    if bits & 0x3F == 0x3F:
        r.u32()
    off = r.u32() if bits & (1 << 31) else r.u64()
    usize = r.u32() if bits & (1 << 30) else r.u64()
    csize = (r.u32() if bits & (1 << 29) else r.u64()) if comp else usize
    if nblocks > 1 or (nblocks == 1 and enc):
        r.raw(4 * nblocks)
    return {"usize": usize, "csize": csize, "comp": comp, "enc": enc, "blocks": nblocks, "off": off}


def pak(path: str, size: int, find: str = "") -> list[str] | None:
    f = _pak_footer(path, size)
    if not f:
        return None
    ver = f["ver"]
    out = ["構造（Unreal Engine の .pak）:",
           f"  形式の版 {ver}{f['sub'] and '（' + f['sub'] + '）'}・目録は {f['idx_off']:,} バイト目から {f['idx_size']:,} バイト"
           + (f"・圧縮の方式 {', '.join(c for c in f['comps'] if c) or 'なし'}" if ver >= 8 else "")]
    if f["enc_index"]:
        g = f["key_guid"].hex() if f["key_guid"] and any(f["key_guid"]) else ""
        out += ["  ★目録が暗号化されています（AES-256）。ファイルの一覧を読むには鍵が要ります"
                + (f"（鍵の GUID {g}）" if g else "（既定の鍵）") + "。",
                "  → 鍵はゲームの実行ファイルに埋め込まれていることが多い（AESKeyFinder・FModel のコミュニティの鍵表）。"
                "鍵があれば repak（`repak --aes-key <0x…> list <file>`）か FModel で開けます。"]
        return out
    with open(path, "rb") as fh:
        fh.seek(f["idx_off"])
        idx = fh.read(min(f["idx_size"], 256 << 20))
    r = _R(idx)
    mount = r.fstring()
    count = r.u32()
    files: list[tuple[str, int]] = []
    meta: list[dict] = []
    t0 = time.time()
    note = ""
    if ver < 10:
        for _ in range(count):
            name = r.fstring()
            e = _pak_entry(r, ver, f["sub"])
            files.append((name, e["usize"]))
            meta.append(e)
            if time.time() - t0 > _TIME:
                note = f"（{_TIME:.0f} 秒で打ち切り＝下限）"
                break
    else:
        r.u64()                                          # path hash の種
        has_phi = r.u32()
        if has_phi:
            r.u64(); r.u64(); r.raw(20)
        has_fdi = r.u32()
        fdi = None
        if has_fdi:
            fdi_off, fdi_size = r.u64(), r.u64()
            r.raw(20)
            with open(path, "rb") as fh:
                fh.seek(fdi_off)
                fdi = fh.read(min(fdi_size, 256 << 20))
        enc_size = r.u32()
        encoded = r.raw(enc_size)
        n_plain = r.u32()
        plain = [_pak_entry(r, ver, f["sub"]) for _ in range(n_plain)]
        if fdi is None:
            out.append(f"  ファイル {count:,} 個（名前の目録が入っていない＝path hash だけ。名前の一覧は出せません）")
            return out
        d = _R(fdi)
        er = _R(encoded)
        for _ in range(d.u32()):
            dname = d.fstring()
            for _ in range(d.u32()):
                fname = d.fstring()
                eo = d.i32()
                if eo == -0x80000000:
                    continue
                if eo >= 0:
                    er.p = eo
                    e = _pak_encoded(er)
                else:
                    e = plain[-eo - 1] if -eo - 1 < len(plain) else {"usize": 0, "csize": 0, "comp": 0, "enc": False}
                files.append(((dname.lstrip("/") + fname), e["usize"]))
                meta.append(e)
            if time.time() - t0 > _TIME:
                note = f"（{_TIME:.0f} 秒で打ち切り＝下限）"
                break
    total_u = sum(m["usize"] for m in meta)
    total_c = sum(m["csize"] for m in meta)
    n_enc = sum(1 for m in meta if m.get("enc"))
    used: dict[str, int] = {}
    for m in meta:
        if m.get("comp"):
            k = m["comp"] - 1
            nm = f["comps"][k] if 0 <= k < len(f["comps"]) and f["comps"][k] else f"番号 {m['comp']}"
            used[nm] = used.get(nm, 0) + 1
    out.append(f"  中のファイル {len(files):,} 個{note}・展開すると {_mb(total_u)}（格納 {_mb(total_c)}）・置き場 {mount!r}")
    out.append("  圧縮: " + ("・".join(f"{k} {v:,} 個" for k, v in used.items()) if used else "なし")
               + (f"・★本体が暗号化された物 {n_enc:,} 個（展開に鍵が要る）" if n_enc else ""))
    files.sort(key=lambda x: x[0])
    out += _listing("ファイル", files, find)
    if any(p.endswith((".uasset", ".umap")) for p, _ in files):
        out.append("  → 中の .uasset の頭（名前・import・export）は、取り出してから Read で読めます。取り出しは "
                   "`repak unpack <file>`（鍵があれば --aes-key）か FModel。")
    return out


# ── IoStore（.utoc／.ucas）──────────────────────────────────────────────────

_TOC_FLAGS = [(1, "圧縮"), (2, "★暗号化"), (4, "署名つき"), (8, "目録あり"), (16, "OnDemand")]
_CHUNK5 = {0: "Invalid", 1: "ExportBundleData", 2: "BulkData", 3: "OptionalBulkData", 4: "MemoryMappedBulkData",
           5: "ScriptObjects", 6: "ContainerHeader", 7: "ExternalFile", 8: "ShaderCodeLibrary", 9: "ShaderCode",
           10: "PackageStoreEntry", 11: "DerivedData", 12: "EditorDerivedData", 13: "PackageResource"}


def _b40(b: bytes) -> int:
    return int.from_bytes(b[:5], "big")


def utoc(path: str, size: int, find: str = "") -> list[str]:
    with open(path, "rb") as fh:
        data = fh.read(min(size, 512 << 20))
    r = _R(data)
    if r.raw(16) != TOC_MAGIC:
        return ["構造（IoStore の .utoc）: 印が合いません"]
    ver = r.u8(); r.u8(); r.u16()
    hsize, n_entries, n_blocks, block_entry_size = r.u32(), r.u32(), r.u32(), r.u32()
    n_methods, method_len, block_size, dir_size = r.u32(), r.u32(), r.u32(), r.u32()
    r.u32()                                              # パーティションの数
    container_id = r.u64()
    key_guid = r.raw(16)
    flags = r.u8()
    enc_method = r.u8()
    r.u16()
    seeds = r.u32()
    r.u64()                                              # パーティションの大きさ
    no_hash = r.u32()
    r.p = hsize
    flag_txt = "・".join(t for b, t in _TOC_FLAGS if flags & b) or "なし"
    out = ["構造（Unreal Engine の IoStore 目録 .utoc）:",
           f"  形式の版 {ver}・チャンク {n_entries:,} 個・圧縮のブロック {n_blocks:,} 個（1 つ {_mb(block_size)}）・"
           f"コンテナ ID {container_id:016x}・{flag_txt}"]
    cas = os.path.splitext(path)[0] + ".ucas"
    out.append(f"  本体: {os.path.basename(cas)}"
               + (f"（{_mb(os.path.getsize(cas))}）" if os.path.exists(cas) else "（★同じ場所にありません）"))
    ids = r.raw(12 * n_entries)
    offlen = r.raw(10 * n_entries)
    if ver >= 4:
        r.raw(4 * seeds)
    if ver >= 5:
        r.raw(4 * no_hash)
    blocks = r.raw(12 * n_blocks)
    names = r.raw(n_methods * method_len)
    methods = [names[i * method_len:(i + 1) * method_len].split(b"\x00")[0].decode("ascii", "replace")
               for i in range(n_methods)]
    used: dict[int, int] = {}
    for k in range(n_blocks):
        m = blocks[k * 12 + 11]
        used[m] = used.get(m, 0) + 1
    out.append("  圧縮: " + "・".join(f"{('なし' if m == 0 else (methods[m - 1] if m - 1 < len(methods) else f'番号 {m}'))} "
                                    f"{c:,} ブロック" for m, c in sorted(used.items())))
    kinds: dict[int, int] = {}
    for k in range(n_entries):
        t = ids[k * 12 + 11]
        kinds[t] = kinds.get(t, 0) + 1
    out.append("  チャンクの種類（UE5 の番号で読んだ名前）: "
               + "・".join(f"{_CHUNK5.get(t, f'種類 {t}')} {c:,}" for t, c in sorted(kinds.items(), key=lambda kv: -kv[1])))
    if flags & 2:
        g = key_guid.hex()
        out += [f"  ★暗号化されています（鍵の GUID {g if any(key_guid) else '既定の鍵'}）。目録（ファイル名）も本体も鍵が要ります。",
                "  → 鍵があれば FModel・retoc で開けます。鍵はゲームの実行ファイルから探すのが普通です（AESKeyFinder）。"]
        return out
    if flags & 4:
        hs = r.i32()
        r.raw(hs + hs + 20 * n_blocks)
    if not (flags & 8) or dir_size == 0 or ver < 2:
        out.append("  ファイル名の目録がありません（チャンクの番号だけ）。global.utoc などはこの形です。")
        return out
    d = _R(r.raw(dir_size))
    mount = d.fstring()
    nd = d.u32()
    dirs = [struct.unpack_from("<IIII", d.raw(16)) for _ in range(nd)]
    nf = d.u32()
    fents = [struct.unpack_from("<III", d.raw(12)) for _ in range(nf)]
    strs = [d.fstring() for _ in range(d.u32())]
    files: list[tuple[str, int]] = []
    stack = [(0, mount.rstrip("/") + "/" if mount else "")]
    none = 0xFFFFFFFF
    t0 = time.time()
    while stack and time.time() - t0 < _TIME:
        di, prefix = stack.pop()
        name_i, child, sib, ffile = dirs[di]
        here = prefix + (strs[name_i] + "/" if name_i != none and name_i < len(strs) and strs[name_i] else "")
        if sib != none:
            stack.append((sib, prefix))
        if child != none:
            stack.append((child, here))
        fi = ffile
        guard = 0
        while fi != none and fi < len(fents) and guard < 1_000_000:
            fname, nxt, user = fents[fi]
            sz = _b40(offlen[user * 10 + 5:user * 10 + 10]) if user < n_entries else 0
            files.append((here + (strs[fname] if fname < len(strs) else "?"), sz))
            fi, guard = nxt, guard + 1
    files = [(p.replace("../../../", ""), s) for p, s in files]
    files.sort(key=lambda x: x[0])
    out.append(f"  ファイル {len(files):,} 個・置き場 {mount!r}")
    out += _listing("ファイル", files, find)
    out.append("  → 中の .uasset は UE5 の zen 形式（IoStore 用の頭）です。取り出しと中身は FModel か retoc"
               "（`retoc to-legacy` で .pak 用の .uasset に戻せる）。")
    return out


# ── パッケージ（.uasset／.umap）─────────────────────────────────────────────

_PKG_FLAGS = [(0x00000200, "cooked"), (0x00002000, "★プロパティは版なしで保存（読むには .usmap＝型の地図が要る）"),
              (0x00020000, "マップ（ULevel）を含む"), (0x80000000, "エディタ用の物を除いた（出荷用）"),
              (0x00000040, "エディタ専用")]
# 版なし（unversioned）のときに当てる版。UE5（-8 以降）と UE4。新しい順。
_UE5_TRY = list(range(1018, 999, -1))
_UE4_TRY = [522, 521, 520, 519, 518, 517, 516, 515, 514, 513, 512, 511, 510, 508, 507, 505, 504, 503, 499, 498,
            491, 482, 471, 459]


def _pkg_summary(b: bytes, size: int, ue4: int | None, ue5: int | None) -> dict:
    r = _R(b)
    tag = r.u32()
    if tag not in (PKG_TAG,):
        raise ValueError(f"印 0x{tag:08X} はパッケージの物ではありません")
    legacy = r.i32()
    if legacy >= 0:
        raise ValueError(f"UE3 以前の形（legacy {legacy}）は読みません")
    if legacy != -4:
        r.i32()
    f_ue4 = r.i32()
    f_ue5 = r.i32() if legacy <= -8 else 0
    lic = r.i32()
    unversioned = f_ue4 == 0 and f_ue5 == 0 and lic == 0
    v4 = ue4 if unversioned else f_ue4
    v5 = (ue5 if unversioned else f_ue5) or 0
    if v4 is None:
        raise ValueError("版なし")
    s: dict = {"legacy": legacy, "ue4": v4, "ue5": v5, "lic": lic, "unversioned": unversioned}
    if v5 >= 1016:
        r.raw(20)
        s["total_header"] = r.i32()
    ncv = r.i32()
    if not 0 <= ncv <= 2000:
        raise ValueError(f"カスタム版の数 {ncv} が合いません")
    if legacy == -2:
        r.raw(8 * ncv)
    elif -5 <= legacy <= -3:
        for _ in range(ncv):
            r.raw(20); r.fstring(256)
    else:
        r.raw(20 * ncv)
    s["custom_versions"] = ncv
    if v5 < 1016:
        s["total_header"] = r.i32()
    s["folder"] = r.fstring(1024)
    flags = r.u32()
    s["flags"] = flags
    s["name_count"], s["name_off"] = r.i32(), r.i32()
    if v5 >= 1008:
        r.i32(); r.i32()
    if not (flags & 0x80000000) and v4 >= 516:
        s["loc_id"] = r.fstring(256)
    if v4 >= 459:
        r.i32(); r.i32()
    s["export_count"], s["export_off"] = r.i32(), r.i32()
    s["import_count"], s["import_off"] = r.i32(), r.i32()
    if v5 >= 1015:
        r.raw(16)
    if v5 >= 1014:
        r.i32()
    s["depends_off"] = r.i32()
    if v4 >= 384:
        r.i32(); r.i32()
    if v4 >= 510:
        r.i32()
    r.i32()                                              # サムネイルの表
    if v5 >= 1018:
        r.i32(); r.i32()
    if v5 < 1016:
        r.raw(16)
    if not (flags & 0x80000000):
        if v4 >= 518:
            r.raw(16)
            if v4 < 520:
                r.raw(16)
    gens = r.i32()
    if not 0 <= gens <= 10000:
        raise ValueError(f"世代の数 {gens} が合いません")
    r.raw(8 * gens)
    try:
        if v4 >= 336:
            maj, mnr, pat, cl = r.u16(), r.u16(), r.u16(), r.u32()
            br = r.fstring(512)
            if maj in (4, 5) and mnr < 40:                 # 版を当てて読んだときに位置がずれると別の数になる
                s["engine"] = f"{maj}.{mnr}.{pat}" + (f"-{cl & 0x7FFFFFFF}" if cl else "") + (f"+{br}" if br else "")
    except ValueError:
        pass
    # つじつま（版なしの推定に使う）
    th = s.get("total_header") or 0
    lim = size if th <= 0 or th > size else th
    for k in ("name_off", "import_off", "export_off"):
        if not 0 < s[k] <= lim:
            raise ValueError(f"{k} {s[k]} が頭の範囲（{lim:,}）にありません")
    for k in ("name_count", "import_count", "export_count"):
        if not 0 <= s[k] <= 2_000_000:
            raise ValueError(f"{k} {s[k]} が合いません")
    if s["name_count"] == 0:
        raise ValueError("名前が 0 個")
    s["header_end"] = r.p
    return s


def _pkg_names(b: bytes, s: dict) -> list[str]:
    r = _R(b, s["name_off"])
    out = []
    for _ in range(s["name_count"]):
        n = r.fstring(1024)
        if s["ue4"] >= 504:
            r.raw(4)
        out.append(n)
    return out


def _fname(names: list[str], idx: int, num: int) -> str:
    base = names[idx] if 0 <= idx < len(names) else f"?{idx}"
    return f"{base}_{num - 1}" if num > 0 else base


def _stride(s: dict, key_off: str, count: int, sizes: set[int] | None) -> int | None:
    """表の 1 件の大きさを、次の表までの隙間から決める（版ごとの並びを全部持たないため）。"""
    if count <= 0:
        return None
    start = s[key_off]
    nexts = [s[k] for k in ("name_off", "import_off", "export_off", "depends_off", "total_header")
             if isinstance(s.get(k), int) and s[k] > start]
    if not nexts:
        return None
    gap = min(nexts) - start
    if gap % count:
        return None
    st = gap // count
    return st if (sizes is None or st in sizes) else None


# export の 1 件の大きさ（UE4.0〜UE5.7 の並びで取りうる長さ）。隙間から決められないときに当てる。
_EXPORT_STRIDES = [104, 112, 120, 96, 100, 108, 116, 124, 128, 92, 88, 84, 80, 76, 72, 68, 132, 136]


def _exports_fit(b: bytes, s: dict, names: list[str], n_imp: int, st: int) -> bool:
    """この大きさで export を全部読んで、どれも辻褄が合うか（型と外側の番号・名前の番号・本体の位置）。"""
    n_exp = s["export_count"]
    if st < 40 or s["export_off"] + st * n_exp > len(b):
        return False
    wide = s["ue4"] >= 511
    for k in range(min(n_exp, 4000)):
        o = s["export_off"] + k * st
        cls, sup = struct.unpack_from("<ii", b, o)
        o += 8
        if s["ue4"] >= 508:
            o += 4
        outer, ni, nn = struct.unpack_from("<iii", b, o)
        o += 16
        ssz, soff = struct.unpack_from("<qq" if wide else "<ii", b, o)
        if not (-n_imp <= cls <= n_exp and -n_imp <= sup <= n_exp and -n_imp <= outer <= n_exp):
            return False
        if not (0 <= ni < len(names) and 0 <= nn < 1 << 20):
            return False
        if ssz < 0 or soff < 0:
            return False
    return True


def _pkg_tables(b: bytes, s: dict, names: list[str]) -> tuple[list[dict], list[dict], str]:
    note = ""
    imports: list[dict] = []
    ist = _stride(s, "import_off", s["import_count"], {28, 32, 36, 40})
    if ist is None and s["import_count"]:
        ist = 28 + (8 if s["ue4"] >= 520 and not s["flags"] & 0x80000000 else 0) + (4 if s["ue5"] >= 1003 else 0)
        note = "import の 1 件の大きさを版から決めた（隙間から確かめられなかった）"
    for k in range(s["import_count"]):
        r = _R(b, s["import_off"] + k * ist)
        cpk = _fname(names, r.i32(), r.i32())
        cls = _fname(names, r.i32(), r.i32())
        outer = r.i32()
        nm = _fname(names, r.i32(), r.i32())
        imports.append({"class_package": cpk, "class": cls, "outer": outer, "name": nm})
    exports: list[dict] = []
    est = _stride(s, "export_off", s["export_count"], None)
    if s["export_count"] and (est is None or not _exports_fit(b, s, names, len(imports), est)):
        est = next((c for c in _EXPORT_STRIDES if _exports_fit(b, s, names, len(imports), c)), None)
    if s["export_count"] and est is None:
        note = (note + "・" if note else "") + "export の表の 1 件の大きさが決められず、名前だけ読めない"
        return imports, exports, note
    for k in range(s["export_count"]):
        r = _R(b, s["export_off"] + k * est)
        cls_i = r.i32()
        r.i32()                                          # 基底
        if s["ue4"] >= 508:
            r.i32()                                      # 手本
        outer = r.i32()                                  # 外側
        nm = _fname(names, r.i32(), r.i32())
        r.u32()                                          # フラグ
        ssz = r.i64() if s["ue4"] >= 511 else r.i32()
        soff = r.i64() if s["ue4"] >= 511 else r.i32()
        if cls_i < 0 and -cls_i - 1 < len(imports):
            cls = imports[-cls_i - 1]["name"]
        elif cls_i > 0:
            cls = cls_i                                  # このパッケージの export（下で名前に直す）
        else:
            cls = "Class"
        exports.append({"class": cls, "name": nm, "size": ssz, "offset": soff, "outer": outer})
    for e in exports:
        if isinstance(e["class"], int):
            i = e["class"] - 1
            e["class"] = (exports[i]["name"] + "（このパッケージの型）") if 0 <= i < len(exports) else f"export {e['class']}"
    return imports, exports, note


def uasset(path: str, size: int, find: str = "") -> list[str]:
    with open(path, "rb") as fh:
        b = fh.read(min(size, 64 << 20))
    tag = struct.unpack_from("<I", b, 0)[0]
    if tag == PKG_TAG_SWAPPED:
        return ["構造（Unreal Engine のパッケージ・ビッグエンディアン）: この形は読みません（家庭用機の古い物）。"]
    s = None
    err = ""
    guessed = False
    try:
        s = _pkg_summary(b, size, None, None)
    except ValueError as e:
        err = str(e)
    if s is None and err == "版なし":
        legacy = struct.unpack_from("<i", b, 4)[0]
        cands = ([(522, v) for v in _UE5_TRY] if legacy <= -8 else []) + [(v, 0) for v in _UE4_TRY]
        fits: list[tuple[int, int]] = []
        for v4, v5 in cands:
            try:
                t = _pkg_summary(b, size, v4, v5)
                names = _pkg_names(b, t)
                imps, exps, note = _pkg_tables(b, t, names)
                if t["export_count"] and not exps:
                    continue
                if any(n.startswith("?") for n in [i["class"] for i in imps]):
                    continue
                fits.append((v4, v5))
                if s is None:
                    s, guessed = t, True
            except (ValueError, struct.error, IndexError):
                continue
        if s is not None:
            s["fits"] = fits
        if s is None:
            return ["構造（Unreal Engine のパッケージ .uasset）:",
                    "  ★版なし（unversioned）で保存されていて、ありうる版をすべて当てても表のつじつまが合いませんでした。",
                    "  → ゲーム独自に頭を変えている可能性。FModel・UAssetGUI でエンジンの版を指定して開いてください。"]
    if s is None:
        return ["構造（Unreal Engine のパッケージ .uasset）:", f"  頭を読めません: {err}"]
    names = _pkg_names(b, s)
    imports, exports, note = _pkg_tables(b, s, names)
    ver = f"UE4 の版 {s['ue4']}" + (f"・UE5 の版 {s['ue5']}" if s["ue5"] else "")
    if guessed:
        fits = s.get("fits") or []
        if any(v5 for _v4, v5 in fits):
            r5 = sorted({v5 for _v4, v5 in fits if v5})
            ver = f"UE5 の版 {r5[0]}" + (f"〜{r5[-1]}" if r5[-1] != r5[0] else "") + " のどれか"
        else:
            r4 = sorted({v4 for v4, _v5 in fits})
            ver = f"UE4 の版 {r4[0]}" + (f"〜{r4[-1]}" if r4[-1] != r4[0] else "") + " のどれか"
    out = ["構造（Unreal Engine のパッケージ " + os.path.splitext(path)[1] + "）:",
           f"  {ver}" + ("（★版なしで保存＝表のつじつまが合う版の幅。正確な版はゲームの実行ファイルか FModel で）" if guessed else "")
           + (f"・保存したエンジン {s['engine']}" if s.get("engine") and not s["engine"].startswith("0.0.0") else "")
           + f"・カスタム版 {s['custom_versions']} 個・頭 {_mb(s.get('total_header') or 0)}",
           f"  パッケージ名 {s['folder']!r}・" + "・".join(t for bit, t in _PKG_FLAGS if s["flags"] & bit),
           f"  名前 {len(names):,} 個・import {len(imports):,} 個・export {len(exports):,} 個"]
    if note:
        out.append(f"  （{note}）")
    exp_path = os.path.splitext(path)[0] + ".uexp"
    if os.path.exists(exp_path):
        out.append(f"  本体の続き: {os.path.basename(exp_path)}（{_mb(os.path.getsize(exp_path))}）"
                   "＝export の中身はこちら（位置は頭の大きさを引いた所から）")
    blk = os.path.splitext(path)[0] + ".ubulk"
    if os.path.exists(blk):
        out.append(f"  大きなデータ: {os.path.basename(blk)}（{_mb(os.path.getsize(blk))}）＝テクスチャの mip・音声など")
    if find:
        k = find.lower()
        out.append(f"  名前のうち「{find}」を含む: " + ", ".join(n for n in names if k in n.lower())[:2000])
    if exports:
        by: dict[str, int] = {}
        for e in exports:
            by[e["class"]] = by.get(e["class"], 0) + 1
        out.append("  export の種類: " + "・".join(f"{c} {n}" for c, n in sorted(by.items(), key=lambda kv: -kv[1])[:15]))
        top = [e for e in exports if e.get("outer") == 0]
        rest = [e for e in exports if e.get("outer") != 0]
        if top:
            out.append(f"  パッケージ直下の export（資産そのもの）{len(top)} 個:")
            for e in top[:_MAX_LIST]:
                out.append(f"    {e['class']}  {e['name']}  {e['size']:,} バイト")
        exports = rest if top else exports
        out.append(f"  ほかの export（先頭 {min(_MAX_LIST, len(exports))} 個・種類 名前 大きさ）:")
        for e in exports[:_MAX_LIST]:
            out.append(f"    {e['class']}  {e['name']}  {e['size']:,} バイト")
        if len(exports) > _MAX_LIST:
            out.append(f"    …ほか {len(exports) - _MAX_LIST:,} 個")
    if imports:
        pk = sorted({i["name"] for i in imports if i["class"] == "Package"})
        out.append(f"  参照しているパッケージ {len(pk)} 個: " + ", ".join(pk[:20]) + (" …" if len(pk) > 20 else ""))
        cls = sorted({i["name"] for i in imports if i["class"] in ("Class", "BlueprintGeneratedClass", "ScriptStruct")})
        if cls:
            out.append("  使っている型: " + ", ".join(cls[:30]) + (" …" if len(cls) > 30 else ""))
    show = [n for n in names if not n.startswith("/")][:60]
    out.append("  名前（先頭 60・パスを除く）: " + ", ".join(show))
    if s["flags"] & 0x00002000:
        out.append("  → 項目の値は版なしの形（unversioned properties）。FModel・UAssetGUI に .usmap（Dumper-7 などでゲームから作る）を渡すと読めます。")
    return out


# ── zen 形式のパッケージ（IoStore の中の .uasset・FModel/retoc で取り出した物）──────────────

def _name_batch(r: "_R", count: int | None = None) -> list[str]:
    """名前の束（UE5 の FNameBatch・UE4.27 の IoStore の名前）。count が None なら頭の数・hash も読む。"""
    if count is None:
        n = r.i32()
        if n == 0:
            return []
        if not 0 < n <= 500_000:
            raise ValueError(f"名前の数 {n} が合いません")
        nbytes = r.u32()
        r.u64()                                      # hash の版
        r.raw(8 * n)
    else:
        n, nbytes = count, None
    # UE5 は頭（2 バイト）を全部並べてから文字。UE4.26-4.27 の IoStore は 1 つずつ頭と文字が交互（UTF-16 は 2 の倍数に揃える）。
    heads = [r.raw(2) for _ in range(n)] if count is None else [None] * n
    out = []
    used = 0
    for h in heads:
        if h is None:
            h = r.raw(2)
        utf16 = bool(h[0] & 0x80)
        ln = ((h[0] & 0x7F) << 8) | h[1]
        if utf16:
            if count is not None and r.p % 2:
                r.p += 1
            raw = r.raw(ln * 2)
            out.append(raw.decode("utf-16-le", "replace"))
            used += ln * 2
        else:
            raw = r.raw(ln)
            out.append(raw.decode("utf-8", "replace"))
            used += ln
    if nbytes is not None and used != nbytes:
        raise ValueError(f"名前の文字のバイト数 {used} が頭の {nbytes} と合いません")
    if any(any(ord(c) < 32 for c in x) for x in out[:200]):
        raise ValueError("名前に制御文字があります")
    return out


def _zen5(b: bytes, size: int) -> dict:
    r = _R(b)
    has_ver = r.u32()
    if has_ver not in (0, 1):
        raise ValueError("UE5 の zen の頭ではありません")
    hsize = r.u32()
    nidx, nnum = r.u32(), r.u32()
    flags = r.u32()
    r.u32()                                          # cooked の頭の大きさ
    hashes_off, import_off, export_off, bundle_off = r.i32(), r.i32(), r.i32(), r.i32()
    if not (0 < hashes_off <= import_off <= export_off <= bundle_off <= hsize <= size):
        raise ValueError("UE5 の zen の表の位置が合いません")
    base = r.p
    last = None
    for extra in (12, 4):                           # UE5.3 以降は 3 つ・それより前は 1 つ
        for cells in (0, 8):
            try:
                rr = _R(b, base + extra)
                ver = {}
                if has_ver:
                    ver["zen"] = rr.u32()
                    ver["ue4"], ver["ue5"], ver["lic"] = rr.i32(), rr.i32(), rr.i32()
                    ncv = rr.i32()
                    if not 0 <= ncv <= 2000:
                        raise ValueError("カスタム版の数が合いません")
                    rr.raw(20 * ncv)
                    ver["custom"] = ncv
                    if cells != (8 if ver["ue5"] >= 1015 else 0):
                        continue
                rr.p += cells
                names = _name_batch(rr)
                if rr.p > hashes_off:
                    raise ValueError("名前の束が表にかぶさります")
                return {"form": "UE5", "has_ver": bool(has_ver), "ver": ver, "hsize": hsize, "flags": flags,
                        "name": (nidx, nnum), "names": names, "import_off": import_off, "export_off": export_off,
                        "bundle_off": bundle_off, "newer": extra == 12}
            except (ValueError, struct.error) as e:
                last = e
    raise ValueError(f"UE5 の zen の名前の束を読めません: {last}")


def _zen4(b: bytes, size: int) -> dict:
    r = _R(b)
    nidx, nnum = r.u32(), r.u32()
    r.u32(); r.u32()                                 # 元の名前
    flags = r.u32()
    r.u32()
    names_off, names_size, hashes_off, hashes_size = r.i32(), r.i32(), r.i32(), r.i32()
    import_off, export_off, bundles_off, graph_off, graph_size = r.i32(), r.i32(), r.i32(), r.i32(), r.i32()
    if not (names_off >= 64 and hashes_size % 8 == 0 and import_off <= export_off <= bundles_off <= graph_off
            and graph_off + graph_size <= size and names_off + names_size <= size):
        raise ValueError("UE4 の IoStore の頭ではありません")
    n = hashes_size // 8 - 1
    names = _name_batch(_R(b, names_off), n)
    return {"form": "UE4.26-4.27", "has_ver": False, "ver": {}, "hsize": graph_off + graph_size, "flags": flags,
            "name": (nidx, nnum), "names": names, "import_off": import_off, "export_off": export_off,
            "bundle_off": bundles_off, "newer": False}


def zen(path: str, size: int, find: str = "") -> list[str] | None:
    with open(path, "rb") as fh:
        b = fh.read(min(size, 64 << 20))
    z = None
    for fn in (_zen5, _zen4):
        try:
            z = fn(b, size)
            break
        except (ValueError, struct.error):
            continue
    if z is None:
        return None
    names = z["names"]
    nm = names[z["name"][0]] if 0 <= z["name"][0] < len(names) else "?"
    n_imp = (z["export_off"] - z["import_off"]) // 8
    n_exp = (z["bundle_off"] - z["export_off"]) // 72
    v = z["ver"]
    out = [f"構造（Unreal Engine の zen 形式のパッケージ・{z['form']} の IoStore の中の形）:",
           f"  パッケージ名 {nm!r}・頭 {_mb(z['hsize'])}・" + "・".join(t for bit, t in _PKG_FLAGS if z["flags"] & bit),
           ("  版: UE4 " + str(v.get("ue4")) + (f"・UE5 {v['ue5']}" if v.get("ue5") else "") + f"・カスタム版 {v.get('custom', 0)} 個")
           if z["has_ver"] else "  版の情報なし（版なしで保存）" + ("・頭の並びは UE5.3 以降の形" if z["newer"] else ""),
           f"  名前 {len(names):,} 個・import {n_imp:,} 個・export {n_exp:,} 個"]
    exps = []
    for k in range(min(n_exp, 5000)):
        o = z["export_off"] + k * 72
        soff, ssz = struct.unpack_from("<QQ", b, o)
        ni, nn = struct.unpack_from("<II", b, o + 16)
        outer = struct.unpack_from("<Q", b, o + 24)[0]
        exps.append((_fname(names, ni, nn), ssz, outer == 0xFFFFFFFFFFFFFFFF))
    top = [e for e in exps if e[2]]
    if top:
        out.append(f"  パッケージ直下の export {len(top)} 個（名前 大きさ）:")
        out += [f"    {n}  {sz:,} バイト" for n, sz, _t in top[:_MAX_LIST]]
    if find:
        k = find.lower()
        out.append(f"  名前のうち「{find}」を含む: " + ", ".join(x for x in names if k in x.lower())[:2000])
    out.append("  名前（先頭 60・パスを除く）: " + ", ".join([x for x in names if not x.startswith("/")][:60]))
    out.append("  → zen 形式では型（クラス）が番号（global.utoc の表）で書かれていて、この形だけでは型の名前は出ません。"
               "型と値まで見るには FModel か `retoc to-legacy`（.pak 用の .uasset に戻す）。")
    return out


# ── 翻訳の表（.locres）──────────────────────────────────────────────────────

_LOCRES_VER = {0: "Legacy", 1: "Compact", 2: "Optimized（CRC32）", 3: "Optimized（CityHash64）"}


def locres(path: str, size: int, find: str = "") -> list[str]:
    with open(path, "rb") as fh:
        b = fh.read(min(size, 128 << 20))
    r = _R(b)
    ver = 0
    if b[:16] == LOCRES_MAGIC:
        r.p = 16
        ver = r.u8()
    strings: list[str] = []
    if ver >= 1:
        so = r.i64()
        if so != -1:
            sr = _R(b, so)
            for _ in range(sr.u32()):
                strings.append(sr.fstring(1 << 20))
                if ver >= 2:
                    sr.i32()
    if ver >= 2:
        r.u32()
    entries: list[tuple[str, str, str]] = []
    nns = r.u32()
    for _ in range(nns):
        if ver >= 2:
            r.u32()
        ns = r.fstring(4096)
        for _ in range(r.u32()):
            if ver >= 2:
                r.u32()
            key = r.fstring(4096)
            r.u32()                                      # 元の文の hash
            if ver >= 1:
                i = r.i32()
                text = strings[i] if 0 <= i < len(strings) else "?"
            else:
                text = r.fstring(1 << 20)
            entries.append((ns, key, text))
    out = ["構造（Unreal Engine の翻訳の表 .locres）:",
           f"  形式 {_LOCRES_VER.get(ver, ver)}・名前空間 {nns:,} 個・キー {len(entries):,} 個・文 {len(strings) or len(entries):,} 個"]
    pick = entries
    if find:
        k = find.lower()
        pick = [e for e in entries if k in e[0].lower() or k in e[1].lower() or k in e[2].lower()]
        out.append(f"  「{find}」を含む: {len(pick):,} 個")
    for ns, key, text in pick[:40]:
        t = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "⏎")
        out.append(f"    [{ns}] {key} = {t[:160]}{'…' if len(t) > 160 else ''}")
    if len(pick) > 40:
        out.append(f"    …ほか {len(pick) - 40:,} 個（find=<語> で絞れます）")
    return out


# ── 入口 ─────────────────────────────────────────────────────────────────────

def describe(path: str, head: bytes, size: int, find: str = "") -> list[str] | None:
    """UE の形なら構造の行。違えば None。"""
    low = path.lower()
    if low.endswith(".pak"):
        return pak(path, size, find)
    if head.startswith(TOC_MAGIC):
        return utoc(path, size, find)
    if low.endswith(".ucas"):
        toc = os.path.splitext(path)[0] + ".utoc"
        return ["構造（Unreal Engine の IoStore 本体 .ucas）:",
                f"  中身は圧縮のブロックの並びで、目録は {os.path.basename(toc)}"
                + ("（同じ場所にある＝そちらを Read）。" if os.path.exists(toc) else "（★同じ場所にありません）。")]
    if len(head) >= 4 and struct.unpack_from("<I", head, 0)[0] in (PKG_TAG, PKG_TAG_SWAPPED):
        return uasset(path, size, find)
    if head.startswith(LOCRES_MAGIC) or (low.endswith(".locres")):
        return locres(path, size, find)
    if low.endswith((".uexp", ".ubulk", ".uptnl")):
        ua = os.path.splitext(path)[0] + ".uasset"
        if not os.path.exists(ua):
            ua = os.path.splitext(path)[0] + ".umap"
        return [f"構造（Unreal Engine の {os.path.splitext(path)[1]}）:",
                "  パッケージの本体の続き（頭は同じ名前の .uasset/.umap）。"
                + (f"頭は {os.path.basename(ua)}＝そちらを Read すると export の一覧と位置が出ます。" if os.path.exists(ua)
                   else "★同じ名前の .uasset/.umap が同じ場所にありません。")]
    if low.endswith((".uasset", ".umap", ".uzenasset")) and len(head) >= 4:
        zz = zen(path, size, find)
        if zz:
            return zz
        return ["構造（Unreal Engine のパッケージ " + os.path.splitext(path)[1] + "）:",
                f"  ★頭の 4 バイトが {head[:4].hex(' ')}（{head[:4].decode('latin-1')!r}）で、パッケージの印 c1 83 2a 9e ではありません。",
                "  → ゲームが独自に暗号化・変形している形（例: Ace Combat 7 は 'ACE7' で始まる）。そのゲーム用の復号（UAssetAPI・"
                "FModel のゲーム別の設定）が要ります。壊れたファイルかどうかは、同じフォルダの他の .uasset と頭を比べると分かります"
                "（Read compare_with=<別のファイル>）。"]
    if low.endswith(".usmap") and head[:2] == b"\xc4\x30":
        return ["構造（UE の型の地図 .usmap）:", f"  版 {head[2]}・圧縮 {['なし', 'Oodle', 'Brotli', 'ZStandard'][head[3]] if head[3] < 4 else head[3]}",
                "  → FModel・UAssetGUI に渡すと、版なしで保存された項目の値が読めます。"]
    return None


def is_ue(path: str, head: bytes) -> bool:
    low = path.lower()
    return (low.endswith((".pak", ".ucas", ".utoc", ".uexp", ".ubulk", ".locres", ".usmap", ".uasset", ".umap", ".uzenasset"))
            or head.startswith(TOC_MAGIC)
            or (len(head) >= 4 and struct.unpack_from("<I", head, 0)[0] == PKG_TAG))
