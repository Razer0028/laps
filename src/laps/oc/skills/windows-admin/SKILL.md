---
name: windows-admin
description: Windows administration and scripting from the command line - PowerShell essentials, cmd quirks, file paths with spaces and Japanese, encodings (UTF-8 vs CP932/Shift-JIS), services, processes, event logs, scheduled tasks, registry, networking, firewall, permissions/UAC, winget, and remote management. Use for any task on a Windows machine.
keywords: powershell, レジストリ, registry, タスクスケジューラ, task scheduler, winget, バッチファイル, イベントログ, event log, windows+サービス, windows+service, ファイアウォール, firewall
---

# Windows administration

## Which shell
The Bash tool on Windows runs cmd.exe and detects PowerShell syntax. For anything beyond one simple command, call PowerShell explicitly:
`powershell -NoProfile -ExecutionPolicy Bypass -Command "..."` (Windows PowerShell 5.1, always present) or `pwsh -NoProfile -Command "..."` (PowerShell 7, if installed).
For multi-line logic write a `.ps1` file and run `powershell -NoProfile -ExecutionPolicy Bypass -File script.ps1` - quoting is far more reliable than inline commands.

## Paths and encoding (the usual source of failures)
- Quote every path: `"C:\Users\name\My Project\file.txt"`. In PowerShell use `-LiteralPath` for paths with `[ ]`.
- User profiles can contain spaces and full-width characters (including the full-width space U+3000). Use `$env:USERPROFILE` / `%USERPROFILE%` instead of typing the path.
- cmd output encoding is the console code page (CP932 on Japanese Windows): run `chcp 65001 >nul &` before commands that print Unicode, or use PowerShell.
- Windows PowerShell 5.1 writes UTF-16 with `>` and `Out-File` by default. Write UTF-8 explicitly: `Set-Content -Encoding utf8` (5.1 adds a BOM) or `[IO.File]::WriteAllText($p, $text, [Text.UTF8Encoding]::new($false))` (no BOM).
- Reading Japanese legacy files: `Get-Content -Encoding Default` (5.1, system ANSI = CP932) or in Python `open(p, encoding="cp932")`.
- `findstr` does not understand UTF-8 well; prefer `Select-String` or the Grep tool.

## PowerShell essentials
```powershell
Get-ChildItem -Recurse -File -Filter *.log | Sort-Object Length -Descending | Select-Object -First 20 FullName, Length
Get-Process | Sort-Object CPU -Descending | Select-Object -First 15 Id, ProcessName, CPU, WorkingSet64
Get-NetTCPConnection -State Listen | Select-Object LocalAddress, LocalPort, OwningProcess
Get-Service | Where-Object Status -eq 'Running'
Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, LastBootUpTime
Test-NetConnection host -Port 443
Get-FileHash file.zip -Algorithm SHA256
```
- Objects, not text: filter with `Where-Object`, select with `Select-Object`, format last (`Format-Table -AutoSize`, `ConvertTo-Json -Depth 5`).
- Errors: `$ErrorActionPreference = 'Stop'` in scripts and `try { } catch { $_ | Out-String }`. Native exe exit code: `$LASTEXITCODE`.

## Services, tasks, logs
- Services: `Get-Service name`, `Restart-Service name` (admin), `sc.exe query name` (note `sc.exe`, because `sc` is an alias for Set-Content in PowerShell).
- Event logs: `Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2,3; StartTime=(Get-Date).AddHours(-6)} -MaxEvents 100 | Format-List TimeCreated, Id, ProviderName, Message`. Application crashes: LogName Application, Id 1000/1001.
- Scheduled tasks: `Get-ScheduledTask`, `Register-ScheduledTask` with `New-ScheduledTaskAction/Trigger`; `schtasks /query /fo LIST /v` in cmd.

## Registry and environment
- `Get-ItemProperty 'HKCU:\Environment'`; user PATH changes: `[Environment]::SetEnvironmentVariable('Path', $new, 'User')` (never truncate PATH with `setx` - it cuts at 1024 chars).
- Installed programs: `Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' | Select DisplayName, DisplayVersion`.

## Admin rights
- Check: `([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrators')`.
- A process cannot elevate itself silently; `Start-Process powershell -Verb RunAs` shows a UAC prompt to the user. Prefer per-user installs and the Blackwall tools folder.

## Networking and firewall
`ipconfig /all`, `Resolve-DnsName name`, `Get-NetRoute`, `netsh advfirewall firewall show rule name=all | findstr /i "Rule Name"`, `New-NetFirewallRule -DisplayName "App 8080" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow` (admin).

## Files
- Copy large trees with `robocopy src dst /E /R:1 /W:1 /NFL /NDL` (exit codes 0-7 are success, 8+ failure).
- Archives: `Expand-Archive`, `Compress-Archive`, or `tar -xf file.zip` (bsdtar ships with Windows 10+).
- Locked files: find the holder with Resource Monitor or `handle.exe` (Sysinternals).
