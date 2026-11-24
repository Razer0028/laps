"""機能の道具（画面操作・音声・ブラウザなど）。

使う機会の少ない道具を毎回モデルに読ませない。名前の一覧だけを渡し、説明は呼ばれたときに返す
（Claude Code の後から読み込む道具と同じ考え）。道具の一覧を変えると推論機の読み込みの控えが効かなく
なるので、一覧を増やすのではなく Tools の中から呼ぶ。

中身は laps/oc/ の既存の実装をそのまま呼ぶ（機能そのものは中核の作り直しの対象ではない）。"""
from __future__ import annotations

import base64
import json
import re
import threading
import uuid
from typing import Any

from . import Ctx, Result, Tool, clip, fail

#: Mcp の先のサーバーのうち、利用者のブラウザを動かす物の名前（設定の名前で見分ける・起動はしない）。
_BROWSERISH = re.compile(r"browser|chrome|playwright|devtools|edge", re.I)
#: Desktop の launch に渡された「Web ページ」。URL（http・https）と、手元の .html のファイル（file:/// かパス）を分けて見る。
#: ★Browser が開けるのは http と https だけ（oc/browsercdp.to_url）。0.1.667 は手元の .html も「Browser で開け」と案内していた
#:   （パスを渡すと検索語として扱われ、file:/// は断られる＝2 手番の無駄）。手元のページは Serve で配ってから開く（2026-11-13）。
_WEBURL = re.compile(r"^\s*[\"']?https?://", re.I)
_LOCALPAGE = re.compile(r"^\s*[\"']?(?:file:///|(?:[A-Za-z]:)?[\\/])[^\"'\s]*\.html?(?:[?#][^\"'\s]*)?[\"']?\s*$", re.I)


def _web_redirect(arg: dict, ctx: Ctx) -> Result | None:
    """Desktop の launch で Web ページを開こうとしたら、1 回目だけ開かずに Browser を案内する（2026-11-13 オーナー
    「desktop 操作メインでやっている」）。記録では Edge に開いて scroll・type で読んでいた＝ページの文字も要素も Browser なら
    そのまま読める。★同じ物を 2 回頼まれたら開く（本当にデスクトップのブラウザの窓が要る場面を塞がない）。"""
    if str(arg.get("action") or "").strip().lower() != "launch":
        return None
    target = str(arg.get("command") or arg.get("path") or arg.get("target") or arg.get("url") or "").strip()
    if not target or target in ctx.web_redirected:
        return None
    if _LOCALPAGE.search(target) and not _WEBURL.search(target):
        ctx.web_redirected.add(target)
        return fail("Not launched: this is a local web page. To read or operate it, serve its folder with "
                    "Tools(name=\"Serve\", input={\"directory\": \"<its folder>\", \"path\": \"/<file>.html\", \"screenshot\": true}) "
                    "and open the http://localhost URL it gives with Tools(name=\"Browser\") — Browser opens http and https "
                    "only, and reads the page's text and elements directly. If you only need it shown in the user's desktop "
                    "browser window, send the same Desktop launch again.", head="Desktop")
    if not _WEBURL.search(target):
        return None
    ctx.web_redirected.add(target)
    return fail("Not launched: this is a web page. Open it with Tools(name=\"Browser\", input={\"action\": \"open\", "
                f"\"url\": {json.dumps(target)}}}) — Browser reads the page's text and elements directly, so you do not have "
                "to scroll and look at screenshots. For a site that needs the user's own logged-in Chrome, use Mcp's browser "
                "server. If you really need it in a desktop browser window, send the same Desktop launch again.",
                head="Desktop")

#: 一覧に載せる道具と一言。Task・Explore のように中核と重なる物は載せない。
LISTED = {
    # ★2026-11-13 オーナー「ローカル LLM が MCP の chrome とかをあまり使わないで desktop 操作メインでやっている」→ 記録で
    #   Web ページを Desktop の launch で Edge に開き、scroll・type で見ていた走行があった（10-30〜11-13）。Web ページの道は Browser
    #   か Mcp の browser と一言目で分かるようにした。
    "Browser": "open and operate web pages by URL (http/https): read the text and elements, click, type, fill forms, "
               "upload, screenshot. Use it for web pages instead of Desktop (a local .html: Serve its folder, then open "
               "the localhost URL). Several steps in one call: action=batch",
    "Desktop": "see and operate native Windows apps and dialogs: windows, clicks, keys, screenshots. Not for web pages "
               "(use Browser, or Mcp's browser server for the user's own logged-in Chrome). Several steps in one call: action=batch",
    "Listen": "listen to audio or video: transcribe, compare voices, measure",
    "Voice": "synthesize speech",
    "Lab": "work on another machine: 作業用のコンテナ (Kali container) and the lab hosts",
    "Mcp": "call tools of configured MCP servers",
    "Serve": "serve a web page or web game locally and check it in a browser",
    "App": "start and watch a desktop application",
    "Doc": "make Word, Excel, PowerPoint and PDF documents",
    "Check": "find and run this project's linter, type checker and tests",
    "Monitor": "watch a log or process and be told when something happens",
    "Api": "call a web API with a saved key",
    "UeMappings": "Unreal Engine usmap and mappings work",
    # ★2026-11-17 案 3・4（engine/tools/code.py・旧い道具の定義に無い＝ここで受ける）
    "Code": "code map without a language server: outline a file, find a name's definition or all its uses, check a file",
    "Editor": "show a file to the user in the app's editor at a line, or as a before/after diff of your changes",
    # ★2026-11-20 オーナー「ネットから探してきた画像や、ローカルにある画像を画面に表示できるように」（engine/tools/image.py）
    "Image": "show an image (local file or web image URL) to the user in the chat",
}
_lock = threading.Lock()
_specs: dict | None = None


def _load(ctx: Ctx) -> dict:
    """既存の道具の定義を読み込む（重いので呼ばれたときに 1 回だけ）。"""
    global _specs
    from ...tools import appcall
    with _lock:
        # 窓口は走行ごとに差し替える（古い走行の id で頼むと、アプリが受け取らず時間切れまで固まる）。
        appcall.set_caller(ctx.app_call if ctx.app_caps else None, caps=ctx.app_caps)
        from ...oc import tools as old
        old.set_cancel_probe(ctx.stopped)
        # ★待ち（Monitor(wait)・App の待ち）は普通の一言でも抜ける（Bash と BashOutput と同じ・2026-11-13: 30 秒待たせた後に
        #   道具ごと止めていた）。
        old.set_preempt_probe(ctx.has_message if ctx.has_message is not None and not ctx.child else (lambda: False))
        if _specs is None:
            _specs = old.specs()
            try:                       # 利用者の ~/.blackwall/plugins/*.py の道具も Tools の中から呼べる
                from ...plugin_loader import discover_plugins
                for p in discover_plugins():
                    _specs.setdefault(p.name, p)
                    LISTED.setdefault(p.name, (getattr(p, "description", "") or p.name).split("\n")[0][:100])
            except Exception:
                pass
    return _specs


def _native() -> dict:
    """旧い道具の定義を通さずに、この中核で持つ道具（名前 → (関数, 説明, 引数の形)）。"""
    from . import code, image
    return {"Code": (code.code, code.CODE_DESC, code.CODE_SCHEMA),
            "Editor": (code.editor, code.EDITOR_DESC, code.EDITOR_SCHEMA),
            "Image": (image.show, image.DESC, image.SCHEMA)}


def _save_image(data: Any, ctx: Ctx) -> str:
    """機能の道具が撮った写真（data["image"]＝base64）を会話のフォルダの shots/ に置く。画面とモデルはこのファイルを見る。"""
    img = data.get("image") if isinstance(data, dict) else None
    b64 = img.get("b64") if isinstance(img, dict) else None
    if not b64:
        return ""
    try:
        d = ctx.root / "shots"
        d.mkdir(parents=True, exist_ok=True)
        ext = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(str(img.get("mime")), ".png")
        p = d / f"{uuid.uuid4().hex[:12]}{ext}"
        p.write_bytes(base64.b64decode(b64))
        return str(p)
    except (OSError, ValueError):
        return ""


def _result(name: str, out: Any, ctx: Ctx, head: str = "", effect: bool = True) -> Result:
    text = str(out)
    ok = getattr(out, "ok", None)
    if ok is None:
        ok = not text.lstrip().startswith("Error:")
    shot = _save_image(getattr(out, "data", None), ctx)
    if shot and not ctx.vision:
        text += f"\n[This model cannot see images. The image is saved at {shot}]"
    body, cut = clip(ctx, text)
    return Result(body, ok=bool(ok), kind="ok" if ok else "error", head=head or name, truncated=cut, effect=effect,
                  shot=shot, images=[shot] if shot and ctx.vision else [])


def call_legacy(name: str, inp: dict, ctx: Ctx, head: str = "", effect: bool = True) -> Result:
    """既存の道具を 1 回呼ぶ。"""
    spec = _load(ctx).get(name)
    if spec is None:
        return fail(f"{name} is not available on this machine.", head=head or name)
    try:
        from ...oc.tools import adapt_keys
        inp, _ = adapt_keys(spec, dict(inp))
        return _result(name, spec.handler(dict(inp)), ctx, head, effect)
    except Exception as e:                 # 機能の道具の不具合で走行を落とさない
        return fail(f"{name} failed: {e}", head=head or name)


def _how_to(name: str, spec: Any, vision: bool) -> tuple[str, dict]:
    """使い方の説明。★画像を読めないモデルには写真の話をしない（届かない写真に 1 手と読み込みを払わせない・2026-11-03）。"""
    if not vision and name in ("Browser", "Desktop"):
        try:
            import importlib
            m = importlib.import_module("...oc." + ("browsertool" if name == "Browser" else "desktop"), __package__)
            return m.description(False), m.schema(False)
        except Exception:
            pass
    return spec.description, spec.schema


def tools(inp: dict, ctx: Ctx) -> Result:
    # 機能の道具の説明は旧い書き方（Tools(call="X", input=…)・Tools(names=["X"])）で書かれているので、どちらも受ける。
    name = str(inp.get("name") or inp.get("call") or "").strip()
    names = inp.get("names")
    if not name and isinstance(names, list) and names:
        name = str(names[0]).strip()
    specs = _load(ctx)
    avail = {k: v for k, v in LISTED.items() if k in specs or k in _native()}
    if not name:
        rows = "\n".join(f"- {k}: {v}" for k, v in avail.items())
        return Result(f"Tools you can call through this tool:\n{rows}\nTools(name=\"X\") shows how to call one.",
                      head="一覧", tail=f"{len(avail)} 個", effect=False)
    if name not in avail:
        return fail(f"No tool named {name!r}. Available: {', '.join(avail)}", head=name)
    nat = _native().get(name)
    if nat is not None:
        fn, desc, schema = nat
        if "input" not in inp or inp.get("input") in (None, ""):
            return Result(f"{name}: {desc}\n\nParameters (JSON schema): {json.dumps(schema, ensure_ascii=False)}\n\n"
                          f"Call it with Tools(name=\"{name}\", input={{...}}).", head=name, tail="使い方", effect=False)
        arg = inp.get("input")
        if isinstance(arg, str):
            try:
                arg = json.loads(arg)
            except ValueError:
                return fail("input must be a JSON object", head=name)
        if not isinstance(arg, dict):
            return fail("input must be a JSON object", head=name)
        try:
            return fn(arg, ctx)
        except Exception as e:             # 道具の不具合で走行を落とさない
            return fail(f"{name} failed: {e}", head=name)
    spec = specs[name]
    if "input" not in inp or inp.get("input") in (None, ""):
        desc, schema = _how_to(name, spec, ctx.vision)
        if name in BATCHABLE:
            desc += BATCH_NOTE
        return Result(f"{name}: {desc}\n\nParameters (JSON schema): "
                      f"{json.dumps(schema, ensure_ascii=False)}\n\nCall it with Tools(name=\"{name}\", input={{...}}).",
                      head=name, tail="使い方", effect=False)
    arg = inp.get("input")
    if isinstance(arg, str):
        try:
            arg = json.loads(arg)
        except ValueError:
            return fail("input must be a JSON object", head=name)
    if not isinstance(arg, dict):
        return fail("input must be a JSON object", head=name)
    if name == "Monitor":
        # ★背景の仕事（Bash run_in_background の bg1 など）の出力はファイルに無いので Monitor では見張れない（2026-11-13）。
        jid = str(arg.get("pid") or arg.get("id") or "").strip()
        if jid and not arg.get("command") and not arg.get("file") and ctx.jobs is not None and (
                jid in ctx.jobs.items or any(str(j.proc.pid) == jid for j in ctx.jobs.items.values())):
            return fail(f"{jid} is a background job of Bash; its output is not in a file. Wait for it with "
                        f"BashOutput(id=\"{jid}\", wait=<seconds>, until=\"<regex>\") instead. Monitor follows a log file "
                        "(file=<path>) or a command it starts itself (command=...).", head="Monitor")
    if name in BATCHABLE and str(arg.get("action") or "").strip().lower() == "batch":
        return _batch(name, arg, ctx)
    if name == "Desktop":
        r = _web_redirect(arg, ctx)
        if r is not None:
            return r
    return call_legacy(name, arg, ctx, head=name)


#: 1 回の呼び出しで続けて行える道具（2026-11-17 オーナー「ツールとハーネス強化」の案 2）。
#: 記録（11-14〜17・Gyro-M 36 走行）で Desktop は 244 回・1 走行で最大 45 回＝1 操作ごとにモデルの手番（生成と読み込み）を払っていた。
BATCHABLE = ("Desktop", "Browser")
BATCH_MAX = 20
BATCH_NOTE = ("\n\nSeveral steps in one call: input={\"action\": \"batch\", \"steps\": [{...}, {...}]} runs each step "
              "(each is a normal input object, e.g. {\"action\": \"click\", ...}) in order and stops at the first step that "
              f"fails. Up to {BATCH_MAX} steps. Only the last step's screenshot is returned, so end with a look/screenshot "
              "step when you need to see the result. Use it for a known sequence (click, type, key, wait); look first when "
              "you do not know the screen yet.")


def _batch(name: str, arg: dict, ctx: Ctx) -> Result:
    """Desktop・Browser の手順を続けて行う。最初に失敗した所で止め、そこまでの結果を 1 行ずつ返す。
    写真は最後の手順の物だけ（途中の写真をモデルに全部見せると読み込みが増える）。"""
    steps = arg.get("steps")
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except ValueError:
            steps = None
    if not isinstance(steps, list) or not steps:
        return fail(f"{name} batch needs steps: a list of input objects, e.g. "
                    "[{\"action\": \"click\", ...}, {\"action\": \"type\", ...}].", head=name)
    if len(steps) > BATCH_MAX:
        return fail(f"{name} batch takes up to {BATCH_MAX} steps (got {len(steps)}). Split it.", head=name)
    rows: list[str] = []
    last: Result | None = None
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict) or str(st.get("action") or "").strip().lower() in ("", "batch"):
            rows.append(f"[{i}] skipped: each step must be an input object with an action (not batch).")
            last = fail("bad step", head=name)
            break
        if ctx.stopped():
            rows.append(f"[{i}] not run: stopped.")
            break
        if name == "Desktop":
            r = _web_redirect(st, ctx)
        else:
            r = None
        res = r if r is not None else call_legacy(name, st, ctx, head=name)
        act = str(st.get("action"))
        text = (res.output or "").strip()
        if i < len(steps) or not res.ok:
            text = text[:600] + ("…" if len(text) > 600 else "")
        rows.append(f"[{i}] {act}: " + ("ok" if res.ok else "FAILED") + (f"\n{text}" if text else ""))
        last = res
        if not res.ok:
            if i < len(steps):
                rows.append(f"Stopped at step {i} of {len(steps)}; steps {i + 1}-{len(steps)} were not run.")
            break
    ok = bool(last is not None and last.ok and len(rows) >= len(steps))
    body, cut = clip(ctx, "\n".join(rows))
    shot = last.shot if last is not None else ""
    ctx_images = last.images if last is not None else []
    return Result(body, ok=ok, kind="ok" if ok else "error", head=name, tail=f"{len(steps)} 手順", truncated=cut,
                  effect=True, shot=shot, images=ctx_images)


def _mcp_hint() -> str:
    """Mcp の先にあるサーバーの名前を一覧の説明に足す。★名前だけでは、モデルは同梱の REA を知らず
    「REA ってゲームの道具？」と推測していた（2026-11-03 オーナーの実機の思考）。設定ファイルと同梱の有無だけで
    決まる（起動はしない）＝この process の間は変わらないので、推論機の読み込みの控えを崩さない。"""
    try:
        from ...oc import mcp
        if not mcp.enabled():
            return ""
        cfgs = {k: v for k, v in mcp.load_config().items() if not k.startswith("!")}
    except Exception:
        return ""
    if not cfgs:
        return ""
    parts = []
    for name, cfg in sorted(cfgs.items()):
        if name == "rea":
            parts.append(_rea_hint(cfg))
        elif _BROWSERISH.search(name):
            # ★名前だけでは、これが利用者のログイン済みの Chrome だとモデルに伝わらない（2026-11-13・Web ページを Desktop で見ていた）。
            #   起動して道具の一覧を見れば分かる（oc/mcp.py の _is_browser）が、一覧を引く前の判断には間に合わない。
            parts.append(f"{name} (the user's own Chrome/Edge tabs with their logins — use it for sites that need "
                         "the user's login; not Desktop)")
        else:
            parts.append(name)
    return "\nMcp reaches these MCP servers: " + ", ".join(parts) + "."


def _rea_hint(cfg: dict) -> str:
    """REA を「いつ使うか」と「最初に何を呼ぶか」（2026-11-04 オーナー「REA とかをモデルが自律的に使うか判断できるように」）。
    ★旧は扱える形式を並べただけで、11-03 の試験では序盤の数手を使い方探しに使った。Ghidra の無い機械では逆コンパイルの道具が
    provider_unavailable になるので、その機械で動く物だけを書く（mcp.rea_info）。"""
    from ...oc import mcp
    info = mcp.rea_info() or {"built_in": cfg.get("_source") == "built-in", "native": True}
    if info["native"]:
        return ("rea (reverse-engineering server REA. Use it when you need to know what a program does inside — an executable, "
                ".NET assembly, Electron/JS app, APK or Unity IL2CPP game — instead of guessing with scripts. Start with "
                "tool open_binary(path=<absolute path>), then binary_overview, search_procedures, and procedure_pseudo_code"
                "(procedure=<name or address>) to decompile; SQLite: inspect_sqlite_database(path))")
    return ("rea (reverse-engineering server REA, static readers only on this machine: inspect_sqlite_database, "
            "inspect_il2cpp_metadata, inspect_managed_artifact for .NET, inspect_apk_signature — each takes path=<absolute path>. "
            "Decompiling native code needs Ghidra, which is not set up here)")


TOOLS = Tool(
    "Tools",
    # ★2026-11-15 名前だけでなく何のための物かを並べる（どの場面で使うかが分からず、Bash や Desktop で済ませていた）。
    "More tools, on demand. Tools(name=\"X\") shows X's parameters; Tools(name=\"X\", input={...}) calls it; Tools() lists "
    "what this machine has.\n" + "\n".join(f"- {k}: {v}" for k, v in LISTED.items()) + _mcp_hint(),
    {"name": {"type": "string", "description": "Tool name"},
     "input": {"type": "object", "description": "The tool's arguments"}},
    tools, effect=True)
