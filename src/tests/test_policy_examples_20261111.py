"""★2026-11-11 決まりの例を決まりの横に置いて試す（手本: Codex の execpolicy の match / not_match）。

policy.EXAMPLES は「このモードでこの命令は通す・止める」の表。決まりを変えて例から外れたら、ここで落ちる。
"""
from __future__ import annotations

import pytest

from laps import policy


@pytest.mark.parametrize("mode", ["readonly", "workspace", "trusted"])
def test_policy_examples_hold(mode, tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", mode)
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert policy.self_check(mode, str(tmp_path)) == []


def test_every_mode_has_both_kinds_of_example():
    for mode in ("readonly", "workspace", "trusted"):
        kinds = {want for m, _cmd, want in policy.EXAMPLES if m == mode}
        assert kinds == {True, False}, mode
