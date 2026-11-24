"""Read tool: read file with line numbers; image/PDF aware."""
from __future__ import annotations
import base64
import mimetypes
import os
from pathlib import Path

import pathlib

from . import pathfix as _pathfix
from . import ToolSpec
from . import limits
from ..policy import authorize_read, PolicyDenied


_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_PDF_EXTS = {".pdf"}

# 出力の上限。★ここで黙って切ると、モデルは「全部読めた」と思い込む。
# 実測（2026-08-11）: `blackwall/agent.py` の Read 出力はちょうど 50,000 文字 ＝
# 「... (N more lines)」の印はスライスの *前* に足されるので、大きいファイルでは
# 印ごと切り落とされていた。切ったことは必ず出力に書く。
_MAX_TEXT_CHARS = 50000
_MAX_PDF_CHARS = 80000


class BadArg(ValueError):
    """道具の引数が数として読めない。**呼び手が読める文で**返すために使う。"""


def int_arg(inp: dict, key: str, default: int) -> int:
    """`inp[key]` を整数として読む。読めなければ BadArg（`bash.py` / `grep.py` と共有）。

    ★なぜ要るか（2026-09-02）: 素の `int(inp.get("limit", 2000))` は、モデルが
      "all" や "2k" や 10.5 を渡した瞬間に例外になり、利用者に届くのは
      agent.py が包んだ
        `Error in Read: invalid literal for int() with base 10: 'all'`
      だけ。**どの引数の話かも、何を渡せばよいかも書かれていない。**
      同じ形は `notebook_edit.py` と `bash_output.py` で既に直されていた
      （そちらのコメントが同じ文言を引いている）＝**片方で直して片方に残した**。
    ★負の値も止める。`lines[-5:]` は例外を出さず**末尾から読む**ので、
      「読めたのに中身が違う」という、いちばん気づけない形になる。
    ★実測での出現は 0 件（走行 8 本・実機セッション）。頻度ではなく
      「起きたときに何も分からない」ことを直している。
    """
    v = inp.get(key, default)
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        raise BadArg(f"{key} は整数で指定してください（受け取った値: {v!r}）")
    if isinstance(v, float) and v != int(v):
        # ★`int(1.5)` は例外を出さず **1** になる。黙って切り下げると
        #   「頼んだ範囲と違うものが返ってきた」ことに誰も気づけない。
        raise BadArg(f"{key} は整数で指定してください（受け取った値: {v!r}）")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise BadArg(f"{key} は整数で指定してください（受け取った値: {v!r}）")
    if n < 0:
        raise BadArg(f"{key} に負の値は使えません（受け取った値: {n}）。"
                     f"先頭から読むなら offset=0、行数を絞るなら limit に正の数を。")
    return n


def _did_you_mean(path: str, limit: int = 5) -> str:
    """見つからなかったとき、**近い名前を添える**。

    ★実機で見えた形（2026-08-22・v0.1.79）: Read が **9回中9回**失敗した。
      `Glob(**)` で一覧を取っているのに、そこに無いパス（README.md /
      core/agent.py / frontend/ui.py …）を読もうとしていた。
      返るのは「file not found」だけなので、モデルは**次に何を読めばよいか
      分からず、また別の想像したパスを試す**。9手番が消えた。

    ★「無い」で終わらせず「では何があるか」を返す。行き止まりを訂正に変える。
    ★作業フォルダの中しか見ない。走査は打ち切る（大きな木で固まらないため）。
    """
    import difflib
    import os as _os
    try:
        ws = Path(_os.environ.get("LAPS_WORKSPACE") or _os.getcwd())
        if not ws.is_dir():
            return ""
        # ★比べる前に**同じ土俵に載せる**。最初は絶対パスと相対名を
        #   difflib に渡しており、当然どれも近くならなかった（実測で0件）。
        try:
            rel = str(Path(path).resolve().relative_to(ws.resolve()))
        except (ValueError, OSError):
            rel = path
        want = Path(path).name
        names: list[str] = []
        # ★件数だけでは止まらない（2026-09-06・owner 実機 P0）。ディレクトリが深く
        #   ファイルが少ない木（ゲームの入ったフォルダ・ネットワークドライブ）では
        #   `len(names) > 4000` に一度も届かないまま歩き続ける。しかもここは
        #   **Read が失敗するたびに**通る道で、実機の記録では「想像したパス」で
        #   2 回続けて失敗している＝2 回通っている。壁時計を足す。
        walk = limits.Walk()
        for root, dirs, files in _os.walk(ws):
            if walk.expired():
                break
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d not in
                       ("node_modules", "__pycache__", "venv", ".venv", "target")]
            for f in files:
                try:
                    names.append(str(Path(root, f).relative_to(ws)))
                except ValueError:
                    continue
            if len(names) > 4000:       # 走査の打ち切り（固まらせない）
                break
        if not names:
            return ""
        # まず同じファイル名、次に綴りの近いもの。
        same = [n for n in names if Path(n).name == want][:limit]
        near = difflib.get_close_matches(rel, names, n=limit, cutoff=0.5)
        hits: list[str] = []
        for n in same + near:
            if n not in hits:
                hits.append(n)
        if hits:
            return ("\n近いものがあります: " + " / ".join(hits[:limit])
                    + "\n（想像したパスを試さず、この中から選ぶか Glob で一覧を確認してください）")
        # ★綴りが近くないことのほうが多い。実機の失敗は core/agent.py を
        #   読もうとして core/inference_engine.py があった形で、名前は似ていない。
        #   そういうときは「その場所に何があるか」を返すほうが役に立つ。
        parent = Path(path).parent
        if parent.is_dir():
            here = sorted(x.name for x in parent.iterdir())[:12]
            if here:
                return (f"\n{parent} にあるのは: " + " / ".join(here)
                        + "\n（この中から選んでください）")
        top = sorted({n.split("/")[0].split("\\")[0] for n in names})[:12]
        return ("\n作業フォルダの直下にあるのは: " + " / ".join(top)
                + "\n（想像したパスを試さず、ここから辿ってください）")
    except Exception:
        return ""


def _text_cap() -> int:
    """1回の Read が返してよい文字数。**窓に対して相対**に決める（2026-08-19）。

    ★なぜ固定値ではいけないか: 50,000 文字は、窓 16,384 トークンの推論器では
      **窓そのものより大きい**。1回の Read で会話が入らなくなり、
      `context_exhausted` で走行が打ち切られる。実測で 95 走行中 13 本が
      文脈切れで止まっており、その一因がこれ（もう一因は生成余白で、別途修正済み）。
      `agent.py` の該当コメントも「本当の直しは道具の側」と書いていた。

    ★窓の 25% までとする。残り 75% は システム接頭辞・これまでの会話・生成余白に要る。
      1トークン≒2文字（日本語混在の保守側の見積り）で換算する。
      窓が分からないときは従来どおり 50,000（保守側に倒さない＝振る舞いを変えない）。

    ★切ったことは必ず出力に書き、**続きの読み方を名指しする**（既存の実装どおり）。
      黙って切ると「全部読めた」と思い込む。
    """
    # ★2026-09-13: ここは `limits.out_cap` の**写し**だった（`int(env) // 4 * 2` を直書き）。
    #   `limits.py` の docstring は「道具が返してよい量を**1箇所で決める**」と宣言している
    #   のに、**いちばん量を返す Read が唯一その 1 箇所を通っていなかった**。
    #   実害: 割合を可変にする `LAPS_OUT_CAP_FRACTION` を足しても Read には効かず、
    #   圧縮回数を下げる A/B が**同じ挙動どうしの比較**になっていた（気づかず 2 本測った）。
    #   ＝この木が繰り返す「写しは片方だけ直される」の実例。写しを消して 1 箇所に戻す。
    from . import limits
    return limits.out_cap(_MAX_TEXT_CHARS)


def _truncated(out: str, limit: int, how: str):
    """上限で切ったなら、切ったと書く。切っていないならそのまま。

    ★切ったときは **`ToolResult(truncated=True)` を返して名乗る**（2026-09-02）。
      それまで切り詰めは**本文の文言にしか出ていなかった**ので、記録の
      `truncated` 欄は 10,904 件すべて false（受け口の 100,000 字上限しか見ていない）。
      一方で本文に「切り詰め」と書かれた記録は 161 件ある。
      ＝**道具ごとの上限の効果を測る手段が無かった。**
      `ToolResult` は `str` の派生なので、呼び出し側は 1 行も変えなくてよい。
    """
    if len(out) <= limit:
        return out
    from . import ToolResult
    return ToolResult(out[:limit] + (
        f"\n… (ここで打ち切りました: 出力が上限 {limit} 文字を超えました"
        f"／残り約 {len(out) - limit} 文字。{how})"
    ), truncated=True)


def _image_b64_cap() -> int:
    """画像を base64 で埋め込んでよい文字数。既定は本文と同じ予算。

    ★環境変数 `LAPS_READ_IMAGE_MAX_B64` で上げられる（multimodal の
      バックエンドを繋いだときに、ここだけ緩められるように）。0 で埋め込み禁止。
    """
    env = os.environ.get("LAPS_READ_IMAGE_MAX_B64")
    if env and env.strip().lstrip("-").isdigit():
        return max(0, int(env))
    return _text_cap()


def _image_dims(p: Path) -> str:
    """PNG / GIF / JPEG の画素数（頭のバイトから読む。外部の道具は要らない）。分からなければ空。"""
    import struct
    try:
        with p.open("rb") as fh:
            h = fh.read(32)
            if h.startswith(b"\x89PNG\r\n\x1a\n") and h[12:16] == b"IHDR":
                w, hh = struct.unpack(">II", h[16:24])
                return f"pixels: {w}x{hh}\n"
            if h[:6] in (b"GIF87a", b"GIF89a"):
                w, hh = struct.unpack("<HH", h[6:10])
                return f"pixels: {w}x{hh}\n"
            if h[:2] == b"\xff\xd8":
                fh.seek(2)
                for _ in range(200):
                    b = fh.read(4)
                    if len(b) < 4 or b[0] != 0xFF:
                        break
                    seg = struct.unpack(">H", b[2:4])[0]
                    if 0xC0 <= b[1] <= 0xCF and b[1] not in (0xC4, 0xC8, 0xCC):
                        d = fh.read(5)
                        hh, w = struct.unpack(">HH", d[1:5])
                        return f"pixels: {w}x{hh}\n"
                    fh.seek(seg - 2, 1)
    except Exception:
        pass
    return ""


def _read_image(p: Path) -> str:
    """画像の素性を返す。base64 は**予算に収まるときだけ**載せる。

    ★直したのはここ（2026-09-02）。旧実装は data URI を**上限なしで丸ごと**返していた。
      Read のテキスト経路には窓に対する予算（`_text_cap`）があり、PDF にも上限が
      あるのに、**画像だけ一つも無かった**。1MB の PNG は base64 で 1.4MB＝
      窓 16,384 トークンの推論器では 1 回の Read で会話が入らなくなる
      （この現場で走行が打ち切られる最大の原因が `context_exhausted`）。

    ★さらに、その base64 を**読む相手がこの製品の中に一人もいない**
      （`grep -rn "data:image" src/laps/` は 0 件。エンジンは text-only の
      llama.cpp）。＝作って渡しているが繋がっていない出力だった。
      能力は残す（env で上げれば従来どおり全部載る）が、既定では
      「何のファイルか」と**次の一手**を返す。
    """
    try:
        mt, _ = mimetypes.guess_type(p.name)
        mt = mt or "image/png"
        size = p.stat().st_size
        cap = _image_b64_cap()
        need = (size + 2) // 3 * 4          # base64 にしたときのおよその長さ
        head = (f"[image:{p.name}]\n"
                f"path: {p}\n"
                f"mime: {mt}\n"
                f"bytes: {size:,}\n"
                f"{_image_dims(p)}"
                # ★2026-09-21 友人の実機: Read が画像の素性しか返していないのに、モデルは返答に
                #   「成績（画像より）… 最終 Equity 約 1,100,000〜1,200,000（目視）」と**見たように書いた**。
                #   旧文は「base64 は予算を超えるので載せていません」と**仕組みの話**をしていて、
                #   肝心の「あなたには見えていない」を言っていなかった。事実を先に、平たく言う。
                "★**この画像の中身は、あなたには見えていません。** いまの推論機は画像を読めないので、"
                "分かるのは上のファイルの事実だけです。**何が写っているか・グラフの形や数値は分かりません。"
                "見たように書かないこと**（利用者には「画像は読めない」と伝える）。\n"
                "→ 数値が要るなら、この画像を**作ったスクリプトや元データ**（CSV / JSON / ログ）を探して読むか、"
                "スクリプトを実行して出力を見てください。\n")
        if cap <= 0 or need > cap:
            return head
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        return head + f"---\ndata:{mt};base64,{b64}\n"
    except Exception as e:
        return f"Error reading image {p}: {e}"


def _read_pdf(p: Path, offset: int = 0, limit: int = 20) -> str:
    """Extract text from PDF via pypdf if available; otherwise note absence."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return (
            f"[pdf:{p.name}] pypdf not installed.\n"
            f"Install with: pip install pypdf\n"
            f"Fallback: use Bash with pdftotext if available."
        )
    try:
        reader = PdfReader(str(p))
        npages = len(reader.pages)
        start = max(0, offset)
        end = min(npages, start + limit)
        chunks = [f"[pdf:{p.name} pages {start+1}-{end}/{npages}]"]
        for i in range(start, end):
            try:
                txt = reader.pages[i].extract_text() or ""
            except Exception as e:
                txt = f"(extract failed: {e})"
            chunks.append(f"\n--- page {i+1} ---\n{txt}")
        out = "\n".join(chunks)
        return _truncated(out, _MAX_PDF_CHARS, "offset/limit でページ範囲を狭めて読み直してください")
    except Exception as e:
        return f"Error reading PDF {p}: {e}"


# 同名の探索でディレクトリを歩きすぎない上限。★大きな木で読み取りが遅くなると、
# 「安全のための機能が仕事を遅くする」になって外されるため。
_TWIN_SCAN_MAX = 4000
_TWIN_REPORT_MAX = 3


def _decode(p) -> str:
    """中身を文字にする。**符号化を決め打ちしない。**

    ★2026-08-14 実機の画像で判明: Windows のログ（cp932）を UTF-8 として読み、
    `INFO:root:\ufffdX\ufffdN\ufffd\ufffd\ufffdC\ufffds\ufffd\ufffd\ufffdO` のような
    文字化けが**そのまま回答に載っていた**。サイドカーは PYTHONUTF8=1 を渡すので、
    既定は常に UTF-8 になり、日本語 Windows のファイルはまず化ける。

    順に試し、**置換文字（U+FFFD）が最も少ないもの**を採る。
    ★「最初に例外を出さなかったもの」では駄目: cp932 は多くのバイト列を
      例外なしに読めてしまうので、化けたまま通る。
    """
    return decode_bytes(p.read_bytes())


def decode_bytes(raw: bytes) -> str:
    """バイト列を文字にする。`_decode` の実体で、**`bash.py` と共有している**。

    ★2026-08-16 に切り出した。`cmd.exe` の出力も同じ問題を持っているのに
    `tools/bash.py` は `encoding=` 指定なしで読んでおり、日本語のエラーが全部
    U+FFFD に潰れていた。実機の記録にあった文字化けが
    「指定されたパスが見つかりません。」の cp932 を UTF-8 として読んだものと
    **バイト単位で一致**することを確認済み。Windows で最も多い失敗
    （パスが無い・拒否された・使用中）の文面が全部読めない＝**頻度ではなく種類の問題**。

    ★同じ判定を2箇所に書き写すと、必ず片方だけ直る。実際に read.py だけが直り、
      bash.py が2日間そのままだった（コミット e38e9a0 は agent+read のみ）。
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    # ★末尾で文字が切れているだけ（書きかけのログ・途中で切った出力）なら UTF-8 のまま読む（2026-10-16 の点検: 最後の 1 字が
    #   途中だっただけで全体が latin-1 になり、全行が `å\x87¦ç…` に化けていた）。
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.reason == "unexpected end of data" and e.start >= len(raw) - 3:
            return raw[:e.start].decode("utf-8")
    for enc in ("cp932", "euc-jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    # ★1 つの符号化で読めない＝混ざっている（Windows で python の UTF-8 の出力の後に cmd の cp932 のエラーが続く・壊れたバイトが
    #   少しある）。行ごとに UTF-8 → cp932 で読み、読めない行だけ置換文字にする（2026-10-16 の点検: 旧形は全体を latin-1 にして
    #   両方を化けさせた）。ほとんどが読めないときだけ、旧形どおり latin-1 で全体を返す（文字でない物）。
    lines = raw.splitlines(keepends=True)
    parts, bad = [], 0
    for ln in lines:
        for enc in ("utf-8", "cp932"):
            try:
                parts.append(ln.decode(enc))
                break
            except UnicodeDecodeError:
                pass
        else:
            u, c = ln.decode("utf-8", errors="replace"), ln.decode("cp932", errors="replace")
            parts.append(u if u.count("\ufffd") <= c.count("\ufffd") else c)
            bad += 1
    if lines and bad <= max(1, len(lines) // 3):
        return "".join(parts)
    best, best_bad = None, None
    for enc in ("utf-8", "cp932", "euc-jp", "latin-1"):
        try:
            t = raw.decode(enc)
            return t                     # 例外なく完全に読めたら、それが正解
        except UnicodeDecodeError:
            t = raw.decode(enc, errors="replace")
        bad = t.count("\ufffd")
        if best_bad is None or bad < best_bad:
            best, best_bad = t, bad
        if bad == 0:
            break
    return best if best is not None else raw.decode("utf-8", errors="replace")


def _same_name_elsewhere(p) -> list:
    """作業ディレクトリの中に、同じ名前の別ファイルがあるか。

    ★見つけて黙っているのが一番悪い。読んだ瞬間に言う。
    """
    import os
    from ..policy import _workspace  # 遅延 import（読み取りの度に policy を引かない）
    try:
        root = _workspace()
        p = p.resolve()
        if root not in p.parents and root != p.parent:
            return []          # 作業ディレクトリの外は探さない
        name = p.name
        found, seen = [], 0
        # ★`seen`（ファイル数）だけでは止まらない。ディレクトリばかりで
        #   ファイルの少ない木では `_TWIN_SCAN_MAX` に届かない（上の `_near_paths`
        #   と同じ形）。こちらは**読み取りが成功するたび**に通る道なので、
        #   固まると「読めたのに返ってこない」になる。
        walk = limits.Walk()
        for dirpath, dirnames, filenames in os.walk(root):
            if walk.expired():
                break
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", "node_modules", ".venv")]
            seen += len(filenames)
            if name in filenames:
                q = pathlib.Path(dirpath) / name
                if q.resolve() != p:
                    found.append(q)
                    if len(found) >= _TWIN_REPORT_MAX:
                        return found
            if seen > _TWIN_SCAN_MAX:
                break
        return found
    except Exception:
        return []               # 探索で読み取りを失敗させない


#: 中身から見分ける（拡張子は当てにならない。実測の works.db は .db だが、
#: モデルは拡張子なしの一時ファイルも読んでいた）。
_MAGIC = (
    (b"SQLite format 3\x00", "SQLite のデータベース",
     'sqlite3 <file> ".tables" で表を、sqlite3 <file> "select * from <表> limit 5" で中身を見る'),
    (b"\x89PNG", "PNG 画像", "Read はそのまま画像として扱えます（この関門に来たのは別の理由）"),
    (b"PK\x03\x04", "ZIP 書庫", "unzip -l <file> で一覧、unzip -p <file> <中のパス> で中身"),
    (b"\x1f\x8b", "gzip 圧縮", "zcat <file> | head で中身"),
    (b"\x7fELF", "ELF 実行ファイル", "file <file> / strings <file> | head で素性を見る"),
    (b"%PDF", "PDF", "Read は PDF に対応しています（この関門に来たのは別の理由）"),
)


#: 魔法数の表に無いが、素性だけは言えるもの（開け方は OS で違うので `_binary_next` に任せる）。
_MAGIC_NAMES = (
    (b"MZ", "Windows の実行ファイル / DLL（PE 形式）"),
    (b"OggS", "Ogg コンテナ（音声・動画）"),
    (b"RIFF", "RIFF コンテナ（WAV / AVI / WebP）"),
    (b"\xff\xd8\xff", "JPEG 画像"),
    (b"GIF8", "GIF 画像"),
    (b"7z\xbc\xaf", "7-Zip 書庫"),
    (b"Rar!", "RAR 書庫"),
    (b"UnityFS", "Unity のアセットバンドル"),
    (b"\xca\xfe\xba\xbe", "Java の class / Mach-O（fat）"),
    # ★圧縮形式（2026-10-05）。test2 の走行で、モデルは識別バイトの無いファイルを
    #   「LZ4 だろう」と決めて時間を使い切った。印があるかどうかは事実として言える。
    (b"\x28\xb5\x2f\xfd", "zstd 圧縮"),
    (b"\xfd7zXZ\x00", "xz 圧縮"),
    (b"BZh", "bzip2 圧縮"),
    (b"\x04\x22\x4d\x18", "LZ4（フレーム形式）圧縮"),
    (b"\x78\x01", "zlib 圧縮の可能性（印が 2 バイトだけなので偶然もありうる）"),
    (b"\x78\x5e", "zlib 圧縮の可能性（印が 2 バイトだけなので偶然もありうる）"),
    (b"\x78\x9c", "zlib 圧縮の可能性（印が 2 バイトだけなので偶然もありうる）"),
    (b"\x78\xda", "zlib 圧縮の可能性（印が 2 バイトだけなので偶然もありうる）"),
)


def _entropy(blob: bytes) -> float:
    """バイトの偏り（シャノンのエントロピー・ビット/バイト・0〜8）。"""
    import math
    if not blob:
        return 0.0
    counts = [0] * 256
    for b in blob:
        counts[b] += 1
    n = len(blob)
    return -sum((c / n) * math.log2(c / n) for c in counts if c)


def _entropy_facts(blob: bytes, size: int, known: bool) -> list[str]:
    """エントロピーと大きさから**言える範囲だけ**を書く（2026-10-05）。

    ★結論は出さない。暗号化か圧縮かを決めるのはモデル。ここは「形式の印が無い」
      「分布がほぼ一様」「大きさが 16 の倍数」という観測と、その観測と矛盾しない形式を並べる。
    """
    if len(blob) < 256:
        return []
    e = _entropy(blob)
    out = [f"  バイトの偏り: エントロピー {e:.3f} ビット/バイト（最大 8.000・先頭 {len(blob):,} バイトで計算）"]
    if e >= 7.95:
        if known:
            out.append("    ＝ほぼ一様。上の素性（圧縮・書庫など）と矛盾しない")
        else:
            out.append("    ＝ほぼ一様で、先頭に圧縮形式の印（gzip・zlib・zstd・xz・bzip2・LZ4 フレーム・ZIP）が無い。"
                       "暗号文、またはヘッダの無い圧縮（LZ4 ブロック・生の deflate など）と矛盾しない")
            if size and size % 16 == 0:
                out.append(f"    大きさ {size:,} は 16 の倍数＝AES などの 16 バイト単位のブロック暗号と矛盾しない"
                           "（その場合、先頭 16 バイトが IV のことが多い）")
    elif e >= 7.0:
        out.append("    ＝偏りが小さい。圧縮された部分か、詰め込まれたバイナリを含む")
    else:
        out.append("    ＝偏りがある。構造を持つバイナリ（実行ファイル・独自の保存形式・表など）")
    return out


#: Windows での開け方（`tar` は Windows 10 以降に標準で入っている bsdtar。zip も読める）。
_MAGIC_HOW_WIN = {
    b"PK\x03\x04": "Bash で `tar -tf <file>` で一覧、`tar -xf <file> -C <展開先>` で展開（Windows 10 以降に標準）",
    b"\x1f\x8b": "Bash で `tar -tzf <file>`（tar.gz のとき）。単体の .gz は Python の gzip で開く",
    b"SQLite format 3\x00": ("Python があれば Bash で "
                            "`python -c \"import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
                            "print(c.execute('select name from sqlite_master').fetchall())\" <file>`"),
    b"\x89PNG": "Read はそのまま画像として扱えます（この関門に来たのは別の理由）",
    b"%PDF": "Read は PDF に対応しています（この関門に来たのは別の理由）",
}


def _binary_facts(p: Path, head: bytes) -> str:
    """バイナリについて**いま言える事実**（大きさ・先頭のバイト列・中の読める文字列）。

    ★2026-09-21 owner 実機（Windows）: `Gameexe.dat` と `Config.exe` を Read したら
      「`file` で素性を確かめ…それ以外は `strings`」と返していた。**Windows に file も strings も無い。**
      モデルは言われたとおり `strings` を打って失敗した（＝私の案内が行き止まりを指していた）。
      goose は切り詰めの案内を OS で出し分ける（developer/shell.rs `truncation_notice`）。
      ここではもう一歩進めて、**`file` と `strings | head` の結果に当たるものを Read 自身が返す**
      ——OS を問わず、往復が 1 回減る。
    """
    import re as _re
    lines = []
    try:
        size = p.stat().st_size
        lines.append(f"  大きさ: {size:,} バイト")
    except OSError:
        size = 0
    known = False
    for magic, what in _MAGIC_NAMES:
        if head.startswith(magic):
            lines.append(f"  素性: {what}")
            known = True
            break
    lines.append("  先頭 32 バイト: " + head[:32].hex(" "))
    try:
        with p.open("rb") as fh:
            blob = fh.read(1024 * 1024)
    except OSError:
        blob = head
    found: list[str] = []
    seen: set[str] = set()
    for m in _re.finditer(rb"[\x20-\x7e]{6,}", blob):
        t = m.group().decode("ascii")
        if t not in seen:
            seen.add(t); found.append(t)
        if len(found) >= 40:
            break
    if len(found) < 40:                      # Windows の実行ファイルは UTF-16LE の文字列が多い
        for m in _re.finditer(rb"(?:[\x20-\x7e]\x00){6,}", blob):
            t = m.group().decode("utf-16-le")
            if t not in seen:
                seen.add(t); found.append(t)
            if len(found) >= 40:
                break
    if found:
        lines.append(f"  中の読める文字列（先頭 1MB から最大 40 本・6 字以上）:")
        lines += [f"    {t[:100]}" for t in found]
    else:
        # ★結論はエントロピーの行に任せる（2026-10-05）。偏りの大きい独自形式でも
        #   6 字以上の文字列が無いことはあり、「圧縮か暗号化」と言い切ると誤る。
        lines.append("  中に読める文字列（6 字以上）は見つかりません（先頭 1MB）")
    lines += _entropy_facts(blob, size, known)
    return "\n".join(lines)


def _binary_next() -> str:
    """続きを調べるときの**この OS で実際に打てる**コマンド。"""
    import sys as _sys
    if _sys.platform == "win32":
        return ("→ 続きは Bash で: `powershell -NoProfile -Command \"Format-Hex -Path '<file>' | Select-Object -First 32\"`"
                "（バイト列）／実行ファイルの版情報は `powershell -NoProfile -Command \"(Get-Item '<file>').VersionInfo | Format-List\"`"
                "／ハッシュは `certutil -hashfile <file> SHA256`。Windows に file / strings は無い。")
    return ("→ 続きは Bash で: `file <file>`（素性）／`strings -n 8 <file> | head -80`（文字列）／"
            "`xxd <file> | head -40`（バイト列）。SQLite なら sqlite3、書庫なら unzip -l / tar -tf。")


def _read_sqlite(p: Path):
    """SQLite を、**表の構造**として返す（2026-10-05・Claude Code の Read が画像/PDF/ノートブックを
    中身に合った形で返すのに倣う）。

    ★それまでは「SQLite です。sqlite3 で開いて」と断って返していた（`_binary_hint`）。
      実測（09-01 以降の 99 走行）: その拒否は 102 回出て、直後にレシピどおり sqlite3 を
      Bash で叩いたのは **41 回**だけ。**29 回はまた Read**、13 回は Grep。
      同じ走行・同じファイルで最大 **11 回**繰り返していた。
      モデルが次に知りたいのは「どんな表と列があり、何行あるか」なので、それを返す。
    ★**読み取り専用**で開く（`mode=ro`）。書き換えはしない。数えるのは時間を区切る
      （大きな表の count(*) は全走査になるので、0.3 秒/表・全体 2 秒で打ち切る）。
    ★開けないとき（壊れている・暗号化・ロック）は None を返し、従来の案内に任せる。
    """
    import sqlite3
    import sys as _sys
    import time as _time
    try:
        con = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
    except Exception:
        return None
    try:
        rows = con.execute(
            "select type, name from sqlite_master where type in ('table','view') "
            "and name not like 'sqlite_%' order by type, name").fetchall()
    except Exception:
        con.close()
        return None
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    n_tab = sum(1 for t, _ in rows if t == "table")
    out = [f"{p} は **SQLite のデータベース**です。行としては読めないので、"
           f"**構造**を返します（読み取り専用で開きました）。",
           f"  大きさ: {size:,} バイト ／ 表 {n_tab} 個・ビュー {len(rows) - n_tab} 個"]
    stop_all = _time.monotonic() + 2.0
    shown = 0
    for typ, name in rows:
        if shown >= 40:
            out.append(f"  …ほか {len(rows) - shown} 個")
            break
        q = '"' + str(name).replace('"', '""') + '"'
        try:
            cols = con.execute(f"pragma table_info({q})").fetchall()
            import re as _re
            # ★空白や記号を含む列名は引用符つきで見せる（そのまま SQL に書き写せるように）
            colstr = ", ".join(
                (f"{c[1]}" if _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(c[1]))
                 else '"' + str(c[1]).replace('"', '""') + '"') + (f" {c[2]}" if c[2] else "")
                for c in cols)
        except Exception:
            colstr = "（列を読めません）"
        if len(colstr) > 300:
            colstr = colstr[:300] + " …"
        count = ""
        if typ == "table":
            limit = min(_time.monotonic() + 0.3, stop_all)
            con.set_progress_handler(lambda: 1 if _time.monotonic() > limit else 0, 20000)
            try:
                count = f"{con.execute(f'select count(*) from {q}').fetchone()[0]:,} 行"
            except sqlite3.OperationalError:
                count = "行数は時間切れで未計数"
            finally:
                con.set_progress_handler(None, 0)
        label = "表" if typ == "table" else "ビュー"
        out.append(f"  - {label} {name}" + (f"（{count}）" if count else "") + f": {colstr}")
        shown += 1
    con.close()
    if _sys.platform == "win32":
        how = ("Bash で `python -c \"import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
               "[print(r) for r in c.execute(sys.argv[2])]\" <file> \"select * from <表> limit 5\"`")
    else:
        how = 'Bash で `sqlite3 <file> "select * from <表> limit 5"`'
    out.append(f"→ 中身を見るには {how}（★Read をもう一度呼んでも同じ構造が返るだけです）")
    from . import ToolResult
    return ToolResult("\n".join(out), ok=True, kind="ok")


def _binary_hint(p: Path) -> str | None:
    """バイナリなら「何か」と「正しい開け方」を返す。文字なら None。"""
    try:
        head = p.open("rb").read(4096)
    except OSError:
        return None
    if not head:
        return None
    for magic, what, how in _MAGIC:
        if head.startswith(magic):
            import sys as _sys
            if _sys.platform == "win32":
                # ★表の開け方は Linux のコマンド（sqlite3 / unzip / zcat / file / strings）。
                #   Windows に無いものを案内しない。
                how = _MAGIC_HOW_WIN.get(magic, _binary_next().lstrip("→ "))
            return (f"Error: {p} は**{what}**です（テキストではありません）。\n"
                    f"Read は行として読むので、中身は文字化けします。\n"
                    f"→ {how}")
    # 魔法数が無くても NUL を含むならバイナリ扱い（実測の一時ファイル対策）
    if b"\x00" in head:
        return (f"Error: {p} はバイナリです（先頭 4KB に NUL バイト）。行としては読めないので、分かる事実を返します。\n"
                f"{_binary_facts(p, head)}\n"
                f"{_binary_next()}")
    return None


#: 一度に読める本数の上限。多すぎると 1 回の結果で窓を食い潰す。
_MANY_MAX = 12


def _run_many(inp: dict, paths: list) -> str:
    """複数のファイルを 1 回で読む。**1 本ずつ `_run` に通す**（判定を二重に持たない）。

    ★区切りは `===== <path> =====`。読めなかった物も**その場所に**理由を出す
      （黙って落とすと「読んだつもり」になる——この現場で何度も踏んだ形）。
    ★1 本でも切り詰めたら全体を `truncated` として名乗る。
    """
    from . import ToolResult
    if not paths:
        return "Error: file_path が空の配列です"
    if len(paths) > _MANY_MAX:
        return (f"Error: 一度に読めるのは {_MANY_MAX} 本までです（{len(paths)} 本渡されました）。"
                "分けて呼んでください。")
    parts: list[str] = []
    cut = False
    for one in paths:
        sub = dict(inp)
        sub["file_path"] = one
        sub.pop("path", None)
        r = _run(sub)
        cut = cut or bool(getattr(r, "truncated", False))
        parts.append(f"===== {one} =====\n{r}")
    return ToolResult("\n\n".join(parts), truncated=cut)


def _run(inp: dict) -> str:
    """1 本読む。★`file_path` に**配列**を渡せば、まとめて読む（下の `_run_many`）。"""
    path = inp.get("file_path") or inp.get("path")
    # ★**配列を受ける**（2026-09-02・cline の道具定義を参考に）。
    #   実測: 単発の読み取りが連続する手番が **2,234/9,397 = 23.8%**。
    #   「まとめて呼べます」の一言は 488 回発火したが、次の手番が複数呼び出しに
    #   なったのは **1 回（0.20%）**で、全手番の基準 2.32% を下回った
    #   ＝**一言では出力の形は変わらない。形を変えたいなら道具の形を変える。**
    #   ★互換: 文字列を渡す従来の呼び方は 1 文字も変わらない。
    if isinstance(path, (list, tuple)):
        return _run_many(inp, list(path))
    if not path:
        return "Error: missing 'file_path'"
    try:
        authorize_read(path)
    except PolicyDenied as e:
        return f"Error: policy denied: {e}"
    p = Path(path)
    if not p.exists():
        if p.is_symlink():
            # ★`exists()` は**辿った先**を見るので、壊れたリンクは「無い」になる。
            #   リンク自体は在るのだから「file not found」は嘘で、しかも
            #   直し方（リンク先を作る／張り直す）へ辿り着けない（2026-09-02）。
            try:
                tgt = os.readlink(p)
            except OSError:
                tgt = "(読めません)"
            return (f"Error: {path} は**壊れた記号リンク**です（リンクは在りますが、"
                    f"指す先 {tgt} がありません）。\n"
                    f"→ リンク先を作るか、ln -sfn <正しい先> {path} で張り直してください。")
        return f"Error: file not found: {path}" + _did_you_mean(path)
    if not p.is_file():
        return f"Error: not a file: {path}"
    suffix = p.suffix.lower()
    if suffix in _IMG_EXTS:
        return _read_image(p)
    if suffix in _PDF_EXTS:
        try:
            return _read_pdf(p, offset=int_arg(inp, "offset", 0),
                             limit=int_arg(inp, "limit", 20))
        except BadArg as e:
            return f"Error: {e}"
    # ★バイナリを黙って文字にしない（2026-08-28・監査 S4）。
    #   実測: モデルは works.db（SQLite）を 3 走行で 14/19/23 回 Read し、
    #   run31 では Read の出力の **29.6%** が読めないバイト列だった。
    #   復号は「成功」するので、返るのは**文字化けした本文**——モデルには
    #   「読めたが中身が意味不明」に見え、同じ読みを何度も繰り返す。
    #   ここで断り、**その形式の正しい開け方**を教える（空観測の置換と同じ作法）。
    # ★SQLite は断らずに**構造**として返す（2026-10-05・`_read_sqlite` の注記）。
    #   開けなければ None が返り、下の従来の案内に落ちる。LAPS_READ_SQLITE=0 で外せる。
    if os.environ.get("LAPS_READ_SQLITE", "1") != "0":
        try:
            with p.open("rb") as _fh:
                _is_sqlite = _fh.read(16) == b"SQLite format 3\x00"
        except OSError:
            _is_sqlite = False
        if _is_sqlite:
            _sq = _read_sqlite(p)
            if _sq is not None:
                return _sq
    _binary = _binary_hint(p)
    if _binary and os.environ.get("LAPS_READ_BINARY_GUARD", "1") != "0":
        return _binary
    # Plain text path
    try:
        offset = int_arg(inp, "offset", 0)
        limit = int_arg(inp, "limit", 2000)
    except BadArg as e:
        return f"Error: {e}"
    try:
        lines = _decode(p).splitlines()
    except Exception as e:
        return f"Error: {e}"
    selected = lines[offset: offset + limit]
    width = len(str(offset + len(selected)))
    numbered = [f"{i+offset+1:>{width}}  {ln}" for i, ln in enumerate(selected)]
    # 文字数の上限は**行の境目で**当てる。素朴に out[:50000] すると行の途中で
    # 切れるうえ、「どこから読み直せばよいか」を正直に言えなくなる。
    _cap = _text_cap()
    # ★1 行そのものが上限より長いと、この輪は**1 行も入れずに抜ける**（2026-09-21 実測）。
    #   すると `shown_end = offset + 0 = offset` になり、案内が
    #   `Read(offset=同じ値)` ＝ ★**同じ壁に何度でも当たる**（minify した JS・
    #   1 行の JSON で実際に起きる。120KB の 1 行 JSON で中身 0 行を確認した）。
    #   ⇒ **長すぎる 1 行は、その行の中で切る。** 少なくとも 1 行は必ず進む。
    #   出典の型: cline `file-read.ts:140-141` が同じことをしている
    #   （`line.slice(0, MAX) + " [line truncated]"`）。goose の edit.rs も同様。
    _line_cap = max(2000, _cap // 4)
    _clipped = 0
    _numbered = []
    for ln in numbered:
        if len(ln) > _line_cap:
            ln = ln[:_line_cap] + f"  … (この行はここで切りました: 全 {len(ln):,} 文字)"
            _clipped += 1
        _numbered.append(ln)
    numbered = _numbered
    kept, used = [], 0
    for ln in numbered:
        if used + len(ln) + 1 > _cap:
            break
        kept.append(ln)
        used += len(ln) + 1
    # ★それでも 1 行も入らないなら、**1 行だけは必ず入れる**（進まないより切れたほうがよい）。
    if not kept and numbered:
        kept = [numbered[0][:_cap]]
        used = len(kept[0])
    # ★どのファイルを読んだかを最初の行に出す。
    #   2026-08-12 実機: 同じ名前の `scraper.py` がルートと VDIR に2つあり、
    #   相対パスで読んだ側と絶対パスで直した側が食い違って
    #   「old_string not found」になった。**出力にパスが無いと、モデルは
    #   自分がどちらを読んだのか判別できない。**
    #   同名が他にもあるなら、その場で言う（後で気づくより、読んだ瞬間に）。
    # ★**解決後の絶対パス**を出す。渡された文字列をそのまま出しても、
    #   相対と絶対の食い違い（実機の取り違えの正体）は見えない。
    try:
        shown_path = p.resolve()
    except OSError:
        shown_path = p
    head = f"# {shown_path}"
    twins = _same_name_elsewhere(p)
    if twins:
        head += ("\n# ⚠ 同じ名前のファイルが他にもあります: "
                 + " ".join(str(t) for t in twins)
                 + "\n#   直すときは、いま読んだ上のパスをそのまま使ってください。")
    out = head + "\n" + "\n".join(kept)
    shown_end = offset + len(kept)          # 次に読むべき行（0 起点）
    if len(kept) < len(numbered):
        out += (
            f"\n… (ここで打ち切りました: 出力が上限 {_cap} 文字に達しました"
            f"／{len(lines) - shown_end} 行が未読。"
            f"Read(file_path=\"{path}\", offset={shown_end}) で続きを読めます)")
        # ★切ったことを**型で名乗る**（2026-09-02・上の `_truncated` と同じ理由）。
        from . import ToolResult
        return ToolResult(out, truncated=True)
    elif shown_end < len(lines):
        out += f"\n... ({len(lines) - shown_end} more lines)"
    # ★成功も**型で名乗る**（2026-09-21）。素の str で返すと `as_result` が
    #   **読めた中身の言葉**で成否を当てにいく。実測: ごく普通のログを読んだだけで
    #
    #       app.log の本文に "Permission denied" の行がある
    #       → Read は成功しているのに ok=False / kind=error
    #
    #   になった（`_FAILED_BODY` の語に中身が一致したため）。読んだ内容は
    #   **観測であって、道具の成否ではない**。ログ・エラー記録・障害報告を読むほど
    #   失敗に見えるので、開発用途ほど当たる。道具は自分の成否を知っている。
    from . import ToolResult
    return ToolResult(out, ok=True, kind="ok")


SPEC = ToolSpec(
    name="Read",
    description=(
        "ファイルの中身を読む。行番号つきで返るので file:line の引用にそのまま使える"
        "（OS を問わず同じように動く）。"
        "**ファイルの中身を見るときは、シェルで cat / type / head / tail / more を"
        "組み立てるより、こちらを使うこと。**"
        "ファイル名だけ分かっていれば file_path にそのまま渡してよい"
        "（作業ディレクトリからの相対で解決するので、場所を先に調べ直さなくてよい）。"
        "plain text: offset/limit で行範囲（先頭20行なら limit=20）。"
        "画像 (.png/.jpg/.webp 等): base64 の data URI（multimodal のバックエンド向け）。"
        "PDF: ページ範囲で抽出（offset=開始ページ, limit=ページ数, pypdf が要る）。"
        "上限で打ち切ったときはそう書いて返すので、続きは offset を進めて読む。"
        # ★「直すなら Edit」を書きたくなるが、書けない。
        #   test_review_fixes.py::test_render_tools_section_subset_reflects_restriction が
        #   「部分集合を渡したら他の道具の名前は出てこない」を固定していて、説明文中の
        #   相互参照はその名前として数えられる。Bash→Glob/Read/Grep の誘導は測って
        #   効果が出たので残し、依頼の範囲外だったこの1本は落とした。
        #   ⚠ 相互参照を増やすなら、あの試験の意図（レンダラの絞り込み）と
        #   説明文の言及を区別できるようにするのが先。
    ),
    schema={
        "type": "object",
        "properties": {
            # ★配列も受ける（2026-09-02）。独立した読み取りは 1 回でまとめて済む。
            "file_path": {
                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                "description": ("読むファイル。**複数を配列で渡せる**"
                                f"（最大 {_MANY_MAX} 本・独立した読み取りは 1 回で済ませる）"),
            },
            "offset": {"type": "integer", "description": "Text: start line. PDF: start page (0-indexed)"},
            "limit": {"type": "integer", "description": "Text: max lines (default 2000). PDF: max pages (default 20)"},
        },
        "required": ["file_path"],
    },
    readonly=True, parallel_safe=True,
    # ★見た目が同じで文字だけ違うパスを入る前に直す（2026-10-05・tools/pathfix.py）
    handler=_pathfix.wrap(_run, "file_path", "path", mode="existing"),
)
