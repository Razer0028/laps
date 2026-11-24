"""さらに多くのファイル形式の中身を読む（oc/fileformats.describe から呼ぶ・2026-10-09）。

オーナー「様々なファイル形式の解析なども」。動画・音声・フォント・画像の元データ・テクスチャ・3D・Live2D・書庫・
ゲームのエンジンや音声ミドルウェアの書庫・実行系。どれも**頭（と必要なら目次）だけ**を読み、判断に要る事実を返す。
読めない・壊れているときは、そこまでの事実と理由を返す。中身の展開・復号はしない。
"""
from __future__ import annotations

import json
import os
import struct

_MAX = 30


def _mb(n: int) -> str:
    return f"{n / 1048576:,.1f} MB" if n >= 1048576 else f"{n:,} バイト"


def _secs(s: float) -> str:
    s = max(0.0, s)
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return (f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}") + f"（{s:,.1f} 秒）"


# ── 動画・音声のコンテナ（MP4 / MOV / M4A・ISO BMFF）──────────────────────────

def _boxes(fh, start: int, end: int, limit: int = 4000):
    pos, n = start, 0
    while pos + 8 <= end and n < limit:
        fh.seek(pos)
        h = fh.read(16)
        if len(h) < 8:
            return
        size, kind = struct.unpack(">I", h[:4])[0], h[4:8].decode("latin-1")
        hdr = 8
        if size == 1 and len(h) >= 16:
            size, hdr = struct.unpack(">Q", h[8:16])[0], 16
        elif size == 0:
            size = end - pos
        if size < hdr:
            return
        yield kind, pos, size, hdr
        pos += size
        n += 1


def mp4(path: str, size: int) -> list[str]:
    out = ["構造（MP4 / MOV＝ISO BMFF のコンテナ）:"]
    with open(path, "rb") as fh:
        fh.seek(8)
        brand = fh.read(4).decode("latin-1")
        top = list(_boxes(fh, 0, size))
        out.append(f"  ブランド {brand}・最上位の箱: " + " ".join(f"{k}({_mb(s)})" for k, _p, s, _h in top[:12]))
        moov = next(((p, s, h) for k, p, s, h in top if k == "moov"), None)
        if not moov:
            out.append("  ★moov（目次）がありません＝途中で切れた録画か、断片化した形式（fMP4）")
            return out
        p, s, h = moov
        tracks = []
        dur = None
        for k, bp, bs, bh in _boxes(fh, p + h, p + s):
            if k == "mvhd":
                fh.seek(bp + bh)
                v = fh.read(1)[0]
                fh.seek(bp + bh + (20 if v == 1 else 12))
                ts = struct.unpack(">I", fh.read(4))[0]
                d = struct.unpack(">Q" if v == 1 else ">I", fh.read(8 if v == 1 else 4))[0]
                dur = d / ts if ts else None
            if k == "trak":
                kind, codec, extra = "?", "?", ""
                stack = [(bp + bh, bp + bs)]
                while stack:
                    a, b = stack.pop()
                    for k2, p2, s2, h2 in _boxes(fh, a, b, 200):
                        if k2 in ("mdia", "minf", "stbl"):
                            stack.append((p2 + h2, p2 + s2))
                        elif k2 == "hdlr":
                            fh.seek(p2 + h2 + 8)
                            kind = fh.read(4).decode("latin-1")
                        elif k2 == "stsd":
                            fh.seek(p2 + h2 + 8 + 4)
                            codec = fh.read(4).decode("latin-1")
                            if kind == "vide":
                                fh.seek(p2 + h2 + 8 + 8 + 24)
                                w, hh = struct.unpack(">HH", fh.read(4))
                                extra = f" {w}×{hh}"
                tracks.append({"vide": "映像", "soun": "音声", "subt": "字幕", "text": "文字"}.get(kind, kind)
                              + f" {codec}{extra}")
        if dur is not None:
            out.append(f"  長さ {_secs(dur)}")
        out.append("  トラック: " + ("・".join(tracks) if tracks else "なし"))
    return out


# ── 音声 ─────────────────────────────────────────────────────────────────

_MP3_BR = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
           2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def mp3(path: str, size: int) -> list[str]:
    out = ["構造（MP3）:"]
    with open(path, "rb") as fh:
        h = fh.read(10)
        start = 0
        tags = {}
        if h[:3] == b"ID3":
            tsize = (h[6] << 21) | (h[7] << 14) | (h[8] << 7) | h[9]
            body = fh.read(min(tsize, 1 << 20))
            start = 10 + tsize
            p = 0
            while p + 10 <= len(body) and h[3] >= 3:
                fid = body[p:p + 4]
                fs = struct.unpack(">I", body[p + 4:p + 8])[0]
                if not fid.strip(b"\x00") or fs <= 0 or p + 10 + fs > len(body):
                    break
                if fid in (b"TIT2", b"TPE1", b"TALB"):
                    raw = body[p + 11:p + 10 + fs]
                    enc = body[p + 10]
                    try:
                        txt = raw.decode({0: "latin-1", 1: "utf-16", 2: "utf-16-be", 3: "utf-8"}.get(enc, "latin-1"))
                    except UnicodeDecodeError:
                        txt = ""
                    tags[{b"TIT2": "題", b"TPE1": "演者", b"TALB": "アルバム"}[fid]] = txt.strip("\x00")[:80]
                p += 10 + fs
            out.append(f"  ID3v2.{h[3]} のタグ {tsize:,} バイト" + ("・" + "・".join(f"{k} {v}" for k, v in tags.items())
                                                                 if tags else ""))
        fh.seek(start)
        buf = fh.read(65536)
    i = next((k for k in range(len(buf) - 4) if buf[k] == 0xFF and buf[k + 1] & 0xE0 == 0xE0), -1)
    if i < 0:
        return out + ["  音のフレームの頭が見つかりません"]
    b1, b2, b3 = buf[i + 1], buf[i + 2], buf[i + 3]
    ver = (b1 >> 3) & 3
    layer = 4 - ((b1 >> 1) & 3)
    br = _MP3_BR[1 if ver == 3 else 2][b2 >> 4] if (b2 >> 4) < 15 else 0
    sr = _MP3_SR.get(ver, [0, 0, 0])[(b2 >> 2) & 3] if ((b2 >> 2) & 3) < 3 else 0
    ch = "モノラル" if (b3 >> 6) == 3 else "ステレオ"
    vbr = b"Xing" in buf[i:i + 200] or b"VBRI" in buf[i:i + 200]     # ★"Info" は固定ビットレートの印（可変ではない）
    est = (size - start) * 8 / (br * 1000) if br else 0
    # Xing / Info の欄にフレーム数があれば、それで正確な長さを出す（1 フレーム＝MPEG-1 の Layer 3 は 1152 標本）
    for tag in (b"Xing", b"Info"):
        j = buf.find(tag, i, i + 200)
        if j >= 0 and j + 12 <= len(buf) and struct.unpack_from(">I", buf, j + 4)[0] & 1 and sr:
            frames = struct.unpack_from(">I", buf, j + 8)[0]
            per = 1152 if (ver == 3 or layer != 3) else 576
            est = frames * per / sr
            vbr = False if tag == b"Info" else vbr
            break
    out.append(f"  MPEG-{ {3: '1', 2: '2', 0: '2.5'}.get(ver, '?') } Layer {layer}・{br} kbps"
               f"{'（可変・最初のフレームの値）' if vbr else ''}・{sr:,} Hz・{ch}"
               + (f"・長さ {_secs(est)}" if est else ""))
    return out


def flac(path: str) -> list[str]:
    with open(path, "rb") as fh:
        fh.read(4)
        h = fh.read(4)
        b = fh.read(34)
    if len(b) < 18:
        return ["構造（FLAC）:", "  STREAMINFO を読めません"]
    x = int.from_bytes(b[10:18], "big")
    rate, ch, bits, total = x >> 44, ((x >> 41) & 7) + 1, ((x >> 36) & 31) + 1, x & ((1 << 36) - 1)
    return ["構造（FLAC）:", f"  {rate:,} Hz・{ch} ch・{bits} bit" + (f"・長さ {_secs(total / rate)}" if rate and total else "")]


def wwise_bank(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(16)
    ver, bid = struct.unpack_from("<II", h, 8)
    return ["構造（Wwise のサウンドバンク .bnk）:", f"  版 {ver}・バンクの ID {bid}",
            "  → 中の音（.wem）は vgmstream-cli で WAV に戻せます。名前は同じ場所の .txt / .json / .xml（SoundbanksInfo）にあることが多い。"]


def cri_awb(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(16)
    n = struct.unpack_from("<I", h, 8)[0]
    return ["構造（CRI の AWB＝AFS2 の音声の束）:", f"  音 {n:,} 本",
            "  → 名前は同じ名前の .acb にある。vgmstream-cli で .acb と並べて置けば名前つきで WAV に戻せます。"]


def cri_utf(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(64)
    size = struct.unpack_from(">I", h, 4)[0]
    return ["構造（CRI の @UTF 表＝ACB・CPK の目次）:", f"  表の大きさ {size:,} バイト",
            "  → .acb なら同じ名前の .awb が音の本体。vgmstream-cli で両方を並べて扱います。"]


def cri_hca(path: str, head: bytes) -> list[str]:
    masked = bytes(b & 0x7F for b in head[:4]) == b"HCA\x00" and head[:4] != b"HCA\x00"
    ver = struct.unpack_from(">H", head, 4)[0]
    out = ["構造（CRI の HCA 音声）:", f"  版 {ver >> 8}.{ver & 0xFF}" + ("・★印が隠されている（暗号化された HCA のことが多い）" if masked else "")]
    i = head.find(b"fmt\x00") if not masked else -1
    if i >= 0:
        ch = head[i + 4]
        rate = int.from_bytes(head[i + 5:i + 8], "big")
        out.append(f"  {ch} ch・{rate:,} Hz")
    return out


# ── フォント ─────────────────────────────────────────────────────────────

def font(path: str, head: bytes) -> list[str]:
    out = ["構造（フォント・TrueType / OpenType）:"]
    with open(path, "rb") as fh:
        base = 0
        if head[:4] == b"ttcf":
            n = struct.unpack_from(">I", head, 8)[0]
            out.append(f"  フォント集（TTC）・{n} 書体")
            base = struct.unpack_from(">I", head, 12)[0]
        fh.seek(base)
        h = fh.read(12)
        ntab = struct.unpack_from(">H", h, 4)[0]
        tabs = {}
        for k in range(min(ntab, 64)):
            e = fh.read(16)
            tabs[e[:4].decode("latin-1")] = struct.unpack_from(">II", e, 8)
        out.append(f"  {'CFF（OpenType）' if 'CFF ' in tabs or 'CFF2' in tabs else 'TrueType'} の輪郭・表 {ntab} 個")
        if "maxp" in tabs:
            fh.seek(tabs["maxp"][0] + 4)
            out.append(f"  字形 {struct.unpack('>H', fh.read(2))[0]:,} 個")
        if "name" in tabs:
            off, ln = tabs["name"]
            fh.seek(off)
            t = fh.read(min(ln, 1 << 20))
            cnt, so = struct.unpack_from(">HH", t, 2)
            names = {}
            for k in range(cnt):
                pid, eid, lid, nid, l2, o2 = struct.unpack_from(">HHHHHH", t, 6 + k * 12)
                if nid in (1, 2, 4) and nid not in names:
                    raw = t[so + o2:so + o2 + l2]
                    names[nid] = raw.decode("utf-16-be", "replace") if pid in (0, 3) else raw.decode("latin-1")
            if names:
                out.append(f"  名前: {names.get(4) or names.get(1, '')}（{names.get(2, '')}）")
    return out


# ── 画像の元データ・テクスチャ・3D・Live2D ─────────────────────────────────

_PSD_MODE = {0: "白黒 2 値", 1: "グレー", 2: "インデックス", 3: "RGB", 4: "CMYK", 7: "マルチチャンネル", 8: "ダブルトーン", 9: "Lab"}


def psd(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(26)
        ver, ch, hgt, wid, depth, mode = struct.unpack_from(">HxxxxxxHIIHH", h, 4)
        cml = struct.unpack(">I", fh.read(4))[0]
        fh.seek(cml, 1)
        irl = struct.unpack(">I", fh.read(4))[0]
        fh.seek(irl, 1)
        lmi = struct.unpack(">Q" if ver == 2 else ">I", fh.read(8 if ver == 2 else 4))[0]
        layers = None
        if lmi:
            fh.read(8 if ver == 2 else 4)
            layers = abs(struct.unpack(">h", fh.read(2))[0])
    return ["構造（Photoshop の " + ("PSB" if ver == 2 else "PSD") + "）:",
            f"  {wid:,}×{hgt:,}・{_PSD_MODE.get(mode, mode)}・{depth} bit・チャンネル {ch}"
            + (f"・レイヤー {layers} 枚" if layers is not None else "・レイヤー情報なし（1 枚絵）")]


_DXGI = {28: "RGBA8", 29: "RGBA8 sRGB", 71: "BC1", 72: "BC1 sRGB", 74: "BC2", 77: "BC3", 78: "BC3 sRGB", 80: "BC4",
         83: "BC5", 95: "BC6H", 98: "BC7", 99: "BC7 sRGB"}


def dds(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(148)
    hgt, wid = struct.unpack_from("<II", h, 12)
    mips = struct.unpack_from("<I", h, 28)[0]
    fourcc = h[84:88].decode("latin-1")
    fmt = fourcc
    if fourcc == "DX10" and len(h) >= 132:
        fmt = _DXGI.get(struct.unpack_from("<I", h, 128)[0], f"DXGI {struct.unpack_from('<I', h, 128)[0]}")
    elif not fourcc.strip("\x00"):
        fmt = f"非圧縮 {struct.unpack_from('<I', h, 88)[0]} bit"
    return ["構造（DDS テクスチャ）:", f"  {wid:,}×{hgt:,}・形式 {fmt}・ミップマップ {max(1, mips)} 段"]


def ktx(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(64)
    glfmt, wid, hgt = struct.unpack_from("<I", h, 28)[0], *struct.unpack_from("<II", h, 36)
    mips = struct.unpack_from("<I", h, 56)[0]
    return ["構造（KTX テクスチャ）:", f"  {wid:,}×{hgt:,}・glInternalFormat 0x{glfmt:04x}・ミップマップ {max(1, mips)} 段"]


def glb(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(20)
        ver, total = struct.unpack_from("<II", h, 4)
        jlen = struct.unpack_from("<I", h, 12)[0]
        js = fh.read(min(jlen, 8 << 20))
    out = ["構造（glTF のバイナリ .glb）:", f"  版 {ver}・全体 {_mb(total)}"]
    try:
        j = json.loads(js.decode("utf-8"))
        gen = (j.get("asset") or {}).get("generator", "")
        cnt = "・".join(f"{k} {len(j[k])}" for k in ("scenes", "nodes", "meshes", "materials", "textures",
                                                      "animations", "skins") if isinstance(j.get(k), list))
        out.append(f"  {cnt}" + (f"・作成 {gen}" if gen else ""))
    except Exception:  # noqa: BLE001
        out.append("  中の JSON を読めません")
    return out


def fbx(path: str, head: bytes) -> list[str]:
    ver = struct.unpack_from("<I", head, 23)[0]
    return ["構造（FBX のバイナリ）:", f"  版 {ver}（{ver // 1000}.{(ver % 1000) // 100}）",
            "  → Blender（読み込み）や Assimp で開けます。"]


_MOC3_VER = {1: "3.0", 2: "3.3", 3: "4.0", 4: "4.2", 5: "5.0", 6: "5.3"}


def moc3(path: str, head: bytes, size: int) -> list[str]:
    v, e = head[4], head[5]
    return ["構造（Live2D Cubism の .moc3）:",
            f"  版 {v}（Cubism {_MOC3_VER.get(v, '?')} 以降で読める形）・{'ビッグ' if e else 'リトル'}エンディアン・{_mb(size)}",
            "  → 表示や部品の確認は Cubism Viewer か、同じ場所の .model3.json（テクスチャ・動き・物理の並び）を Read で。"]


# ── 書庫・ディスクの像 ───────────────────────────────────────────────────

def sevenzip(path: str, head: bytes) -> list[str]:
    major, minor = head[6], head[7]
    nxt_off, nxt_size = struct.unpack_from("<QQ", head, 12)
    return ["構造（7-Zip 書庫）:", f"  形式の版 {major}.{minor}・目次は {32 + nxt_off:,} バイト目から {nxt_size:,} バイト",
            "  → 中身の一覧は `7z l <file>`（Windows は 7-Zip を入れて 7z.exe）。"]


def rar(path: str, head: bytes) -> list[str]:
    v5 = head.startswith(b"Rar!\x1a\x07\x01\x00")
    return ["構造（RAR 書庫）:", f"  RAR {'5' if v5 else '4 以前'} の形式",
            "  → 中身の一覧は `7z l <file>` か `unrar l <file>`。"]


def ms_cab(path: str, head: bytes) -> list[str]:
    size, nfolders, nfiles = struct.unpack_from("<I", head, 8)[0], *struct.unpack_from("<HH", head, 26)
    return ["構造（Microsoft の CAB 書庫）:", f"  ファイル {nfiles:,} 個・フォルダ {nfolders}・{_mb(size)}",
            "  → 一覧は `expand -D <file>`（Windows）か `cabextract -l`。"]


def ole(path: str) -> list[str]:
    ext = os.path.splitext(path)[1].lower()
    what = {".msi": "Windows のインストーラ（MSI）", ".doc": "旧 Word 文書", ".xls": "旧 Excel", ".ppt": "旧 PowerPoint",
            ".msg": "Outlook のメール"}.get(ext, "OLE の複合文書（MSI・旧 Office 文書など）")
    return [f"構造（{what}）:", "  中は小さなファイルの束（ストリーム）。MSI なら `msiexec /a` の管理者展開か lessmsi で中身を出せます。"]


def iso(path: str) -> list[str] | None:
    with open(path, "rb") as fh:
        fh.seek(0x8000)
        pvd = fh.read(2048)
    if pvd[1:6] != b"CD001":
        return None
    vol = pvd[40:72].decode("latin-1").strip()
    blocks = struct.unpack_from("<I", pvd, 80)[0]
    return ["構造（ISO 9660 のディスクの像）:", f"  ボリューム名 {vol or '（なし）'}・{_mb(blocks * 2048)}",
            "  → Windows はダブルクリックでマウント、Linux は `7z l` か `isoinfo -l`。"]


# ── ゲームのエンジンの書庫 ───────────────────────────────────────────────

def renpy_rpa(path: str, head: bytes) -> list[str]:
    line = head.split(b"\n", 1)[0].decode("latin-1")
    return ["構造（Ren'Py の書庫 .rpa）:", f"  頭の行: {line[:60]}",
            "  → 目次は zlib で圧縮された Python の pickle。★pickle は開くと台本が動く形式なので自前で読み込まず、"
            "unrpa（`unrpa -l <file>`）で一覧を見ます。"]


def xp3(path: str, head: bytes) -> list[str]:
    off = struct.unpack_from("<Q", head, 11)[0]
    return ["構造（吉里吉里の書庫 .xp3）:", f"  目次の位置 {off:,} バイト目",
            "  → 一覧と取り出しは GARbro（Windows）や KrkrExtract。暗号化された物は鍵が要ります。"]


def rpgmv(path: str) -> list[str]:
    ext = os.path.splitext(path)[1].lower()
    what = {".rpgmvp": "画像", ".png_": "画像", ".rpgmvo": "音声（ogg）", ".ogg_": "音声（ogg）", ".rpgmvm": "音声（m4a）",
            ".m4a_": "音声（m4a）"}.get(ext, "素材")
    return ["構造（RPG ツクール MV/MZ の暗号化された素材）:",
            f"  中身は{what}。頭の 16 バイトが「RPGMV」の印で、その後ろの先頭部分が暗号化されている。",
            "  戻し方（位置は元のファイルで数える）: ① 頭の 16 バイト（RPGMV の印・オフセット 0〜15）を捨てる ② 次の 16 バイト"
            "（オフセット 16〜31＝元のファイルの 17〜32 バイト目）だけを、data/System.json の encryptionKey"
            "（32 桁の 16 進＝16 バイト）と 1 バイトずつ XOR する ③ オフセット 32（元のファイルの 33 バイト目）から先は"
            " XOR せずにそのまま書く（鍵を繰り返して全部に当てると、頭だけ PNG に見えて画像としては開けない）。",
            "  Python では: out = bytes(b ^ k for b, k in zip(d[16:32], key)) + d[32:]   （d はファイルの全バイト・key は 16 バイト）",
            "  戻した画像の頭は 89 50 4E 47 0D 0A 1A 0A（PNG）、音声は OggS になる。ならなければ鍵か手順が違う＝"
            "書き出した 1 つを Read で確かめてから全部を処理する。"]


def _ext_counts(names: list[str]) -> str:
    from collections import Counter
    c = Counter(os.path.splitext(n)[1].lower() or "（拡張子なし）" for n in names)
    return "・".join(f"{e} {k:,}" for e, k in c.most_common(10))


def vpk(path: str) -> list[str]:
    """Source エンジンの VPK（_dir.vpk の目次を読み、ファイル数と拡張子を数える）。"""
    with open(path, "rb") as fh:
        h = fh.read(28)
        ver, tree = struct.unpack_from("<II", h, 4)
        fh.seek(12 if ver == 1 else 28)
        data = fh.read(min(tree, 64 << 20))

    def cstr(pos: int) -> tuple[str, int]:
        end = data.index(b"\x00", pos)
        return data[pos:end].decode("utf-8", "replace"), end + 1

    names: list[str] = []
    pos = 0
    try:
        while pos < len(data):
            ext, pos = cstr(pos)
            if not ext:
                break
            while True:
                d, pos = cstr(pos)
                if not d:
                    break
                while True:
                    n, pos = cstr(pos)
                    if not n:
                        break
                    preload = struct.unpack_from("<H", data, pos + 4)[0]
                    pos += 18 + preload
                    names.append(f"{d.strip(' ')}/{n}.{ext}".lstrip("/"))
    except (ValueError, struct.error):
        pass
    out = ["構造（Source エンジンの VPK）:", f"  版 {ver}・目次 {tree:,} バイト・ファイル {len(names):,} 個",
           f"  拡張子: {_ext_counts(names)}"]
    out += [f"  {n}" for n in names[:12]]
    out.append("  → 取り出しは `pip install vpk` の `vpk -x out <file>_dir.vpk`（_000.vpk などの本体を同じ所に置く）か GCFScape。"
               "モデル .mdl は Crowbar で戻す。")
    return out


def bethesda_bsa(path: str, head: bytes) -> list[str]:
    ver, _off, flags, folders, files = struct.unpack_from("<IIIII", head, 4)
    game = {103: "Oblivion", 104: "Fallout 3/NV・Skyrim LE", 105: "Skyrim SE"}.get(ver, "?")
    return ["構造（Bethesda の書庫 .bsa）:", f"  版 {ver}（{game}）・フォルダ {folders:,}・ファイル {files:,}"
            + ("・圧縮あり" if flags & 0x4 else ""),
            "  → 一覧と取り出しは BSA Browser か bsarch（`bsarch unpack x.bsa out`）。"]


def bethesda_ba2(path: str, head: bytes) -> list[str]:
    ver = struct.unpack_from("<I", head, 4)[0]
    kind = head[8:12].decode("latin-1")
    files = struct.unpack_from("<I", head, 12)[0]
    what = {"GNRL": "一般（メッシュ・音など）", "DX10": "テクスチャ（DDS）", "GNMF": "PS4 テクスチャ"}.get(kind, kind)
    return ["構造（Bethesda の書庫 .ba2・Fallout 4／Starfield）:", f"  版 {ver}・種類 {what}・ファイル {files:,}",
            "  → 取り出しは BSA Browser、または公式の Archive2（Creation Kit 付属）。"]


def swf(path: str, head: bytes, size: int) -> list[str]:
    comp = {b"F": "圧縮なし", b"C": "zlib", b"Z": "LZMA"}[head[:1]]
    ver, full = head[3], struct.unpack_from("<I", head, 4)[0]
    return ["構造（Flash の SWF）:", f"  SWF 版 {ver}・{comp}・展開後 {_mb(full)}（ファイルは {_mb(size)}）",
            "  → 絵・音・ActionScript の取り出しは JPEXS Free Flash Decompiler"
            "（`ffdec-cli -export all out file.swf`）。動かすなら Ruffle。"]


def xnb(path: str, head: bytes) -> list[str]:
    plat = {b"w": "Windows", b"m": "Windows Phone", b"x": "Xbox 360", b"a": "Android", b"i": "iOS"}.get(head[3:4], "?")
    ver, flags = head[4], head[5]
    size = struct.unpack_from("<I", head, 6)[0]
    comp = "LZX 圧縮" if flags & 0x80 else "LZ4 圧縮" if flags & 0x40 else "圧縮なし"
    return ["構造（XNA／MonoGame の .xnb）:", f"  {plat}・XNB 版 {ver}・{comp}・{_mb(size)}",
            "  → xnbcli（`xnbcli unpack in out`）で PNG・WAV・JSON に戻す。Stardew Valley・Terraria など。"]


def re_engine_pak(path: str, head: bytes) -> list[str]:
    ver, files = struct.unpack_from("<II", head, 4)
    return ["構造（RE Engine の .pak＝KPKA）:", f"  版 {ver & 0xFFFF}・ファイル {files:,}",
            "  ★名前は持たず、パスの murmur3 ハッシュだけ。取り出しは REtool にそのゲームの名前の一覧（list ファイル）を渡す。"]


def cri_cpk(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(0x20)
    enc = h[0x10:0x14] != b"@UTF"
    return ["構造（CRI の書庫 .cpk）:", "  目次（@UTF 表）が" + ("隠されている（XOR されている）" if enc else "そのまま読める"),
            "  → 一覧と取り出しは CriFsLib.GUI（Windows）、YACpkTool、または QuickBMS の cpk 台本。"
            "中の .acb/.awb/.usm は vgmstream・ffmpeg で。"]


def unity_webdata(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(20)
        hdr = struct.unpack_from("<I", h, 16)[0]
        fh.seek(20)
        body = fh.read(min(hdr, 4 << 20) - 20)
    names, pos = [], 0
    while pos + 12 <= len(body):
        _o, sz, nl = struct.unpack_from("<III", body, pos)
        names.append((body[pos + 12:pos + 12 + nl].decode("utf-8", "replace"), sz))
        pos += 12 + nl
    out = ["構造（Unity WebGL の .data＝UnityWebData1.0）:", f"  中のファイル {len(names):,} 個"]
    out += [f"  {n}  {_mb(s)}" for n, s in names[:_MAX]]
    out.append("  → UnityPy／AssetRipper にこの .data をそのまま渡せる（data.unity3d・*.assets が入っている）。")
    return out


def asar(path: str) -> list[str]:
    with open(path, "rb") as fh:
        h = fh.read(16)
        n = struct.unpack_from("<I", h, 12)[0]
        raw = fh.read(min(n, 32 << 20))
    try:
        tree = json.loads(raw.decode("utf-8"))
    except ValueError:
        return ["構造（Electron の asar）:", "  目次の JSON を読めなかった"]
    names: list[str] = []

    def walk(node: dict, pre: str) -> None:
        for k, v in (node.get("files") or {}).items():
            if "files" in v:
                walk(v, pre + k + "/")
            else:
                names.append(pre + k)
    walk(tree, "")
    out = ["構造（Electron の asar）:", f"  ファイル {len(names):,} 個・拡張子: {_ext_counts(names)}"]
    out += [f"  {x}" for x in names[:12]]
    out.append("  → 展開は `npx @electron/asar extract app.asar out`。中身は素の JS/HTML（TyranoScript などもここ）。")
    return out


# ── 実行系 ───────────────────────────────────────────────────────────────

_PYC = {3413: "3.8", 3425: "3.9", 3439: "3.10", 3495: "3.11", 3531: "3.12", 3571: "3.13", 3603: "3.14"}


def pyc(path: str, head: bytes) -> list[str]:
    magic = struct.unpack_from("<H", head, 0)[0]
    ver = _PYC.get(magic) or next((v for m, v in sorted(_PYC.items()) if m >= magic), "?")
    return ["構造（Python のバイトコード .pyc）:", f"  Python {ver} 前後で作られた（印 {magic}）",
            "  → 元のソースに戻すのは pycdc / decompyle3（版によって対応が違う）。"]


def java_class(path: str, head: bytes) -> list[str]:
    minor, major = struct.unpack_from(">HH", head, 4)
    return ["構造（Java の class）:", f"  class の版 {major}.{minor}（Java {major - 44} 向け）",
            "  → 中身は `javap -c -p <file>` か、CFR / Fernflower でソースに戻せます。"]


def wasm(path: str, head: bytes) -> list[str]:
    return ["構造（WebAssembly）:", f"  版 {struct.unpack_from('<I', head, 4)[0]}",
            "  → 中身は `wasm-objdump -x` か `wasm2wat`（wabt）で見られます。"]


# ── 入口 ─────────────────────────────────────────────────────────────────

def describe_more(path: str, head: bytes, size: int) -> list[str] | None:
    ext = os.path.splitext(path)[1].lower()
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return mp4(path, size)
    if head.startswith(b"ID3") or (ext == ".mp3" and len(head) > 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return mp3(path, size)
    if head.startswith(b"fLaC"):
        return flac(path)
    if head.startswith(b"BKHD"):
        return wwise_bank(path)
    if head.startswith(b"AFS2"):
        return cri_awb(path)
    if head.startswith(b"@UTF"):
        return cri_utf(path)
    if len(head) >= 8 and bytes(b & 0x7F for b in head[:4]) == b"HCA\x00":
        return cri_hca(path, head)
    if head.startswith(b"CRID"):
        return ["構造（CRI の動画 .usm）:", "  → vgmstream（音）や ffmpeg（映像は VP9/H.264 が多い）で。暗号化された物は鍵が要ります。"]
    if head[:4] in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf") and ext in (".ttf", ".otf", ".ttc", ".otc", "") \
            or head[:4] in (b"OTTO", b"ttcf"):
        return font(path, head)
    if head.startswith(b"wOFF") or head.startswith(b"wOF2"):
        return ["構造（Web フォント WOFF" + ("2" if head[3:4] == b"2" else "") + "）:", "  → 中身は TrueType / OpenType を圧縮した物。"]
    if head.startswith(b"8BPS"):
        return psd(path)
    if head.startswith(b"DDS "):
        return dds(path)
    if head.startswith(b"\xabKTX 11\xbb"):
        return ktx(path)
    if head.startswith(b"glTF"):
        return glb(path)
    if head.startswith(b"Kaydara FBX Binary"):
        return fbx(path, head)
    if head.startswith(b"MOC3") and len(head) >= 6:
        return moc3(path, head, size)
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return sevenzip(path, head)
    if head.startswith(b"Rar!\x1a\x07"):
        return rar(path, head)
    if head.startswith(b"MSCF"):
        return ms_cab(path, head)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ole(path)
    if head.startswith(b"RPA-"):
        return renpy_rpa(path, head)
    if head.startswith(b"XP3\r\n \n\x1a\x8b\x67\x01"):
        return xp3(path, head)
    if head.startswith(b"RPGMV"):
        return rpgmv(path)
    if head.startswith(b"\x34\x12\xaa\x55") and len(head) >= 12:
        return vpk(path)
    if head.startswith(b"BSA\x00") and len(head) >= 24:
        return bethesda_bsa(path, head)
    if head.startswith(b"BTDX") and len(head) >= 16:
        return bethesda_ba2(path, head)
    if head[:3] in (b"FWS", b"CWS", b"ZWS") and len(head) >= 8 and 1 <= head[3] <= 60:
        return swf(path, head, size)
    if head.startswith(b"XNB") and len(head) >= 10 and head[3:4] in b"wmxai":
        return xnb(path, head)
    if head.startswith(b"KPKA") and len(head) >= 12:
        return re_engine_pak(path, head)
    if head.startswith(b"CPK "):
        return cri_cpk(path)
    if head.startswith(b"UnityWebData1.0\x00"):
        return unity_webdata(path)
    if ext == ".asar" and head.startswith(b"\x04\x00\x00\x00") and len(head) >= 16:
        return asar(path)
    if ext == ".pyc" and len(head) >= 4 and head[2:4] == b"\r\n":
        return pyc(path, head)
    if head.startswith(b"\xca\xfe\xba\xbe") and len(head) >= 8 and 45 <= struct.unpack_from(">H", head, 6)[0] <= 80:
        return java_class(path, head)
    if head.startswith(b"\x00asm"):
        return wasm(path, head)
    if size > 0x8800 and ext in (".iso", ".img", ""):
        return iso(path)
    return None
