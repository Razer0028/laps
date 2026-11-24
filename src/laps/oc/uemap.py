"""Unreal Engine のゲームの型の地図（.usmap）を作る（UeMappings 道具・2026-10-12）。

オーナー「エージェントの強化でマッピングファイルの生成も可能にしてください」。
UE5 の多くのゲームは資産のプロパティを「版なし」で保存する（uefile の PKG_FLAGS）。CUE4Parse・FModel・UAssetGUI は
型の地図（.usmap）が無いと読めない（2026-10-12 の走行の「Could not load asset」）。地図は動いているゲームのメモリからしか作れない。

決まった手順なので 1 回の呼び出しで終える（モデルに何段も踏ませない）:
 1. ゲームの本体（*-Win64-Shipping.exe）を探す。アンチチート（EasyAntiCheat / BattlEye など）があれば断る。
 2. UE4SS（MIT・UE4SS-RE/RE-UE4SS の experimental-latest）を取ってくる。GitHub が出す sha256 と照合する。
    ★安定版 3.0.1 には Lua の DumpUSMAP が無い（2026-10-12 に確かめた）。実験版は毎回入れ替わるので固定の照合値は持てない
      → 取るたびに GitHub の照合値と突き合わせ、使った版を結果に書く。
 3. 本体の隣に置き、地図を書き出すだけの Lua のモッド（BlackwallUsmap）を足す。同梱のほかのモッドは切る。
 4. ゲームを起こし、地図が書き出されたらゲームを閉じる（窓へ「閉じて」→ 応じなければ終わらせる）。
 5. 本体の隣に増えた物（UE4SS・記録・印）を全部消して元に戻す。地図は out_dir へ。
"""
from __future__ import annotations

from .. import paths as _lp
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from ..tools import ToolResult

API = "https://api.github.com/repos/UE4SS-RE/RE-UE4SS/releases/tags/experimental-latest"
_ASSET = re.compile(r"^UE4SS_v[\d.]+(?:-\d+-g[0-9a-f]+)?\.zip$")
_SHIPPING = re.compile(r"-(?:Win64|WinGDK)-Shipping\.exe$", re.I)
_AC = re.compile(r"easyanticheat|battleye|beservice|xigncode|nprotect|gameguard|vanguard|denuvo_?anti", re.I)
MOD = "BlackwallUsmap"
MAX_WAIT = 900


class UeMapError(Exception):
    pass


def _home() -> Path:
    return Path(_lp.home_dir())


# ── 本体を探す ─────────────────────────────────────────────────────────

def _walk(root: str, depth: int, limit: int = 20000):
    base = root.rstrip("\\/").count(os.sep)
    n = 0
    for d, dirs, files in os.walk(root):
        if d.count(os.sep) - base >= depth:
            dirs[:] = []
        dirs[:] = [x for x in dirs if x.lower() not in ("content", "saved", "intermediate", "deriveddatacache")]
        for f in files:
            n += 1
            if n > limit:
                return
            yield d, f


def find_exe(game: str) -> tuple[str, str]:
    """(本体の exe, ゲームの根のフォルダ)。"""
    p = os.path.abspath(game)
    if os.path.isfile(p) and p.lower().endswith(".exe"):
        if _SHIPPING.search(os.path.basename(p)):
            root = p
            for _ in range(4):                               # <根>/<名前>/Binaries/Win64/x.exe
                root = os.path.dirname(root)
            return p, root if os.path.isdir(root) else os.path.dirname(p)
        root = os.path.dirname(p)                            # 包みの exe: 隣の <名前>/Binaries/Win64 を探す
    elif os.path.isdir(p):
        root = p
    else:
        raise UeMapError(f"{game} is neither a folder nor an .exe")
    found = []
    for d, f in _walk(root, 5):
        if f.lower().endswith(".exe") and (_SHIPPING.search(f) or
                                           (os.path.basename(d).lower() == "win64" and "binaries" in d.lower()
                                            and "crash" not in f.lower())):
            try:
                found.append((1 if _SHIPPING.search(f) else 0, os.path.getsize(os.path.join(d, f)), os.path.join(d, f)))
            except OSError:
                pass
    if not found:
        raise UeMapError(f"no Unreal game executable (…\\Binaries\\Win64\\*-Win64-Shipping.exe) under {root}")
    found.sort(reverse=True)
    return found[0][2], root


def anti_cheat(root: str) -> str:
    """アンチチートの印（ファイル名かフォルダ名）。空のフォルダも見る。"""
    base = root.rstrip("\\/").count(os.sep)
    n = 0
    for d, dirs, files in os.walk(root):
        if d.count(os.sep) - base >= 3:
            dirs[:] = []
        for x in dirs + files:
            n += 1
            if _AC.search(x):
                return os.path.join(d, x)
        if n > 8000:
            break
    return ""


# ── UE4SS を取ってくる ──────────────────────────────────────────────────

def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "blackwall-uemap", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch_ue4ss() -> tuple[Path, str]:
    """(照合した zip, 版の名前)。網に出られなければ前に照合した物を使う。"""
    cache = _home() / "ue4ss"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        rel = _get_json(API)
        asset = next((a for a in rel.get("assets") or [] if _ASSET.match(a.get("name", ""))), None)
        if not asset:
            raise UeMapError("the UE4SS release has no UE4SS_v*.zip")
        digest = str(asset.get("digest") or "")
        if not digest.startswith("sha256:"):
            raise UeMapError("GitHub gave no sha256 for the UE4SS download, so it cannot be verified")
        want = digest.split(":", 1)[1].lower()
        dest = cache / asset["name"]
        if not (dest.exists() and _sha256(dest) == want):
            part = dest.with_name(dest.name + ".part")
            req = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "blackwall-uemap"})
            with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            got = _sha256(part)
            if got != want:
                part.unlink(missing_ok=True)
                raise UeMapError(f"the UE4SS download does not match GitHub's sha256 ({got[:16]}… vs {want[:16]}…)")
            os.replace(part, dest)
            (cache / (dest.name + ".sha256")).write_text(want, encoding="utf-8")
        return dest, asset["name"]
    except UeMapError:
        raise
    except Exception as e:  # noqa: BLE001 — 網に出られない: 前に照合した物
        ok = []
        for z in sorted(cache.glob("UE4SS_v*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
            s = z.with_name(z.name + ".sha256")
            if s.exists() and _sha256(z) == s.read_text(encoding="utf-8").strip():
                ok.append(z)
        if ok:
            return ok[0], ok[0].name + " (cached)"
        raise UeMapError(f"could not download UE4SS: {e}") from e


# ── 置く・消す ─────────────────────────────────────────────────────────

def _snapshot(d: str) -> set[str]:
    out = {d}
    for r, dirs, files in os.walk(d):
        out.update(os.path.join(r, x) for x in dirs + files)
    return out


def _lua(delay_ms: int, blueprints: bool) -> str:
    arg = "" if blueprints else "false"
    return f"""-- Blackwall: write Mappings.usmap once, then mark done (removed again by Blackwall)
local done = false
local function dump(why)
  if done then return end
  done = true
  print("[{MOD}] DumpUSMAP (" .. why .. ")\\n")
  local ok, err = pcall(function() DumpUSMAP({arg}) end)
  local f = io.open("{MOD}.done", "w")
  if f then f:write(ok and "ok" or ("error: " .. tostring(err))) f:close() end
end
pcall(function()
  RegisterInitGameStatePostHook(function() ExecuteWithDelay(3000, function() dump("game state") end) end)
end)
ExecuteWithDelay({delay_ms}, function() dump("delay") end)
"""


def install(zip_path: Path, exe_dir: str, delay_ms: int, blueprints: bool) -> None:
    if os.path.exists(os.path.join(exe_dir, "dwmapi.dll")) or os.path.isdir(os.path.join(exe_dir, "ue4ss")):
        raise UeMapError(f"{exe_dir} already has dwmapi.dll or a ue4ss folder (another mod loader or UE4SS is "
                         "installed); not touching it. Remove it first or make the mappings with that install.")
    with zipfile.ZipFile(zip_path) as z:
        for i in z.infolist():
            name = i.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/") or ":" in name:
                raise UeMapError(f"unexpected path in the UE4SS zip: {name}")
            if not (name == "dwmapi.dll" or name.startswith("ue4ss/")):
                continue
            z.extract(i, exe_dir)
    mods = os.path.join(exe_dir, "ue4ss", "Mods")
    # 同梱のモッドは全部切り、地図のモッドだけを入れる
    names = [x for x in os.listdir(mods) if os.path.isdir(os.path.join(mods, x)) and x != "shared"]
    with open(os.path.join(mods, "mods.txt"), "w", encoding="utf-8") as f:
        f.write("".join(f"{n} : 0\n" for n in names) + f"{MOD} : 1\n")
    with open(os.path.join(mods, "mods.json"), "w", encoding="utf-8") as f:
        json.dump([{"mod_name": n, "mod_enabled": False} for n in names] + [{"mod_name": MOD, "mod_enabled": True}], f, indent=4)
    sd = os.path.join(mods, MOD, "Scripts")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "main.lua"), "w", encoding="utf-8") as f:
        f.write(_lua(delay_ms, blueprints))
    with open(os.path.join(mods, MOD, "enabled.txt"), "w", encoding="utf-8") as f:
        f.write("")


def remove_new(exe_dir: str, before: set[str]) -> list[str]:
    """本体の隣に増えた物を消す（元から在った物は触らない）。消せなかった物の並び。"""
    left = []
    now = _snapshot(exe_dir)
    for p in sorted(now - before, key=lambda x: x.count(os.sep), reverse=True):
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=False)
            elif os.path.exists(p):
                os.remove(p)
        except OSError:
            left.append(p)
    return [p for p in left if os.path.exists(p)]


# ── 動かす ─────────────────────────────────────────────────────────────

def _new_usmap(exe_dir: str, t0: float) -> Path | None:
    best = None
    for r, _d, files in os.walk(exe_dir):
        for f in files:
            if f.lower().endswith(".usmap"):
                p = Path(r) / f
                try:
                    if p.stat().st_mtime >= t0 - 1 and (best is None or p.stat().st_mtime > best.stat().st_mtime):
                        best = p
                except OSError:
                    pass
    return best


def _stop_game(pid: int) -> None:
    from . import apptool as _a
    pids = _a._tree(pid)
    try:
        wins = _a._windows(pids)
        if wins:
            _a._ask_close([h for h, _p, _t in wins])
            t0 = time.time()
            while time.time() - t0 < 8 and any(_a._info(p)[0] for p in pids):
                time.sleep(0.5)
    except Exception:
        pass
    left = {p for p in pids if _a._info(p)[0]}
    if left:
        _a._kill_tree(left, pid)


def _log_tail(exe_dir: str, n: int = 15) -> str:
    p = os.path.join(exe_dir, "ue4ss", "UE4SS.log")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return "\n".join(f.read().splitlines()[-n:])
    except OSError:
        return ""


def run(game: str, out_dir: str, wait: int, delay_ms: int, blueprints: bool, cancelled=lambda: False) -> str:
    if sys.platform != "win32":
        raise UeMapError("UeMappings runs the game, so it works only on Windows")
    exe, root = find_exe(game)
    ac = anti_cheat(root)
    if ac:
        raise UeMapError(f"this game ships anti-cheat ({ac}); injecting UE4SS could get the account banned, so "
                         "UeMappings does not run it. Ask the user how they want to proceed.")
    exe_dir = os.path.dirname(exe)
    zpath, build = fetch_ue4ss()
    before = _snapshot(exe_dir)
    notes: list[str] = []
    result = ""
    proc = None
    try:
        install(zpath, exe_dir, delay_ms, blueprints)
        t0 = time.time()
        flags = 0x00000200 if os.name == "nt" else 0                  # 新しいプロセスグループ
        proc = subprocess.Popen([exe], cwd=exe_dir, creationflags=flags, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        last_size, found = -1, None
        while time.time() - t0 < wait:
            if cancelled():
                raise UeMapError("cancelled by the user")
            time.sleep(2)
            found = _new_usmap(exe_dir, t0)
            marker = os.path.exists(os.path.join(exe_dir, f"{MOD}.done"))
            if found:
                size = found.stat().st_size
                if size > 0 and size == last_size and (marker or time.time() - t0 > 10):
                    break
                last_size = size
            elif marker:
                with open(os.path.join(exe_dir, f"{MOD}.done"), encoding="utf-8", errors="replace") as f:
                    m = f.read().strip()
                if m != "ok":
                    raise UeMapError(f"UE4SS ran but DumpUSMAP failed: {m}\nUE4SS.log:\n{_log_tail(exe_dir)}")
            if proc.poll() is not None and not found:
                from . import apptool as _a
                if not any(_a._info(p)[0] for p in _a._tree(proc.pid)):
                    raise UeMapError(f"the game exited (code {proc.returncode}) before the mappings were written."
                                     + (f"\nUE4SS.log:\n{_log_tail(exe_dir)}" if _log_tail(exe_dir) else
                                        "\nUE4SS wrote no log, so it probably did not load (the game may need a newer"
                                        " UE4SS, or it does not load dwmapi.dll from its folder)."))
        else:
            tail = _log_tail(exe_dir)
            raise UeMapError(f"no .usmap after {wait} s." + (f"\nUE4SS.log:\n{tail}" if tail else
                             "\nUE4SS wrote no log (it did not load). The game may start through a launcher that "
                             "runs a different copy, or need a newer UE4SS."))
        name = _SHIPPING.sub("", os.path.basename(exe)).removesuffix(".exe") or "game"
        os.makedirs(out_dir, exist_ok=True)
        dest = os.path.join(out_dir, f"{name}.usmap")
        if os.path.exists(dest):
            dest = os.path.join(out_dir, f"{name}-{time.strftime('%Y%m%d-%H%M%S')}.usmap")
        shutil.copy2(found, dest)
        result = (f"Wrote {dest} ({os.path.getsize(dest):,} bytes) from {os.path.basename(exe)} with UE4SS {build} "
                  f"in {int(time.time() - t0)} s.")
    finally:
        if proc is not None:
            try:
                _stop_game(proc.pid)
            except Exception as e:  # noqa: BLE001
                notes.append(f"could not close the game: {e}")
        time.sleep(1.5)                                             # 終わったゲームがファイルを放すのを待つ
        left = remove_new(exe_dir, before)
        if left:
            time.sleep(3)
            left = remove_new(exe_dir, before)
        if left:
            notes.append("could not remove (still in use): " + ", ".join(left[:5]))
    return result + ("\nRemoved UE4SS and everything it added next to the game." if not notes else "\n" + "\n".join(notes)) \
        + ("\nUse it with: CUE4Parse `provider.MappingsContainer = new FileUsmapTypeMappingsProvider(<path>)` "
           "(CUE4Parse.CLI: its mappings option), FModel Settings > Mapping file, or UAssetGUI Utils > Import Mappings.")


# ── 道具 ──────────────────────────────────────────────────────────────

def has_unreal(cwd: str, budget: float = 0.4) -> bool:
    """作業場に Unreal のゲームらしい物があるか（道具を渡すかの判定・時間を区切って浅く見る）。"""
    t0 = time.time()
    try:
        for d, f in _walk(cwd, 5, 6000):
            fl = f.lower()
            if fl.endswith((".utoc", ".ucas")) or _SHIPPING.search(f) or (fl.endswith(".pak") and "paks" in d.lower()):
                return True
            if time.time() - t0 > budget:
                return False
    except Exception:
        return False
    return False


def available(cwd: str | None = None) -> bool:
    if sys.platform != "win32" or os.environ.get("LAPS_UEMAP", "1") == "0":
        return False
    return has_unreal(cwd or os.getcwd())


def uemap_tool(inp: dict) -> ToolResult:
    from . import tools as _oc
    inp = inp if isinstance(inp, dict) else {}
    game = str(inp.get("game") or "").strip().strip('"')
    if not game:
        return ToolResult("Error: game is required (the game's folder or its .exe).", ok=False, kind="error")
    game = _oc._abs(game)
    out_dir = _oc._abs(str(inp["out_dir"])) if inp.get("out_dir") else os.path.join(_oc._cwd(), "_mappings")
    try:
        wait = max(30, min(int(inp.get("wait_seconds") or 180), MAX_WAIT))
    except (TypeError, ValueError):
        wait = 180
    try:
        delay = max(5, min(int(inp.get("delay_seconds") or 20), 300)) * 1000
    except (TypeError, ValueError):
        delay = 20000
    blueprints = inp.get("include_blueprints") not in (False, "false", "False", 0, "0")
    try:
        text = run(game, out_dir, wait, delay, blueprints, cancelled=getattr(_oc, "_cancel_probe", lambda: False))
        return ToolResult(text, ok=True, kind="ok")
    except UeMapError as e:
        return ToolResult(f"Error: {e}", ok=False, kind="error")
    except Exception as e:  # noqa: BLE001
        return ToolResult(f"Error: UeMappings failed: {type(e).__name__}: {e}", ok=False, kind="error")


DESCRIPTION = """Make a .usmap mapping file for an Unreal Engine 4/5 game on this PC. Needed when its assets use unversioned properties: CUE4Parse, FModel or UAssetGUI fail with "Could not load asset" / ask for mappings, or Read shows "unversioned".
It finds the game's *-Win64-Shipping.exe, downloads UE4SS (verified against GitHub's sha256), puts it next to the exe with a tiny mod that writes Mappings.usmap, starts the game, waits until the file is written, closes the game, removes everything it added, and saves <out_dir>/<Game>.usmap. The game window opens for a while; tell the user before calling it. It refuses games that ship anti-cheat (EasyAntiCheat, BattlEye, ...). If the game needs to reach the main menu first, raise delay_seconds."""

SCHEMA_PROPS = {
    "game": {"type": "string", "description": "The game's folder or its .exe"},
    "out_dir": {"type": "string", "description": "Where to save the .usmap (default: <workspace>/_mappings)"},
    "wait_seconds": {"type": "integer", "description": "How long to wait for the file (default 180, up to 900)"},
    "delay_seconds": {"type": "integer", "description": "Seconds after UE4SS starts before writing (default 20)"},
    "include_blueprints": {"type": "boolean", "description": "Also map Blueprint-made types (default true)"},
}
