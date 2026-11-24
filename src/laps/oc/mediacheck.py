"""作ったファイルが拡張子どおりの中身かを確かめる（2026-10-12）。

オーナーが見せた作業係の答え:「確認済み: 抽出されたファイルは正しい PNG 形式であり、正常に表示できることを確認しています」
→ 実際は画像ビューアで「サポートされていない形式」。スクリプトが終了 0 で終わったことを「確かめた」と数え（evidence）、
中身は一度も見ていなかった。声の係も同じ問いに答える道具が無く、作業係の報告を繰り返した。

ここは頭のバイトだけで決められることを返す（速い・読むだけ）。PNG は最初の区画（IHDR）の検査値（CRC）まで見る
（RPG ツクールの暗号は頭の 16 バイトだけを XOR するので、戻し方を誤ると印か IHDR が壊れる）。
"""
from __future__ import annotations

import os
import struct
import zlib

#: (頭の印, 名前, この中身の拡張子)
KINDS = (
    (b"\x89PNG\r\n\x1a\n", "PNG 画像", (".png",)),
    (b"\xff\xd8\xff", "JPEG 画像", (".jpg", ".jpeg")),
    (b"GIF8", "GIF 画像", (".gif",)),
    (b"BM", "BMP 画像", (".bmp",)),
    (b"RIFF", "RIFF（WAV / WebP / AVI）", (".wav", ".webp", ".avi")),
    (b"OggS", "Ogg 音声", (".ogg", ".opus")),
    (b"ID3", "MP3 音声", (".mp3",)),
    (b"\xff\xfb", "MP3 音声", (".mp3",)),
    (b"\xff\xf3", "MP3 音声", (".mp3",)),
    (b"fLaC", "FLAC 音声", (".flac",)),
    (b"%PDF", "PDF", (".pdf",)),
    (b"PK\x03\x04", "ZIP 書庫", (".zip", ".docx", ".xlsx", ".pptx", ".jar", ".apk")),
    (b"MZ", "Windows の実行ファイル / DLL", (".exe", ".dll")),
    (b"RPGMV", "RPG ツクール MV/MZ の暗号化ファイル（先頭が暗号化されたまま）",
     (".rpgmvp", ".rpgmvo", ".rpgmvm", ".png_", ".ogg_", ".m4a_")),
    (b"UnityFS", "Unity のアセットバンドル", (".bundle", ".unity3d", ".ab")),
    # ★2026-10-13: Unity の音声（AudioClip）は FMOD の FSB5。画像のデータから誤った位置で切り出した .fsb を音声と読み続けた走行への手当て。
    (b"FSB5", "FMOD の音声バンク（FSB5）", (".fsb", ".bank")),
    (b"FSB4", "FMOD の音声バンク（FSB4）", (".fsb",)),
    (b"\x1f\x8b", "gzip 圧縮", (".gz",)),
    (b"glTF", "glTF（.glb）", (".glb",)),
    (b"Kaydara FBX Binary", "FBX", (".fbx",)),
)

#: 確かめる意味のある「作った物」の拡張子（中身で正否が決まる物）。
MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ogg", ".opus", ".mp3", ".wav", ".flac", ".m4a", ".fsb",
              ".mp4", ".mov", ".pdf", ".zip", ".glb", ".fbx"}


#: ★作った物のうち、形が決まっていて読めるかで正否が分かるデータ（2026-10-13 オーナー「様々なタスクにも当てはめて」）。
#:   変換・設定・取り出しの作業で台本が書き出した JSON や XML が壊れていても、終了 0 なら気づかない。Write/Edit で書いた物は
#:   postedit の構文検査が見るが、台本が書いた物は見ていなかった。
DATA_EXTS = {".json", ".xml", ".toml", ".yaml", ".yml"}
#: 中身を確かめる拡張子の全部（作った物の確かめ・作業用のコンテナ の labhost・Read の添え書きが使う）。
CHECK_EXTS = MEDIA_EXTS | DATA_EXTS
DATA_MAX = 20_000_000


def _data_check(path: str, ext: str) -> tuple[bool | None, str]:
    """JSON・XML・TOML・YAML として読めるか（大きすぎる物と、読む部品が無い YAML は分からない＝None）。"""
    try:
        size = os.path.getsize(path)
        if size > DATA_MAX:
            return None, f"{ext[1:].upper()}（大きいので確かめていない）"
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return False, f"読めない（{e.__class__.__name__}）"
    if not raw.strip():
        return False, "空のファイル"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False, f"{ext[1:].upper()} のはずだが UTF-8 の文字として読めない（バイナリ・別の文字コード）"
    try:
        if ext == ".json":
            import json as _json
            _json.loads(text)
        elif ext == ".xml":
            import xml.etree.ElementTree as _et
            _et.fromstring(text)
        elif ext == ".toml":
            try:
                import tomllib as _toml
            except ImportError:
                return None, "TOML（読む部品が無い）"
            _toml.loads(text)
        else:
            try:
                import yaml as _yaml
            except ImportError:
                return None, "YAML（読む部品が無い）"
            list(_yaml.safe_load_all(text))
    except Exception as e:  # noqa: BLE001 — 読めない理由をそのまま言う
        return False, f"{ext[1:].upper()} として読めない（{str(e).splitlines()[0][:160]}）"
    return True, f"{ext[1:].upper()}（読める）"


def kind_of(head: bytes, ext: str) -> tuple[str, bool | None]:
    """(中身の名前, 拡張子と合っているか・分からなければ None)。"""
    for magic, name, exts in KINDS:
        if head.startswith(magic):
            if magic == b"RIFF" and len(head) >= 12:
                name = {b"WAVE": "WAV 音声", b"WEBP": "WebP 画像", b"AVI ": "AVI 動画"}.get(head[8:12], name)
                if ext in (".wav", ".webp", ".avi"):
                    return name, {".wav": b"WAVE", ".webp": b"WEBP", ".avi": b"AVI "}[ext] == head[8:12]
            return name, (ext in exts) if ext else None
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "MP4 / M4A / MOV", (ext in (".mp4", ".m4a", ".mov")) if ext else None
    if head[:64] and all(b in b"\t\r\n" or 32 <= b < 127 or b >= 0x80 for b in head[:64]):
        try:
            head[:64].decode("utf-8")
            return "文字のファイル", False if ext in MEDIA_EXTS else None
        except UnicodeDecodeError:
            pass
    if not head:
        return "空のファイル（0 バイト）", False if ext in MEDIA_EXTS else None
    return ("形式の分からないバイナリ（暗号化・圧縮・独自形式のどれか）",
            False if ext in MEDIA_EXTS else None)


#: 中身まで確かめる上限（これより大きいファイルは頭だけ見る）。
DEEP_MAX = 40_000_000
_BPP = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}     # PNG の色の型 → 1 画素の成分数


def _png_deep(path: str) -> tuple[bool, str] | None:
    """PNG を区画ごとに読み、検査値（CRC）・IEND・画素のデータ（IDAT の展開と大きさ）まで確かめる。
    大きすぎて読まないときは None。★2026-10-12 オーナー「モデル自身がこういった判断ができるように」: 頭の印と IHDR だけを
    見ていた（区画の長さや画素が壊れていても通る）。どこから壊れているかの位置を返す＝戻す範囲の誤りが分かる。"""
    try:
        if os.path.getsize(path) > DEEP_MAX:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return False, f"読めない（{e.__class__.__name__}）"
    pos, idat, w, h, depth, ctype, interlace = 8, [], 0, 0, 0, 0, 0
    seen_end = False
    while pos + 12 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        name = typ.decode("latin-1") if all(65 <= b <= 122 for b in typ) else typ.hex()
        if ln > len(data) - pos - 12:
            return False, (f"{pos} バイト目の区画「{name}」の長さ {ln:,} がファイルの残りより大きい"
                           f"（{pos} バイト目から先が壊れている）")
        body = data[pos + 8:pos + 8 + ln]
        crc = struct.unpack(">I", data[pos + 8 + ln:pos + 12 + ln])[0]
        if zlib.crc32(typ + body) & 0xFFFFFFFF != crc:
            return False, f"{pos} バイト目の区画「{name}」の検査値（CRC）が合わない（{pos} バイト目から先が壊れている）"
        if typ == b"IHDR" and ln >= 13:
            w, h = struct.unpack(">II", body[:8])
            depth, ctype, interlace = body[8], body[9], body[12]
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            seen_end = True
            break
        pos += 12 + ln
    if not seen_end:
        return False, f"終わりの区画 IEND が無い（{len(data):,} バイトで途中で切れている）"
    if not idat:
        return False, "画素のデータ（IDAT）が無い"
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return False, "画素のデータ（IDAT）を展開できない（圧縮されたデータが壊れている）"
    if not interlace and ctype in _BPP and depth:
        row = (w * _BPP[ctype] * depth + 7) // 8 + 1
        if len(raw) != row * h:
            return False, f"画素のデータの大きさが合わない（{len(raw):,} バイト・{w}×{h} なら {row * h:,} バイト）"
    return True, f"PNG 画像（{w}×{h}・区画の検査値と画素のデータまで確かめた）"


def _jpeg_deep(path: str) -> tuple[bool, str] | None:
    """JPEG の区切り（マーカー）を SOS まで辿り、終わりの印（FF D9）があるかを見る。"""
    try:
        if os.path.getsize(path) > DEEP_MAX:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return False, f"読めない（{e.__class__.__name__}）"
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return False, f"{pos} バイト目に区切りの印（FF）が無い（{pos} バイト目から先が壊れている）"
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        seg = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker == 0xDA:                      # 画素のデータの始まり（ここから先は区切りを辿らない）
            tail = data[-16:]
            if b"\xff\xd9" not in tail:
                return False, "JPEG の終わりの印（FF D9）が無い（途中で切れているか、後ろが壊れている）"
            return True, "JPEG 画像（区切りと終わりの印まで確かめた）"
        if seg < 2:
            return False, f"{pos} バイト目の区切りの長さがおかしい（{pos} バイト目から先が壊れている）"
        pos += 2 + seg
    return False, "JPEG の画素のデータ（SOS）まで辿れない（区切りが壊れている）"


def _ogg_crc_table() -> list[int]:
    t = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = ((r << 1) ^ 0x04C11DB7) if r & 0x80000000 else (r << 1)
        t.append(r & 0xFFFFFFFF)
    return t


_OGG_T: list[int] = []


def _ogg_deep(path: str, pages: int = 8) -> tuple[bool, str] | None:
    """Ogg の頭から数ページの検査値（CRC）を確かめる。"""
    global _OGG_T
    if not _OGG_T:
        _OGG_T = _ogg_crc_table()
    try:
        with open(path, "rb") as f:
            data = f.read(1_000_000)
    except OSError as e:
        return False, f"読めない（{e.__class__.__name__}）"
    pos = n = 0
    while n < pages and pos + 27 <= len(data):
        if data[pos:pos + 4] != b"OggS":
            return False, f"{pos} バイト目にページの印（OggS）が無い（{pos} バイト目から先が壊れている）"
        segs = data[pos + 26]
        if pos + 27 + segs > len(data):
            break
        size = 27 + segs + sum(data[pos + 27:pos + 27 + segs])
        if pos + size > len(data):
            break
        page = bytearray(data[pos:pos + size])
        want = struct.unpack("<I", bytes(page[22:26]))[0]
        page[22:26] = b"\0\0\0\0"
        crc = 0
        for b in page:
            crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_T[((crc >> 24) & 0xFF) ^ b]
        if crc != want:
            return False, f"{pos} バイト目の Ogg のページの検査値（CRC）が合わない（{pos} バイト目から先が壊れている）"
        pos += size
        n += 1
    return (True, f"Ogg 音声（頭の {n} ページの検査値まで確かめた）") if n else None


# ── MP3 ──────────────────────────────────────────────────────────────
# ★2026-10-13: 画像の圧縮データのコピーから「ID3」の 3 文字を頼りに切り出した 19 本（7〜137MB）が、頭の印だけの判定で
#   「MP3 音声」として通っていた（`file` は「ID3 version 2.51.0」＝存在しない版と出していた）。ID3v2 の頭が成り立つか
#   （版 2〜4・大きさの 4 バイトが 7 ビットずつ）と、その後ろに MPEG の音声フレームが 3 つ続くかまで見る。
_MP3_BR = {
    (3, 3): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),   # MPEG1 Layer I
    (3, 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),      # MPEG1 Layer II
    (3, 1): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),       # MPEG1 Layer III
    (2, 3): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),      # MPEG2/2.5 Layer I
    (2, 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),           # MPEG2/2.5 Layer II
    (2, 1): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),           # MPEG2/2.5 Layer III
}
_MP3_SR = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame_len(h: bytes) -> int | None:
    """MPEG 音声フレームの頭 4 バイトならフレームの長さ。そうでなければ None。"""
    if len(h) < 4 or h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    ver, layer = (h[1] >> 3) & 3, (h[1] >> 1) & 3
    bri, sri, pad = (h[2] >> 4) & 0xF, (h[2] >> 2) & 3, (h[2] >> 1) & 1
    if ver == 1 or layer == 0 or bri in (0, 15) or sri == 3:
        return None
    br = _MP3_BR[(3 if ver == 3 else 2, layer)][bri] * 1000
    sr = _MP3_SR[ver][sri]
    if layer == 3:
        return (12 * br // sr + pad) * 4
    if layer == 1 and ver != 3:
        return 72 * br // sr + pad
    return 144 * br // sr + pad


def _mp3_deep(path: str) -> tuple[bool, str] | None:
    """ID3v2 の頭が成り立ち、MPEG の音声フレームが 3 つ続くか。分からなければ None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            start = 0
            if head[:3] == b"ID3":
                major, rev, flags = head[3], head[4], head[5]
                size_b = head[6:10]
                if major not in (2, 3, 4) or rev == 0xFF or any(b >= 0x80 for b in size_b):
                    return False, (f"ID3 の印はあるが ID3v2 の頭として成り立たない（版 2.{major}.{rev}・大きさの欄が壊れている）"
                                   "＝MP3 ではない可能性が高い（画像や他のデータの中に「ID3」の 3 文字が偶然あっただけ）")
                tag = (size_b[0] << 21) | (size_b[1] << 14) | (size_b[2] << 7) | size_b[3]
                start = 10 + tag + (10 if flags & 0x10 else 0)
            f.seek(start)
            buf = f.read(65536)
    except OSError:
        return None
    i = 0
    while i < min(len(buf), 8192) and buf[i] == 0:            # タグの後ろの詰め物
        i += 1
    for off in range(i, min(len(buf) - 4, i + 4096)):
        n = _mp3_frame_len(buf[off:off + 4])
        if not n:
            continue
        a = off + n
        n2 = _mp3_frame_len(buf[a:a + 4])
        if n2 and _mp3_frame_len(buf[a + n2:a + n2 + 4]):
            return True, "MP3 音声（MPEG の音声フレームが続いている）"
    return False, "MP3 の印はあるが、MPEG の音声フレームが続いていない＝音声のデータではない可能性が高い"


# ── WAV（PCM）──────────────────────────────────────────────────────
# ★2026-10-13: 画像のデータ 512KB を ffmpeg に「生の PCM（-f s16le）」として読ませて WAV にし、音量の測りが -6.2 dB だったのを
#   「非常に有望」と読んだ走行があった。生の PCM としての読み込みはどんなバイトでも成功する。中身が音かどうかは、波形が 0 を
#   横切る割合（ゼロ交差率）でかなり分かる: その画像のデータは 0.38〜0.50（雑音）、声や音楽は平均でふつう 0.2 未満。
WAV_NOISE_ZCR = 0.3
WAV_SAMPLE_BYTES = 2_000_000


def _wav_deep(path: str) -> tuple[bool, str] | None:
    """PCM の WAV の中身が雑音に近いか（ゼロ交差率）。分からなければ None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
                return None
            fmt = None
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    return None
                cid, n = ch[:4], struct.unpack("<I", ch[4:8])[0]
                if cid == b"fmt ":
                    body = f.read(n + (n & 1))
                    if len(body) < 16:
                        return None
                    fmt = struct.unpack("<HHIIHH", body[:16])
                elif cid == b"data":
                    if not fmt:
                        return None
                    tag, chans, rate, _bps, _align, bits = fmt
                    if tag not in (1, 0xFFFE) or bits not in (8, 16) or chans < 1:
                        return None
                    raw = f.read(min(n, WAV_SAMPLE_BYTES))
                    break
                else:
                    f.seek(n + (n & 1), 1)
    except (OSError, struct.error):
        return None
    width = bits // 8
    step = width * chans
    if len(raw) < step * 2048:
        return None
    if bits == 16:
        vals = struct.unpack(f"<{len(raw) // 2}h", raw[: len(raw) // 2 * 2])[::chans]
    else:
        vals = [b - 128 for b in raw[::chans]]
    loud = [v for v in vals if abs(v) > (64 if bits == 16 else 1)]
    if len(loud) < len(vals) * 0.05:
        return None                                   # ほとんど無音＝判定しない
    z = sum(1 for a, c in zip(vals, vals[1:]) if (a < 0) != (c < 0)) / max(1, len(vals) - 1)
    if z > WAV_NOISE_ZCR:
        return False, (f"WAV（{rate} Hz・{bits} bit・{chans} ch）だが、中身が雑音に近い（ゼロ交差率 {z:.2f}・声や音楽は平均で"
                       "ふつう 0.2 未満）。音でないバイト（画像・圧縮されたデータ）を生の PCM として読んだ可能性が高い"
                       "（生の PCM としての読み込みは、どんなバイトでも成功し、音量も出る）。雨・風・ホワイトノイズのような効果音なら正常")
    return True, f"WAV 音声（{rate} Hz・{bits} bit・{chans} ch・ゼロ交差率 {z:.2f}）"


def check_file(path: str) -> tuple[bool | None, str]:
    """(拡張子どおりか・分からなければ None, 説明)。PNG・JPEG・Ogg は中身（検査値・区切り）まで確かめる。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in DATA_EXTS:
        return _data_check(path, ext)
    try:
        with open(path, "rb") as f:
            head = f.read(64)
            size = os.fstat(f.fileno()).st_size
            tail = b""
            if ext in (".jpg", ".jpeg") and size > 4:
                f.seek(max(0, size - 4))
                tail = f.read(4)
    except OSError as e:
        return False, f"読めない（{e.__class__.__name__}）"
    name, ok = kind_of(head, ext)
    if ok and ext == ".png":
        if len(head) < 33 or head[12:16] != b"IHDR":
            return False, ("PNG の印はあるが最初の区画が IHDR ではない（頭の 16 バイトが正しく戻っていない。RPG ツクール MV/MZ なら、"
                           "元のファイルの頭 16 バイト（RPGMV の印）を捨て、次の 16 バイト（元のファイルの 17〜32 バイト目）を "
                           "encryptionKey と XOR する）")
        crc = struct.unpack(">I", head[29:33])[0]
        if zlib.crc32(head[12:29]) & 0xFFFFFFFF != crc:
            # ★2026-10-12 の実機: 印の後ろを全部、鍵を繰り返して XOR していた（17 バイト目以降は暗号化されていない）。
            #   頭の 16 バイト（印と IHDR の名前）は合い、IHDR の中身（幅・高さ）から先が壊れる＝ここに来る。
            # ★位置は元のファイルで数える（「17 バイト目以降は XOR せず」を、作業係は元のファイルの 17 バイト目と読み、
            #   何も戻さない形に直そうとした・2026-10-12 14:18）。
            return False, ("PNG の印と IHDR の名前は合っているが、IHDR の中身（幅・高さ）の検査値が合わない＝暗号を戻す範囲が広すぎる"
                           "可能性が高い（RPG ツクール MV/MZ は、元のファイルの頭 16 バイト（RPGMV の印）を捨て、次の 16 バイト"
                           "（元のファイルの 17〜32 バイト目）だけを鍵で XOR し、元のファイルの 33 バイト目から先は XOR せずに"
                           "そのまま書き写す。Python: bytes(b ^ k for b, k in zip(d[16:32], key)) + d[32:]）")
        w, h = struct.unpack(">II", head[16:24])
        deep = _png_deep(path)
        if deep is not None:
            return deep
        return True, f"PNG 画像（{w}×{h}・大きいので頭だけ確かめた）"
    if ok and tail and not tail.endswith(b"\xff\xd9") and b"\xff\xd9" not in tail:
        return False, "JPEG の印はあるが終わりの印（FF D9）が無い（途中で切れている）"
    if ok and ext in (".jpg", ".jpeg"):
        deep = _jpeg_deep(path)
        if deep is not None:
            return deep
    if ok and ext in (".ogg", ".opus"):
        deep = _ogg_deep(path)
        if deep is not None:
            return deep
    if ok and ext == ".mp3":
        deep = _mp3_deep(path)
        if deep is not None:
            return deep
    if ok and ext == ".wav":
        deep = _wav_deep(path)
        if deep is not None:
            return deep
    return ok, name


def sample_check(paths: list[str], limit: int = 4) -> tuple[int, list[tuple[str, str]], list[tuple[str, str]]]:
    """作ったファイルの中から作った物（MEDIA_EXTS）を limit 本まで散らして選んで確かめる。
    (作った物の本数, 合わなかった (場所, 説明), 合った (場所, 説明))。"""
    media = [p for p in paths if os.path.splitext(p)[1].lower() in CHECK_EXTS]
    if not media:
        return 0, [], []
    step = max(1, len(media) // limit)
    picked = list(dict.fromkeys(media[::step][:limit] + media[-1:]))[:limit]
    bad, good = [], []
    for fp in picked:
        ok, why = check_file(fp)
        (bad if ok is False else good if ok else []).append((fp, why))
    return len(media), bad, good


def check_outputs(paths: list[str], since: float, limit: int = 12) -> tuple[int, list[tuple[str, str]]]:
    """答えに挙がった場所（ファイルかフォルダ）のうち since 以降に書かれた作った物を確かめる。
    (確かめた数, 合わなかった (場所, 説明) の並び)。古い物（この走行で書いていない）は見ない。"""
    picked: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            found = []
            n = 0
            for root, _dirs, files in os.walk(p):
                for name in files:
                    n += 1
                    if n > 20000:
                        break
                    if os.path.splitext(name)[1].lower() in CHECK_EXTS:
                        fp = os.path.join(root, name)
                        try:
                            if os.path.getmtime(fp) >= since - 1:
                                found.append(fp)
                        except OSError:
                            pass
                if n > 20000:
                    break
            if found:
                step = max(1, len(found) // limit)
                picked += found[::step][:limit]
        elif os.path.isfile(p) and os.path.splitext(p)[1].lower() in CHECK_EXTS:
            try:
                if os.path.getmtime(p) >= since - 1:
                    picked.append(p)
            except OSError:
                pass
    picked = list(dict.fromkeys(picked))[: limit * 2]
    bad = []
    for fp in picked:
        ok, why = check_file(fp)
        if ok is False:
            bad.append((fp, why))
    return len(picked), bad
