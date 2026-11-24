"""Desktop — 画面の部品を文字で読み、番号で押す道具（Windows）。

★なぜ画面写真ではないか: 推論機のモデル（Qwen3.6-35B-A3B）は文字しか読めない。
  そこで Windows の UI Automation の木を「1 部品 1 行・番号つき」の文字にして渡し、
  モデルはその番号で押す・打つ（goose の computer_control と同じ「see → click → type」の流れ。
  refs/goose/crates/goose-mcp/src/computercontroller/mod.rs:845-880。あちらは macOS の Peekaboo）。
★2026-10-19 look: 推論機（KAT-Coder）に画像の部品が付いたので、窓の写真に snapshot と同じ番号の赤い枠を描いて渡す
  （Set-of-Marks）。番号はそのまま id で押せ、枠の無い所（描画だけの画面・ゲーム）は rx・ry（写真の上の 0〜1000）で押す。
  文字の一覧が主で、写真は「見た目」を足す物（文字だけでも動く形は崩さない）。

★外部の部品を足さない: 補助プログラム（desktop_helper.cs）を、Windows に必ず入っている
  .NET Framework 4 の csc.exe で**利用者の機械の上で一度だけ**組み立て、常駐させて行単位の
  JSON で話す。部品の番号は補助プログラムが覚えている（同じ部品は次の snapshot でも同じ番号）。

★固まらないための約束（knowledge/windows-powershell-traps.md の実測）:
  ・組み立ては procrun（パイプを使わない）。
  ・常駐の出力は読取スレッド＋キューで受け、待つのは時間つき。答えなければ**補助だけ**を殺す
    （taskkill /T は使わない＝補助が launch で起こしたアプリまで道連れにするため）。
  ・窓を出さない（CREATE_NO_WINDOW）。stderr はファイルへ。
"""
from __future__ import annotations

from .. import paths as _lp
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path

from ..tools import ToolResult

HELPER_SOURCE = Path(__file__).with_name("desktop_helper.cs")

ACTIONS = ("windows", "snapshot", "web_snapshot", "look", "read", "ocr", "launch", "focus", "click", "type", "set_value", "key", "scroll",
           "move", "drag", "cursor", "wait")

# 点を受ける操作（rx・ry と coordinate を画面の x・y に直す）
_POINT_ACTIONS = ("click", "scroll", "move", "drag")

_ACTION_ALIASES = {
    "list": "windows", "list_windows": "windows", "window": "windows",
    "web": "web_snapshot", "page": "web_snapshot", "page_snapshot": "web_snapshot",
    "see": "snapshot", "inspect": "snapshot", "tree": "snapshot", "observe": "snapshot",
    "read_text": "read", "get_text": "read", "text": "read",
    "screen_text": "ocr", "recognize": "ocr", "read_screen": "ocr",
    "screenshot": "look", "capture": "look", "picture": "look", "view": "look", "look_at": "look",
    "open": "launch", "start": "launch", "run": "launch",
    "activate": "focus", "switch": "focus",
    "double_click": "click", "doubleclick": "click", "right_click": "click", "rightclick": "click",
    # ★Claude の Computer Use の名前（モデルが覚えている形をそのまま受ける・2026-10-19）
    "left_click": "click", "middle_click": "click", "triple_click": "click", "tripleclick": "click",
    "mouse_move": "move", "hover": "move", "move_mouse": "move",
    "left_click_drag": "drag", "drag_and_drop": "drag", "drag_drop": "drag",
    "cursor_position": "cursor", "mouse_position": "cursor",
    "sleep": "wait", "pause": "wait",
    "zoom": "look", "hold_key": "key",
    "write": "type", "input": "type", "type_text": "type",
    "setvalue": "set_value", "set": "set_value", "fill": "set_value",
    "keys": "key", "press": "key", "hotkey": "key", "keypress": "key",
}

_KEY_ALIASES = {
    # ★2026-11-19 記録の失敗から: action を op と書いた（launch が「unknown action ''」で断られた）
    "op": "action", "operation": "action", "do": "action",
    "element": "id", "ref": "id", "element_id": "id", "control": "id",
    "title": "window", "app": "window", "target_window": "window",
    "program": "command", "url": "command", "app_path": "command",
    "key": "keys", "hotkey": "keys", "value": "text", "content": "text",
    "arguments": "args", "dy": "amount", "clicks": "amount", "scroll_amount": "amount",
    "duration": "seconds", "start_id": "from_id", "source": "from_id", "to_id": "id", "target": "id",
    "monitor": "screen", "display": "screen",
}

# 1 回の答えを待つ上限（秒）。snapshot は補助の側でも 18 秒で打ち切る（応答しないアプリは
# UI Automation の呼び出し 1 回で ~20 秒止めることがあるので、既定も少し長めに取る）。
_TIMEOUT = {"launch": 75.0, "snapshot": 45.0, "web_snapshot": 45.0, "look": 60.0, "read": 45.0, "ocr": 60.0}
_DEFAULT_TIMEOUT = 45.0
_BUILD_TIMEOUT = 180.0
_CREATE_NO_WINDOW = 0x08000000


class DesktopError(Exception):
    pass


def available() -> bool:
    """この機械で本当に動くときだけ True（必ず失敗する道具は棚に載せない）。"""
    if os.environ.get("LAPS_DESKTOP", "1").strip() == "0":
        return False
    if sys.platform != "win32":
        return False
    return HELPER_SOURCE.exists() and _framework_dir() is not None


def _framework_dir() -> Path | None:
    win = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    for sub in ("Framework64", "Framework"):
        d = Path(win) / "Microsoft.NET" / sub / "v4.0.30319"
        if (d / "csc.exe").exists() and (d / "WPF" / "UIAutomationClient.dll").exists():
            return d
    return None


def _cache_dir() -> Path:
    # ★アプリ（Windows）は LAPS_DESKTOP_DIR を <データ根>\desktop に向ける（2026-10-07）。
    #   ~/.blackwall を消されると毎回組み立て直しになり、利用者名の全角空白で csc がつまずいた前例もある。
    d = os.environ.get("LAPS_DESKTOP_DIR")
    return Path(d).expanduser() if d else _lp.dot_dir() / "desktop"


def prepare() -> str:
    """補助プログラムを先に組み立てておく（oc/parts.py から・使う前の待ちを無くす）。"""
    if not available():
        return "desktop: この機械では使わない"
    return f"desktop: そろっている（{_build()}）"


def _build() -> Path:
    """補助プログラムを組み立てる（同じ中身なら作り直さない）。"""
    src = HELPER_SOURCE.read_bytes()
    tag = hashlib.sha256(src).hexdigest()[:12]
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    exe = d / f"bw-desktop-{tag}.exe"
    if exe.exists():
        return exe
    fw = _framework_dir()
    if fw is None:
        raise DesktopError("Error: the .NET Framework 4 compiler (csc.exe) was not found, so the desktop "
                           "helper cannot be built on this machine.")
    cs = d / f"bw-desktop-{tag}.cs"
    cs.write_bytes(src)
    tmp = d / f"bw-desktop-{tag}.tmp.exe"
    wpf = fw / "WPF"
    # ★置き場（利用者のプロファイルの下）は argv に載せない。cwd にして相対名だけ渡す。
    #   実機（利用者の PC・2026-10-05）: プロファイルに全角空白がある（`田中　優輝`）と csc はそこで引数を
    #   切り、`CS2001: ソース ファイル 'C:\Users\田中' が見つかりません` で落ちた（Python の
    #   list2cmdline は ASCII の空白しか引用しない）。参照する DLL は C:\Windows の下なので載せてよい。
    argv = [str(fw / "csc.exe"), "/nologo", "/utf8output", "/optimize+", "/target:exe", "/platform:anycpu",
            f"/out:{tmp.name}",
            f"/r:{wpf / 'UIAutomationClient.dll'}", f"/r:{wpf / 'UIAutomationTypes.dll'}",
            f"/r:{wpf / 'WindowsBase.dll'}", f"/r:{fw / 'System.Web.Extensions.dll'}",
            cs.name]
    # ★OCR（Windows.Media.Ocr）は WinRT の定義（System32\\WinMetadata）を参照して組む。
    #   無い・組めない機械では OCR なしで組み直す（ocr だけが「使えない」と答える）。
    win = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
    meta = win / "System32" / "WinMetadata"
    ocr_refs = [f"/r:{meta / n}" for n in ("Windows.Foundation.winmd", "Windows.Globalization.winmd",
                                            "Windows.Graphics.winmd", "Windows.Media.winmd",
                                            "Windows.Storage.winmd")]
    ocr_refs += [f"/r:{fw / n}" for n in ("System.Runtime.WindowsRuntime.dll", "System.Runtime.dll",
                                          "System.Drawing.dll")]
    from .. import procrun
    # ★look（窓の写真に番号の札）は OCR と同じ撮影と System.Drawing を使う。新しい部分が組めない機械でも
    #   今までの道具（OCR つき・OCR なし）は残るよう、試す順に 1 段ずつ削る（2026-10-19）。
    # ★操作中の表示（2026-11-18・BW_OVERLAY）は WinForms で描く。組めない機械では表示なしの形へ 1 段ずつ戻る。
    forms = [f"/r:{fw / 'System.Windows.Forms.dll'}", f"/r:{fw / 'System.Drawing.dll'}"]
    tries = [argv[:-1] + ["/define:BW_OCR;BW_LOOK;BW_OVERLAY"] + ocr_refs + [forms[0]] + argv[-1:],
             argv[:-1] + ["/define:BW_OCR;BW_LOOK"] + ocr_refs + argv[-1:],
             argv[:-1] + ["/define:BW_OCR"] + ocr_refs + argv[-1:], argv]
    cp = None
    for attempt in tries:
        try:
            cp = procrun.run(attempt, timeout=_BUILD_TIMEOUT, text=True, cwd=str(d))
        except subprocess.TimeoutExpired:
            raise DesktopError(f"Error: building the desktop helper with csc.exe took over {_BUILD_TIMEOUT:.0f} s.")
        if cp.returncode == 0 and tmp.exists():
            break
    if cp is None or cp.returncode != 0 or not tmp.exists():
        out = ((cp.stdout or "") + (cp.stderr or "")).strip()
        lines = out.splitlines()[-15:]
        raise DesktopError(f"Error: building the desktop helper failed (csc.exe exit {cp.returncode}):\n"
                           + "\n".join(lines))
    os.replace(tmp, exe)
    # 古い版の補助（中身が変わるたびに名前が変わる）を片付ける。使用中で消せない物は次の機会に。
    for old in d.glob("bw-desktop-*"):
        if old.name not in (exe.name, cs.name):
            try:
                old.unlink()
            except OSError:
                pass
    return exe


class _Helper:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue | None = None
        self.lock = threading.Lock()
        self.log_path = _cache_dir() / "helper.log"

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self) -> None:
        exe = _build()
        log = open(self.log_path, "ab")
        try:
            flags = _CREATE_NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            # executable= で起動する物を名指しする（コマンド行の切り方に頼らない。上の全角空白の件）。
            self.proc = subprocess.Popen([exe.name], executable=str(exe), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=log, cwd=str(exe.parent),
                                         creationflags=flags)
        finally:
            log.close()
        self.lines = queue.Queue()
        threading.Thread(target=self._read, args=(self.proc, self.lines), daemon=True,
                         name="bw-desktop-reader").start()
        first = self._next(30.0)
        if not first.get("ok"):
            self._stop()
            raise DesktopError("Error: the desktop helper did not start: " + str(first.get("text")))

    @staticmethod
    def _read(proc: subprocess.Popen, q: queue.Queue) -> None:
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                q.put(raw)
        except Exception:
            pass
        q.put(None)

    def _log_tail(self) -> str:
        try:
            data = self.log_path.read_bytes()[-1500:].decode("utf-8", "replace").strip()
            return ("\nhelper log: " + data) if data else ""
        except Exception:
            return ""

    def _next(self, timeout: float) -> dict:
        assert self.lines is not None
        try:
            raw = self.lines.get(timeout=timeout)
        except queue.Empty:
            self._stop()
            raise DesktopError(f"Error: the desktop helper did not answer within {timeout:.0f} s, so it was "
                               "restarted. Control ids from earlier snapshots are no longer valid.")
        if raw is None:
            tail = self._log_tail()
            self._stop()
            raise DesktopError("Error: the desktop helper stopped unexpectedly. It will restart on the next "
                               "call; take a new snapshot." + tail)
        try:
            return json.loads(raw.decode("ascii", "replace"))
        except ValueError:
            return {"ok": False, "text": "Error: unreadable answer from the desktop helper: "
                    + raw.decode("ascii", "replace")[:300]}

    def _stop(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()   # ★補助だけ。launch で起こしたアプリは残す。
            except Exception:
                pass

    def call(self, req: dict, timeout: float) -> dict:
        with self.lock:
            if not self._alive():
                self._start()
            assert self.proc is not None and self.proc.stdin is not None
            try:
                self.proc.stdin.write((json.dumps(req, ensure_ascii=True) + "\n").encode("ascii"))
                self.proc.stdin.flush()
            except OSError:
                self._stop()
                raise DesktopError("Error: the desktop helper stopped unexpectedly. It will restart on the "
                                   "next call; take a new snapshot." + self._log_tail())
            return self._next(timeout)


_helper: _Helper | None = None
_helper_lock = threading.Lock()


def _get_helper() -> _Helper:
    global _helper
    with _helper_lock:
        if _helper is None:
            _helper = _Helper()
        return _helper


def _guard_pids() -> list[int]:
    """Blackwall 自身のプロセス（エージェント・アプリ本体・アプリの画面の WebView2）。補助はこれらの窓を読むだけにし、
    押す・打つ・巻く・前に出すはどの経路でも断る（2026-10-19 オーナー「自身のアプリを終了してしまったようです」）。"""
    from . import selfproc
    return selfproc.guard_pids()


def normalize(inp: dict) -> dict:
    out: dict = {}
    for k, v in inp.items():
        out[_KEY_ALIASES.get(k, k)] = v
    raw = str(out.get("action") or "").strip().lower().replace("-", "_").replace(" ", "_")
    action = _ACTION_ALIASES.get(raw, raw)
    if raw in ("double_click", "doubleclick"):
        out.setdefault("double", True)
    if raw in ("right_click", "rightclick"):
        out.setdefault("button", "right")
    if raw == "middle_click":
        out.setdefault("button", "middle")
    if raw in ("triple_click", "tripleclick"):
        out.setdefault("count", 3)
    out["action"] = action
    # ★2026-11-19 記録の失敗から: keys を ["ctrl", "f"] の配列で渡すと、補助が "System.Object[]" と読んで断っていた。
    #   修飾キーがあれば 1 つの組み合わせ（ctrl+f）、無ければ順に押す（tab tab enter）。
    if isinstance(out.get("keys"), (list, tuple)):
        ks = [str(k).strip().lower() for k in out["keys"] if str(k).strip()]
        mods = {"ctrl", "control", "alt", "shift", "win", "meta", "cmd", "super"}
        out["keys"] = "+".join(ks) if any(k in mods for k in ks[:-1]) else " ".join(ks)
    # Computer Use の scroll_direction（up/down）を符号つきの amount に
    sd = str(out.pop("scroll_direction", "") or out.pop("direction", "") or "").lower()
    if action == "scroll" and sd in ("up", "down"):
        n = abs(_as_int(out.get("amount"), 5))
        out["amount"] = n if sd == "up" else -n
    # hold_key の duration は秒（seconds の別名で入る）
    if raw == "hold_key" and "hold" not in out:
        out["hold"] = out.pop("seconds", 1)
    # coordinate=[x, y]（Computer Use の形）は look の写真の上の点。写真が無ければ画面の画素とみなす。
    for key, prefix in (("coordinate", ""), ("start_coordinate", "from_")):
        c = out.pop(key, None)
        if isinstance(c, str):
            c = [p for p in c.replace("(", "").replace(")", "").replace("[", "").replace("]", "").split(",")]
        if isinstance(c, (list, tuple)) and len(c) == 2:
            out.setdefault(prefix + ("rx" if _LAST_LOOK else "x"), _as_num(c[0]))
            out.setdefault(prefix + ("ry" if _LAST_LOOK else "y"), _as_num(c[1]))
    for k in ("id", "from_id"):
        if isinstance(out.get(k), str):
            s = out[k].strip().strip("[]")
            if s.isdigit():
                out[k] = int(s)
    # path は ocr では画像のパス、launch では起動する物（command の別名）。
    if action == "launch" and "command" not in out and isinstance(out.get("path"), str):
        out["command"] = out.pop("path")
    if isinstance(out.get("screen"), str):
        s = out["screen"].strip().lower().lstrip("s")
        if s.isdigit():
            out["screen"] = int(s)
    return out


def _as_int(v, dflt: int) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return dflt


def _as_num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return v


def _resolve_launch(command: str) -> tuple[str, str]:
    """launch の command を実在のパスへ。(command, 一言)

    ★補助の作業場は自分の置き場（~/.blackwall/desktop）なので、相対パスは作業場から解決しないと
      必ず「ファイルが見つかりません」になる（実機 2026-10-05: `AssetRipper.GUI.Free.exe`）。
    ★全角空白のパスを半角で書き写したものは、他の道具と同じ pathfix で直す（同じ実機で 3 回失敗）。"""
    cmd = command.strip().strip('"')
    if not cmd or "://" in cmd or cmd.startswith(("ms-", "shell:")):
        return command, ""
    looks_path = any(ch in cmd for ch in "\\/") or cmd.lower().endswith((".exe", ".bat", ".cmd", ".lnk"))
    if not looks_path:
        return command, ""
    path = cmd if os.path.isabs(cmd) else os.path.join(os.getcwd(), cmd)
    if os.path.exists(path):
        return os.path.abspath(path), ""
    try:
        from ..tools import pathfix
        fixed = pathfix.fix_for(path, mode="existing")
        if fixed:
            return fixed, " " + pathfix.note(path, fixed)
    except Exception:
        pass
    return command, ""


def _resolve_file(path: str) -> tuple[str, str]:
    """画像のパスを作業場から解決し、全角空白の書き写し違いを直す（launch と同じ理由）。"""
    p = path.strip().strip('"')
    full = p if os.path.isabs(p) else os.path.join(os.getcwd(), p)
    if os.path.exists(full):
        return os.path.abspath(full), ""
    try:
        from ..tools import pathfix
        fixed = pathfix.fix_for(full, mode="existing")
        if fixed:
            return fixed, " " + pathfix.note(full, fixed)
    except Exception:
        pass
    return full, ""


#: 直前の look で撮った範囲（画面の画素）。rx・ry（写真の上の 0〜1000）を画面の位置に直すのに使う。
_LAST_LOOK: dict = {}
LOOK_IMAGE_NAME = "desktop-look.png"


def _from_picture(req: dict, prefix: str = "") -> str | None:
    """click・scroll・move・drag の rx・ry を、直前の look の範囲で画面の x・y に直す。だめなときは理由の文。
    prefix="from_" は drag の始点。"""
    if prefix + "rx" not in req and prefix + "ry" not in req:
        return None
    rx, ry = req.pop(prefix + "rx", None), req.pop(prefix + "ry", None)
    if req.get(prefix + "id") is not None:
        return None                     # 番号があればそちらで押す（補助の側も id を先に見る）
    try:
        rx, ry = float(rx), float(ry)
    except (TypeError, ValueError):
        return "Error: give both rx and ry (0-1000 across and down the last look picture), or an id."
    if not (0 <= rx <= 1000 and 0 <= ry <= 1000):
        return "Error: rx and ry go from 0 to 1000 (0,0 is the top left of the last look picture, 1000,1000 the bottom right)."
    if not _LAST_LOOK:
        return "Error: rx and ry point into the last look picture, and there is none yet. Use look first."
    w, h = _LAST_LOOK["w"], _LAST_LOOK["h"]
    req[prefix + "x"] = _LAST_LOOK["left"] + min(w - 1, int(round(rx * w / 1000)))
    req[prefix + "y"] = _LAST_LOOK["top"] + min(h - 1, int(round(ry * h / 1000)))
    return None


def _zoom_region(req: dict) -> None:
    """look の region=[rx0, ry0, rx1, ry1]（前の写真の上の 0〜1000）を画面の画素の範囲に直す。
    補助は x0..y1 の範囲だけを撮る（Computer Use の zoom に当たる）。"""
    reg = req.pop("region", None)
    if isinstance(reg, str):
        reg = [p for p in reg.strip("[]() ").replace(",", " ").split() if p]
    if not (isinstance(reg, (list, tuple)) and len(reg) == 4):
        return
    try:
        a, b, c, d = (float(v) for v in reg)
    except (TypeError, ValueError):
        return
    if not _LAST_LOOK:
        # 前の写真が無いときは Computer Use の zoom と同じく画面の画素として読む
        x0, x1 = sorted((int(a), int(c)))
        y0, y1 = sorted((int(b), int(d)))
        req.update(x0=x0, y0=y0, x1=max(x0 + 8, x1), y1=max(y0 + 8, y1))
        return
    w, h = _LAST_LOOK["w"], _LAST_LOOK["h"]
    x0, x1 = sorted((a, c))
    y0, y1 = sorted((b, d))
    req["x0"] = _LAST_LOOK["left"] + int(x0 * w / 1000)
    req["y0"] = _LAST_LOOK["top"] + int(y0 * h / 1000)
    req["x1"] = _LAST_LOOK["left"] + max(int(x0 * w / 1000) + 8, int(x1 * w / 1000))
    req["y1"] = _LAST_LOOK["top"] + max(int(y0 * h / 1000) + 8, int(y1 * h / 1000))


def _look_result(text: str, out: Path, screen: bool = False, region: bool = False) -> ToolResult:
    """補助の答え（1 行目は道具あての範囲）と保存した写真から、モデルへ渡す結果を作る。"""
    head, _, rest = text.partition("\n")
    parts = head.split()
    try:
        if len(parts) != 6 or parts[0] != "picture":
            raise ValueError
        left, top, w, h, marked = (int(p) for p in parts[1:])
    except ValueError:
        return ToolResult("Error: the desktop helper did not describe the picture it took.\n" + text,
                          ok=False, kind="error")
    try:
        raw = out.read_bytes()
    except OSError as e:
        return ToolResult(f"Error: the picture of the window could not be read: {e}", ok=False, kind="error")
    if not raw:
        return ToolResult("Error: the picture of the window came back empty. Use snapshot or ocr.",
                          ok=False, kind="error")
    import base64
    from . import tools as oc_tools
    mime, note = "image/png", ""
    # ★読み手の上限に合わせて縮め、0〜1000 の目盛りを重ねる（2026-11-15・labeye の考え方・oc/lookaid.py）。
    from . import lookaid
    prep = lookaid.prepare(raw)
    shown = None
    if prep:
        raw, shown = prep
    else:
        fit = oc_tools._fit_for_view(raw, mime, False)
        if fit:
            raw, mime, note = fit
    _LAST_LOOK.clear()
    _LAST_LOOK.update(left=left, top=top, w=w, h=h)
    if region:
        lines = [f"The zoomed picture ({w}x{h} desktop pixels{'; ' + note if note else ''}) is attached. rx and ry "
                 "(0-1000) now point into this zoomed picture.", "", rest]
    elif screen:
        lines = [f"The picture of the whole screen ({w}x{h} pixels{'; ' + note if note else ''}) is attached.",
                 "To act inside a window, snapshot or look at it (window=w<number>) and use ids. For anything else click "
                 "or scroll at rx and ry: 0-1000 across and down this picture (rx=500, ry=500 is its center).",
                 "", rest]
    else:
        lines = [f"The picture of the window ({w}x{h} pixels{'; ' + note if note else ''}) is attached. Red boxes are "
                 f"numbered with the same ids as the list below ({marked} marked): act on them with id.",
                 "For something with no box (drawn content, canvases, games), click or scroll at rx and ry: 0-1000 across "
                 "and down the picture (rx=500, ry=500 is its center). Look again after the window moves or changes.",
                 "", rest]
    if shown is not None:
        aid = lookaid.note(shown)
        if aid:
            lines.insert(1, aid)
    data = {"image": {"path": LOOK_IMAGE_NAME, "mime": mime, "b64": base64.b64encode(raw).decode("ascii")}}
    return ToolResult("\n".join(lines).rstrip(), data=data)


def desktop_tool(inp: dict) -> ToolResult:
    req = normalize(inp if isinstance(inp, dict) else {})
    action = req["action"]
    if action not in ACTIONS:
        return ToolResult(f"Error: unknown action '{action}'. Use one of: {', '.join(ACTIONS)}.",
                          ok=False, kind="error")
    if action == "wait":
        import time
        s = _as_num(req.get("seconds", req.get("wait", 2)))
        secs = max(0.0, min(s if isinstance(s, float) else 2.0, 10.0))
        time.sleep(secs)
        req = {"action": "snapshot", **({"window": req["window"]} if req.get("window") else {})}
        action = "snapshot"
        waited = f"Waited {secs:g} s.\n\n"
    else:
        waited = ""
    if action in _POINT_ACTIONS:
        # 写真の上の位置を窓なしで押した: 写真を撮った窓を宛先にする（前に Blackwall があると断られていた・走行記録の 5 回）
        if req.get("window") is None and req.get("id") is None and "rx" in req and _LAST_LOOK_WINDOW[0]:
            req["window"] = _LAST_LOOK_WINDOW[0]
        for prefix in ("", "from_"):
            problem = _from_picture(req, prefix)
            if problem:
                return ToolResult(problem.replace("rx and ry", prefix + "rx and " + prefix + "ry", 1) if prefix else problem,
                                  ok=False, kind="error")
    for k in ("rx", "ry", "from_rx", "from_ry"):
        req.pop(k, None)
    if action == "look":
        if "region" not in req:
            _LAST_LOOK_WINDOW[0] = str(req.get("window") or "") if req.get("screen") is None else ""
        _zoom_region(req)
        # ★2026-10-19 実走行（0.1.550）: Gemma が look に screen=2 と window=w1 を毎回両方渡し、screen が勝って
        #   画面全体の写真ばかり受け取った。窓を名指ししたならその窓を撮る。
        if req.get("window") and req.get("screen") is not None and "x0" not in req:
            req.pop("screen", None)
    if action == "click" and req.get("id") is None and "x" not in req and isinstance(req.get("text"), str) \
            and req["text"].strip():
        # ★部品の木に同じ名前があれば番号で押す（2026-11-03 実測: Chrome の中のボタンを OCR の座標で押すと効かず、番号なら効いた）
        problem = None if _click_target_from_tree(req) else _click_target_from_ocr(req)
        if problem:
            return ToolResult(problem, ok=False, kind="error")
    look_out = None
    if action == "look":
        look_out = _cache_dir() / LOOK_IMAGE_NAME
        try:
            look_out.unlink()           # 前の写真を答えと取り違えない
        except OSError:
            pass
        req["out"] = str(look_out)
    fix_note = ""
    if action == "launch" and isinstance(req.get("command"), str):
        req["command"], fix_note = _resolve_launch(req["command"])
        req["cwd"] = os.getcwd()
    if action == "ocr" and isinstance(req.get("path"), str) and req["path"].strip():
        req["path"], fix_note = _resolve_file(req["path"])
    then_snapshot = bool(req.pop("then_snapshot", False))
    ocr_note = req.pop("_ocr_note", "")
    req = {k: v for k, v in req.items() if v is not None and v != ""}
    req["guard_pids"] = _guard_pids()
    # ★操作中の表示（2026-11-18・補助の BW_OVERLAY）: 札の文と、出すかどうか（LAPS_DESKTOP_INDICATOR=0 で出さない）
    req["indicator"] = os.environ.get("LAPS_DESKTOP_INDICATOR", "1") != "0"
    req["label"] = _overlay_label(req)
    helper = _get_helper()
    watch = _watch_region(req) if action == "click" and req.get("id") is None and "x" in req else None
    if watch and req.get("window"):
        # 窓を先に前へ出してから撮る（押したせいではなく「前に出た」だけの変化を数えない）
        try:
            helper.call({"action": "focus", "window": req["window"], "guard_pids": req["guard_pids"]},
                        _DEFAULT_TIMEOUT)
        except DesktopError:
            pass
    before = _shot(helper, watch, "a", req["guard_pids"]) if watch else None
    try:
        ans = helper.call(req, _TIMEOUT.get(action, _DEFAULT_TIMEOUT))
    except DesktopError as e:
        return ToolResult(str(e), ok=False, kind="error")
    ok = bool(ans.get("ok"))
    if action == "snapshot" and ok and _CHROMIUM_HEAD.search(str(ans.get("text") or "")[:300]) \
            and "Document" not in str(ans.get("text") or "") and req.get("id") is None:
        # ★Chromium は問い合わせが来てから中身の木を作る: 最初の 1 回はツールバーだけ（2026-11-03 実測）
        import time
        time.sleep(1.0)
        try:
            ans = helper.call(req, _TIMEOUT.get(action, _DEFAULT_TIMEOUT))
            ok = bool(ans.get("ok"))
        except DesktopError:
            pass
    if action == "web_snapshot" and not ok and "no web page found" in str(ans.get("text") or ans.get("error") or ""):
        # 同じく Chromium は最初の問い合わせでページの木をまだ作っていないことがある
        import time
        time.sleep(1.0)
        try:
            ans = helper.call(req, _TIMEOUT["web_snapshot"])
            ok = bool(ans.get("ok"))
        except DesktopError:
            pass
    if action == "click":
        change = None
        if ok and before is not None:
            after = _shot(helper, watch, "b", req["guard_pids"])
            change = _changed(before, after) if after is not None else None
        ans = dict(ans, text=ocr_note + str(ans.get("text") or "") + _click_feedback(req, ok, change))
    if ok and look_out is not None:
        return _look_result(str(ans.get("text") or ""), look_out, screen=req.get("screen") is not None,
                            region="x0" in req)
    text = waited + str(ans.get("text") or "") + fix_note
    if ok and then_snapshot and action not in ("snapshot", "windows"):
        try:
            snap = helper.call({"action": "snapshot", "guard_pids": req["guard_pids"]}, _TIMEOUT["snapshot"])
            text += "\n\n" + str(snap.get("text") or "")
        except DesktopError as e:
            text += f"\n\n(snapshot after the action failed: {e})"
    if not ok and ("pressed the stop key" in text or "pressed Esc to stop" in text):   # 後ろは旧い補助（Esc）の文言
        return ToolResult(text, ok=False, kind="denied")      # 利用者が止めるキー（Ctrl+Alt+Q）で止めた＝失敗ではなく止められた
    return ToolResult(text, ok=ok, kind="ok" if ok else "error")


_OVERLAY_VERBS = {"click": "クリック", "type": "入力", "key": "キー", "set_value": "値を入れる", "scroll": "スクロール",
                  "move": "マウスを動かす", "drag": "ドラッグ", "launch": "起動", "focus": "窓を前へ"}


def _overlay_label(req: dict) -> str:
    """画面の右下の札の文（何をしているか）。"""
    a = str(req.get("action") or "")
    verb = _OVERLAY_VERBS.get(a, a)
    what = req.get("text") if a in ("click", "type", "set_value") else req.get("key") if a == "key" else \
        req.get("command") if a == "launch" else req.get("window") if a == "focus" else None
    if what in (None, "") and req.get("id") is not None:
        what = f"[{req.get('id')}]"
    w = str(req.get("window") or "")
    s = f"{verb}: {str(what)[:50]}" if what not in (None, "") else verb
    return s + (f"（{w[:30]}）" if w and a != "focus" else "")


_CHROMIUM_HEAD = re.compile(r'" - (chrome|msedge|brave|vivaldi|opera)\b', re.I)
_LAST_LOOK_WINDOW = [""]
_TREE_LINE = re.compile(r'^\s*\[(\d+)\] (\w+) "([^"]*)"', re.M)
_PRESSABLE = ("Button", "Hyperlink", "MenuItem", "TabItem", "CheckBox", "RadioButton", "ListItem", "TreeItem", "ComboBox", "SplitButton")


def _click_target_from_tree(req: dict) -> bool:
    """click の text="…" を部品の木から探す。押せる部品で名前が同じ物が 1 つだけなら id にして True。"""
    q = {"action": "snapshot", "guard_pids": _guard_pids()}
    if req.get("window"):
        q["window"] = req["window"]
    try:
        ans = _get_helper().call(q, _TIMEOUT["snapshot"])
        text = str(ans.get("text") or "")
        if ans.get("ok") and _CHROMIUM_HEAD.search(text[:300]) and "Document" not in text:
            import time
            time.sleep(1.0)
            text = str(_get_helper().call(q, _TIMEOUT["snapshot"]).get("text") or "")
    except DesktopError:
        return False
    key = _norm_text(str(req.get("text") or ""))
    hits = [int(i) for i, role, name in _TREE_LINE.findall(text) if role in _PRESSABLE and _norm_text(name) == key]
    if len(hits) != 1:
        return False
    req["id"] = hits[0]
    req.pop("text", None)
    return True


_OCR_PIECE = re.compile(r'"([^"]*)" @(-?\d+),(-?\d+)')


def _norm_text(s: str) -> str:
    return unicodedata.normalize("NFKC", s).casefold().replace(" ", "").replace("\u3000", "")


def _click_target_from_ocr(req: dict) -> str | None:
    """click の text="…"（画面に描かれた文字）を、その窓の OCR から探して x・y に直す。だめなときは理由の文。
    ★2026-10-19 実走行（0.1.550・Gemma）: OCR の "胸を揉む" @3540,45 を y=85 と書き写して押し、外れた所を繰り返し押した。
      座標を写させずに文字で押せるようにする（部品の木が無いゲーム・描画だけのアプリ向け）。"""
    want = req.pop("text").strip()
    q = {"action": "ocr", "guard_pids": _guard_pids()}
    if req.get("window"):
        q["window"] = req["window"]
    try:
        ans = _get_helper().call(q, _TIMEOUT["ocr"])
    except DesktopError as e:
        return str(e)
    if not ans.get("ok"):
        return "Error: could not read the text on screen to find " + json.dumps(want, ensure_ascii=False) + ": " \
            + str(ans.get("text") or "")
    pieces = [(t, int(x), int(y)) for t, x, y in _OCR_PIECE.findall(str(ans.get("text") or ""))]
    key = _norm_text(want)
    found = [p for p in pieces if _norm_text(p[0]) == key] \
        or [p for p in pieces if key and key in _norm_text(p[0])] \
        or [p for p in pieces if _norm_text(p[0]) and _norm_text(p[0]) in key and len(_norm_text(p[0])) * 2 >= len(key)]
    if not found:
        seen = ", ".join(json.dumps(t, ensure_ascii=False) for t, _, _ in pieces[:25]) or "no text at all"
        return (f"Error: not clicked: {json.dumps(want, ensure_ascii=False)} was not found by OCR in the window. "
                f"OCR read: {seen}. Click one of these texts, or use x and y (OCR can misread decorated game fonts).")
    t, x, y = found[0]
    req["x"], req["y"] = x, y
    more = f" ({len(found) - 1} more matches; the first in reading order was used)" if len(found) > 1 else ""
    req["_ocr_note"] = f"Found {json.dumps(t, ensure_ascii=False)} by OCR at {x},{y}{more}. "
    return None


#: 直前の数回の click（同じ所を変化なしで押し続けるのを知らせる）
_RECENT_CLICKS: list = []


def _watch_region(req: dict) -> tuple[int, int, int, int] | None:
    """座標で押す click の前後で撮る範囲（画面の画素）。直前の look の範囲に入っていればそれ、無ければ点の周り。"""
    try:
        x, y = int(req["x"]), int(req["y"])
    except (KeyError, TypeError, ValueError):
        return None
    lk = _LAST_LOOK
    if lk and lk["left"] <= x < lk["left"] + lk["w"] and lk["top"] <= y < lk["top"] + lk["h"]:
        return lk["left"], lk["top"], lk["left"] + lk["w"], lk["top"] + lk["h"]
    return x - 320, y - 240, x + 320, y + 240


def _shot(helper, region, tag: str, guard: list) -> bytes | None:
    """範囲の写真（補助の look の region）。撮れない機械・失敗は None（確かめを飛ばすだけ）。"""
    out = _cache_dir() / f"desktop-diff-{tag}.png"
    try:
        out.unlink()
    except OSError:
        pass
    x0, y0, x1, y1 = region
    try:
        ans = helper.call({"action": "look", "x0": x0, "y0": y0, "x1": x1, "y1": y1, "out": str(out),
                           "guard_pids": guard}, _TIMEOUT["look"])
        if not ans.get("ok"):
            return None
        return out.read_bytes() or None
    except (DesktopError, OSError):
        return None


def _changed(a: bytes, b: bytes) -> float | None:
    """2 枚の写真で変わった画素の割合（0〜1）。比べられなければ None。"""
    if a == b:
        return 0.0
    try:
        import io
        from PIL import Image, ImageChops
        ia, ib = Image.open(io.BytesIO(a)).convert("RGB"), Image.open(io.BytesIO(b)).convert("RGB")
        if ia.size != ib.size:
            return 1.0
        small = (max(1, ia.width // 4), max(1, ia.height // 4))
        d = ImageChops.difference(ia.resize(small), ib.resize(small)).convert("L").point(lambda v: 255 if v > 24 else 0)
        return d.histogram()[255] / float(small[0] * small[1])
    except Exception:
        return None


def _click_feedback(req: dict, ok: bool, change: float | None) -> str:
    """click の結果に足す一言: 画面が変わったか・同じ所を変化なしで押し続けていないか。
    ★2026-10-19 実走行（0.1.550）: ゲームの画面で「Clicked at … on Pane」しか返らず、効いたか分からないまま
      rx=500,ry=500 や同じ点を何度も押した。文字しか読めないモデルにも分かるよう、変化を言葉で返す。"""
    if req.get("id") is not None:
        key = ("id", req.get("id"))
    elif "x" in req:
        key = ("xy", str(req.get("window") or ""), int(req["x"]) // 12, int(req["y"]) // 12)
    else:
        return ""
    if not ok:
        return ""
    prev = _RECENT_CLICKS[-1] if _RECENT_CLICKS else None
    _RECENT_CLICKS.append((key, change))
    del _RECENT_CLICKS[:-6]
    note = ""
    if change is not None:
        if change < 0.002:
            note = ("\nNothing changed on screen around the click: it probably hit nothing that reacts. Do not repeat it; "
                    "read the screen again (ocr) and pick another target, or use key.")
        else:
            note = f"\nThe screen changed after the click ({max(1, round(change * 100))}% of the area around it). Read it again before the next step."
    if prev and prev[0] == key and (change is None or change < 0.002) and (prev[1] is None or prev[1] < 0.002):
        note += ("\nWarning: this is the same click as the last one and nothing showed that it worked. Clicking the "
                 "same spot again will not help: check the screen, choose a different target, or try keys (enter, space, esc).")
    return note


DESKTOP_DESCRIPTION = """Operate graphical applications on this computer's desktop (Windows) through the accessibility tree. `snapshot` returns a window's controls as numbered text lines, and you act on them by id. `look` also shows you a picture of the window with the same ids drawn on it.

Workflow:
1. `launch` the program, or `windows` to find one that is already open.
2. `snapshot` the window -> lines like `[12] Button "Save" (shortcut Ctrl+S)` or `[15] Edit "File name" value="a.txt" (focused)`.
3. `click` / `type` / `set_value` / `key` using those ids. When an action opens a window (a dialog), the result already contains that window's snapshot.
4. `snapshot` again (or pass then_snapshot=true) to check the result before the next step.
Prefer the keyboard when you can: if a control lists a shortcut (shortcut Ctrl+S) or the step has a common one (Ctrl+S, Ctrl+L, Alt+F4, Tab, Enter, Esc), use `key` instead of clicking — it does not miss and needs no picture.
Look closely before acting: `read` gives the full text of a document, log or page, `look` shows how the window actually looks (layout, icons, colors, drawn content), and `ocr` reads whatever text is drawn on screen even when the window lists no controls (games, canvas-drawn apps, images).

Actions:
- windows: list the screens (s1 is the main one; size, position and scale of each) and the open windows as w1, w2, ... with the screen each one is on.
- snapshot: list the controls of `window` (w-number, part of the title, or process name). Without `window` it reads the window you last worked in (or the one in front). `id` reads only that container; `offscreen=true` also lists controls scrolled out of view. `wait` = seconds to wait for the window to appear. Lines may carry value=, tip= (tooltip), status=, and flags such as disabled, focused, checked, selected, expanded, read-only, modal, N rows x M columns, shortcut Ctrl+S.
- web_snapshot: only the web page inside a Chrome or Edge `window` (no tabs, no address bar), with its title and URL; the same numbered lines as snapshot. Use it for the user's own browser when the Browser tool's Chrome extension is not connected. Clicking an id below the visible part scrolls it into view first; fill boxes with set_value (whole value at once). It needs the window in front, moves the real mouse, and cannot run JavaScript or read the page source, so prefer Browser when it works. Firefox pages are not listed.
- look: a picture of `window` with a numbered red box on each listed control (the same ids as snapshot), plus the list. For things with no box, click or scroll at `rx`,`ry` (0-1000 across and down that picture). `screen`=2 instead takes a picture of the whole screen s2 and lists the windows on it.
- read: the full text of control `id` or of `window` (documents, editors, logs, web pages, long values): up to 30,000 characters.
- ocr: recognize the text drawn in `window` (or in the image file `path`) with Windows OCR. Each piece of text comes with its center as @x,y; for a window these are screen coordinates for click with x and y.
- launch: open `command` (a program such as notepad or calc, a file path, or a URL) with optional `args`; reports the new window and brings it to the front.
- focus: bring `window` to the front (a window left on no screen is moved onto the main screen).
- click: click control `id` (or `rx`,`ry` in the last look picture, or screen pixel `x`,`y`, or `text`: words drawn on screen, found by OCR in `window`). `button` left/right/middle; `double` for a double-click, `count`=3 for a triple-click. A click by position reports whether the screen around it changed.
- type: type `text` into control `id`, or into the focused control of `window`. Any language works; "\\n" presses Enter.
- set_value: replace the whole value of edit box or combo box `id` with `text`.
- key: press keys in `window` (or control `id`): "ctrl+s", "enter", "alt+f4", "ctrl+a delete" (a space-separated sequence). `hold`=seconds keeps one key held down.
- scroll: scroll `amount` notches over control `id` or at `rx`,`ry` (negative = down, positive = up).
- move: put the mouse on `id` / `rx`,`ry` / `x`,`y` and leave it there (hover menus, tooltips).
- drag: press at the start (`from_id`, or `from_rx`,`from_ry`, or `from_x`,`from_y`), move, release at the end (`id`, `rx`,`ry` or `x`,`y`): sliders, moving items, selecting.
- cursor: where the mouse is and what is under it.
- wait: wait `seconds` (up to 10), then snapshot (for loading screens and animations).
- look with `region`=[rx0,ry0,rx1,ry1] zooms into part of the last look picture at full resolution.
Claude Computer Use names also work (left_click, triple_click, mouse_move, left_click_drag, cursor_position, hold_key, zoom; coordinate=[x,y] is a point in the last look picture, or screen pixels before any look).

Mouse and keyboard:
- click and scroll use the real mouse pointer (it is put back where it was afterwards). A control with no place on screen, or hidden behind another window, is pressed through the accessibility interface instead.
- type and key use the real keyboard and bring the window to the front. Without id or window they go to the window you last worked in, never to an unrelated window that happens to be in front. For text fields, set_value is the most reliable.

Games and drawn windows (no controls listed):
- Read the screen with ocr, then click the words you want with `text` (e.g. text="Start") and `window`; do not copy @x,y numbers by hand.
- If a click reports that nothing changed, do not click the same spot again: read the screen again, try another target, or keys such as enter, space, esc or arrow keys.
- Never press alt+f4 or close the window unless the task is to close it.

Several screens:
- x and y are desktop pixels across all screens; a screen left of or above the main one has negative ones. A point on no screen is refused, and the error lists the screens.
- Prefer id, or rx/ry from a look, over typing pixel numbers yourself.

Blackwall's own window:
- The window of the app you run in is marked "(Blackwall itself: read only)". You may read or look at it, but click, type, key, scroll, set_value and focus on it are refused, also when it covers the spot you aim at: closing it would stop this session.

Notes:
- An id stays valid while its control exists, and the same control keeps the same id in later snapshots.
- Dialogs (file pickers, confirmations) are listed by windows as their own windows.
- Off-screen controls are not listed; scroll, or snapshot again after the window changes.
- Use Bash for files and command-line programs; use this tool only for things that need the graphical interface."""


def description(vision: bool = True) -> str:
    """モデルが画像を見られないときは、写真（look・rx/ry・region）を前提にした行を外した説明にする。
    ★2026-10-19 オーナー「使っているモデルに応じて変わるようにして無駄を省きましょう」。look は写真が届かず
      札の一覧（snapshot と同じ）しか役に立たないので、勧めると 1 手と読み込みを無駄にする。"""
    if vision:
        return DESKTOP_DESCRIPTION
    out = []
    for line in DESKTOP_DESCRIPTION.split("\n"):
        if line.startswith(("- look", "Look closely")):
            continue
        line = line.replace(" `look` also shows you a picture of the window with the same ids drawn on it.", "")
        line = line.replace(" (or `rx`,`ry` in the last look picture, or screen pixel `x`,`y`", " (or screen pixel `x`,`y`")
        line = line.replace(" or at `rx`,`ry`", " or at `x`,`y`").replace(" / `rx`,`ry`", "")
        line = line.replace(", or `from_rx`,`from_ry`,", ",").replace(", `rx`,`ry` or", " or")
        line = line.replace("coordinate=[x,y] is a point in the last look picture, or screen pixels before any look",
                            "coordinate=[x,y] is in screen pixels")
        line = line.replace(", zoom;", ";").replace("Prefer id, or rx/ry from a look,", "Prefer id")
        line = line.replace("You may read or look at it", "You may read it")
        out.append(line)
    out.insert(out.index("Actions:"), "This model cannot view pictures: read the screen with `snapshot` (controls), "
               "`read` (full text) and `ocr` (text drawn on screen, with @x,y positions to click).\n")
    return "\n".join(out)


def schema(vision: bool = True) -> dict:
    s = _schema()
    if not vision:
        props = s["properties"]
        props["action"]["enum"] = [a for a in ACTIONS if a != "look"]
        for k in ("rx", "ry", "from_rx", "from_ry", "region", "screen"):
            props.pop(k, None)
    return s


def _schema() -> dict:
    return {"type": "object", "properties": {
        "action": {"type": "string", "enum": list(ACTIONS), "description": "What to do"},
        "window": {"type": "string", "description": "Target window: w-number from windows, part of the "
                   "title, or process name (e.g. notepad)"},
        "id": {"type": "integer", "description": "Control id from snapshot"},
        "text": {"type": "string", "description": "Text to type (type), the new value (set_value), or words on screen "
                 "to click, found by OCR (click)"},
        "keys": {"type": "string", "description": 'Keys to press (key), e.g. "ctrl+s" or "tab tab enter"'},
        "command": {"type": "string", "description": "Program, file path or URL to open (launch)"},
        "args": {"type": "string", "description": "Command-line arguments for launch"},
        "rx": {"type": "integer", "description": "click/scroll/move/drag: 0-1000 across the last look picture"},
        "ry": {"type": "integer", "description": "click/scroll/move/drag: 0-1000 down the last look picture"},
        "x": {"type": "integer", "description": "Screen x in pixels (click/scroll without id)"},
        "y": {"type": "integer", "description": "Screen y in pixels (click/scroll without id)"},
        "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button (click)"},
        "double": {"type": "boolean", "description": "Double-click (click)"},
        "amount": {"type": "integer", "description": "Scroll notches: negative = down, positive = up (scroll)"},
        "wait": {"type": "number", "description": "Seconds to wait for the window to appear "
                 "(snapshot/focus/launch)"},
        "then_snapshot": {"type": "boolean", "description": "After the action, also return a snapshot of "
                          "the foreground window"},
        "offscreen": {"type": "boolean", "description": "snapshot: also list controls that are scrolled out of view"},
        "path": {"type": "string", "description": "ocr: an image file to read instead of a window"},
        "screen": {"type": "integer", "description": "look: picture of a whole screen (1 = the main screen; windows lists "
                   "them). Leave it out to look at a window"},
        "count": {"type": "integer", "description": "click: 3 = triple-click"},
        "hold": {"type": "number", "description": "key: seconds to hold the key down"},
        "seconds": {"type": "number", "description": "wait: seconds (up to 10)"},
        "from_id": {"type": "integer", "description": "drag: start control"},
        "from_rx": {"type": "integer", "description": "drag: start, 0-1000 across the last look picture"},
        "from_ry": {"type": "integer", "description": "drag: start, 0-1000 down the last look picture"},
        "region": {"type": "array", "items": {"type": "number"}, "description": "look: [rx0,ry0,rx1,ry1] in the last look picture to zoom into"},
    }, "required": ["action"]}
