"""成功したのに何も確かめていない命令を、道具が名乗る（2026-10-13 オーナー「様々なタスクにも当てはめて」）。

PC の走行は、画像のデータを `ffmpeg -f s16le` で生の PCM として読ませて WAV を作り、音量が出たのを「音声の抽出に成功」と読んだ。
生の PCM としての読み込みは、どんなバイトでも成功する。同じ形は開発の作業にもある: 試験が 0 本しか走らなかった
試験の命令は、終了 0 でも何も確かめていない。どれも命令と出力から決められる事実なので、推論機は呼ばない。
Bash（oc/tools.py）と Lab（oc/labtool.py）の結果に同じ一文を足す。
"""
from __future__ import annotations

import re

#: 試験を 1 本も走らせていない出力（pytest・unittest・jest・go・cargo・npm・mocha・vitest）。
_ZERO_TESTS = re.compile(
    r"collected 0 items|no tests ran|\bRan 0 tests\b|No tests found|\[no test files\]|running 0 tests|"
    r"Error: no test specified|^\s*0 passing\b|No test files found|Tests:\s+0 total",
    re.I | re.M)
#: 生のバイトをそのまま音・映像として読む ffmpeg の入力の指定（-f の後・-i より前）。
_RAW_IN = re.compile(r"\bffmpeg\b[^|;&]*?-f\s+(s8|u8|s16le|s16be|u16le|s24le|s32le|f32le|f32be|f64le|mulaw|alaw|"
                     r"rawvideo|data)\b[^|;&]*?-i\b", re.I)
_SOX_RAW = re.compile(r"\bsox\b[^|;&]*-t\s+raw\b", re.I)


def note(command: str, output: str, ok: bool = True) -> str:
    """命令と出力から言える「成功しても何も確かめていない」事実の一文（無ければ ""）。"""
    cmd = command or ""
    out = output or ""
    rows = []
    if _ZERO_TESTS.search(out):
        rows.append("This run executed zero tests, so its result says nothing about whether the code works. Point the "
                    "runner at the tests (a path, a pattern, -k) and check that the output reports how many tests ran.")
    if ok and (_RAW_IN.search(cmd) or _SOX_RAW.search(cmd)):
        rows.append("Reading bytes as raw PCM or raw video always succeeds, whatever the bytes are, and gives a "
                    "non-zero volume; a successful conversion does not show the bytes are sound or video. Check the "
                    "source format first (a header or container you can name), and judge the result by listening to or "
                    "measuring a short sample against real audio.")
    return " ".join(rows)
