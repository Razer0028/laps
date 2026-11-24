---
name: linux-server-ops
description: Operate and troubleshoot Linux servers - systemd services and timers, journald logs, disks and filesystems, memory/CPU, processes and ports, networking (ip, ss, DNS, routes), firewalls (ufw, nftables), nginx/Caddy reverse proxies and TLS, users/permissions/sudo, cron, package management, SSH, and a step-by-step incident playbook. Use for any Linux host task, local or over SSH.
keywords: systemd, systemctl, journalctl, journald, ubuntu, debian, linux+サーバ, linux+server, nginx, cron, ディスクが, dmesg, ログを調
---

# Linux server operations

## Incident playbook (in this order)
1. **What changed and when?** `journalctl --since -1h -p warning --no-pager | tail -100`, `last -x | head`, recent package changes (`grep -h " install \| upgrade " /var/log/dpkg.log | tail`; `dnf history`).
2. **Is the service running and what does it say?** `systemctl status <unit> --no-pager -l`, `journalctl -u <unit> -n 200 --no-pager`, `systemctl show <unit> -p NRestarts,ActiveEnterTimestamp,Result`.
   "active" is not "working": `Restart=always` hides crash loops (check NRestarts), and oneshot units with `RemainAfterExit=yes` stay "active" after their job broke. Check the actual output (port, file, response).
3. **Resources**: `df -h` and `df -i` (inodes), `free -h`, `uptime` (load vs `nproc`), `top -b -n1 | head -20`, `dmesg -T | tail -50` (OOM killer, disk errors).
4. **Network**: `ss -ltnp` (who listens where), `curl -sv http://127.0.0.1:PORT/health`, `ip -br a`, `ip route`, `resolvectl status` or `cat /etc/resolv.conf`, `getent hosts name`.
5. Fix the cause, then verify the same way you observed the failure.

## systemd
- Unit at `/etc/systemd/system/app.service`:
```
[Unit]
Description=App
After=network-online.target
Wants=network-online.target

[Service]
User=app
WorkingDirectory=/opt/app
EnvironmentFile=-/etc/app/env
ExecStart=/opt/app/.venv/bin/python -m app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```
- After editing: `systemd-analyze verify app.service; systemctl daemon-reload; systemctl restart app; systemctl status app`.
- Overrides without touching the packaged unit: `systemctl edit app` (creates a drop-in).
- Timers instead of cron for new jobs: `app-job.service` (Type=oneshot) + `app-job.timer` (`OnCalendar=*-*-* 03:00`, `Persistent=true`); list with `systemctl list-timers`.
- Logs: `journalctl -u app -f`, `--since "2026-10-05 10:00"` or relative `--since -30min` (the server's time zone may not be yours: check `timedatectl`).

## Disks
- Full disk: `du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -15` then drill down; common culprits: journald (`journalctl --vacuum-size=500M`), docker (`docker system df`), logs, old kernels, core dumps.
- Deleted-but-open files still use space: `lsof +L1`; restart the holder.
- New disk: `lsblk -f`, `mkfs.ext4 /dev/sdX1`, mount by UUID in `/etc/fstab` with `nofail`; test with `mount -a` before rebooting.

## Processes and ports
- `ps -eo pid,ppid,user,%cpu,%mem,etimes,cmd --sort=-%cpu | head`; who holds a port: `ss -ltnp 'sport = :8080'`.
- Stop processes by PID or unit, not by `pkill -f <word>` (it can match your own shell). Check with `pgrep -a <name>` first.

## Firewall
- ufw: `ufw status verbose`, `ufw allow 443/tcp`, `ufw allow from 192.168.1.0/24 to any port 22`. Before enabling on a remote host, allow SSH first.
- nftables: `nft list ruleset`. Never flush rules on a remote host without a timed rollback (e.g. `at now + 5 min` restoring the saved ruleset).

## Web server and TLS
- nginx: config test `nginx -t` before `systemctl reload nginx`; sites in `/etc/nginx/sites-available` linked to `sites-enabled`. Common errors: 502 (backend down / wrong port / SELinux), 413 (`client_max_body_size`), 504 (backend slow; `proxy_read_timeout`).
- Caddy: automatic HTTPS; `caddy validate --config /etc/caddy/Caddyfile`, `systemctl reload caddy`.
- Certificates: `certbot --nginx -d example.com`; check expiry `openssl s_client -connect host:443 -servername host </dev/null 2>/dev/null | openssl x509 -noout -dates`.

## Users and permissions
- `id user`, `sudo -l -U user`; add sudo via `/etc/sudoers.d/<file>` and validate with `visudo -cf <file>`.
- Permission denied: check every directory on the path (`namei -l /path/to/file`), ownership, mode, ACLs (`getfacl`), and SELinux/AppArmor (`ausearch -m avc -ts recent`, `aa-status`).

## SSH
- Run remote commands non-interactively: `ssh -o BatchMode=yes -o ConnectTimeout=10 host 'cmd'`. Quote the remote command in single quotes; `$VAR` inside expands remotely.
- Copy: `scp`, `rsync -avz --partial src host:dst/` (trailing slashes matter).
- Host key changed: understand why (reinstalled host?) before `ssh-keygen -R host`.

## Packages
- Debian/Ubuntu: `apt-get update && apt-get install -y pkg`; held/broken: `apt-mark showhold`, `dpkg -l | awk '$1!="ii"'`. Read what `apt` plans before confirming upgrades of drivers/kernels.
- RHEL/Fedora: `dnf install -y`, `dnf history undo <id>`.

## Changing config safely
Backup with a timestamp (`cp -a f f.bak-$(date +%Y%m%d-%H%M)`), edit, validate with the program's own checker, reload (not restart) when supported, verify, and keep the backup path in your report.
