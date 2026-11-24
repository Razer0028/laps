"""孫がパイプを握っても固まらない subprocess（2026-09-06・実機の git check-ignore 永久停止の再発防止）。"""
import subprocess, sys, time
import pytest
from laps import procrun


@pytest.mark.skipif(sys.platform == "win32", reason="posix の再現")
def test_孫がパイプを握っても即戻る():
    # 旧 run(capture_output=True) は sleep が終わるまで（30 秒）EOF を待って戻らない形。
    t0 = time.time()
    r = procrun.run(["sh", "-c", "sleep 30 & echo hi; exit 0"], timeout=5, text=True)
    assert r.returncode == 0 and "hi" in r.stdout and time.time() - t0 < 3


@pytest.mark.skipif(sys.platform == "win32", reason="posix の再現")
def test_timeoutで木ごと殺して例外():
    t0 = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        procrun.run(["sh", "-c", "sleep 30; echo late"], timeout=1, text=True)
    assert time.time() - t0 < 8


def test_inputと戻り値の互換():
    r = procrun.run([sys.executable, "-c", "import sys;print(sys.stdin.read().upper())"], timeout=10, text=True, input="abc")
    assert r.returncode == 0 and r.stdout.strip() == "ABC"
    r2 = procrun.run([sys.executable, "-c", "import sys;sys.exit(3)"], timeout=10, capture_output=True)
    assert r2.returncode == 3 and r2.stdout == b""
