"""よく出会うバイナリ形式の**中の構造**を読む（Read がバイナリに当たったとき・2026-10-09）。

オーナー「エージェントにある解析機能をさらに強化できませんか」。
それまでの Read は形式の印を見分けて名前を言うだけ（印が分かった物は「テキストではありません」のエラーとして
返していた）。走行記録（0.1.343・3 時間）では、エージェントが Unity のバンドルの生のバイト列を探して
「圧縮されているのか」で迷い続けた。ヘッダとブロックの一覧を読めば一目で分かる。
⇒ 形式ごとに、**判断に要る事実**（版・大きさ・圧縮の種類・中のファイル・壊れている所）を道具が読んで返す。

読むだけ。展開は小さな目録（Unity のブロック情報・ZIP の目録）に限り、本体は展開しない。
読めない・壊れているときは、そこまで読めた事実と理由を返す（黙って一般の扱いに落とさない）。
"""
from __future__ import annotations

import lzma
import os
import struct
import zipfile
import zlib

_MAX_LIST = 30


def _mb(n: int) -> str:
    return f"{n / 1048576:,.1f} MB" if n >= 1048576 else f"{n:,} バイト"


# ── LZ4 のブロック（Unity のブロック情報を読むだけに使う・小さい）──────────────

def lz4_block(src: bytes, out_size: int) -> bytes:
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n or len(dst) >= out_size:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off == 0 or start < 0:
            raise ValueError("LZ4 の参照が壊れています")
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            # ★重なる参照＝直前 off バイトの繰り返し。1 バイトずつ足すと 0 の連なり（Unity のデータに多い）で
            #   何十倍も遅くなる（2026-10-10・オブジェクトを読むためにバンドル全体を展開するようになった）。
            pat = bytes(dst[start:])
            dst += (pat * (ml // off + 1))[:ml]
    return bytes(dst[:out_size])


_FAST_LZ4 = None


def lz4_decode(src: bytes, out_size: int) -> bytes:
    """lz4 の拡張（C）があればそれで、無ければ上の Python で展開する（2026-10-10）。"""
    global _FAST_LZ4
    if _FAST_LZ4 is None:
        try:
            import lz4.block as _lb  # type: ignore
            _FAST_LZ4 = _lb
        except Exception:
            _FAST_LZ4 = False
    if _FAST_LZ4:
        try:
            return _FAST_LZ4.decompress(src, uncompressed_size=out_size)
        except Exception:
            pass
    return lz4_block(src, out_size)


def decode_block(raw: bytes, usz: int, kind: int) -> bytes:
    """UnityFS のデータのブロック 1 つを展開する。"""
    if kind == 0:
        return raw
    if kind in (2, 3):
        return lz4_decode(raw, usz)
    if kind == 1:
        return _lzma_unity(raw, usz)
    raise ValueError(f"圧縮の種類 {kind} は展開できません")


def _lzma_unity(src: bytes, out_size: int) -> bytes:
    props = src[0]
    lc, props = props % 9, props // 9
    lp, pb = props % 5, props // 5
    dict_size = int.from_bytes(src[1:5], "little")
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": lc, "lp": lp, "pb": pb}])
    return dec.decompress(src[5:], out_size)


_UNITY_COMP = {0: "無圧縮", 1: "LZMA", 2: "LZ4", 3: "LZ4HC", 4: "LZHAM"}


def _cstr(fh) -> str:
    b = bytearray()
    while len(b) < 256:
        c = fh.read(1)
        if not c or c == b"\x00":
            break
        b += c
    return b.decode("utf-8", "replace")


def bundle_layout(path: str, size: int) -> dict:
    """UnityFS の頭とブロック情報（データのブロック・中のファイル）。読めなかった所は "error" に理由を入れて返す。

    ★2026-10-10: オブジェクトを読む側（oc/unityobjects.py）も同じ並びを要るので、目録の行を作る所から分けた。
    """
    d: dict = {"blocks": [], "nodes": [], "kinds": {}, "error": None}
    with open(path, "rb") as fh:
        fh.read(8)                                   # "UnityFS\0"
        ver = struct.unpack(">I", fh.read(4))[0]
        player, engine = _cstr(fh), _cstr(fh)
        total, csize, usize, flags = struct.unpack(">qIII", fh.read(20))
        comp = flags & 0x3F
        at_end = bool(flags & 0x80)
        d.update(ver=ver, player=player, engine=engine, total=total, csize=csize, usize=usize, comp=comp,
                 at_end=at_end)
        if ver >= 7:
            pos = fh.tell()
            fh.seek((pos + 15) // 16 * 16)
        hdr_end = fh.tell()
        if at_end:
            fh.seek(size - csize)
        raw = fh.read(csize)
        data_start = hdr_end + (0 if at_end else csize)
        if flags & 0x200:                            # BlockInfoNeedPaddingAtStart
            data_start = (data_start + 15) // 16 * 16
        d["data_start"] = data_start
    if usize > 64 * 1024 * 1024 or csize > 64 * 1024 * 1024:
        d["error"] = "ブロック情報が大きすぎるので読んでいません"
        return d
    try:
        if comp == 0:
            info = raw
        elif comp in (2, 3):
            info = lz4_block(raw, usize)
        elif comp == 1:
            info = _lzma_unity(raw, usize)
        else:
            d["error"] = "ブロック情報の圧縮の種類を展開できません（ここまで）"
            return d
    except Exception as e:  # noqa: BLE001
        d["error"] = f"ブロック情報を展開できません: {e}（暗号化・独自の加工の可能性もある）"
        return d
    try:
        p = 16                                       # 未圧縮データのハッシュ
        nblocks = struct.unpack_from(">i", info, p)[0]
        p += 4
        kinds: dict[str, int] = {}
        usum = csum = 0
        blocks: list[tuple[int, int, int, int]] = []
        for _ in range(nblocks):
            u, c, f = struct.unpack_from(">IIH", info, p)
            p += 10
            k = _UNITY_COMP.get(f & 0x3F, f"種類 {f & 0x3F}")
            kinds[k] = kinds.get(k, 0) + 1
            blocks.append((data_start + csum, c, u, f & 0x3F))
            usum += u
            csum += c
        nnodes = struct.unpack_from(">i", info, p)[0]
        p += 4
        nodes = []
        for _ in range(nnodes):
            off, sz, nf = struct.unpack_from(">qqI", info, p)
            p += 20
            e = info.index(b"\x00", p)
            nodes.append((info[p:e].decode("utf-8", "replace"), off, sz))
            p = e + 1
    except Exception as e:  # noqa: BLE001
        d["error"] = f"ブロック情報の中身を読めません: {e}（独自の加工の可能性）"
        return d
    d.update(blocks=blocks, nodes=nodes, kinds=kinds, usum=usum, csum=csum)
    return d


def unityfs(path: str, size: int) -> list[str]:
    out = ["構造（UnityFS＝Unity のアセットバンドル）:"]
    lay = bundle_layout(path, size)
    total, comp = lay["total"], lay["comp"]
    out.append(f"  形式の版 {lay['ver']}・Unity {lay['engine']}（{lay['player']}）・全体 {total:,} バイト"
               + ("" if total == size else f"（★実際の大きさ {size:,} と合わない＝途中で切れている可能性）"))
    out.append(f"  ブロック情報: {lay['csize']:,} → {lay['usize']:,} バイト（{_UNITY_COMP.get(comp, f'不明な種類 {comp}')}）・"
               + ("ファイルの末尾にある" if lay["at_end"] else "ヘッダの直後にある"))
    if lay["error"]:
        out.append("  " + lay["error"])
        return out
    blocks, nodes, kinds, data_start = lay["blocks"], lay["nodes"], lay["kinds"], lay["data_start"]
    nblocks, nnodes = len(blocks), len(nodes)
    kinds_s = "・".join(f"{k} {v:,}" for k, v in sorted(kinds.items(), key=lambda x: -x[1]))
    out.append(f"  データのブロック: {nblocks:,} 個（{kinds_s}）・圧縮後 {_mb(lay['csum'])} → 展開すると {_mb(lay['usum'])}")
    out.append(f"  中のファイル: {nnodes} 個")
    for name, off, sz in nodes[:_MAX_LIST]:
        out.append(f"    {name}  （展開後の位置 {off:,}・{_mb(sz)}）")
    if nnodes > _MAX_LIST:
        out.append(f"    …ほか {nnodes - _MAX_LIST} 個")
    packed = sum(v for k, v in kinds.items() if k != "無圧縮")
    if packed:
        out.append(f"  → データのブロック {nblocks:,} 個のうち {packed:,} 個が圧縮されています。ファイルの生のバイト列を探しても"
                   "圧縮後の形しか無く、短い文字列は一部そのまま見えても、すぐ後ろの数値やヘッダは崩れています。"
                   "中身は UnityPy などで読み込めば展開して扱えます（ブロックを手で解く必要はありません）。")
    else:
        out.append("  → データは無圧縮です。生のバイト列をそのまま探せます"
                   + (f"（データはファイルの {data_start:,} バイト目から）" if data_start else "") + "。")
    if any(n.lower().endswith((".ress", ".resource")) for n, _o, _s in nodes):
        out.append("  ・.resS / .resource は Texture2D・AudioClip・動画などの本体（アセットからは位置で参照される）。"
                   "中身は UnityPy でアセットを読めば一緒に取り出せる。")
    # ★中のアセット（CAB）の目録（oc/unityfile.py・2026-10-09 オーナー「Unity 関連も頼みます」）。
    #   ★2026-10-10: 台本（MonoScript）が同じバンドルの別のファイルにあっても名前を引く（oc/unityobjects.py）。
    try:
        from . import unityfile as _uf
        from . import unityobjects as _uo
        import time as _time

        ws = _uo.Workspace.from_bundle(path, lay)
        deadline = _time.time() + _uf.TIME_BUDGET
        try:
            if ws.labels:
                out.append("  中のアセットの目録:")
            for name in ws.labels[:8]:
                if _time.time() > deadline:
                    out.append("  （時間の上限で、残りのアセットは読んでいません）")
                    break
                out += ws.summarize(name, deadline=deadline)
            if ws.decoded_bytes():
                out.append(f"  （目録のために {_mb(ws.decoded_bytes())} ぶんのブロックを展開した）")
            out.append("  " + _uo.HOWTO)
        finally:
            ws.close()
    except Exception as e:  # noqa: BLE001
        out.append(f"  中のアセットの目録を読めません: {type(e).__name__}: {e}")
    return out


# ── ZIP（docx / xlsx / jar / apk も同じ形）──────────────────────────────────

def zip_listing(path: str) -> list[str]:
    out = ["構造（ZIP 書庫）:"]
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except Exception as e:  # noqa: BLE001
        return out + [f"  目録を読めません: {e}"]
    files = [i for i in infos if not i.is_dir()]
    enc = sum(1 for i in files if i.flag_bits & 0x1)
    methods: dict[str, int] = {}
    for i in files:
        m = {0: "無圧縮", 8: "deflate", 12: "bzip2", 14: "LZMA", 93: "zstd", 99: "AES"}.get(i.compress_type,
                                                                                         f"方式 {i.compress_type}")
        methods[m] = methods.get(m, 0) + 1
    out.append(f"  ファイル {len(files):,} 個・展開すると {_mb(sum(i.file_size for i in files))}・"
               + "・".join(f"{k} {v}" for k, v in methods.items())
               + (f"・★暗号化 {enc} 個" if enc else ""))
    names = {i.filename for i in files}
    for sig, what in (("word/document.xml", "Word 文書（docx）"), ("xl/workbook.xml", "Excel（xlsx）"),
                      ("ppt/presentation.xml", "PowerPoint（pptx）"), ("AndroidManifest.xml", "Android アプリ（apk）"),
                      ("META-INF/MANIFEST.MF", "Java の書庫（jar）")):
        if sig in names:
            out.append(f"  中身の形: {what}")
            break
    for i in files[:_MAX_LIST]:
        out.append(f"    {i.filename}  （{i.file_size:,} バイト）")
    if len(files) > _MAX_LIST:
        out.append(f"    …ほか {len(files) - _MAX_LIST} 個")
    return out


# ── Windows の実行ファイル（PE）─────────────────────────────────────────────

_PE_MACHINE = {0x14C: "x86（32 ビット）", 0x8664: "x64", 0xAA64: "ARM64", 0x1C4: "ARMv7"}
_PE_SUBSYS = {2: "画面あり（GUI）", 3: "コンソール", 10: "EFI"}


def pe(path: str, size: int) -> list[str]:
    out = ["構造（PE＝Windows の実行ファイル / DLL）:"]
    with open(path, "rb") as fh:
        head = fh.read(4096)
        lfanew = struct.unpack_from("<I", head, 0x3C)[0] if len(head) >= 0x40 else 0
        if lfanew + 24 > len(head):
            fh.seek(lfanew)
            head = head[:lfanew] + fh.read(4096)
    if head[lfanew:lfanew + 4] != b"PE\x00\x00":
        return out + ["  PE の印が見つかりません（MZ だけの古い形式か、壊れている）"]
    machine, nsec, stamp, _, _, optsize, chars = struct.unpack_from("<HHIIIHH", head, lfanew + 4)
    opt = lfanew + 24
    magic = struct.unpack_from("<H", head, opt)[0]
    plus = magic == 0x20B
    subsys = struct.unpack_from("<H", head, opt + 68)[0]
    dd = opt + (112 if plus else 96)
    clr = struct.unpack_from("<II", head, dd + 14 * 8) if dd + 15 * 8 <= len(head) else (0, 0)
    import datetime as _dt
    when = (_dt.datetime.fromtimestamp(stamp, _dt.timezone.utc).strftime("%Y-%m-%d")
            if 0 < stamp < 4102444800 else f"0x{stamp:08x}")
    out.append(f"  {'DLL' if chars & 0x2000 else '実行ファイル'}・{_PE_MACHINE.get(machine, hex(machine))}・"
               f"{'PE32+' if plus else 'PE32'}・{_PE_SUBSYS.get(subsys, f'subsystem {subsys}')}・"
               f"{'.NET（マネージド）' if clr[0] else 'ネイティブ'}・ビルドの日付 {when}（偽装されることがある）")
    sec = opt + optsize
    last_end = 0
    rows = []
    for k in range(nsec):
        o = sec + k * 40
        if o + 40 > len(head):
            break
        name = head[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
        vsize, _va, rsize, rptr = struct.unpack_from("<IIII", head, o + 8)
        last_end = max(last_end, rptr + rsize)
        rows.append(f"    {name:<8}  ファイル上 {rsize:,} バイト・読み込み後 {vsize:,} バイト")
    out.append(f"  セクション {nsec} 個:")
    out += rows
    if last_end and size > last_end + 512:
        out.append(f"  ★末尾にセクションの外のデータ {_mb(size - last_end)}（インストーラ・自己展開・同梱データでよくある形）")
    if clr[0]:
        # ★型・項目・メソッドの名前を道具が読む（oc/dotnetmeta.py・2026-10-10）。走行記録では DLL の中の文字列を
        #   自作の台本で拾って型の名前を探していた。
        try:
            from . import dotnetmeta as _dm
            out += _dm.summary(path)
        except Exception as e:  # noqa: BLE001
            out.append(f"  .NET のメタデータを読めません: {type(e).__name__}: {e}")
            out.append("  → .NET の実行ファイルは ILSpy（ilspycmd）でソースに戻して読めます。")
    elif os.path.basename(path).lower() == "gameassembly.dll":
        out.append("  → Unity の IL2CPP のゲーム本体。型と項目の名前は <Game>_Data/il2cpp_data/Metadata/global-metadata.dat と"
                   "この DLL から Il2CppDumper か Cpp2IL で書き出せます（DummyDll を作れば Read の find で項目を見られる）。")
    return out


# ── 音声: FMOD の FSB5・RIFF/WAV ──────────────────────────────────────────

_FSB_MODE = {1: "PCM8", 2: "PCM16", 3: "PCM24", 4: "PCM32", 5: "PCM float", 6: "GC ADPCM", 7: "IMA ADPCM",
             8: "VAG", 9: "HEVAG", 10: "XMA", 11: "MPEG", 12: "CELT", 13: "AT9", 14: "XWMA", 15: "Vorbis",
             16: "FADPCM", 17: "Opus"}


def fsb5(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(64)
    ver, nsamp, shs, nts, ds, mode = struct.unpack_from("<IIIIII", h, 4)
    return ["構造（FSB5＝FMOD の音声バンク）:",
            f"  版 {ver}・音 {nsamp:,} 本・符号化 {_FSB_MODE.get(mode, f'mode {mode}')}・音のデータ {_mb(ds)}"
            f"・名前の表 {'あり' if nts else 'なし'}",
            "  → vgmstream-cli で 1 本ずつ WAV に戻せます（`vgmstream-cli -S 0 -o \"out/?n.wav\" <file>`）。"]


def riff(path: str, size: int) -> list[str]:
    out = []
    with open(path, "rb") as fh:
        h = fh.read(12)
        kind = h[8:12].decode("ascii", "replace")
        out.append(f"構造（RIFF・{kind}）:")
        chunks = []
        fmt = None
        data_len = 0
        while len(chunks) < 40:
            ch = fh.read(8)
            if len(ch) < 8:
                break
            cid, clen = ch[:4].decode("ascii", "replace"), struct.unpack("<I", ch[4:])[0]
            chunks.append(f"{cid}({clen:,})")
            if cid == "fmt ":
                fmt = fh.read(min(clen, 40))
                fh.seek(clen - len(fmt) + (clen & 1), 1)
            else:
                if cid == "data":
                    data_len = clen
                fh.seek(clen + (clen & 1), 1)
    out.append("  チャンク: " + " ".join(chunks))
    if kind == "WAVE" and fmt and len(fmt) >= 16:
        tag, chn, rate, bps, _align, bits = struct.unpack_from("<HHIIHH", fmt)
        name = {1: "PCM", 3: "PCM float", 2: "MS ADPCM", 0x11: "IMA ADPCM", 0x55: "MP3", 0xFFFE: "拡張"}.get(tag, hex(tag))
        secs = data_len / bps if bps else 0
        out.append(f"  {name}・{chn} ch・{rate:,} Hz・{bits} bit・長さ 約 {secs:,.1f} 秒")
    return out


# ── PNG（チャンクと CRC。壊れた所を名指しする）────────────────────────────

def png(path: str, size: int) -> list[str]:
    out = ["構造（PNG）:"]
    rows = []
    bad = 0
    with open(path, "rb") as fh:
        sig = fh.read(8)
        if sig != b"\x89PNG\r\n\x1a\n":
            return out + ["  先頭 8 バイトが PNG の印と違う: " + sig.hex(" ")]
        n = 0
        while n < 200:
            pos = fh.tell()
            h = fh.read(8)
            if len(h) < 8:
                rows.append(f"    （{pos:,} バイト目でファイルが終わった・IEND が無い）")
                bad += 1
                break
            ln, ctype = struct.unpack(">I", h[:4])[0], h[4:]
            if ln > size:
                rows.append(f"    {pos:,}: 長さ {ln:,} がファイルより大きい＝ここから壊れている（種類の欄 {ctype.hex(' ')}）")
                bad += 1
                break
            body = fh.read(ln)
            crc = fh.read(4)
            ok = len(crc) == 4 and zlib.crc32(ctype + body) & 0xFFFFFFFF == struct.unpack(">I", crc)[0]
            name = ctype.decode("ascii", "replace")
            extra = ""
            if name == "IHDR" and len(body) >= 13:
                w, hgt, depth, color = struct.unpack(">IIBB", body[:10])
                extra = f" {w}×{hgt}・{depth} bit・色の種類 {color}"
            if not ok:
                bad += 1
            if n < _MAX_LIST or not ok:
                rows.append(f"    {pos:,}: {name}（{ln:,}）{'' if ok else ' ★CRC が合わない'}{extra}")
            n += 1
            if name == "IEND":
                tail = size - fh.tell()
                if tail > 0:
                    rows.append(f"    ★IEND の後ろに {tail:,} バイト（付け足されたデータ）")
                break
    out += rows
    out.append("  → 壊れた所は無い" if not bad else f"  → 壊れた所 {bad} か所（上の ★）")
    return out


# ── gzip・tar ────────────────────────────────────────────────────────────

def gzip_info(path: str, size: int) -> list[str]:
    out = ["構造（gzip 圧縮）:"]
    with open(path, "rb") as fh:
        h = fh.read(10)
        flags, mtime = h[3], struct.unpack("<I", h[4:8])[0]
        name = ""
        if flags & 0x04:
            xlen = struct.unpack("<H", fh.read(2))[0]
            fh.seek(xlen, 1)
        if flags & 0x08:
            b = bytearray()
            while len(b) < 512:
                c = fh.read(1)
                if not c or c == b"\x00":
                    break
                b += c
            name = b.decode("latin-1")
        fh.seek(size - 4)
        isize = struct.unpack("<I", fh.read(4))[0]
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(mtime, _dt.timezone.utc).strftime("%Y-%m-%d") if mtime else "記録なし"
    out.append(f"  元の名前 {name or '（記録なし）'}・日付 {when}・展開すると約 {_mb(isize)}（4GB を超えると正しくない値）")
    if path.lower().endswith((".tar.gz", ".tgz")):
        out += tar_listing(path)[1:]
        return out
    out += _gzip_peek(path)
    return out


def _gzip_peek(path: str) -> list[str]:
    """展開した頭を見る（2026-10-10・セーブデータが gzip の中に JSON や BinaryFormatter を持つ形が走行記録にあった）。"""
    from . import nrbf as _nb
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_nb.MAX_BYTES)
        d = zlib.decompressobj(31)
        head = d.decompress(raw, 65536)
    except Exception as e:  # noqa: BLE001
        return [f"  展開できません: {e}（gzip の頭の後ろが暗号化・加工されている可能性）"]
    if _nb.looks_nrbf(head):
        try:
            full = zlib.decompressobj(31).decompress(raw, _nb.MAX_BYTES)
        except Exception:  # noqa: BLE001
            full = head
        return ["  展開した中身は .NET の BinaryFormatter:"] + ["  " + ln for ln in _nb.describe(full)[1:]]
    txt = None
    try:
        txt = head[:2000].decode("utf-8")
    except UnicodeDecodeError:
        pass
    if txt and sum(c.isprintable() or c in "\r\n\t" for c in txt) >= len(txt) * 0.95:
        one = " ".join(txt.split())
        return [f"  展開した頭（文字）: {one[:400]}" + ("…" if len(one) > 400 else ""),
                "  → 中身を全部読むなら展開して書き出す（例: py -c \"import gzip,shutil;shutil.copyfileobj(gzip.open(r'IN'),"
                "open(r'OUT','wb'))\"）か、その場で gzip.open で読む。"]
    rows = describe_bytes(head)
    return ["  展開した頭 32 バイト: " + head[:32].hex(" ")] + (["  " + ln for ln in rows] if rows else [])


def describe_bytes(head: bytes) -> list[str] | None:
    """ファイルではなくバイト列の頭だけで分かる形（今は BinaryFormatter だけ）。"""
    from . import nrbf as _nb
    if _nb.looks_nrbf(head):
        return _nb.describe(head)
    return None


def tar_listing(path: str) -> list[str]:
    import tarfile
    out = ["構造（tar 書庫）:"]
    try:
        with tarfile.open(path) as tf:
            members = []
            for m in tf:
                members.append(m)
                if len(members) >= 5000:
                    break
    except Exception as e:  # noqa: BLE001
        return out + [f"  目録を読めません: {e}"]
    files = [m for m in members if m.isfile()]
    out.append(f"  ファイル {len(files):,} 個（{'5,000 個で打ち切り・' if len(members) >= 5000 else ''}合計 "
               f"{_mb(sum(m.size for m in files))}）・リンク {sum(1 for m in members if m.issym() or m.islnk())} 個")
    for m in files[:_MAX_LIST]:
        out.append(f"    {m.name}  （{m.size:,} バイト）")
    if len(files) > _MAX_LIST:
        out.append(f"    …ほか {len(files) - _MAX_LIST} 個")
    return out


# ── Linux の実行ファイル（ELF）────────────────────────────────────────────

_ELF_MACHINE = {0x3E: "x86-64", 0x03: "x86", 0xB7: "AArch64", 0x28: "ARM", 0xF3: "RISC-V"}
_ELF_TYPE = {1: "再配置可能（.o）", 2: "実行ファイル", 3: "共有ライブラリか PIE の実行ファイル", 4: "コアダンプ"}


def elf(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(64)
    bits = {1: "32 ビット", 2: "64 ビット"}.get(h[4], "?")
    endian = "<" if h[5] == 1 else ">"
    etype, machine = struct.unpack_from(endian + "HH", h, 16)
    return ["構造（ELF＝Linux などの実行ファイル）:",
            f"  {_ELF_TYPE.get(etype, f'種類 {etype}')}・{_ELF_MACHINE.get(machine, hex(machine))}・{bits}・"
            f"{'リトル' if endian == '<' else 'ビッグ'}エンディアン",
            "  → 依存は `ldd`、記号は `nm -D`、文字列は `strings` で見られます（Linux の場合）。"]


# ── Ogg ─────────────────────────────────────────────────────────────────

def ogg(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(512)
    nseg = h[26]
    body = h[27 + nseg:]
    out = ["構造（Ogg）:"]
    if body[1:7] == b"vorbis" and len(body) >= 16:
        ch, rate = body[11], struct.unpack_from("<I", body, 12)[0]
        out.append(f"  Vorbis の音声・{ch} ch・{rate:,} Hz")
    elif body[:8] == b"OpusHead" and len(body) >= 16:
        out.append(f"  Opus の音声・{body[9]} ch・元の {struct.unpack_from('<I', body, 12)[0]:,} Hz")
    elif body[1:7] == b"theora":
        out.append("  Theora の動画")
    else:
        out.append("  中の符号化を判定できません（先頭のページ: " + body[:8].hex(" ") + "）")
    return out


# ── Godot の .pck・Unreal の .pak（目録の位置まで）──────────────────────────

def godot_pck(path: str) -> list[str]:
    out = ["構造（Godot の .pck）:"]
    with open(path, "rb") as fh:
        h = fh.read(4 + 4 * 4)
        ver, ma, mi, pa = struct.unpack_from("<IIII", h, 4)
        if ver >= 2:
            flags, base = struct.unpack("<IQ", fh.read(12))
        else:
            flags, base = 0, 0
        fh.read(16 * 4)
        n = struct.unpack("<I", fh.read(4))[0]
        out.append(f"  形式の版 {ver}・Godot {ma}.{mi}.{pa}・ファイル {n:,} 個"
                   + ("・★目録が暗号化されている" if flags & 1 else ""))
        if flags & 1:
            return out
        for k in range(min(n, _MAX_LIST)):
            ln = struct.unpack("<I", fh.read(4))[0]
            if ln > 4096:
                out.append("  目録の読みが途中でずれました")
                break
            name = fh.read(ln).rstrip(b"\x00").decode("utf-8", "replace")
            off, sz = struct.unpack("<QQ", fh.read(16))
            fh.read(16 + (4 if ver >= 2 else 0))
            out.append(f"    {name}  （{sz:,} バイト）")
        if n > _MAX_LIST:
            out.append(f"    …ほか {n - _MAX_LIST} 個")
    out.append("  → 取り出しは GDRE Tools（gdsdecomp）。")
    return out


def unreal_pak(path: str, size: int) -> list[str] | None:
    """.pak は末尾に印（E1 12 6F 5A）がある。版と目録の位置だけ読む。"""
    with open(path, "rb") as fh:
        fh.seek(max(0, size - 512))
        tail = fh.read()
    i = tail.rfind(b"\xe1\x12\x6f\x5a")
    if i < 0 or i + 28 > len(tail):
        return None
    ver, idx_off, idx_size = struct.unpack_from("<IQQ", tail, i + 4)
    enc = tail[i - 1] if i >= 1 else None          # bEncryptedIndex は印の直前の 1 バイト（版 4 以降）
    return ["構造（Unreal Engine の .pak）:",
            f"  形式の版 {ver}・目録は {idx_off:,} バイト目から {idx_size:,} バイト"
            + ("・★目録が暗号化されている（鍵が要る）" if enc == 1 else ""),
            "  → 中身の一覧と取り出しは repak（`repak list <file>`）。UE5 の .utoc/.ucas は FModel か retoc。"]


# ── 入口 ─────────────────────────────────────────────────────────────────

def describe(path: str, head: bytes) -> list[str] | None:
    """分かる形式なら構造の行。分からなければ None（今までの扱いに任せる）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    try:
        # ★Unreal Engine とモデル（2026-10-11 オーナー「ゲーム解析の強化…Unity や UE も含めて。モデル解析系なども」）。
        #   旧の浅い読み（unreal_pak・formats_more の glb/fbx/moc3）より先に、目録・中身の数まで読む方へ回す。
        from . import uefile as _ue
        if _ue.is_ue(path, head):
            rows = _ue.describe(path, head, size)
            if rows:
                return rows
        from . import modelfile as _mf
        rows = _mf.describe(path, head, size)
        if rows:
            return rows
        if head.startswith(b"UnityFS\x00"):
            return unityfs(path, size)
        if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
            return zip_listing(path)
        if head.startswith(b"MZ"):
            return pe(path, size)
        if head.startswith(b"FSB5"):
            return fsb5(path)
        if head.startswith(b"RIFF") and len(head) >= 12:
            return riff(path, size)
        if head.startswith(b"\x89PNG"):
            return png(path, size)
        if head.startswith(b"\x1f\x8b"):
            return gzip_info(path, size)
        if len(head) > 262 and head[257:262] == b"ustar":
            return tar_listing(path)
        if head.startswith(b"\x7fELF"):
            return elf(path)
        if head.startswith(b"OggS"):
            return ogg(path)
        if head.startswith(b"GDPC"):
            return godot_pck(path)
        from . import nrbf as _nb
        if _nb.looks_nrbf(head):                     # .NET の BinaryFormatter（セーブデータ・設定）
            return _nb.describe_file(path)
        if path.lower().endswith(".pak"):
            return unreal_pak(path, size)
        from . import formats_more as _fm             # 動画・音声・フォント・PSD・テクスチャ・3D・moc3・書庫・エンジン・実行系
        more = _fm.describe_more(path, head, size)
        if more:
            return more
        from . import unityfile as _uf
        if _uf.looks_serialized(head, size):          # 単体の .assets・level0・globalgamemanagers など（印が無い）
            import time as _time
            from . import unityobjects as _uo
            ws = _uo.Workspace.from_file(path)        # ★台本が隣のファイル（globalgamemanagers.assets）でも名前を引く
            try:
                return (["構造（Unity のアセット＝SerializedFile）:"]
                        + ws.summarize(ws.labels[0], deadline=_time.time() + _uf.TIME_BUDGET) + ["  " + _uo.HOWTO])
            finally:
                ws.close()
    except Exception as e:  # noqa: BLE001 — 読めた所までで止める
        return [f"構造を読む途中で止まりました: {type(e).__name__}: {e}"]
    return None
