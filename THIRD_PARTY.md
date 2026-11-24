# Third-party code and assets

L.A.P.S itself is licensed under AGPL-3.0-or-later (see `LICENSE`). The following parts come from other projects
and keep their own licenses.

## Bundled in this repository

| What | Where | License |
|---|---|---|
| xterm.js and its addons (fit, unicode11, webgl) | `app/ui/vendor/xterm.js`, `addon-*.js` | MIT — `app/ui/vendor/xterm.LICENSE` |
| Monaco Editor | `app/ui/vendor/monaco/` | MIT — `app/ui/vendor/monaco/LICENSE` |
| html2canvas 1.4.1 | `app/ui/vendor/html2canvas.min.js` | MIT — header in the file |
| RNNoise WebAssembly build (@shiguredo/rnnoise-wasm, wrapping Xiph RNNoise) | `app/ui/vendor/rnnoise.js` | Apache-2.0 — `app/ui/vendor/rnnoise.LICENSE` |
| IBM Plex Sans JP, IBM Plex Mono, Saira Condensed | `app/ui/fonts/` | SIL Open Font License 1.1 — `app/ui/fonts/OFL.txt` |
| `frontend-design` skill (Anthropic, unmodified) | `src/laps/oc/skills/frontend-design/` | Apache-2.0 — `LICENSE.txt` in that folder |

## Code written after studying other projects

See `src/laps/engine/THIRD_PARTY.md`. In short: parts of the run loop, the edit matcher
(`src/laps/engine/tools/edit_match.py`, a port of opencode's replacer) and tool descriptions follow
[opencode](https://github.com/anomalyco/opencode) (MIT, Copyright (c) 2025 opencode); the patch format, sandbox
and several other pieces were rewritten in Python after reading [Codex CLI](https://github.com/openai/codex)
(Apache-2.0, Copyright 2025 OpenAI).

## Dependencies (installed, not bundled)

Python: httpx, rich, click, prompt_toolkit, textual, tomli (optional: websockets, pillow, beautifulsoup4, pypdf).
Rust: Tauri 2 and the crates in `app/src-tauri/Cargo.lock`. Each keeps its own license.
