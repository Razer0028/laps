"""A benchmark that runs without a backend produces numbers that mean nothing.

The old BLACKWALL's saved results show what that costs. `autonomy_v2_final.json`
records 12/30, and five of those passes — hello_world, fizzbuzz,
requirements_pin, json_write, git_init — have
`output: "[backend error] Cannot send a request, as the client has been closed."`
and `passed: true`. Their checks only asked whether a file existed and whether a
command ran, both of which a dead run can satisfy from leftover state. Design
documents written afterwards quote 24/30 and 27/30 as the baseline, figures that
cannot be reproduced from anything saved.

This project has a preflight that refuses to start when the backend is
unreachable, and until now nothing held it in place. Deleting it would be silent:
the benchmark would still run, still print a score, and the score would be
fiction. So the guard gets a test, and the test says why.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SRC))
import pytest


def _run_bench(env_extra: dict, timeout: int = 120) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env.update(env_extra)
    return subprocess.run(
        [str(SRC.parent / ".venv" / "bin" / "python"), "bench/autonomy_v2.py", "--clean"],
        cwd=str(SRC), env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_the_bench_refuses_to_run_without_a_backend(tmp_path):
    """Port 9 is discard: nothing listens, and the connection fails immediately."""
    p = _run_bench({"LAPS_BACKEND": "http://127.0.0.1:9/v1",
                    "LAPS_POLICY": "workspace",
                    "LAPS_TOOLSET": "focused"})
    out = p.stdout + p.stderr
    assert "preflight" in out, f"preflight が働いていない:\n{out[:400]}"
    assert p.returncode != 0, "到達不可なのに正常終了している"
    # and crucially: no score was printed
    for fake in ("/30", "/59", "合格", "passed"):
        assert f"{fake} " not in p.stdout.split("preflight")[0], \
            "preflight より前に点数らしきものを出している"


def test_a_dead_run_cannot_satisfy_a_file_only_check():
    """The shape of the old failure, stated as a property of our checks.

    Five old cases passed on a dead backend because their checks were "does this
    file exist" and "does this command exit 0" — satisfiable by leftovers. Ours
    run in a fresh temporary directory per case, so nothing survives to satisfy
    them.
    """
    text = (SRC / "bench" / "autonomy_v2.py").read_text()
    assert "mkdtemp" in text or "TemporaryDirectory" in text, \
        "ケースごとの作業ディレクトリが使い捨てでない"
