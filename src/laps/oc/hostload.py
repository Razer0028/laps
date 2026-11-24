"""作業中のホストのリソース（2026-10-13 オーナー「実行されているホストのリソース状況を確認しながら作業を進めさせる仕組みも追加」）。

道具の結果の頭に `[Host]` の一行を添える（この機械＝Bash が動く所の CPU・メモリ・ディスク・GPU）。添えるのは:
  ・走行の最初の道具の結果（いまの状態の基準）
  ・限界に近い物が出たとき・それが悪くなったとき（同じ状態のままなら繰り返さない・_levels の注記）
  ・長くかかった道具（LONG 秒以上）の後（その命令がどれだけ食ったかが見える）
  ・それ以外は EVERY 秒に 1 回
★前置き（system）には入れない。毎手番変わる値を前に置くと、推論機の読み込みの使い回し（先頭一致）が崩れて全部読み直しになる。
★測るだけで何も止めない。どうするかはモデルが決める。
★Lab（作業用のコンテナ）の結果には付けない（命令は 作業用のコンテナ で走る。作業用のコンテナ の資源は Lab action=status）。
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

#: 定期の一行の間隔（秒）。
EVERY = 600
#: これ以上かかった道具の後は一行を添える（秒）。
LONG = 60
#: 旧: 同じ警告を繰り返す間隔（秒）。★2026-10-16 から使わない（悪くなったときだけ言い直す・Watch._levels）。
HOLD = 300
#: 測り直す間隔（秒）。これより新しい値は使い回す。
FRESH = 15

# 限界の目安
MEM_LOW_FRAC, MEM_LOW_BYTES = 0.10, 1.5 * 2**30
DISK_LOW_FRAC, DISK_LOW_BYTES = 0.05, 5 * 2**30
GPU_MEM_HIGH = 0.95
GPU_TEMP_HIGH = 85
CPU_BUSY = 95
COMMIT_LOW_FRAC = 0.08          # Windows のコミット（物理メモリ＋ページファイル）の残り

_LOCK = threading.Lock()


def enabled() -> bool:
    return (os.environ.get("LAPS_HOSTLOAD") or "1").strip().lower() not in ("0", "false", "off", "no")


def _every() -> float:
    try:
        return max(60.0, float(os.environ.get("LAPS_HOSTLOAD_EVERY") or EVERY))
    except ValueError:
        return float(EVERY)


def _gb(n: float) -> str:
    return f"{n / 2**30:.1f} GB" if n < 100 * 2**30 else f"{n / 2**30:,.0f} GB"


# ── 測る ──────────────────────────────────────────────────────────────

def _cpu_times() -> tuple[float, float] | None:
    """(busy, total) の累計。差を取って使用率にする。"""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                return None
            v = [(f.dwHighDateTime << 32) | f.dwLowDateTime for f in (idle, kern, user)]
            total = v[1] + v[2]                       # kernel は idle を含む
            return float(total - v[0]), float(total)
        with open("/proc/stat", encoding="ascii") as f:
            parts = [float(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)
        total = sum(parts[:8])
        return total - idle, total
    except Exception:
        return None


def _memory() -> dict:
    out: dict = {}
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + \
                           [(n, ctypes.c_ulonglong) for n in ("t", "a", "tp", "ap", "tv", "av", "ae")]
            ms = MS()
            ms.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
                out = {"total": ms.t, "avail": ms.a, "commit_total": ms.tp, "commit_avail": ms.ap}
        else:
            vals = {}
            with open("/proc/meminfo", encoding="ascii") as f:
                for ln in f:
                    k, _, v = ln.partition(":")
                    if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                        vals[k] = int(v.split()[0]) * 1024
            out = {"total": vals.get("MemTotal", 0), "avail": vals.get("MemAvailable", 0),
                   "swap_total": vals.get("SwapTotal", 0), "swap_free": vals.get("SwapFree", 0)}
            # コンテナ（cgroup v2）の上限が機械より小さければ、そちらが実際の天井
            try:
                with open("/sys/fs/cgroup/memory.max", encoding="ascii") as f:
                    mx = f.read().strip()
                with open("/sys/fs/cgroup/memory.current", encoding="ascii") as f:
                    cur = int(f.read().strip())
                if mx.isdigit() and 0 < int(mx) < out["total"]:
                    with open("/sys/fs/cgroup/memory.stat", encoding="ascii") as f:
                        cache = _file_cache(f.read())
                    out["total"], out["avail"] = int(mx), max(0, int(mx) - cur + cache)
            except (OSError, ValueError):
                pass
    except Exception:
        return {}
    return out


def _file_cache(stat: str) -> int:
    """cgroup の memory.stat からファイルのキャッシュの量。memory.current はこれを含むので、引かないと「限界に近い」と誤る
    （2026-10-31 課題集: 22 走行で 22 回・上位 3 つの合計 0.3GB なのに「0.6GB free of 8GB」）。"""
    n = 0
    for ln in stat.splitlines():
        k, _, v = ln.partition(" ")
        if k in ("inactive_file", "active_file") and v.strip().isdigit():
            n += int(v)
    return n


def _mount(p: str) -> str:
    p = os.path.abspath(p)
    if sys.platform == "win32":
        d = os.path.splitdrive(p)[0]
        return d + "\\" if d.endswith(":") else (d or p)
    while not os.path.ismount(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    return p


def _disks(cwd: str) -> list[tuple[str, int, int]]:
    """(場所, 空き, 全体)。作業場の所と一時フォルダの所（別のドライブ・別のファイルシステムなら）。"""
    seen, out = set(), []
    for p in (cwd, tempfile.gettempdir()):
        try:
            u = shutil.disk_usage(p)
            where = _mount(p)
            key = where.lower() if sys.platform == "win32" else os.stat(p).st_dev
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append((where, u.free, u.total))
    return out


_SMI: dict = {"path": None, "looked": False, "fails": 0}


def _gpus() -> list[dict]:
    if not _SMI["looked"]:
        _SMI["path"], _SMI["looked"] = shutil.which("nvidia-smi"), True
    if not _SMI["path"] or _SMI["fails"] >= 2:
        return []
    try:
        kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}   # CREATE_NO_WINDOW
        o = subprocess.run([_SMI["path"], "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6, **kw).stdout
        rows = []
        for ln in o.strip().splitlines():
            n, u, mu, mt, t = [x.strip() for x in ln.split(",")]
            rows.append({"name": n.replace("NVIDIA ", "").replace("GeForce ", ""), "util": int(float(u)),
                         "used": int(float(mu)) * 2**20, "total": int(float(mt)) * 2**20, "temp": int(float(t))})
        _SMI["fails"] = 0
        return rows
    except Exception:
        _SMI["fails"] += 1
        return []


def _top_memory(n: int = 3) -> list[tuple[str, int]]:
    """メモリを多く使っているプログラム（名前でまとめる）。限界に近いときだけ呼ぶ（Windows の tasklist は 1 秒ほどかかる）。"""
    try:
        mem: dict[str, int] = {}
        if sys.platform == "win32":
            import csv
            raw = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, timeout=15,
                                 creationflags=0x08000000).stdout
            try:
                text = raw.decode("cp932")
            except UnicodeDecodeError:
                text = raw.decode("utf-8", "replace")
            for row in csv.reader(text.splitlines()):
                if len(row) >= 5:
                    kb = int("".join(c for c in row[4] if c.isdigit()) or 0)
                    mem[row[0]] = mem.get(row[0], 0) + kb * 1024
        else:
            out = subprocess.run(["ps", "-eo", "rss=,comm="], capture_output=True, text=True, timeout=10).stdout
            for ln in out.splitlines():
                kb, _, name = ln.strip().partition(" ")
                if kb.isdigit():
                    mem[name.strip()] = mem.get(name.strip(), 0) + int(kb) * 1024
        return sorted(mem.items(), key=lambda x: -x[1])[:n]
    except Exception:
        return []


class Watch:
    """1 走行（1 つのエージェントの本体）につき 1 つ。"""

    def __init__(self) -> None:
        self.host = socket.gethostname()
        self.cores = os.cpu_count() or 0
        self._cpu_prev = _cpu_times()
        self._snap: dict | None = None
        self._snap_at = 0.0
        self._last_line_at = 0.0
        self._warned: dict[str, float] = {}      # 警告の種類 → 最後に言った時の悪さ（_levels の値）
        self._busy_streak = 0
        self.lines = 0

    def snapshot(self) -> dict:
        now = time.monotonic()
        if self._snap is not None and now - self._snap_at < FRESH:
            return self._snap
        cur = _cpu_times()
        if self._cpu_prev is None or cur is None or cur[1] - self._cpu_prev[1] <= 0:
            time.sleep(0.25)
            prev, cur = cur, _cpu_times()
        else:
            prev = self._cpu_prev
        cpu = None
        if prev and cur and cur[1] > prev[1]:
            cpu = round(100.0 * (cur[0] - prev[0]) / (cur[1] - prev[1]))
        self._cpu_prev = cur
        snap = {"cpu": cpu, "mem": _memory(), "disks": _disks(os.getcwd()), "gpus": _gpus()}
        self._busy_streak = self._busy_streak + 1 if (cpu or 0) >= CPU_BUSY else 0
        self._snap, self._snap_at = snap, now
        return snap

    def _warnings(self, s: dict) -> dict[str, str]:
        w: dict[str, str] = {}
        m = s.get("mem") or {}
        if m.get("total"):
            if m["avail"] < MEM_LOW_BYTES or m["avail"] < MEM_LOW_FRAC * m["total"]:
                w["mem"] = f"memory {_gb(m['avail'])} free of {_gb(m['total'])}"
            ct, ca = m.get("commit_total") or 0, m.get("commit_avail") or 0
            if ct and ca < COMMIT_LOW_FRAC * ct:
                w["commit"] = f"commit (RAM + page file) {_gb(ca)} left of {_gb(ct)}"
            st, sf = m.get("swap_total") or 0, m.get("swap_free") or 0
            if st and "mem" in w and sf < 0.5 * st:
                w["swap"] = f"swap {_gb(st - sf)} used of {_gb(st)}"
        for where, free, total in s.get("disks") or []:
            if total and (free < DISK_LOW_BYTES or free < DISK_LOW_FRAC * total):
                w[f"disk:{where}"] = f"disk {where} {_gb(free)} free of {_gb(total)}"
        for i, g in enumerate(s.get("gpus") or []):
            if g["total"] and g["used"] >= GPU_MEM_HIGH * g["total"]:
                w[f"gpumem:{i}"] = f"GPU {g['name']} memory {_gb(g['used'])} used of {_gb(g['total'])}"
            if g["temp"] >= GPU_TEMP_HIGH:
                w[f"gputemp:{i}"] = f"GPU {g['name']} at {g['temp']}°C"
        if self._busy_streak >= 2:
            w["cpu"] = f"CPU {s.get('cpu')}% busy on all {self.cores} cores for a while"
        return w

    def _levels(self, s: dict) -> dict[str, tuple[float, float]]:
        """警告の種類 → (悪さ, 言い直す差)。悪さは大きいほど悪い。前に言った時より「言い直す差」以上悪くなったときだけ言い直す。
        ★2026-10-16 の点検: 推論機が自分で GPU のメモリを 23.4/24 GB 使い続ける機械では、HOLD（300 秒）ごとに同じ
          「Near the limit: GPU memory」を言い続けていた（2 時間で 30 回）。変わらない状態を繰り返しても判断の材料にならない。"""
        lv: dict[str, tuple[float, float]] = {}
        m = s.get("mem") or {}
        if m.get("total"):
            lv["mem"] = (-float(m.get("avail") or 0), max(0.5 * 2**30, 0.02 * m["total"]))
            ct, ca = m.get("commit_total") or 0, m.get("commit_avail") or 0
            if ct:
                lv["commit"] = (-float(ca), max(0.5 * 2**30, 0.02 * ct))
            st, sf = m.get("swap_total") or 0, m.get("swap_free") or 0
            if st:
                lv["swap"] = (float(st - sf), max(0.5 * 2**30, 0.05 * st))
        for where, free, total in s.get("disks") or []:
            lv[f"disk:{where}"] = (-float(free), max(2**30, 0.01 * (total or 0)))
        for i, g in enumerate(s.get("gpus") or []):
            if g.get("total"):
                lv[f"gpumem:{i}"] = (float(g["used"]), 0.02 * g["total"])
            lv[f"gputemp:{i}"] = (float(g.get("temp") or 0), 3.0)
        lv["cpu"] = (0.0, 1.0)                   # 忙しさは続いている間は 1 回だけ（収まって戻ればまた言う）
        return lv

    def _line(self, s: dict) -> str:
        bits = []
        if s.get("cpu") is not None:
            bits.append(f"CPU {s['cpu']}% ({self.cores} cores)")
        m = s.get("mem") or {}
        if m.get("total"):
            bits.append(f"memory {_gb(m['avail'])} free of {_gb(m['total'])}")
        for where, free, total in s.get("disks") or []:
            bits.append(f"disk {where} {_gb(free)} free")
        for g in s.get("gpus") or []:
            bits.append(f"GPU {g['name']} {g['util']}%, {_gb(g['used'])}/{_gb(g['total'])}, {g['temp']}°C")
        return " · ".join(bits)

    def note(self, tool: str, secs: float, first: bool) -> str:
        """道具の結果の頭に添える一文（無ければ ""）。"""
        if not enabled() or tool == "Lab":
            return ""
        with _LOCK:
            now = time.monotonic()
            long_run = secs >= LONG
            periodic = now - self._last_line_at >= _every()
            try:
                s = self.snapshot()
            except Exception:
                return ""
            warns = self._warnings(s)
            levels = self._levels(s)
            # 新しく出た物と、前に言った時より悪くなった物だけ（同じ状態のままなら言い直さない）
            fresh_warns = {k: v for k, v in warns.items()
                           if k not in self._warned
                           or levels.get(k, (0.0, 1.0))[0] >= self._warned[k] + levels.get(k, (0.0, 1.0))[1]}
            if not (first or long_run or periodic or fresh_warns):
                return ""
            line = self._line(s)
            if not line:
                return ""
            self._last_line_at = now
            for k in warns:
                if k in fresh_warns:
                    self._warned[k] = levels.get(k, (0.0, 1.0))[0]
            for k in list(self._warned):           # 収まった物は忘れる（次に出たらすぐ言う）
                if k not in warns:
                    del self._warned[k]
            self.lines += 1
            head = f"[Host] {self.host} now: {line}"
            if long_run:
                head += f" (after a {int(secs)} s {tool} call)"
            if fresh_warns:
                low = "; ".join(warns.values())
                top = ""
                if "mem" in warns or "commit" in warns:
                    tops = _top_memory()
                    if tops:
                        top = " Largest programs: " + ", ".join(f"{n} {_gb(b)}" for n, b in tops) + "."
                head += f"\n[Host] Near the limit: {low}.{top}"
            return head
