---
name: shell-scripting
description: Write correct, safe shell scripts - Bash strict mode, quoting, arrays, traps and cleanup, argument parsing, portability (bash vs sh/dash, GNU vs BSD tools), safe file handling, here-docs, and PowerShell script equivalents. Use when writing or fixing .sh/.bash/.ps1 scripts or long one-liners.
keywords: シェルスクリプト, shell script, bash スクリプト, bashスクリプト
---

# Shell scripting

## Bash skeleton
```bash
#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

usage() { echo "usage: ${0##*/} [-n] <input>"; }
dry=0
while getopts ':nh' opt; do case $opt in n) dry=1;; h) usage; exit 0;; *) usage >&2; exit 2;; esac; done
shift $((OPTIND-1)); [[ $# -ge 1 ]] || { usage >&2; exit 2; }
in=$1; [[ -f $in ]] || die "not a file: $in"
```

## Rules that prevent most bugs
- Quote every expansion: `"$var"`, `"${arr[@]}"`, `"$(cmd)"`. Unquoted variables split on spaces and expand globs.
- Arrays for argument lists: `args=(-v --out "$dir"); cmd "${args[@]}"`. Never build commands in strings and `eval` them.
- `[[ ]]` for tests in bash; `$(...)` not backticks; `printf` instead of `echo -e`.
- `set -e` does not fire inside `if`, `&&`/`||` chains or command substitutions in some contexts; check critical commands explicitly. `pipefail` makes a pipeline fail if any part fails.
- Read lines safely: `while IFS= read -r line; do ...; done < file`. Files with spaces/newlines: `find . -name '*.log' -print0 | xargs -0 rm --`.
- `--` before user-supplied paths so names starting with `-` are not options.
- Write files atomically: write to a temp file in the same directory, then `mv` it over the target.
- Here-docs: `<<'EOF'` (quoted) when the content must not expand `$` or backticks; unquoted `<<EOF` expands them.
- Check with `bash -n script.sh` (syntax) and `shellcheck script.sh` when available.

## Portability
- `#!/bin/sh` on Debian/Ubuntu is dash: no arrays, no `[[ ]]`, no `{a,b}` brace expansion, no `source` (use `.`). Use `#!/usr/bin/env bash` when you use bash features.
- GNU vs BSD (macOS): `sed -i` needs `sed -i ''` on BSD; `date -d` is GNU-only; `readlink -f` is GNU (use `realpath` or Python). Prefer portable flags or detect `uname`.
- Line endings must be LF; a script saved with CRLF fails with `$'\r': command not found` (fix: `sed -i 's/\r$//' f`).

## Logging and exit codes
- Messages to stderr, data to stdout. Exit 0 success, 1 failure, 2 usage error. Print what failed and with which input.
- Long-running scripts: log with timestamps `printf '%(%F %T)T %s\n' -1 "msg"`.

## PowerShell equivalents
```powershell
[CmdletBinding()] param([Parameter(Mandatory)][string]$Input, [switch]$DryRun)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
try {
  if (-not (Test-Path -LiteralPath $Input)) { throw "not found: $Input" }
  # ...
} catch { Write-Error $_; exit 1 }
```
Native commands do not throw on failure in 5.1: check `$LASTEXITCODE` after each one. Use `Join-Path`, `-LiteralPath`, and `ConvertFrom-Json`/`ConvertTo-Json` instead of string parsing.
