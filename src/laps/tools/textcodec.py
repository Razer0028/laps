"""書き込み系の道具が共有する、**損なわない**文字コードの往復（2026-09-20）。

★なぜ要るか（敵対レビューで実際に壊して確かめた）:
    cp932 の `run.bat`（日本語の注記つき）に `Edit` で `HOST=localhost` → `HOST=192.0.2.160`。
    返事は「OK: 1 replacement」。ファイルは 92 → 147 バイト、**日本語は全部 U+FFFD（27 個）**。
  `Read` は cp932 を正しく読んで見せるので、モデルにも利用者にも**怪しく見える材料が無い**。
  原因は `edit.py` の `raw.decode("utf-8", "replace")  # best-effort: keep the file editable`。
  ASCII の old_string は化けたままでも一致するので、編集は「成功」し、読めなかったバイトは捨てられた。
  `ApplyDiff` は読みだけ cp932 に対応していて、**書き戻しが UTF-8**＝文字コードが黙って変わっていた。

★決まり:
  1. **厳密に読めたものだけを編集する。** `errors="replace"` で読んだ文字列を書き戻さない。
  2. **読んだのと同じ符号化・同じ BOM で書く。**
  3. 往復して元のバイト列に戻らないなら、**触らずに断る**（理由と次の一手を返す）。
  4. バイナリ（NUL を含む）は編集しない。
  `latin-1` は候補に入れない: どんなバイト列も「読めて」しまい、バイナリが文字として通る。

★候補の順は `read.decode_bytes` と同じ（utf-8 → cp932 → euc-jp）。**Read が見せた文字と、Edit が突き合わせる文字が
  食い違わない**ことが大事なので、順番を変えるときは両方を同時に変えること。
"""
from __future__ import annotations

_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
_CANDIDATES: tuple[str, ...] = ("utf-8", "cp932", "euc-jp")


class NotEditable(ValueError):
    """損なわずに編集できない。メッセージは**そのまま道具の返事に使える**文にする。"""


def decode_for_edit(raw: bytes) -> tuple[str, str, bytes]:
    """`(text, encoding, bom)`。厳密に読めなければ `NotEditable`。"""
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                text = raw[len(bom):].decode(enc)
            except UnicodeDecodeError as e:
                raise NotEditable(
                    f"このファイルは {enc}（BOM つき）に見えますが、途中に読めないバイトがあります（{e.reason}）。"
                    "壊さないために編集しませんでした。") from e
            return text, enc, bom
    if b"\x00" in raw:
        raise NotEditable(
            "このファイルはバイナリです（NUL バイトを含みます）。文字としての編集はできません。"
            "中身を変えたいなら、生成し直すか、専用の道具（Bash など）を使ってください。")
    for enc in _CANDIDATES:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        # ★往復して元に戻ること。cp932 には同じ字に複数のバイト列がある（NEC/IBM の重複）ので、
        #   厳密に読めても書き戻すと別のバイト列になりうる。そのときは触らない。
        if text.encode(enc) == raw:
            return text, enc, b""
    raise NotEditable(
        "このファイルの文字コードを特定できませんでした（UTF-8・cp932・EUC-JP のどれでも、損なわずに往復できません）。"
        "壊さないために編集しませんでした。Bash で `file` や `iconv` を使って確かめてください。")


def encode_back(text: str, encoding: str, bom: bytes = b"") -> bytes:
    """読んだのと同じ符号化で書く。表せない字があれば `NotEditable`（黙って別の字にしない）。"""
    try:
        return bom + text.encode(encoding)
    except UnicodeEncodeError as e:
        bad = text[e.start:e.end]
        raise NotEditable(
            f"このファイルは {encoding} です。書き込もうとした文字 {bad!r} は {encoding} では表せません。"
            "壊さないために書き込みませんでした。その文字を使わない形にするか、"
            "ファイル全体を UTF-8 に変えてよいかを利用者に確かめてください。") from e


def existing_encoding(path) -> tuple[str, bytes] | None:
    """既にあるファイルの `(encoding, bom)`。無い・読めない・バイナリなら None（＝呼び手は UTF-8 で新規に書く）。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    try:
        _t, enc, bom = decode_for_edit(raw)
    except NotEditable:
        return None
    return enc, bom
