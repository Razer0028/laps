#!/usr/bin/env bash
# =============================================================================
# build_windows_installer.sh — Windows 版のインストーラ（NSIS・x64）を Linux から組み立てる
#
#   1. scripts/build_pyruntime.sh で同梱の Python（app/pyruntime）を作る
#   2. tauri build --target x86_64-pc-windows-gnu --bundles nsis
#   3. できた .exe の SHA-256 を出す
#
# 要るもの（Debian / Ubuntu の例）:
#   rustup target add x86_64-pc-windows-gnu
#   apt install mingw-w64 nsis curl unzip rsync python3-pip
#   Node.js 18 以上（npm）
#
# できる物: app/src-tauri/target/x86_64-pc-windows-gnu/release/bundle/nsis/L.A.P.S Console_<版>_x64-setup.exe
# ★署名はしない（コード署名の証明書を持っていない）。自動更新の仕組みも入っていない。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${CARGO_TARGET_DIR:-${ROOT}/app/src-tauri/target}"

bash "${ROOT}/scripts/build_pyruntime.sh"

cd "${ROOT}/app"
npm ci --no-audit --no-fund

# 組み立てた機械のパス（利用者名・作業場所）を実行ファイルに残さない
export RUSTFLAGS="${RUSTFLAGS:-} --remap-path-prefix=${ROOT}=/laps --remap-path-prefix=${CARGO_HOME:-${HOME}/.cargo}=/cargo --remap-path-prefix=${RUSTUP_HOME:-${HOME}/.rustup}=/rustup"

./node_modules/.bin/tauri build --target x86_64-pc-windows-gnu --bundles nsis

OUT_DIR="${TARGET_DIR}/x86_64-pc-windows-gnu/release/bundle/nsis"
ls -l "${OUT_DIR}"/*.exe
( cd "${OUT_DIR}" && sha256sum ./*.exe )
