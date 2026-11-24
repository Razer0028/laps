"""~/.ssh/config の別名を <env> に載せる部分（oc/hostfacts.ssh_hosts）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from laps.oc import hostfacts  # noqa: E402


def test_trailing_comment_is_not_in_value(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text(
        "# 先頭の注記\n"
        "Host pi\n"
        "    HostName 192.0.2.253   # ★2026-09-20 support: 切り戻し\n"
        "    User root\n"
        "Host *\n"
        "    ServerAliveInterval 30\n", encoding="utf-8")
    assert hostfacts.ssh_hosts(cfg) == ["pi (root@192.0.2.253)"]


def test_port_and_user(tmp_path):
    cfg = tmp_path / "config"
    cfg.write_text("Host a b\n  HostName h.lan\n  Port 2222\n", encoding="utf-8")
    assert hostfacts.ssh_hosts(cfg) == ["a (h.lan:2222)", "b (h.lan:2222)"]
