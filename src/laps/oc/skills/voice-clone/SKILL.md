---
name: voice-clone
description: Make speech audio with the Voice tool - narration, game or video dialogue, voice-overs, audio guides - in the built-in voice or a voice cloned from a short reference recording. Covers consent, recording and cleaning the reference, Japanese readings (misread kanji fixed with kana), batches of lines from a script or CSV, checking the result with Listen, tuning emotion and pacing, and converting and loudness-normalizing the output.
keywords: 音声クローン, ボイスクローン, 声の複製, 声を真似, 読み上げ, 音声合成, ナレーション, ボイス+作, 台詞+音声, セリフ+音声, 声優, tts, text to speech, voice clone, voice over, voiceover
---

# Voice cloning and speech (Voice tool)

## Consent first
- Clone only a voice you have the right to use: the user's own, someone who agreed to this use, a voice actor under contract, or a licensed or synthetic voice. If it is unclear whose voice a recording is, or the request is to imitate a real person who did not agree (including public figures), decline that part and offer the built-in voice.
- Never make audio meant to deceive: fake statements, scams, impersonating someone to a third party.
- Record consent when saving: `Voice(voice="ref.wav", save_voice="narrator", consent="my own voice")`. Every output carries an inaudible Perth watermark marking it as AI-generated; do not try to remove it.

## The reference recording (most of the quality comes from here)
- 8-20 s of one person speaking naturally, ideally in the target language and in the style you want (calm narration vs. excited).
- No music, no other voices, no echo, no clipping. Trim silence at both ends.
- Clean it if needed: `ffmpeg -i in.m4a -af "highpass=f=80,lowpass=f=9000,afftdn=nf=-25,silenceremove=start_periods=1:start_threshold=-45dB,loudnorm=I=-18:TP=-2" -ac 1 -ar 24000 ref.wav`
- Check it first with `Listen(paths=["ref.wav"])`: if Listen hears noise or a second voice, the clone will carry it too.

## Japanese text
- Kanji can be misread. Measured: 「同じ声で」 came out sounding like 「同時声で」; 「おなじ こえで」 came out right. Use `check=true`; when a word comes back wrong, rewrite it in kana and make that line again.
- Write numbers, units, dates and acronyms the way they are spoken: `3.5kg` → `さんてんごキロ`, `API` → `エーピーアイ`, `10/8` → `じゅうがつ ようか`.
- Give names in kana. Keep sentences short; the tool splits at 。！？ and joins the parts, but natural breaks sound better. Use 、 for pauses.

## Making lines
- One file: `Voice(text="...", out="voice/intro.wav", voice="narrator")`.
- Many lines (the model loads once, up to 500 per call): `Voice(lines=[{"text": "...", "out": "voice/001.wav"}, ...], voice="narrator", check=true)`. For a script or CSV: Read it, build the list, and name the files by line ID so they can be matched later.
- The same `seed` reproduces a take; another seed gives another reading of the line.
- Emotion: `exaggeration` 0.5 (default); 0.7-1.0 for dramatic lines, together with `cfg_weight` about 0.3 so they do not rush; 0.3-0.4 for calm narration. A fast-talking reference also wants `cfg_weight` about 0.3.

## Check before delivering
- `check=true` transcribes each output with Listen. Compare it with the requested text: a missing or extra phrase or a different word → fix the text (kana, punctuation) or try another seed. Listen itself mixes up homophones (声/恋, 今日/京), so judge by sound, not exact kanji.
- Look at the durations: a short line that came out several times longer than expected usually has a repeated or garbled ending → make it again.

## Output
- `.wav` by default; `.mp3/.ogg/.flac/.m4a` through ffmpeg. Game engines usually want `.ogg`; resample if a project needs it: `ffmpeg -i in.wav -ar 48000 out.ogg`.
- Even loudness across a set of lines: `ffmpeg -i in.wav -af loudnorm=I=-16:TP=-1.5:LRA=11 out.wav` (dialogue around -16 to -18 LUFS).
- Report the files, their durations, which voice (with its consent note) and any line that failed the check.

## Troubleshooting
- The first call installs the engine (Python, PyTorch and the model: about 5-7 GB) and can take several minutes. Say so before starting a long batch; `Voice(prepare=true)` installs it on its own.
- "on cpu" on a PC with an NVIDIA GPU: the driver or CUDA is not usable; it still works, only slower. `LAPS_VOICE_GPU=0` forces the CPU build.
- Out of GPU memory: stop other GPU programs (for example a local LLM server) or use the CPU build.
- Logs: `<voice folder>/install.log` and `<voice folder>/runs/<time>/worker.log` (the voice folder is shown by `Voice(list_voices=true)` errors and is `~/.blackwall/voice` or the app's data folder).
