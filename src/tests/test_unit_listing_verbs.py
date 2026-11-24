"""unit を列挙する動詞（2026-09-07 実用評価 s_sweep: 無いせいで「アクセスできない」と締めた）。"""
import json, sys
import pytest
from laps.tools import system_state as SS


def test_SystemStateにもunit列挙がある_説明つき():
    verbs = SS._verbs()
    for v in ("unit_files", "units"):
        assert v in verbs, v
        assert verbs[v][1].strip(), f"{v} の説明が空（モデルが選べない）"


@pytest.mark.skipif(sys.platform == "win32", reason="Linux の systemctl")
def test_SystemStateのunit_filesが実際に走る():
    out = SS._run({"verb": "unit_files"}) if hasattr(SS, "_run") else SS.SPEC.handler({"verb": "unit_files"})
    assert "exit=0" in str(out) and ".service" in str(out)
