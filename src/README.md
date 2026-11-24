# L.A.P.S — the agent package (`laps`)

L.A.P.S (Local Autonomous Persistent System) is a self-contained coding/infrastructure agent for a local LLM. It needs no Anthropic API,
no MCP server and no cloud service — it talks to any OpenAI-compatible endpoint
(llama-server / llama-swap / Ollama / vLLM) and does the rest itself.

**No model is bundled.** The package is Python only; you point it at a backend.

> Note: the shipped user interface and most in-code documentation are in
> Japanese. This file describes what the code actually does — it was rewritten
> on 2026-08-30 after a review found it still described a REPL that had been
> deleted. If you change behaviour, change this file in the same commit.

## Entry point

| command | what it is |
|---|---|
| `laps` | the terminal client (`laps.term.app`). Interactive by default; `-p` for one-shot. |

## Install

```bash
pip install -e .                   # core (from this folder)
pip install -e '.[full]'           # + pypdf + beautifulsoup4 (PDF read + cleaner web scrape)
pip install -e '.[dev]'            # + pytest + ruff + mypy + websockets + pillow

# from source
pip install -e .
```

Runtime dependencies: `httpx`, `rich`, `click`, `prompt_toolkit` (+ `tomli` on
Python 3.10). Python ≥ 3.10.

## Use — `laps`

```bash
laps                                       # interactive
laps      -p "fix the bug in app.py"       # one-shot; exit code follows the outcome
laps      --prompt-file task.txt           # long task text (keeps it out of argv)
laps      -p "..." --json                  # JSONL events on stdout, for a program to drive
laps      --resume <session-id>            # continue a session
laps      --version
```

Flags: `-p/--prompt`, `--prompt-file`, `--json`, `--backend URL`, `--model NAME`,
`--workspace DIR`, `--policy`, `--ethics`, `--resume ID`, `--phases`, `--calm`,
`--debug FILE`, `--config PATH`, `--version`.

### Slash commands (interactive)

```
/help      /status    /sendlog   /model [name]   /backend [url]
/workspace [path]     /policy [readonly|workspace|trusted]
/ethics [strict|standard|direct|raw]
/tools     /clear     /resume [id]    /expand    /verbose   /trace [n|all]
/cancel    /rc        /update    /exit      (/quit)
```

Unambiguous prefixes work (`/st` → `/status`).


## Configuration

Two separate locations — they are not the same file:

| what | where |
|---|---|
| terminal-client settings | `~/.config/blackwall/config.toml` (honours `XDG_CONFIG_HOME`; `--config` overrides) |
| engine state | `~/.blackwall/` — `sessions/`, `plugins/`, `hooks/`, `settings.json` (`LAPS_HOME` overrides) |

Precedence: **command-line flag > environment variable > config file > default.**

`config.toml` keys and their shipped defaults:

| key | default | meaning |
|---|---|---|
| `backend` | `http://127.0.0.1:8080/v1` | OpenAI-compatible endpoint |
| `model` | `default` | model name as the backend knows it |
| `policy` | `workspace` | execution gate: `readonly` / `workspace` / `trusted` (a 4th mode, `off`, exists in `policy.py` and is reachable via `LAPS_POLICY=off` or editing `config.toml` directly, but is deliberately not offered by `/policy` — see `docs/policy-modes.md`) |
| `ethics` | `raw` | `strict` / `standard` / `direct` / `raw` |
| `tools` | `full` | `full` (23 tools) or `focused` |
| `max_iters` | `0` | iterations per turn; `0` = no limit |
| `telemetry` | (empty) | where `/sendlog` posts; empty = nothing is sent |
| `wave` | `on` | thinking indicator: `on` / `calm` / `off` |

> Nothing is sent anywhere unless you configure it: `telemetry` is empty and `/sendlog`
> is a manual command. Set `backend` to your own server before first use.

### Environment variables

A curated subset — the full generated list (124 switches after the r4 round-4b
integration, machine-checked against the code by
`tests/test_env_switches_documented.py`) is `docs/env-switches.md`.
The interpretation-level pushback gates (`ws_guard`, `host_scope`, `verifier`, …,
each disabled with its own `LAPS_*=0`) are documented separately in
`docs/gates.md`; the 4 execution modes (`readonly`/`workspace`/`off`/`trusted`)
are in `docs/policy-modes.md`.

| variable | purpose |
|---|---|
| `LAPS_BACKEND` | endpoint (same as `backend`) |
| `LAPS_MODEL` | model name |
| `LAPS_API_KEY` | bearer token (default `sk-no-key-required`) |
| `LAPS_POLICY` / `LAPS_ETHICS` | as above |
| `LAPS_TOOLSET` | `full` / `focused` |
| `LAPS_MAX_ITERS` | iterations per turn; default `0` = no limit (read by the terminal client's config resolver, `term/config.py`) |
| `LAPS_MAX_SECONDS` / `LAPS_MAX_TOOL_CALLS` | wall-clock and tool-call budget per run; default `600` / `80`. `0` or less = no limit (read directly in `agent.py`) |
| `LAPS_HOME` | engine state root, default `~/.blackwall` |
| `LAPS_WORKSPACE` | containment boundary used by `policy` |
| `LAPS_YES` | `1` = skip destructive-command confirmation (only reaches a confirmation prompt at all on an interactive terminal; a non-interactive run under `policy=trusted` proceeds without asking either way — see `docs/policy-modes.md` and `PROMPTS/ops-safety.md`) |
| `LAPS_ALLOW_PRIVATE_FETCH` | `1` = WebFetch may hit RFC1918/loopback (default deny) |
| `LAPS_SEARCH_BACKEND` | `ddg` (default) |
| `LAPS_SSH_HOSTS` | allowed `ssh` destinations for `readonly`/`workspace`/`off` policy modes (comma-separated; `trusted` ignores this — see `docs/policy-modes.md`). |
| `LAPS_BENCH_CLEAN` | `1` = disable every path that answers without asking the model. **Always set this when measuring.** |

> Other hosts are reached with plain `ssh` through Bash, like any other command
> (allow-listed by `LAPS_SSH_HOSTS` outside the `trusted` policy).

## Commands

Capabilities are exposed as ordinary commands rather than tool declarations, so the
agent's preamble stays small (522 tokens) and the model never picks between tools.

| command | what it does |
|---|---|
| `laps` | the agent itself (terminal client) |
| `bw-undo` | snapshot the working directory and restore it |
| `bw-flow` | list and print this site's development procedures (advice, not rules) |
| `laps-mcp` | the other direction: an MCP server (stdio) that lends L.A.P.S tools to other MCP clients — file inspection, byte search, and operating the Windows desktop through the app (`laps.mcpserver`). The agent itself does not use it. |
| `bwtool` | lets scripts the model writes call some agent tools (`laps.cli_bwtool`) |

## Tools (21)

| tool | description |
|---|---|
| Bash | shell exec; `run_in_background=true` spawns detached |
| BashOutput | poll a background process |
| Read | text + image (data URI) + PDF (via pypdf) |
| Write | create/overwrite a file. `file_path` is required — it is never guessed from the body |
| Edit | exact-string replacement; atomic multi-edit via `edits=[...]` |
| Grep | ripgrep, with a grep fallback |
| Glob | recursive `rglob` |
| ApplyDiff | apply a unified diff |
| Task | spawn a restricted subagent (`general-purpose` / `Explore` / `Plan` / `Coder` / `Research`) |
| WebFetch | URL → readable text (SSRF-guarded, capped) |
| WebSearch | DuckDuckGo HTML scrape, no API key |
| TodoWrite | session-scoped checklist (the completion check reads it) |
| AskUserQuestion | mid-task clarification (headless-aware) |
| NotebookEdit | `.ipynb` cell insert/replace/delete |
| Verify | run a command and hand back its raw output — the check you run *before* saying it is fixed |
| SystemState | read local system state |
| TaskCreate / TaskList / TaskGet / TaskUpdate / TaskStop | planning tasks |

`LAPS_TOOLSET=focused` trims to Bash / Read / Write / Edit / Grep / Glob /
TodoWrite, plus SystemState unless switched off.

## Recovery patches

Repair paths for a local model that sometimes narrates instead of calling a
tool. They live in `laps/patches/`.

They obey one rule, tightened on 2026-08-30 after a review: **a repair path may
only use values that come from the request or from the model's own output. It
must never carry canned content of its own.** Earlier versions embedded a
benchmark's expected answers (a fixed `deploy.yaml` body, pinned versions, a
fixed symlink target), which both inflated benchmark scores and overwrote what
the user had actually asked for. Anything a benchmark needs belongs in the
tests, not here.

- identity fast-path, clarify-first (both off under `LAPS_BENCH_CLEAN=1`)
- markdown → Write, for `# file:` style blocks with no tool call
- markdown → Bash, **only** when the reply carries a training-sandbox fingerprint
- narration-leak → real Write, using content taken from the request
- JSON `\u` escape recovery; Qwen `✿FUNCTION✿` native-call parser; fenced-JSON fallback
- backend retry with exponential backoff on 5xx / 429

## Extensibility

**Plugins** — drop a Python file at `~/.blackwall/plugins/<name>.py` exporting
`SPEC: ToolSpec`; it is auto-registered.

**Hooks** — executables under
`~/.blackwall/hooks/{pre_tool,post_tool,pre_prompt,post_run}/*.sh` receive JSON
on stdin and may print JSON to block, override the input/result, or rewrite the
prompt.

**Skills** — `laps/skills/*/SKILL.md`, shipped with the package.

## License

MIT
