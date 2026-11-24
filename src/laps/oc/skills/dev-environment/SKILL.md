---
name: dev-environment
description: Set up and fix development environments and toolchains on Windows, Linux and macOS - Python, Node.js, Go, Rust, .NET, Java, C/C++ compilers, Git, ffmpeg, 7-Zip, ripgrep and other CLIs - using winget, Scoop, portable zips in the Blackwall tools folder, apt/dnf, Homebrew, and per-project isolation (venv/uv, npm, cargo). Use when a command is "not found", a tool must be installed, versions conflict, or a new machine/project needs setup.
keywords: 環境構築, 開発環境, ツールチェーン, toolchain, pyenv, nvm, rustup, msys2, パスが通, path が通
---

# Development environments

## First, look at what exists
- The `<env>` block lists working interpreters, commands on PATH, and the **Blackwall tools folder**. Trust it over guesses.
- Check versions before installing: `python --version` / `py -0p` (Windows lists all Pythons), `node -v`, `git --version`, `go version`, `rustc -V`, `dotnet --list-sdks`, `java -version`.
- "Found but not working" Python on Windows is usually the Microsoft Store stub (`%LOCALAPPDATA%\Microsoft\WindowsApps\python.exe`). Use `py` (the launcher) or install a real Python.

## Windows (no admin needed unless noted)
1. **Portable zip into the tools folder** (fastest, no admin, no PATH editing): download the official release zip, extract into the tools folder from `<env>` (e.g. `%USERPROFILE%\.blackwall\tools\ffmpeg\`). Folders that contain executables (including `bin\`) are added to PATH for the Bash tool automatically on the next command.
   ```powershell
   $t = "$env:USERPROFILE\.blackwall\tools"; New-Item -ItemType Directory -Force $t | Out-Null
   Invoke-WebRequest -Uri <official-zip-url> -OutFile "$t\pkg.zip"; Expand-Archive "$t\pkg.zip" "$t\pkg" -Force; Remove-Item "$t\pkg.zip"
   ```
   Find the URL on the project's official release page (search first; GitHub releases API: `https://api.github.com/repos/<owner>/<repo>/releases/latest` lists asset URLs).
2. **winget** (built into Windows 10/11): `winget search <name>`, `winget install --id <Id> -e --accept-source-agreements --accept-package-agreements`. Some packages install per-machine and show a UAC prompt; add `--scope user` when supported. Common ids: `Python.Python.3.12`, `OpenJS.NodeJS.LTS`, `Git.Git`, `GoLang.Go`, `Rustlang.Rustup`, `Microsoft.DotNet.SDK.8`, `EclipseAdoptium.Temurin.21.JDK`, `Gyan.FFmpeg`, `7zip.7zip`, `BurntSushi.ripgrep.MSVC`, `Kitware.CMake`.
3. **Scoop** (per-user package manager, no admin): install once with
   `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned; irm get.scoop.sh | iex`, then `scoop install git python nodejs-lts ffmpeg 7zip ripgrep go`; extra buckets: `scoop bucket add extras` (GUI apps), `scoop bucket add java`.
- PATH changes made by installers are picked up by the Bash tool on the next command (the registry PATH is re-read); other already-open terminals need a restart.
- C/C++ on Windows: Visual Studio Build Tools (`winget install Microsoft.VisualStudio.2022.BuildTools` + the "Desktop development with C++" workload) or MinGW-w64 via Scoop/MSYS2 (`scoop install mingw`).
- Long paths: `git config --global core.longpaths true`. Line endings: `git config --global core.autocrlf true` on Windows only if the repo has no `.gitattributes`.

## Linux
- Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y build-essential git curl python3-venv`; Fedora: `sudo dnf install -y ...`. Use `-y` and `DEBIAN_FRONTEND=noninteractive` so nothing waits for input.
- Newer language versions than the distro: Python via `uv python install 3.12` or pyenv; Node via fnm/nvm or NodeSource; Go from go.dev tarball into `/usr/local/go`; Rust via rustup (`curl https://sh.rustup.rs -sSf | sh -s -- -y`).
- No root? Install into `~/.local/bin` or the Blackwall tools folder.

## macOS
- Xcode Command Line Tools: `xcode-select --install`. Homebrew: `brew install python node go rustup-init ffmpeg ripgrep`.

## Per-project isolation (always)
- Python: `uv venv && uv pip install -r requirements.txt` (fast) or `python -m venv .venv` then `.venv\Scripts\python -m pip ...` (Windows) / `.venv/bin/python -m pip ...`. Call the venv's python directly instead of "activating" in one-shot commands.
- Node: local `npm install` (commit lockfile); run tools with `npx`; pin the version in `.nvmrc` / `engines`.
- Rust/Go: toolchain files (`rust-toolchain.toml`, `go.mod` go directive).
- Record setup steps in the project README or AGENTS.md so the next run does not rediscover them.

## Verify after installing
Run the tool with `--version` from a new Bash call, run a one-line smoke test (compile hello world, `python -c "import x"`), and report the exact versions installed.
