"""依頼 1 件ぶんの変更の控えと取り消し（2026-11-11・Codex の turn_diff_tracker と旧い中核の checkpoint.py を手本に）。

- 依頼の間に Write / Edit が書き換えるファイルを、最初の 1 回だけ会話の置き場（undo/<依頼>/）へ控える。
- 依頼の終わりに、控えと今の中身を比べて「変えたファイル（+行 −行）」を出す。終わった時点の中身の指紋も残す。
- `/undo` で、変更のある直前の依頼を戻す。終わった後に別の手で変わったファイルは上書きしない（飛ばして理由を言う）。
- `/undo since=<エポックのミリ秒>` で、その時刻より後に始めた依頼の変更を、新しい物から順に全部戻す
  （2026-11-15 アプリの「Rewind」＝発言ごとの「ここまで戻す」。新しい順に戻すので、続けて同じファイルを変えた依頼も元に揃う）。
- Bash が変えた物は分からない（Codex も同じ）。戻せないと必ず言う。★2026-11-20 から、壊す形の Bash が触るファイルは命令の前に
  控える（engine/tools/bashsafe.py）＝そのファイルは戻せる。サービス・網などは戻さない。
控えの目録はディスクに置くので、本体を起こし直しても戻せる。古い依頼の控えは KEEP_RUNS を超えたら消す。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

KEEP_RUNS = 12                     # 会話ごとに残す依頼の控えの数
MAX_FILE_BYTES = 32 * 1024 * 1024  # これより大きいファイルは控えない（戻せないと言う）
DIFF_BYTES = 2 * 1024 * 1024       # 行数を数えるのはこれ以下のテキストだけ


def _sha(path: str) -> str:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _lines(data: bytes) -> list[str] | None:
    if len(data) > DIFF_BYTES or b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", "replace").splitlines()


class RunChanges:
    """依頼 1 件の控え。record は道具が書く直前に呼ぶ。例外は外へ出さない（控えの失敗で編集を止めない）。"""

    def __init__(self, conv_root: Path, rid: str) -> None:
        base = Path(conv_root) / "undo"
        self.dir = base / rid
        if not rid or "/" in rid or "\\" in rid or rid in (".", "..") or self.dir.parent != base:
            raise ValueError(f"bad run id for the undo store: {rid!r}")
        self.rid = rid
        self.files: dict[str, dict] = {}
        self.skipped: list[str] = []
        self.bash: list[str] = []
        self.finished = False
        self.undone = False
        self.started = time.time()          # 依頼を始めた時刻（since で選ぶため・古い控えには無い＝at で代える）

    # ── 控える ──────────────────────────────
    def record(self, path: str) -> None:
        try:
            ap = os.path.abspath(path)
            if ap in self.files or self.finished:
                return
            entry: dict[str, Any] = {"existed": os.path.isfile(ap), "blob": "", "mode": None}
            if entry["existed"]:
                size = os.path.getsize(ap)
                if size > MAX_FILE_BYTES:
                    self.skipped.append(ap)
                    return
                self.dir.mkdir(parents=True, exist_ok=True)
                blob = hashlib.sha256(ap.encode("utf-8")).hexdigest()[:24] + ".blob"
                shutil.copy2(ap, self.dir / blob)
                entry["blob"] = blob
                entry["mode"] = os.stat(ap).st_mode & 0o7777
            self.files[ap] = entry
            self._save()
        except OSError:
            self.skipped.append(os.path.abspath(path))

    def note_bash(self, command: str) -> None:
        if len(self.bash) < 50:
            self.bash.append(command[:200])

    # ── 終わり ──────────────────────────────
    def finish(self) -> list[dict]:
        """依頼の終わり。変えたファイルの一覧（+/− 行つき）を返し、終わった時点の指紋を残す。"""
        self.finished = True
        out: list[dict] = []
        for ap, e in self.files.items():
            now = os.path.isfile(ap)
            e["after"] = _sha(ap) if now else ""
            before = b""
            if e["blob"]:
                try:
                    before = (self.dir / e["blob"]).read_bytes()
                except OSError:
                    before = b""
            if e["existed"] and now and e["after"] == hashlib.sha256(before).hexdigest():
                continue                   # 書いたが元と同じ
            if not e["existed"] and not now:
                continue                   # 作って消した
            status = "added" if not e["existed"] else ("deleted" if not now else "modified")
            added = removed = 0
            try:
                after_data = Path(ap).read_bytes() if now else b""
            except OSError:
                after_data = b""
            a, b = _lines(before), _lines(after_data)
            if a is not None and b is not None:
                for line in difflib.unified_diff(a, b, lineterm="", n=0):
                    if line.startswith("+") and not line.startswith("+++"):
                        added += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        removed += 1
            out.append({"path": ap, "status": status, "added": added, "removed": removed})
        if out or self.files:
            self._save(changes=out)
        else:
            self._drop()
        return out

    # ── 戻す ──────────────────────────────
    def undo(self) -> dict:
        """控えへ戻す。終わった後に変わったファイルは飛ばす。{"restored","deleted","skipped","bash"}。"""
        res: dict[str, list] = {"restored": [], "deleted": [], "skipped": [], "bash": list(self.bash)}
        for ap, e in self.files.items():
            now = os.path.isfile(ap)
            cur = _sha(ap) if now else ""
            if cur != e.get("after", cur):
                res["skipped"].append([ap, "changed after the request"])
                continue
            try:
                if e["existed"]:
                    if not e["blob"]:
                        res["skipped"].append([ap, "no copy was kept"])
                        continue
                    os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)   # rm -r で消えたフォルダの中のファイル（2026-11-20）
                    tmp = ap + ".bwundo-tmp"
                    shutil.copy2(self.dir / e["blob"], tmp)
                    if e.get("mode") is not None:
                        os.chmod(tmp, int(e["mode"]))
                    os.replace(tmp, ap)
                    res["restored"].append(ap)
                elif now:
                    os.unlink(ap)
                    res["deleted"].append(ap)
            except OSError as ex:
                res["skipped"].append([ap, str(ex)])
        res["skipped"] += [[p, "too large to keep a copy"] for p in self.skipped]
        # ★何か戻せたときだけ「戻した」にする（2026-11-16 全体のレビュー: 依頼の後に手で変えたファイルを飛ばしただけでも戻した扱いになり、
        #   手の変更を戻した後の /undo が、1 つ古い依頼を戻していた）。
        self.undone = bool(res["restored"] or res["deleted"]) or not any(
            why == "changed after the request" for _, why in res["skipped"])
        self._save()
        return res

    # ── 置き場 ──────────────────────────────
    def _save(self, changes: list[dict] | None = None) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            meta = {"rid": self.rid, "files": self.files, "skipped": self.skipped, "bash": self.bash,
                    "finished": self.finished, "undone": self.undone, "at": time.time(), "started": self.started}
            if changes is not None:
                meta["changes"] = changes
            else:
                old = self._read_meta()
                if old.get("changes") is not None:
                    meta["changes"] = old["changes"]
            tmp = self.dir / "manifest.json.tmp"
            tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.dir / "manifest.json")
        except OSError:
            pass

    def _read_meta(self) -> dict:
        try:
            return json.loads((self.dir / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _drop(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    @classmethod
    def load(cls, d: Path) -> "RunChanges | None":
        rc = cls(d.parent.parent, d.name)
        meta = rc._read_meta()
        if not meta:
            return None
        rc.files = meta.get("files") or {}
        rc.skipped = meta.get("skipped") or []
        rc.bash = meta.get("bash") or []
        rc.finished = bool(meta.get("finished"))
        rc.undone = bool(meta.get("undone"))
        rc.started = float(meta.get("started") or meta.get("at") or 0.0)
        return rc

    def changes(self) -> list[dict]:
        return list(self._read_meta().get("changes") or [])


_SINCE = re.compile(r"^/undo\s+since=(\d{10,16})$")


def parse_command(prompt: str) -> dict | None:
    """取り消しの合図なら {"since": 秒 か None}、違えば None。`/undo since=<ミリ秒>` はアプリの「Rewind」が送る。"""
    p = prompt.strip().lower()
    if p in ("/undo", "/取り消し", "/とりけし"):
        return {"since": None}
    m = _SINCE.match(p)
    if m:
        return {"since": int(m.group(1)) / 1000.0}
    return None


def is_command(prompt: str) -> bool:
    """利用者が打った取り消しの合図（どの端末からでも同じ文で送れる）。"""
    return parse_command(prompt) is not None


def begin(conv_root: Path, rid: str) -> RunChanges | None:
    if os.environ.get("LAPS_UNDO", "1") == "0":
        return None
    prune(Path(conv_root))
    try:
        return RunChanges(Path(conv_root), rid)
    except ValueError:                     # 場所に使えない id（控えを取らない＝戻せないだけ・走行は止めない）
        return None


def prune(conv_root: Path) -> None:
    base = conv_root / "undo"
    try:
        runs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    except OSError:
        return
    for d in runs[:-KEEP_RUNS]:
        shutil.rmtree(d, ignore_errors=True)


def last_undoable(conv_root: Path) -> RunChanges | None:
    """変更があって、まだ戻していない一番新しい依頼。"""
    base = Path(conv_root) / "undo"
    try:
        runs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for d in runs:
        rc = RunChanges.load(d)
        if rc and rc.finished and not rc.undone and rc.changes():
            return rc
    return None


def undoable_since(conv_root: Path, since: float) -> list[RunChanges]:
    """since（秒）より後に始めた、変更があってまだ戻していない依頼。新しい順（この順に戻す）。"""
    base = Path(conv_root) / "undo"
    try:
        dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return []
    out = []
    for d in dirs:
        rc = RunChanges.load(d)
        if rc and rc.finished and not rc.undone and rc.started >= since - 1.0 and rc.changes():
            out.append(rc)
    out.sort(key=lambda r: r.started, reverse=True)
    return out


def merge(results: list[dict]) -> dict:
    """何件かの依頼を戻した結果を 1 つに（同じファイルは最後に戻した物＝いちばん古い依頼の前の中身）。"""
    res: dict[str, list] = {"restored": [], "deleted": [], "skipped": [], "bash": []}
    for r in results:
        for k in ("restored", "deleted", "bash"):
            for x in r.get(k) or []:
                if x not in res[k]:
                    res[k].append(x)
        res["skipped"] += [x for x in r.get("skipped") or [] if x not in res["skipped"]]
    # ★飛ばした物は隠さない（2026-11-16 全体のレビュー: 間で手が入ったファイルは、新しい依頼の分だけ戻り、古い依頼の分が残る。
    #   前は「Restored F」とだけ出て、古い依頼の変更が残っていることを言わなかった）。
    return res


def summary(changes: list[dict], ja: bool) -> str:
    """「変えたファイル」の 1 行（画面の行に出す）。"""
    if not changes:
        return ""
    plus = sum(c["added"] for c in changes)
    minus = sum(c["removed"] for c in changes)
    names = []
    for c in changes[:6]:
        n = os.path.basename(c["path"])
        tag = {"added": " (new)" if not ja else "（新規）", "deleted": " (deleted)" if not ja else "（削除）"}.get(c["status"], "")
        names.append(f"{n}{tag} +{c['added']} −{c['removed']}" if c["status"] == "modified" else f"{n}{tag}")
    more = f" … +{len(changes) - 6}" if len(changes) > 6 else ""
    if ja:
        return f"変えたファイル {len(changes)} 件（+{plus} −{minus}）: " + ", ".join(names) + more + "。戻すには /undo"
    return f"Changed {len(changes)} files (+{plus} −{minus}): " + ", ".join(names) + more + ". Send /undo to undo."


def report(res: dict, ja: bool) -> str:
    """戻した結果を利用者へ（画面の答え）。"""
    def names(xs: list) -> str:
        return ", ".join(os.path.basename(x if isinstance(x, str) else x[0]) for x in xs[:10]) + (" …" if len(xs) > 10 else "")
    lines = []
    if ja:
        if res["restored"]:
            lines.append(f"元に戻しました: {names(res['restored'])}")
        if res["deleted"]:
            lines.append(f"新しく作ったファイルを消しました: {names(res['deleted'])}")
        for p, why in res["skipped"]:
            lines.append(f"戻さなかった: {os.path.basename(p)}（{ {'changed after the request': '依頼の後に変わっていた', 'too large to keep a copy': '大きすぎて控えていない', 'no copy was kept': '控えが無い'}.get(why, why)}）")
        if res["bash"]:
            lines.append(f"コマンド（Bash）で変えた物は、命令の前に控えたファイルだけを戻しました。サービスや網などの変更は戻していません（コマンド {len(res['bash'])} 件）。")
        return "\n".join(lines) or "戻す変更がありませんでした。"
    if res["restored"]:
        lines.append(f"Restored: {names(res['restored'])}")
    if res["deleted"]:
        lines.append(f"Deleted new files: {names(res['deleted'])}")
    for p, why in res["skipped"]:
        lines.append(f"Not undone: {os.path.basename(p)} ({why})")
    if res["bash"]:
        lines.append(f"For shell commands, only files saved before the command were put back; service, network and other changes were not undone ({len(res['bash'])}).")
    return "\n".join(lines) or "There was nothing to undo."


def model_note(res: dict, runs: int = 1) -> str:
    """次の依頼でモデルが知るための一言（英語）。"""
    parts = []
    if res["restored"]:
        parts.append("restored " + ", ".join(res["restored"][:10]))
    if res["deleted"]:
        parts.append("deleted the new files " + ", ".join(res["deleted"][:10]))
    if res["skipped"]:
        parts.append("left " + ", ".join(p for p, _ in res["skipped"][:10]) + " as they were")
    what = "the previous request" if runs <= 1 else f"the last {runs} requests"
    text = f"[Harness] The user undid the file changes of {what}: " + "; ".join(parts or ["nothing changed"]) + "."
    if res["bash"]:
        text += " For shell commands in that request, only files saved before each command were put back; service, network and other changes were not undone."
    return text
