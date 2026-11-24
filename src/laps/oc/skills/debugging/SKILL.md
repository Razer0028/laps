---
name: debugging
description: Troubleshoot a program that fails - read the error and stack trace, reproduce it with the smallest command, locate where it starts, test one hypothesis at a time, fix the cause (not the symptom) and verify the same way it failed. Covers crashes, exceptions, wrong output, hangs, build and dependency errors, "works here but not there", intermittent failures, regressions, and watching a running program's log for errors with Monitor. Use whenever something errors, crashes, hangs or behaves wrongly.
keywords: エラー, error, 例外, exception, traceback, スタックトレース, stack trace, クラッシュ, crash, 落ちる, 落ちた, 動かない, 起動しない, バグ, bug, デバッグ, debug, 原因, segfault, 失敗する, ハング, hang, 固まる, 直して+エラー
---

# Debugging and troubleshooting

## The loop (in this order)
1. **Read the whole error.** The first error in the output is usually the cause; later ones are consequences. In a stack trace find the deepest frame in *this project's* code (Python: the last frames printed; Java/C#/JS: the first `at` line in the project's package; Rust/Go: the panic line, then the first frame in the crate/module). Note the exact message, file:line and exit code.
2. **Reproduce with the smallest command** and keep the full output: `cmd > out.log 2>&1; echo "exit=$?"` (PowerShell: `cmd *> out.log; "exit=$LASTEXITCODE"`), then Read `out.log`. If it does not reproduce, the difference between the two environments is the clue (see below).
3. **Locate.** Read the file at the line from the trace. Grep the code for the message text (many errors are raised by the project itself with a fixed string). Check the inputs: arguments, environment variables, config files, paths, the current directory.
4. **One hypothesis at a time.** Change one thing, re-run the same command, compare. Note what you ruled out. If you cannot explain why a change fixes it, it is not the fix yet.
5. **Fix the cause, not the symptom.** No blanket try/except, no `|| true`, no deleting or skipping the failing test, no longer timeouts to hide a hang, no catching and printing instead of handling.
6. **Verify the same way it failed** (same command, same input), then run the nearest tests. Report what you verified and what you could not.

## Reading common errors
- Python: `ModuleNotFoundError` → wrong interpreter or venv (`python -c "import sys; print(sys.executable)"`) or a missing dependency. `'NoneType' object has no attribute` → a value was None earlier; trace where it came from. `UnicodeDecodeError` → file encoding (Japanese Windows often writes cp932; open with an explicit encoding).
- Node: `Cannot find module` → not installed, wrong cwd, or ESM/CommonJS mismatch (`ERR_REQUIRE_ESM`). `EADDRINUSE` → something already listens on the port: find and stop it, do not pick a random port.
- Java/Kotlin: read the last `Caused by:`. `ClassNotFoundException` / `NoSuchMethodError` → dependency version mismatch (`gradle dependencies`, `mvn dependency:tree`).
- .NET: the inner exception holds the cause (`ex.ToString()`, not `ex.Message`).
- C/C++, Rust, Go builds: fix the **first** compiler error only, then rebuild. `undefined reference` → missing library or link order. Segfault → build with `-g -fsanitize=address,undefined` or get a backtrace (`gdb -batch -ex run -ex bt --args ./app`). Rust: `RUST_BACKTRACE=1`. Go: `GOTRACEBACK=all`.
- `Permission denied` / `Access is denied`: owner and mode, a file held open by another process (Windows locks open files), antivirus, or a protected folder.
- `command not found` / `is not recognized`: PATH in *this* shell; check `command -v x` / `where.exe x`.
- Exit codes: 1 generic, 2 usage, 126 not executable, 127 not found, 137 killed (SIGKILL / OOM — check `dmesg -T | tail`), 139 segfault. Windows: -1073741819 (0xC0000005) access violation, -1073741515 (0xC0000135) missing DLL.

## When it is not the code (check this before editing)
- Windows exit codes above 3,000,000,000 are NTSTATUS values: convert to hex before reading them. 3221225477 = 0xC0000005 (access violation), **3221225478 = 0xC0000006 (in-page error: the file could not be read from disk)**, 3221225501 = 0xC000001D, 3221226505 = 0xC0000409. The Bash tool prints the meaning next to the code; trust that, do not convert from memory.
- `WinError 433` / 「存在しないデバイスを指定しました」, `WinError 1117` (I/O device error), 「遅延書き込みデータの紛失」 / "Delayed Write Failed", or 0xC0000006 crashes on files that were fine a minute ago: the **drive** dropped out or is failing. It is not a lock and not a bug. Stop writing to that drive, look at the System log (`Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddHours(-1)} | Where-Object ProviderName -match 'disk|storahci|stornvme|Ntfs' | Select-Object -First 20 TimeCreated, ProviderName, Id, Message`) and tell the user: cable or power connector, drive firmware, then `chkdsk <drive>: /scan`. Do not add retries or crash handlers to the code for this.
- Same idea on Linux: `dmesg -T | grep -iE 'I/O error|reset|offline|ata[0-9]'` before blaming the program.

## Hangs and slowness
- Is it busy or waiting? CPU (`top`, `ps -o pid,stat,pcpu,etime,args -p N`, Task Manager).
- Where is it? Python `py-spy dump --pid N`; Java `jstack N`; .NET `dotnet-stack report -p N`; Linux in general `cat /proc/N/wchan`, `strace -f -p N`.
- Usual causes: waiting for stdin (run with `< /dev/null`, or pass the answer), a lock held by another process, network timeouts (DNS, proxy, firewall), a deadlock between its own threads, an unbounded retry loop.

## "Works here, not there"
Compare versions (`--version` of the runtime and key packages, the lockfile), environment variables, current directory and relative paths, OS differences (path separators, case sensitivity, CRLF line endings, encodings), permissions, and network (proxy, firewall, `localhost` vs `0.0.0.0`).

## Intermittent failures
Run it N times in a loop and count failures; add timestamps; look for shared state (ports, temp files, globals), order dependence (run the one test alone, shuffle the order), and races (an assumption that something is already started or written).

## Regressions
If it worked before: `git log -p -- <file>`, `git diff <good>..HEAD`, and `git bisect start; git bisect bad; git bisect good <rev>; git bisect run <test command>`.

## Watching a running program
For a server, a training run or any long job, start it and watch it with the **Monitor** tool instead of re-reading its log: `Monitor(command="<start command or tail -f app.log>", filter="<success marker>")` or `Monitor(file="app.log")`. Lines that look like errors are delivered even when they do not match the filter. When a ⚠ delivery arrives, stop and run the loop above on it. If the task is to keep the program healthy, wait with `Monitor(wait=...)` before finishing instead of ending while it is unattended.

## Report
The symptom (the exact message), the cause with its evidence (file:line, log line), the fix, how you verified it, and what is still unverified.
