"""スキル＝手順書を、行き詰まりの場（道具の結果）へ差し込む機構（2026-08-29）。

★救う例と、救ってはいけない例を必ず対で書く。この現場は「作ったのに一度も
  発火しない機構」を 2 つ作っている（停滞検知は正規化を入れるまで**構造的に
  発火不能**だった）。発火の試験だけでは、その型を捕まえられない。

★引き金は**実トレースの本文**で試す。合成した文字列では、正規化前の停滞検知も
  通っていた。`test_実トレースの本文で発火する` がその門。
"""
from __future__ import annotations

from pathlib import Path


TRACES = Path("/home/user/project/scratch/vdirtest")

# 実トレース run34 seq352 の本文（Bash・ok=True）。1 字も直していない。
REAL_BODY = (
    "<br />\n<b>Warning</b>:  PHP Startup: Unable to load dynamic library 'pdo_sqlite' "
    "(tried: /usr/lib/php/20240924/pdo_sqlite (/usr/lib/php/20240924/pdo_sqlite: "
    "cannot open shared object file: No such file or directory), "
    "/usr/lib/php/20240924/pdo_sqlite.so (/usr/lib/php/20240924/pdo_sqlite.so: "
    "undefined symbol: pdo_dbh_ce)) in <b>Unknown</b> on line <b>0</b><br />\n"
)
# 実トレース run29 の本文（同じ壁を「直そうとして」二重登録になった形）。
REAL_BODY_29 = 'PHP Warning:  Module "PDO" is already loaded in Unknown on line 0\n'


# ---- 救う例 ----------------------------------------------------------------


# ---- 救ってはいけない例 ------------------------------------------------------


# ---- 予算 --------------------------------------------------------------------


def test_道具は1個も増えていない() -> None:
    """★スキルは道具ではない。棚は 22 個のまま（50 個で 84〜95%、200 個で 41% の曲線に触らない）。"""
    from laps import tools as tools_mod
    # ★2026-09-21: 22→23（Verify を追加。「直した」と言う前に走らせる道具が無かった）。
    #   ★道具数は選択精度に効くので、増やしたら必ずここを通す（黙って増えない）。
    # ★2026-10-18: 23→22（RemoteShell を 2026-10-05 に外した。README の「Tools (22)」と同じ）。
    # ★公開版: 知識サーバーの道具を外して 21。
    assert len(tools_mod.all_specs()) == 21


# ---- 実トレース回帰 ----------------------------------------------------------


# ---- 手順書が壊れたときに「黙って消える」のを防ぐ（2026-08-30） --------------
#
# ★実際に起きたこと: SKILL.md が上限 900 字を 192 字超えたまま 0.1.147 として
#   出荷され、`load()` の SkillError を `_inject_skill` が握り潰したので、
#   **スキル機構が丸ごと死んだまま labbench 8 走行が走った**。
#   20 分前の走行（0.1.146）では `skill_injected:ping-is-not-liveness=1` が立ち、
#   エージェントは覚え書きに従って TCP → `ssh -v` まで正しく進んでいた。
#   記録のどこにも「機構が消えた」と書いていなかったのが、いちばん高くついた。

# 実記録の現物。labbench `runs/01-20260830-102114/trace.jsonl` の Bash 結果。1 字も直していない。
REAL_PING = (
    "[exit=1]\nPING 192.0.2.251 (192.0.2.251) 56(84) bytes of data.\n\n"
    "--- 192.0.2.251 ping statistics ---\n"
    "3 packets transmitted, 0 received, 100% packet loss, time 2044ms\n\n"
)


