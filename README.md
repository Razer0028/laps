# L.A.P.S — Local Autonomous Persistent System

L.A.P.S is a coding and operations agent that runs on your own machine against a **local LLM**.
It talks to any OpenAI-compatible server (llama.cpp `llama-server`, llama-swap, Ollama, vLLM, …) and does
the rest itself: it reads and edits files, runs commands, keeps a plan, remembers earlier work, and uses
skills. No cloud service or API key is required.

It was built and used for several months inside one person's home lab (development name: *Blackwall*).
This repository is that code with the lab-specific parts taken out.

> **Status: alpha, personal project.** The user interface, most comments and much of the documentation are in
> **Japanese**. Expect rough edges.

日本語の説明は [下](#日本語) にあります。

---

## What is in this repository

| Folder | What it is | Language |
|---|---|---|
| `src/laps/` | The agent: run loop (`engine/`), tools, prompts, safety policy, terminal client (`term/`), MCP server, phone/web gateway and optional relay | Python |
| `src/tests/` | Test suite (about 3,600 tests) | Python |
| `src/docs/` | Environment switches (`env-switches.md`), execution policy modes (`policy-modes.md`), web UI | Markdown |
| `app/` | Desktop app (Tauri 2: `src-tauri/` in Rust, `ui/` in plain HTML/CSS/JS) | Rust, JS |
| `scripts/` | Building the Windows installer and its bundled Python; regenerating the line numbers in `src/docs/env-switches.md` | Bash + Python |

## Requirements

- Python 3.10 or newer.
- An OpenAI-compatible chat completions server **with tool calling**. With llama.cpp:

  ```bash
  llama-server -m your-model.gguf --jinja -c 32768 --port 8080
  ```

  `--jinja` is needed for tool calls. Models that were tested most: Qwen3.x MoE coder/instruct models and their
  derivatives. Small models work, but the agent was tuned for 30B-class MoE models.

## Quick start (terminal)

```bash
git clone <this repository> laps && cd laps
python3 -m venv .venv && .venv/bin/pip install -e "src[dev]"

# point it at your server (default: http://127.0.0.1:8080/v1, model "default")
export LAPS_BACKEND=http://127.0.0.1:8080/v1
export LAPS_MODEL=default

.venv/bin/laps                          # interactive (full-screen terminal UI)
.venv/bin/laps -p "list the Python files here and count their lines" --policy workspace   # one shot
```

Settings live in `~/.config/blackwall/config.toml` (created on first run). Every environment switch is listed in
[`src/docs/env-switches.md`](src/docs/env-switches.md).

## ⚠ Safety: choose an execution policy

The agent runs shell commands. What it may run is decided **outside the model** by `src/laps/policy.py`:

| Policy | What it allows |
|---|---|
| `readonly` | read-only programs only |
| `workspace` | changes inside the working folder; no `sudo`, `ssh` (unless allow-listed), `curl`, package managers |
| `trusted` | everything except a small floor (`authorized_keys`, `.ssh/`, `sudoers`, its own policy files) |

**The default is `workspace`.** Switch to `--policy trusted` (or `LAPS_POLICY=trusted`, or `/policy trusted`) only
inside a VM or container you can throw away — that is how the agent was used in its original lab. The policy is
defense-in-depth, not a sandbox — read [`src/docs/policy-modes.md`](src/docs/policy-modes.md) for what it does
and does not stop. On Linux, `LAPS_SHELL_SANDBOX` can wrap commands in bubblewrap.

## Desktop app (Windows / Linux)

The app in `app/` is a Tauri 2 front end. It starts the Python core as a sidecar.

```bash
cd app && npm install                      # Tauri CLI
export LAPS_PYTHON=/path/to/laps/.venv/bin/python   # a Python that has `laps` installed
npx tauri dev
```

### Windows installer

Each [release](https://github.com/Razer0028/laps/releases) has a Windows installer
(`L.A.P.S Console_<version>_x64-setup.exe`) built from this repository with `scripts/build_windows_installer.sh`.
It bundles a Python 3.12 runtime with the `laps` core, so nothing else needs to be installed. You still need an
inference server (see [Requirements](#requirements)); the app can also download llama.cpp and run a model on the
local NVIDIA GPU (*Models → この PC の GPU*).

- **The installer is not code-signed.** Windows SmartScreen will warn ("Windows protected your PC"); choose
  *More info → Run anyway* only if the checksum matches. Check it in PowerShell:
  `Get-FileHash ".\L.A.P.S Console_<version>_x64-setup.exe" -Algorithm SHA256` and compare with `SHA256SUMS.txt`
  in the release.
- It installs for the current user only (no administrator rights). WebView2 is installed if it is missing.
- There is **no auto-updater**. Install a newer release over the old one.
- Built for Windows 10/11 x64. The UI is mostly Japanese.

### Building the installer yourself

On Linux (Debian/Ubuntu), from the repository root:

```bash
rustup target add x86_64-pc-windows-gnu
sudo apt install mingw-w64 nsis curl unzip rsync python3-pip   # and Node.js 18+
bash scripts/build_windows_installer.sh
```

`scripts/build_pyruntime.sh` downloads the python.org embeddable package (checked against its SHA-256) and the
pinned wheels, and writes `app/pyruntime/WHEELS.txt` (name, version, license, SHA-256 of each wheel) and
`INTEGRITY.json` (used by the app's integrity check).

## Other entry points

| Command | What it does |
|---|---|
| `laps` | terminal client (interactive, or `-p` for one shot) |
| `laps-mcp` | MCP server (stdio) that exposes file-inspection tools to other MCP clients |
| `bw-undo` | roll the workspace back (git-based checkpoint; tar when there is no git) |
| `python -m laps.gateway` | WebSocket gateway for the web UI / remote control |
| `python -m laps.relay` | optional self-hosted relay so several machines can be reached from one place (accounts, optional TLS) |

## What was removed from the private version

These parts only made sense inside the original lab and are not here:

- the lab's knowledge server and its tool shelf, the lab's analysis container, the lab rule book
- the lab's model-switching service and GPU scheduler
- image generation (it had no safeguards against sexual images of minors or of real people without consent)
- the auto-updater and its signing keys, the release pipeline, telemetry endpoints
- the Android app

Defaults now point to `127.0.0.1`. Telemetry, the relay and the Claude API relay are **off** until you configure them.

## Tests

```bash
cd src && ../.venv/bin/python -m pytest -q
```

A few tests fail at the time of publication; they are listed with reasons in `src/tests/known_failures.tsv`
and are marked xfail.

## Engine patches

Patches for the Strata inference engine (keeping conversation state across a two-GPU layer split, and faster
kernels for Pascal GPUs) are published separately: see [laps-engine-patches](https://github.com/Razer0028/laps-engine-patches) (MIT).

## License

L.A.P.S is licensed under the **GNU Affero General Public License v3.0 or later** — see [`LICENSE`](LICENSE).
Third-party code and assets keep their own licenses; see [`THIRD_PARTY.md`](THIRD_PARTY.md).

---

## 日本語

L.A.P.S（Local Autonomous Persistent System・開発名 Blackwall）は、自分の機械のローカル LLM で動くコーディングと運用のエージェントです。
OpenAI 互換の推論機（llama.cpp の llama-server・llama-swap・Ollama・vLLM など）に繋ぐだけで、ファイルを読む・直す・命令を走らせる・
予定を立てる・前の作業を思い出す・スキルを使う、を自分で行います。クラウドも API キーも要りません。

個人の自宅ラボで 1 年使ってきたものから、ラボ専用の部分（知識サーバー・作業用コンテナ・モデルの切り替えの仕組み・画像生成・
自動更新・Android 版）を外して公開しています。画面・コメント・文書の多くは日本語です。

- 入れ方と使い方は上の Quick start のとおりです。既定の推論機は `http://127.0.0.1:8080/v1` です。
- Windows 版のインストーラは [Releases](https://github.com/Razer0028/laps/releases) にあります（Python を同梱・管理者権限は不要）。★コード署名は無いので SmartScreen の警告が出ます。`SHA256SUMS.txt` と `Get-FileHash` で照合してから入れてください。自動更新はありません（新しい版を上から入れる）。組み立て方は上の *Building the installer yourself* です。
- ⚠ 実行の関所の既定は `workspace`（作業フォルダーの中だけ書ける）です。`trusted`（ほぼ何でも走らせる）は使い捨ての VM やコンテナの中だけで使ってください。
- ライセンスは AGPL-3.0-or-later です。
