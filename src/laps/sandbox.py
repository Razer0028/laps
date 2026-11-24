"""Optional sandboxing for Bash tool execution.

Modes:
- none: direct execution (default)
- firejail: wrap with `firejail --quiet` if installed
- systemd-run: wrap with `systemd-run --user --scope` if available
- docker: spin up an ephemeral container (--rm, no network if specified)

Windows 対応:
- firejail は WSL 経由または明示的な設定で使える
- docker は Docker Desktop で Windows 標準対応
- systemd-run は WSL 経由
- LAPS_SANDBOX=docker のとき docker を優先（WSL 以外でも動く）
"""
from __future__ import annotations
import os
import platform
import shutil
import sys
from typing import Literal


SandboxMode = Literal["none", "firejail", "systemd-run", "docker"]


def _is_windows() -> bool:
    """WSL 上でも sys.platform で区別する（2026-10-17 Windows 対応）。"""
    return sys.platform == "win32" or platform.system() == "Windows"


def _docker_available_on_win() -> bool:
    """Windows で docker が使えるか（Docker Desktop 等）。"""
    return bool(shutil.which("docker"))


def _systemd_bus_available() -> bool:
    """systemd-run が実行時に bus へ接続できるか（コンテナ等で失敗する場合の検出）。"""
    import subprocess
    try:
        r = subprocess.run(
            ["systemd-run", "--quiet", "--user", "--echo", ":"],
            capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def detect_mode(prefer: SandboxMode | None = None) -> SandboxMode:
    if prefer == "none":
        return "none"
    explicit = prefer or os.environ.get("LAPS_SANDBOX")
    if explicit and explicit != "auto":
        return explicit  # trust user
    # Windows の場合: firejail は WSL 限定、docker を優先
    if _is_windows():
        if _docker_available_on_win():
            return "docker"
        return "none"
    if shutil.which("firejail"):
        return "firejail"
    if shutil.which("systemd-run") and _systemd_bus_available():
        return "systemd-run"
    return "none"


def wrap_command(cmd: str, mode: SandboxMode = "none", *, cwd: str | None = None,
                 net: bool = True) -> str | list[str]:
    """Return either a shell string or argv list (caller passes shell=True/False accordingly)."""
    if mode == "none":
        return cmd
    if mode == "firejail":
        flags = ["firejail", "--quiet"]
        if not net:
            flags.append("--net=none")
        if cwd:
            flags.extend(["--private-cwd=" + cwd])
        flags.extend(["--", "/bin/bash", "-c", cmd])
        return flags
    if mode == "systemd-run":
        flags = ["systemd-run", "--quiet", "--user", "--scope",
                 "--property=MemoryMax=4G",
                 "--property=CPUQuota=200%"]
        if cwd:
            flags.extend(["--working-directory", cwd])
        flags.extend(["/bin/bash", "-c", cmd])
        return flags
    if mode == "docker":
        flags = ["docker", "run", "--rm"]
        if not net:
            flags.append("--network=none")
        if cwd:
            flags.extend(["-v", f"{cwd}:/workspace", "-w", "/workspace"])
        flags.extend(["ubuntu:22.04", "bash", "-c", cmd])
        return flags
    return cmd
