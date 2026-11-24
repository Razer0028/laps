"""A refusal that names a route must name one that is actually open.

The 60-case bench caught the failure this guards against. A run was denied
`python3 -c`, and the refusal said only that it was not allowed. The model then
wrote, in its own prose, that `python3: command not found` — an error no tool had
produced — concluded that the environment could not run Python at all, and
stopped. It never tried `python3 hash_vs_encrypt.py`, which the gate permits.

So refusals now carry an alternative. That creates a second, worse failure mode:
pointing at a door that is also locked costs the model another turn and teaches
it that the suggestions are noise. Each pair below is (denied command, a concrete
command implementing the suggested alternative), and the test asserts the first
is refused and the second is not.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest

from laps import policy


# (denied, the alternative made concrete, mode)
PAIRS = [
    ("python3 -c 'print(1)'",        "python3 script.py",                 "workspace"),
    ("python -e 'x'",                "python analyse.py",                 "workspace"),
    ("systemctl restart ssh",        "systemctl status ssh",              "readonly"),
    ("systemctl start nginx",        "systemctl --failed",                "readonly"),
    ("find . -exec rm {} +",         "find . -name '*.py'",               "readonly"),
    ("find . -delete",               "find . -type d",                    "readonly"),
    ("git push origin main",         "git status",                        "readonly"),
    ("git merge feature",            "git log --oneline -5",              "readonly"),
    ("sed -e 'w /tmp/x' f",          "sed -n '1,5p' f",                   "readonly"),
]


@pytest.mark.parametrize("denied,alternative,mode", PAIRS)
def test_alternative_is_actually_permitted(denied, alternative, mode, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", mode)
    monkeypatch.setenv("LAPS_WORKSPACE", "/tmp")
    bad = policy.check_command(denied, cwd="/tmp")
    assert not bad.allowed, f"{denied!r} は拒否されるはずが許可された"
    good = policy.check_command(alternative, cwd="/tmp")
    assert good.allowed, (
        f"拒否メッセージが案内する道が塞がっている: {alternative!r} → {good.reason}"
    )


@pytest.mark.parametrize("denied,_alt,mode", PAIRS)
def test_denial_text_reaches_the_model(denied, _alt, mode, monkeypatch):
    """The tools print the exception verbatim, so the text has to be in it."""
    monkeypatch.setenv("LAPS_POLICY", mode)
    monkeypatch.setenv("LAPS_WORKSPACE", "/tmp")
    v = policy.check_command(denied, cwd="/tmp")
    assert not v.allowed
    if v.alternative:
        assert v.alternative in policy._denial_text(v)


def test_no_alternative_is_better_than_a_wrong_one(monkeypatch):
    """Denials with no legitimate route stay bare.

    `sudo` and `ssh` are refused in every mode and there is no permitted way to
    do what they were reaching for. Inventing an alternative there would send the
    model looking for a door that does not exist.
    """
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    for cmd in ("sudo systemctl restart ssh", "ssh g160 uptime", "apt install jq"):
        v = policy.check_command(cmd, cwd="/tmp")
        assert not v.allowed
        assert not v.alternative, f"{cmd!r} に存在しない代替を案内している: {v.alternative}"


def test_the_alternative_must_reach_the_goal_not_merely_be_permitted(monkeypatch):
    """`find -exec du` is refused; the suggestion has to still get sizes.

    The first version said "drop -exec", which is permitted and useless — the
    run wanted directory sizes and dropping -exec loses them. 80 of 200 grounded
    runs abstained on that item; all of them tried; all 97 attempts were
    `find … -exec du -sh {} +` and all 97 were refused. `du -sh dir/*` was
    allowed the entire time.
    """
    monkeypatch.setenv("LAPS_POLICY", "readonly")
    v = policy.check_command("find /home/user/project -type d -exec du -sh {} +", cwd="/home/user/project")
    assert not v.allowed
    assert "du -sh" in v.alternative, "容量を取る手段が案内されていない"
    # and what it names is genuinely open
    for cmd in ("du -sh /home/user/project/*", "du -h --max-depth=1 /home/user/project"):
        assert policy.check_command(cmd, cwd="/home/user/project").allowed, cmd
