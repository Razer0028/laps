"""整合性の確認 — 本体（Python）の側で見られる所を点検する（2026-10-08）。

オーナー「Windows 版に整合性のチェック機能を追加してほしい」。アプリ（app/src-tauri/src/integrity.rs）が
`python -m laps.integrity --json` を走らせ、ここで出した行を画面の一覧に混ぜる。

見るもの（どれも読むだけ・何も直さない。直すのはアプリの「直す」から）:
  - 同梱の Python の中身: 束を組んだときの照合値の一覧（INTEGRITY.json）と、いま動いている束・インストール先の束を突き合わせる
  - 本体のコード: 主な部品が import できるか
  - 道具の部品: Listen（whisper.cpp の本体とモデル）・Desktop（補助プログラム）・ffmpeg がそろっているか

1 行 = {"id", "group", "title", "status": "ok"|"warn"|"fail", "detail", "fix": 直す手の名前 or null}。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

MANIFEST = "INTEGRITY.json"


def _row(id_: str, group: str, title: str, status: str, detail: str, fix: str | None = None) -> dict:
    return {"id": id_, "group": group, "title": title, "status": status, "detail": detail, "fix": fix}


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ★写し（利用者名が ASCII でない環境でアプリが作る pyruntime の複製）には入れない直下のフォルダ。
#   REA（rea/・263MB）はインストール先のものを使う（アプリの sidecar.rs の STAGE_SKIP と同じ・2026-11-03）。
STAGED_SKIP = ("rea/",)


def verify_tree(root: Path, skip: tuple[str, ...] = ()) -> tuple[int, list[str], list[str]] | None:
    """一覧と突き合わせる。(一覧の件数, 無いファイル, 中身が違うファイル)。一覧が無ければ None。
    ★一覧に無いファイル（動かしたときにできる __pycache__ など）は見ない。`skip` で始まる項目も見ない。"""
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        files = {k: v for k, v in data["files"].items() if not k.replace("\\", "/").startswith(skip)}
    except Exception:
        return None
    missing, changed = [], []
    for rel, (size, sha) in files.items():
        p = root / rel
        try:
            if not p.is_file():
                missing.append(rel)
            elif p.stat().st_size != size or _sha256(p) != sha:
                changed.append(rel)
        except OSError:
            missing.append(rel)
    return len(files), missing, changed


def _tree_row(id_: str, title: str, root: Path, fix: str | None, where: str, skip: tuple[str, ...] = ()) -> dict:
    r = verify_tree(root, skip)
    if r is None:
        return _row(id_, "app", title, "warn",
                    f"{root} に照合値の一覧（{MANIFEST}）がありません（0.1.322 より前の束では作っていません）。", None)
    n, missing, changed = r
    if not missing and not changed:
        return _row(id_, "app", title, "ok", f"{n} 個のファイルがすべて一致（{root}）")
    bad = missing + changed
    head = "・".join(bad[:4]) + (f" ほか {len(bad) - 4} 個" if len(bad) > 4 else "")
    return _row(id_, "app", title, "fail",
                f"{n} 個中 無い {len(missing)}・中身が違う {len(changed)}（{head}）。{where}", fix)


# ★2026-10-08 実機: 更新の途中で同梱の Python が掴まれていて、照合値の一覧だけ新しく、変わったファイルは古いまま残った。
#   公開版には配信元が無いので、自動では直さない（入れ直しを案内する）。
_REINSTALL_NOTE = ("アプリを入れ直してください。更新の途中で止まった・ウイルス対策が消した、などで起きます。")


def check_app() -> list[dict]:
    rows = []
    here = Path(sys.executable).resolve().parent              # いま動いている束（写しかインストール先）
    install = (os.environ.get("LAPS_INSTALL_PYRUNTIME") or "").strip()
    inst = Path(install).resolve() if install else None
    if inst is not None and inst != here:
        rows.append(_tree_row("app.install", "インストール先の Python", inst, None, _REINSTALL_NOTE))
        rows.append(_tree_row("app.staged", "動かしている Python の写し", here, "restage",
                              "「直す」でインストール先から写し直します。", STAGED_SKIP))
    else:
        rows.append(_tree_row("app.install", "同梱の Python", here, None, _REINSTALL_NOTE))
    mods = ["laps.serve", "laps.engine.server", "laps.oc.tools", "laps.remotehost",
            "httpx", "websockets"]
    bad = []
    for m in mods:
        try:
            __import__(m)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{m}（{type(e).__name__}: {e}）"[:160])
    rows.append(_row("app.import", "app", "本体のコード", "fail" if bad else "ok",
                     "読み込めない部品: " + "・".join(bad) if bad else f"主な部品 {len(mods)} 個を読み込めます",
                     None))
    return rows


def check_parts() -> list[dict]:
    rows = []
    try:
        from .oc import listen
        if not listen.available():
            rows.append(_row("parts.listen", "parts", "Listen（音声の書き起こし）", "ok", "この機械では使いません"))
        else:
            var = listen._variant()
            eng = listen._root() / "engine" / f"{listen._REL}-{var}"
            eng_ok = eng.is_dir() and (eng / ".ok").exists() and listen._find_cli(eng) is not None
            name, _url, _sha, size = listen._MODEL
            mp = listen._root() / "models" / name
            model_ok = mp.is_file() and mp.stat().st_size == size and mp.with_name(name + ".ok").exists()
            if eng_ok and model_ok:
                rows.append(_row("parts.listen", "parts", "Listen（音声の書き起こし）", "ok",
                                 f"本体（{var}）とモデル（{name}）がそろっています"))
            else:
                lack = [x for x, ok in (("本体", eng_ok), (f"モデル {name}", model_ok)) if not ok]
                rows.append(_row("parts.listen", "parts", "Listen（音声の書き起こし）", "fail",
                                 "そろっていない: " + "・".join(lack) + f"（{listen._root()}）。「直す」で取り寄せます。",
                                 "parts"))
    except Exception as e:  # noqa: BLE001
        rows.append(_row("parts.listen", "parts", "Listen（音声の書き起こし）", "warn", f"確かめられません: {e}"))
    try:
        from .oc import desktop
        if not desktop.available():
            rows.append(_row("parts.desktop", "parts", "Desktop（画面の操作）", "ok", "この機械では使いません"))
        else:
            tag = hashlib.sha256(desktop.HELPER_SOURCE.read_bytes()).hexdigest()[:12]
            exe = desktop._cache_dir() / f"bw-desktop-{tag}.exe"
            if exe.is_file():
                rows.append(_row("parts.desktop", "parts", "Desktop（画面の操作）", "ok", f"補助プログラムがあります（{exe.name}）"))
            else:
                rows.append(_row("parts.desktop", "parts", "Desktop（画面の操作）", "fail",
                                 f"補助プログラムがまだ組み立てられていません（{desktop._cache_dir()}）。「直す」で組み立てます。",
                                 "parts"))
    except Exception as e:  # noqa: BLE001
        rows.append(_row("parts.desktop", "parts", "Desktop（画面の操作）", "warn", f"確かめられません: {e}"))
    try:
        from .oc import voice
        if voice.available():
            inst = voice.installed()
            py = voice._venv_python()
            if inst:
                rows.append(_row("parts.voice", "parts", "Voice（声の複製・読み上げ）", "ok",
                                 f"入っています（{inst.get('flavor')} 版・{voice._root()}）"))
            elif voice._marker().exists() or py.exists():
                rows.append(_row("parts.voice", "parts", "Voice（声の複製・読み上げ）", "fail",
                                 f"入れかけのまま止まっています（{voice._root()}）。「直す」で入れ直します（約 5〜7GB）。", "voice"))
            else:
                rows.append(_row("parts.voice", "parts", "Voice（声の複製・読み上げ）", "ok",
                                 "まだ入れていません（初めて使うときに取り寄せます・約 5〜7GB）"))
    except Exception as e:  # noqa: BLE001
        rows.append(_row("parts.voice", "parts", "Voice（声の複製・読み上げ）", "warn", f"確かめられません: {e}"))
    # ★資料作成（Doc・2026-10-08）。Windows は部品を同梱しているので、実際に読み込めるかまで確かめる
    #   （C 拡張の DLL が欠けると find_spec は通って import で落ちる）。それ以外は専用の環境の有無だけ。
    try:
        from .oc import doctool
        if doctool.in_process():
            import importlib
            from importlib.metadata import version as _ver
            bad = []
            for m in ("lxml.etree", "PIL.Image", "numpy", "docx", "pptx", "openpyxl", "reportlab", "matplotlib.pyplot", "pypdf"):
                try:
                    if m == "matplotlib.pyplot":
                        import matplotlib
                        matplotlib.use("Agg")
                    importlib.import_module(m)
                except Exception as e:  # noqa: BLE001
                    bad.append(f"{m}: {type(e).__name__}: {e}"[:160])
            if bad:
                rows.append(_row("parts.docs", "parts", "Doc（Word・PowerPoint・Excel・PDF）", "fail",
                                 "同梱の部品を読み込めません: " + " / ".join(bad[:3]) + "。アプリを入れ直すと直ります。",
                                 None))
            else:
                vs = " · ".join(f"{n} {_ver(n)}" for n in ("python-docx", "python-pptx", "openpyxl", "reportlab", "matplotlib"))
                rows.append(_row("parts.docs", "parts", "Doc（Word・PowerPoint・Excel・PDF）", "ok", f"同梱: {vs}"))
        else:
            ok = (doctool._root() / "env" / ".ok").exists()
            rows.append(_row("parts.docs", "parts", "Doc（Word・PowerPoint・Excel・PDF）", "ok",
                             f"専用の環境があります（{doctool._root()}）" if ok
                             else "まだ作っていません（初めて使うときに作ります・約 300MB）"))
    except Exception as e:  # noqa: BLE001
        rows.append(_row("parts.docs", "parts", "Doc（Word・PowerPoint・Excel・PDF）", "warn", f"確かめられません: {e}"))
    try:
        from .oc import toolenv
        found = shutil.which("ffmpeg", path=toolenv.child_path())
        if found:
            rows.append(_row("parts.ffmpeg", "parts", "ffmpeg（音声・動画の変換）", "ok", found))
        else:
            rows.append(_row("parts.ffmpeg", "parts", "ffmpeg（音声・動画の変換）", "fail",
                             f"見つかりません（道具の置き場 {toolenv.tools_dir()}）。「直す」で取り寄せます。", "parts"))
    except Exception as e:  # noqa: BLE001
        rows.append(_row("parts.ffmpeg", "parts", "ffmpeg（音声・動画の変換）", "warn", f"確かめられません: {e}"))
    return rows


def check() -> list[dict]:
    return check_app() + check_parts()


def repair_staged() -> tuple[int, list[str]]:
    """動かしている写しの、無い・中身が違うファイルだけをインストール先から写し直す（「直す」の restage）。
    ★写す前に、インストール先のファイルが一覧と一致することを確かめる（壊れた物で上書きしない）。
    ★使用中のファイル（python312.dll 等）は Windows では上書きできない＝失敗として返す（アプリを開き直すと写し直される）。
    戻り: (写し直した数, 写せなかったファイルと理由)。"""
    install = (os.environ.get("LAPS_INSTALL_PYRUNTIME") or "").strip()
    here = Path(sys.executable).resolve().parent
    if not install or Path(install).resolve() == here:
        return 0, ["インストール先と写しが同じ場所です（写し直す物はありません）"]
    src = Path(install).resolve()
    r = verify_tree(here, STAGED_SKIP)
    if r is None:
        return 0, [f"{here} に照合値の一覧がありません"]
    data = json.loads((src / MANIFEST).read_text(encoding="utf-8"))["files"]
    _n, missing, changed = r
    done, bad = 0, []
    for rel in missing + changed:
        size, sha = data.get(rel, (None, None))
        s_p, d_p = src / rel, here / rel
        try:
            if size is None or not s_p.is_file() or s_p.stat().st_size != size or _sha256(s_p) != sha:
                bad.append(f"{rel}（インストール先のほうも壊れている＝アプリを入れ直してください）")
                continue
            d_p.parent.mkdir(parents=True, exist_ok=True)
            tmp = d_p.with_name(d_p.name + ".bwtmp")
            shutil.copyfile(s_p, tmp)
            os.replace(tmp, d_p)
            done += 1
        except OSError as e:
            bad.append(f"{rel}（{e.strerror or e}）")
    return done, bad


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if "--repair-staged" in args:
        done, bad = repair_staged()
        print(json.dumps({"repaired": done, "failed": bad}, ensure_ascii=False))
        return 0 if not bad else 1
    rows = check()
    if "--json" in args:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for r in rows:
            mark = {"ok": "OK  ", "warn": "WARN", "fail": "NG  "}.get(r["status"], "?   ")
            print(f"[{mark}] {r['title']}: {r['detail']}")
    return 1 if any(r["status"] == "fail" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
