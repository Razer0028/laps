# Security policy

L.A.P.S runs shell commands and edits files on the machine it runs on. Please read the "Safety" section of the
README before using it outside a disposable VM or container.

## Reporting a vulnerability

Please report security problems **privately** through GitHub's
[private vulnerability reporting](../../security/advisories/new) for this repository, not in a public issue.
Include what you ran, what happened, and the version (`laps --version`).

This is a personal project maintained in spare time; there is no guaranteed response time.

## Scope

In scope: bypasses of the execution policy (`src/laps/policy.py`), ways for untrusted content (files, web pages,
tool output, skills) to make the agent act without the user's consent, secrets leaking into logs or prompts
(`src/laps/redact.py`), and issues in the optional relay and gateway (`src/laps/relay.py`, `src/laps/gateway.py`).

Out of scope: anything the `trusted` policy allows by design.
