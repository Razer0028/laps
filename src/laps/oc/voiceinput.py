"""音声入力（2026-10-09 オーナー「音声入力の仕組みも欲しい」）。

画面（app/ui の 🎤）が録った音を wav にして渡し、聞き取りの道具（oc/listen.py）と同じ whisper.cpp で文字にする。
文字は入力欄に入るだけで、送るかどうかは利用者が決める（勝手に送らない）。
アプリ（src-tauri の voice_transcribe）が `python -m laps.oc.voiceinput <wav> [言語]` で 1 回ずつ呼ぶ。
標準出力に JSON を 1 行（{"ok": true, "text": …} か {"ok": false, "error": …}）。録音のファイルは呼んだ側が消す。

★whisper が無音や息から作りがちな定型句（「ご視聴ありがとうございました」など）だけが返ったら空にする
  （listen は印を付けて残すが、入力欄にそれが入ると利用者がそのまま送ってしまう）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _out(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if not argv:
        _out({"ok": False, "error": "録音のファイルが渡されていません"})
        return 2
    wav = Path(argv[0])
    lang = (argv[1] if len(argv) > 1 else "").strip() or "auto"
    if not wav.is_file():
        _out({"ok": False, "error": f"録音のファイルがありません: {wav}"})
        return 2
    from . import listen
    if not listen.available():
        _out({"ok": False, "error": "この機械では音声の聞き取り（whisper.cpp）を使えません"})
        return 1
    try:
        results, info = listen._transcribe([wav], language=lang, prompt="", vad=False, notes=[])
    except Exception as e:  # noqa: BLE001 — 理由を画面へ返す
        _out({"ok": False, "error": f"聞き取りに失敗しました: {e}"})
        return 1
    r = results[0] if results else {}
    if r.get("error"):
        _out({"ok": False, "error": str(r["error"])})
        return 1
    text = (r.get("text") or "").strip()
    if r.get("flag"):                   # 無音・息・物音から作った定型句らしい
        text = ""
    _out({"ok": True, "text": text, "language": r.get("language") or "", "backend": info.get("backend") or "",
          "secs": round(float(info.get("elapsed_s") or 0.0), 2), "setup_secs": round(float(info.get("setup_s") or 0.0), 1)})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
