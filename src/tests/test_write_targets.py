"""Where a write-capable program is pointed, not just which program it is.

check_path has enforced workspace containment from the start, and redirects and
the Write/Edit tools went through it, so the gate looked closed. The programs
_WORKSPACE_EXTRA adds never did. An adversarial review of the project plan found
it by asking the gate directly: `rm -rf /etc/cron.d`, `chmod -R 777 /srv`,
`touch /root/pwned` and `mv blackwall/agent.py /tmp/x` were all permitted in
workspace mode — the mode every development benchmark here runs in, on this
machine, with the project's own source in reach.

The module docstring blamed interpreters for workspace being porous. That is
measured and true, and it let this sit unexamined: these commands are permitted
with LAPS_NO_INTERPRETERS=1 too, which the docstring offers as the stricter
configuration.

Both directions are pinned. Over-refusal is the failure mode a fix like this
invites, and it is not obviously cheaper than the hole — an agent that cannot
`rm -rf build` stops being useful and starts being worked around.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest

from laps import policy

WS = "/tmp/ws"

MUST_DENY = [
    "rm -rf /etc/cron.d",
    "rm -rf /etc/*",
    "chmod -R 777 /srv",
    "touch /root/pwned",
    "mkdir -p /etc/systemd/system/x",
    # mv deletes the source wherever the destination is
    "mv /home/user/project/src/laps/agent.py /tmp/x",
    "tar -xf a.tgz -C /etc",
    "cp secret.txt /root/x",
    # relative escapes: resolved against the command's cwd, not this process's
    "rm -rf ../../etc/cron.d",
    "mv build ../../../root/x",
    "tar czf /root/o.tgz /tmp/ws/d",
]

MUST_ALLOW = [
    "rm -rf /tmp/ws/build",
    "rm -rf build",
    "rm -rf *",                       # a glob, unexpanded, anchored at cwd
    "mkdir -p /tmp/ws/a/b",
    "mkdir -p sub/dir",
    "touch /tmp/ws/f",
    "touch out.txt",
    "chmod 755 /tmp/ws/s.sh",         # 755 is a mode, not a path
    "chmod +x /tmp/ws/run.sh",
    "cp /srv/shared/COMMON.md /tmp/ws/",   # copying in reads the source
    "cp -r /tmp/ws/x /tmp/ws/y",
    "mv /tmp/ws/a /tmp/ws/b",
    "mv old.py new.py",
    "tar czf /tmp/ws/o.tgz /tmp/ws/d",     # `czf` is an option bundle
    "tar xzf archive.tgz",
]


@pytest.fixture(autouse=True)
def _workspace(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", WS)


@pytest.mark.parametrize("cmd", MUST_DENY)
def test_writes_outside_the_workspace_are_refused(cmd):
    v = policy.check_command(cmd, cwd=WS)
    assert not v.allowed, f"{cmd!r} が作業ディレクトリ外に書けてしまう"


@pytest.mark.parametrize("cmd", MUST_ALLOW)
def test_ordinary_work_still_runs(cmd):
    v = policy.check_command(cmd, cwd=WS)
    assert v.allowed, f"正当な作業を止めている: {cmd!r} → {v.reason}"


@pytest.mark.parametrize("cmd", MUST_DENY)
def test_holds_without_interpreters(cmd, monkeypatch):
    """The stricter configuration the docstring recommends had the same hole."""
    monkeypatch.setenv("LAPS_NO_INTERPRETERS", "1")
    assert not policy.check_command(cmd, cwd=WS).allowed


def test_readonly_still_refuses_before_reaching_the_path_check():
    """readonly has no write-capable programs at all; this is defence in depth."""
    import os
    os.environ["LAPS_POLICY"] = "readonly"
    try:
        for cmd in ("rm -rf /tmp/ws/x", "touch /tmp/ws/f"):
            assert not policy.check_command(cmd, cwd=WS).allowed
    finally:
        os.environ["LAPS_POLICY"] = "workspace"
