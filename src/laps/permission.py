"""Permission gate for destructive operations.

Default: prompt user. With LAPS_YES=1 or --yes: skip prompts.
Settings (~/.blackwall/settings.json) can pre-allow or pre-deny patterns.
"""
from __future__ import annotations
import os
import re
import sys

from .settings import Settings


DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+-rf?\b"),
    re.compile(r"\bmkfs"),
    re.compile(r"\bdd\s+if="),
    re.compile(r":\(\)\{.*\};:"),  # fork bomb
    re.compile(r"\bshutdown\b"),
    re.compile(r"\bpoweroff\b"),
    re.compile(r"\bsystemctl\s+(stop|disable|mask|restart|reload)\b"),  # restart/reload を追加
    re.compile(r"\bdocker\s+rm\b"),
    re.compile(r"\bdocker\s+system\s+prune"),
    re.compile(r"DROP\s+TABLE", re.IGNORECASE),
    re.compile(r"DELETE\s+FROM", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+--force\b"),
    re.compile(r"\bgit\s+reset\s+--hard\b"),
    re.compile(r"\bfind\b.*\b-delete\b"),  # find -delete を追加（旧 denylist の穴）
    re.compile(r"^>\s*\S"),  # > file (上書き) を追加
    re.compile(r"\btruncate\b"),  # truncate を追加
    re.compile(r"\bcurl\b.*\|\s*(ba)?sh\b"),  # curl | sh を追加
    re.compile(r"\bscp\b"),  # scp を追加（保護ホストへの書き込み）
    re.compile(r"\bsudo\b"),  # sudo を追加
]


def is_destructive(command: str) -> bool:
    return any(p.search(command) for p in DESTRUCTIVE_PATTERNS)


def _match_any(patterns: list[str], command: str) -> bool:
    for p in patterns:
        try:
            if re.search(p, command):
                return True
        except re.error:
            if p in command:
                return True
    return False


def confirm(command: str, kind: str = "Bash") -> bool:
    """Return True if user confirms (or auto-yes/settings allow)."""
    # Settings-level pre-decisions
    try:
        s = Settings.load()
    except Exception:
        s = Settings()
    if s.deny_patterns and _match_any(s.deny_patterns, command):
        sys.stderr.write(f"[denied by settings.deny_patterns] {command}\n")
        return False
    if s.allow_patterns and _match_any(s.allow_patterns, command):
        return True
    if s.auto_yes or os.environ.get("LAPS_YES") == "1":
        return True
    if not sys.stdin.isatty():
        # ★聞けない場（サイドカー・パイプ・cron）の既定は拒否。
        #   ただし **policy=trusted は別**（2026-09-02）。trusted は利用者が
        #   「この機械で全部やらせる」と決めたモードで、policy はもう
        #   `rm -rf` も `sudo` も通している。そこへこの 13 本の正規表現が
        #   **二段目の門**として残り、しかも聞く相手が居ないので必ず拒否になる。
        #   出荷構成（GUI + trusted）では sidecar.rs が意図的に LAPS_YES を
        #   渡さないので、`rm -rf build` の類は**理由も代案も無く**
        #   「destructive command denied by permission gate」で止まっていた。
        #   ここは安全境界ではない（境界は policy）。モードの申告と実装を揃える。
        #   ⚠ 実測できているのは「この経路に来る頻度が低い」ことまで:
        #     監査 17,149 件の許可 Bash のうち is_destructive に当たるのは 3 件
        #     （すべて trusted・`rm -rf _selftest`・2026-08-22）。
        #     GUI 実機の記録はこの機械に無いので、そちら側の頻度は未測定。
        try:
            from .policy import _mode
            if _mode() == "trusted":
                return True
        except Exception:
            pass
        sys.stderr.write(
            f"[permission] 聞ける相手が居ないので拒否しました（{kind}）: {command}\n"
            "  LAPS_YES=1、settings.allow_patterns、または policy=trusted で通ります\n")
        return False
    sys.stderr.write(f"\n⚠ {kind} about to execute destructive command:\n  {command}\nProceed? [y/N] ")
    sys.stderr.flush()
    try:
        ans = input().strip().lower()
    except EOFError:
        return False
    return ans in {"y", "yes"}
