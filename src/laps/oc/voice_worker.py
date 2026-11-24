"""Voice の生成係（2026-10-08）。★blackwall の外の Python（声の専用の環境・PyTorch 入り）で動かす台本。
blackwall を import しない（同梱の Python には PyTorch を入れられないので、別の環境で走らせる）。

入力: 引数 1 つ＝依頼の JSON ファイル。{device, language, voice, items: [{text, out}], exaggeration, cfg_weight,
      temperature, seed, result}
出力: result の JSON ファイル（標準出力はライブラリの警告で汚れるので使わない）。
      {ok, device, sr, load_s, items: [{out, seconds, chunks, gen_s, error}], error}
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback

MAX_CHUNK = 120          # 1 回に作る長さ（字）。長い文は句点で割ってつなぐ（長いと終わりが崩れる・言葉が抜ける）
GAP_S = 0.22             # つなぎ目の無音（秒）


def split_text(text: str) -> list[str]:
    """句点・改行で割り、MAX_CHUNK を超える塊は読点・空白でさらに割る。"""
    text = text.replace("\r", "")
    # 句点・感嘆符・疑問符の後と改行で割る。英文のピリオドは後ろに空白があるときだけ（3.5 や e.g. を割らない）。
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+|\n+", text) if p and p.strip()]
    out: list[str] = []
    for p in parts:
        while len(p) > MAX_CHUNK:
            cut = max(p.rfind("、", 0, MAX_CHUNK), p.rfind(",", 0, MAX_CHUNK), p.rfind(" ", 0, MAX_CHUNK))
            if cut < MAX_CHUNK // 3:
                cut = MAX_CHUNK
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    # 短すぎる塊は前に寄せる（「はい。」だけを 1 回で作ると不自然に伸びやすい）
    merged: list[str] = []
    for p in out:
        if merged and len(p) < 8 and len(merged[-1]) + len(p) <= MAX_CHUNK:
            sep = " " if re.match(r"[A-Za-z0-9]", p) and re.search(r"[A-Za-z0-9.,!?;:]$", merged[-1]) else ""
            merged[-1] = merged[-1] + sep + p
        else:
            merged.append(p)
    return merged


def main() -> int:
    req = json.load(open(sys.argv[1], encoding="utf-8"))
    res: dict = {"ok": False, "items": []}
    try:
        import torch
        import torchaudio
        threads = int(req.get("threads") or 0)
        if threads > 0:
            torch.set_num_threads(threads)
        dev = req.get("device") or "auto"
        if dev == "auto":
            dev = "cuda" if torch.cuda.is_available() else "cpu"
        res["device"] = dev
        if dev == "cuda":
            try:
                res["gpu"] = torch.cuda.get_device_name(0)
            except Exception:
                pass
        seed = req.get("seed")
        if seed is not None:
            torch.manual_seed(int(seed))
        t0 = time.time()
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        model = ChatterboxMultilingualTTS.from_pretrained(device=dev)
        res["load_s"] = round(time.time() - t0, 1)
        res["sr"] = model.sr
        if req.get("load_only"):
            res["ok"] = True
            return 0
        kw = {"language_id": req.get("language") or "ja"}
        for k in ("exaggeration", "cfg_weight", "temperature"):
            if req.get(k) is not None:
                kw[k] = float(req[k])
        voice = req.get("voice")
        if voice:
            kw["audio_prompt_path"] = voice
        gap = torch.zeros(1, int(model.sr * GAP_S))
        for it in req.get("items") or []:
            row = {"out": it.get("out")}
            t1 = time.time()
            try:
                chunks = split_text(str(it.get("text") or ""))
                if not chunks:
                    raise ValueError("text is empty")
                wavs = []
                for i, c in enumerate(chunks):
                    w = model.generate(c, **kw)
                    if w.dim() == 1:
                        w = w.unsqueeze(0)
                    wavs.append(w.detach().cpu())
                    if i < len(chunks) - 1:
                        wavs.append(gap)
                wav = torch.cat(wavs, dim=1)
                os.makedirs(os.path.dirname(os.path.abspath(it["out"])) or ".", exist_ok=True)
                torchaudio.save(it["out"], wav, model.sr)
                row.update(seconds=round(wav.shape[1] / model.sr, 2), chunks=len(chunks),
                           gen_s=round(time.time() - t1, 1))
            except Exception as e:  # noqa: BLE001
                row["error"] = f"{type(e).__name__}: {e}"[:400]
            res["items"].append(row)
            json.dump(res, open(req["result"], "w", encoding="utf-8"), ensure_ascii=False)   # 途中経過も残す
        res["ok"] = not any(r.get("error") for r in res["items"])
    except Exception as e:  # noqa: BLE001
        res["error"] = f"{type(e).__name__}: {e}"[:600]
        res["trace"] = traceback.format_exc()[-1500:]
    finally:
        try:
            json.dump(res, open(req["result"], "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
