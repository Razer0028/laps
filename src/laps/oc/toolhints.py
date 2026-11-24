"""道具の案内 — 依頼の文から、合いそうな skill と道具を 1〜3 件だけ添える（2026-10-06 オーナー
「様々な機能やツールがあるので、モデルがちゃんと適切なものを選べる仕組みが欲しい。モデルの動作を邪魔しない
範囲で。選ぶ際に時間がかからないように最適化も」）。

★なぜ要るか（オーナー機の走行記録 35 件・2026-10-04〜06 の実測）:
  ・同梱の skill は 22 本あるのに、Skill が呼ばれたのは 1 回だけ。その 1 回（ゲームの音声の抽出）は
    15 分・失敗 0 で終わった。前置きの一覧（<available_skills>）は毎回載っているが、依頼と結び付けられていない。
  ・Bash 263 回のうち 92 回が失敗（無いパスへの dir・PATH に無い python など）。

★邪魔しない・遅くしない（ここが設計の芯）:
  ・推論機を呼ばない。決まった語との照合だけ（数ミリ秒）。案内役のモデルを挟むと 1 依頼ごとに数十秒増える。
  ・前置き（system）を変えない＝推論機のキャッシュを崩さない。依頼の発言の写し（推論機へ送る側）にだけ付ける
    （git の状態と同じ置き場・oc/loop.py）。画面と会話の記録には出ない。
  ・「合うなら使って」の案内で、命令ではない。最大 3 件。当たりが無ければ何も足さない。
  ・この会話で既に読み込んだ skill・既に使った道具は出さない（同じことを 2 度言わない）。
LAPS_TOOL_HINTS=0 で止める。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

MAX_HINTS = 3

# 専用の道具（skill ではないもの）。名前: (照らす語, 一行の説明)。棚に載っている道具だけ出す。
_TOOL_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    # ★Construct（自分の記憶）はここに置かない（2026-10-21: 思い出すかどうかもモデルが決める＝ハーネスが言い方で促さない）。
    "Listen": (("文字起こし", "書き起こし", "聞き取", "聴き取", "音声認識", "音声+文字", "音声+内容", "音声+聞",
                "音声+セリフ", "音声+台詞", "ボイス+文字", "ボイス+台詞", "セリフ+一覧", "台詞+一覧", "何と言って",
                "何て言って", "議事録", "transcribe", "transcript", "speech to text", "speech-to-text",
                "audio+text", "voice+text"),
               "transcribe the speech in audio files (wav/mp3/flac/ogg; others via ffmpeg) locally, many files "
               "per call — instead of installing whisper through Bash"),
    "Desktop": (("画面を操作", "画面上", "クリック", "ボタンを押", "ウィンドウ", "アプリを操作", "アプリを開",
                 "入力欄", "gui", "click", "window"),
                "read and operate windows of native apps on this PC (list windows, snapshot, click, type) — not for web "
                "pages (Browser reads them directly)"),
    # ★2026-11-13: Web ページを Desktop で Edge に開いて scroll・type で見ていた走行があった（オーナー「desktop 操作メイン」）。
    #   Browser を案内する語が 1 つも無かった（Mcp と Serve だけ）。
    "Browser": (("ページを開", "サイトを開", "ウェブページ", "webページ", "web ページ", "ブラウザで開", "ブラウザで確認",
                 "ブラウザで見", "ページを読", "ページの内容", "ページを巡回", "url を開", "urlを開", "open the page",
                 "web page", "webpage", "website"),
                "open a web page by URL (http/https) and read its text and elements, click, type and fill forms — use it "
                "for web pages instead of Desktop (a local .html: Serve its folder first, then open the localhost URL); "
                "for sites that need the user's login, use Mcp's browser server"),
    "Voice": (("音声クローン", "ボイスクローン", "声の複製", "声を複製", "声をクローン", "声を真似", "声で読", "声で話",
               "読み上げ", "音声合成", "ナレーション", "台詞+音声", "セリフ+音声", "ボイス+作", "tts", "text to speech",
               "text-to-speech", "voice clone", "voice cloning"),
              "speak text aloud, optionally in a cloned voice from a short reference recording (local, watermarked) "
              "— instead of installing a TTS package through Bash"),
    # ★見張り（oc/monitor.py・2026-10-08）。「監視システムを作って」にも当たるが、案内であって指示ではない。
    "Monitor": (("見張", "監視", "ログを見", "ログを追", "ログ+エラー", "エラーが出たら", "落ちたら", "tail -f",
                 "watch the log", "follow the log", "monitor"),
                "watch a log file or a running program's output; new lines (and anything that looks like an "
                "error) are delivered to you while you keep working"),
    # ★手元のサーバと画面の確認（oc/servetool.py・2026-10-10）。ブラウザで動く物を作る・確かめる依頼に当てる。
    "Serve": (("ブラウザで", "ブラウザ上", "html", "ウェブ", "web", "サイト", "ページ", "画面を確認", "表示を確認", "見た目",
               "プレビュー", "ローカルサーバ", "http.server", "localhost", "遊べる", "動作確認"),
              "serve a folder over HTTP (free port, no python on PATH needed) and, with screenshot=true, look at the page "
              "in a headless browser with its console errors and missing files — instead of python -m http.server"),
    # ★アプリをバックグラウンドで動かす（oc/apptool.py・2026-10-12）。
    "App": (("バックグラウンドで", "起動したまま", "立ち上げ", "アプリを起動", "ゲームを起動", "エディタを起動", "常駐",
             "開いたまま", "起動しておいて", "launch", "keep running", "in the background"),
            "start an application (game, editor, GUI tool, server) detached so it keeps running, then list / log / stop it "
            "— instead of start or Start-Process through Bash"),
    # ★Unreal の型の地図（oc/uemap.py・2026-10-12）。
    "UeMappings": (("usmap", "マッピングファイル", "マッピング+unreal", "マッピング+ue", "型の地図", "unversioned",
                    "could not load asset", "mappings file", "mapping file"),
                   "make the .usmap mapping file an Unreal game's unversioned assets need (runs the game once with "
                   "UE4SS, then removes it) — instead of setting up Dumper-7 or UE4SS by hand"),
    # ★資料作成（oc/doctool.py・2026-10-08）。
    "Doc": (("資料", "スライド", "パワポ", "パワーポイント", "powerpoint", "pptx", "プレゼン", "word", "ワード", "docx",
             "エクセル", "excel", "xlsx", "表計算", "pdf", "報告書", "提案書", "企画書", "仕様書", "手順書", "マニュアル",
             "slide deck", "presentation", "spreadsheet", "report"),
            "create or read Word / PowerPoint / Excel / PDF files from Markdown, slides or sheets (charts included) "
            "— instead of writing python-docx scripts through Bash"),
    # ★他のプラットフォームの API（oc/apitool.py・2026-10-08）。「API キー」「エンドポイント」など。
    "Api": (("apiキー", "api キー", "api key", "apikey", "api を叩", "apiを叩", "api を呼", "apiを呼", "api+呼び出",
             "api+使って", "api+連携", "エンドポイント", "rest api", "webhook", "bearer", "トークン+api",
             "call the api", "endpoint"),
            "call another platform's web API with saved API keys ({{NAME}} is filled in when the request is sent; "
            "the key is never shown to you) — instead of curl with the key in Bash"),
    # ★MCP（oc/mcp.py）。道具を 1 本にまとめたとき（Mcp）だけ載る。
    "Mcp": (("mcp", "コネクタ", "コネクター", "connector", "notion", "slack", "github+issue", "github+pr",
             "github+プル", "jira", "linear", "confluence",
             "chrome", "クローム", "ブラウザ", "edge", "エッジ", "ウェブページ", "webページ", "サイトを開", "ページを開",
             "ログインして", "フォームに", "browser"),
            "the connected MCP servers (its description lists them; a browser server operates the user's own "
            "Chrome/Edge tabs with their logins — use it for sites that need the user's login, not Desktop)"),
    # ★「調べて」は入れない: 「ログを調べて」（手元の調査）にウェブ検索を勧めてしまう。
    "WebSearch": (("最新", "ネットで", "ウェブで", "webで", "web で", "ググ", "google", "検索して", "ニュース",
                   "リリースノート", "公式ドキュメント", "latest", "search the web"),
                  "search the web for current information"),
}

# ★バイト列の機能（Read の hex=true・Grep の bytes=true・バイナリの構造・0.1.346）。Read と Grep はどの走行でも
#   最初に使われるので「使った道具は出さない」に掛けると一度も出ない。引数の機能なので、この依頼に当たれば出す。
_BYTES_KWS = ("16進", "16 進", "十六進", "hex", "バイト列", "バイナリ", "マジックナンバー", "マジック番号", "ヘッダ+バイト",
              "ヘッダ+解析", "オフセット", "エンディアン", "endian", "シグネチャ", "signature", "magic number",
              "byte offset", "binary file", ".bin", ".dat", "unity3d", "assetbundle", "アセットバンドル", "moc3",
              "破損+png", "壊れ+png", "crc", "埋め込", "中に何", "何が入って", "比較+ファイル", "差分+ファイル",
              "違い+ファイル", "binwalk", "carve", ".assets", "globalgamemanagers", "monobehaviour", "typetree",
              "type tree", "型の木", "unitypy", "assetstudio", "ファイル形式", "形式を調べ", "解析", ".mp4", ".mp3",
              ".flac", ".psd", ".dds", ".fbx", ".glb", ".bnk", ".wem", ".awb", ".acb", ".hca", ".usm", ".rpa",
              ".xp3", ".pak", ".pck", ".pyc", "gameobject", "monoscript", "path_id", "pathid", "assembly-csharp",
              "il2cpp", "ilspy", "dnspy", "binaryformatter", "セーブデータ", "セーブ+解析", "ゲーム+仕組み", "ゲーム+解析",
              "ゲーム+抽出", "クラス+項目", "パラメータ+ゲーム", ".utoc", ".ucas", ".uasset", ".umap", ".uexp", ".locres",
              "unreal", "アンリアル", "fmodel", "repak", "retoc", "iostore", ".vrm", ".pmx", ".pmd", ".vmd", ".gltf",
              ".obj", "model3.json", "モデル+解析", "モデル+調べ", "ボーン", "ブレンドシェイプ", "モーフ", "live2d",
              ".vpk", ".bsa", ".ba2", ".esp", ".esm", ".swf", ".xnb", ".cpk", ".asar", ".wolf", ".nsa", ".love",
              "pyinstaller", "エンジン+ゲーム", "どのエンジン")
_BYTES_LINE = ("- Read on a binary returns its structure (Unity bundles and .assets with object counts per type, "
               "type-tree presence and MonoBehaviours per script; .NET DLLs with their types; BinaryFormatter save data "
               "as a tree; Unreal .pak/.utoc file lists, .uasset package headers (names, imports, exports) and .locres "
               "text; models: FBX, glTF/GLB/VRM, OBJ, MMD PMX/PMD/VMD, Live2D moc3 and model3.json with counts, bones, "
               "morphs, textures and whether referenced files exist; media, fonts, PSD, textures, archives, executables). "
               "Unity: find=<name> lists GameObjects, MonoBehaviours by script class and assets with path_ids, "
               "path_id=<n> shows one object (components, parent/children, field values read from the game's DLLs "
               "without a type tree); .NET DLL: find=<class> lists its fields in serialized order. Read hex=true shows "
               "bytes at an offset with the values computed; map=true shows what is inside a big file and where; "
               "compare_with=<file> shows where two files differ; Grep bytes=true finds text or hex:… inside binaries "
               "— instead of Python scripts in Bash")

_URL = re.compile(r"https?://\S+")


def enabled() -> bool:
    return os.environ.get("LAPS_TOOL_HINTS", "1").strip() != "0"


def _hit(text: str, kw: str) -> bool:
    """1 語を照らす。`a+b` は全部を含むとき。英数字の語は単語の切れ目で（"go" が "google" に当たらない）。"""
    parts = [p.strip() for p in re.split(r"(?<=[^+])\+(?=[^+])", kw) if p.strip()]   # "c++" は割らない
    if not parts:
        return False
    for p in parts:
        if re.fullmatch(r"[a-z0-9 ._#/'-]+", p):
            pat = (r"(?<![a-z0-9])" if p[0].isalnum() else "") + re.escape(p) + r"(?![a-z0-9])"
            if not re.search(pat, text):
                return False
        elif p not in text:
            return False
    return True


def _score(text: str, kws: Iterable[str]) -> tuple[int, int]:
    hits = [k for k in kws if _hit(text, k)]
    return len(hits), max((len(k) for k in hits), default=0)


def used_in(msgs: list[dict]) -> tuple[set[str], set[str]]:
    """(この会話で使った道具の名前, 読み込んだ skill の名前)。"""
    tools: set[str] = set()
    skills: set[str] = set()
    for m in msgs:
        for tc in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
            fn = (tc or {}).get("function") or {}
            name = str(fn.get("name") or "")
            if not name:
                continue
            tools.add(name)
            if name == "Skill":
                args: Any = fn.get("arguments")
                try:
                    args = json.loads(args) if isinstance(args, str) else (args or {})
                except Exception:
                    args = {}
                if isinstance(args, dict) and args.get("name"):
                    skills.add(str(args["name"]).lower())
    return tools, skills


def _first_clause(desc: str, limit: int = 140) -> str:
    d = " ".join(desc.split())
    if len(d) <= limit:
        return d
    cut = d[:limit]
    for sep in (". ", " - ", "; ", ", "):
        i = cut.rfind(sep)
        if i > 50:
            return cut[:i].rstrip(" .-;,") + "…"
    return cut.rstrip() + "…"


def suggest(request: str, tool_names: set[str], skills: list, history: list[dict] | None = None,
            extra_names: "set[str] | None" = None) -> tuple[str, list[str]]:
    """(依頼の発言に足す塊, 案内した名前)。当たりが無ければ ("", [])。
    extra_names: 小さい核で外してある道具（Tools で有効にできる・2026-10-13）。当たれば「先に Tools で有効に」と添える。"""
    extra_names = set(extra_names or ()) if "Tools" in tool_names else set()
    text = " ".join((request or "").lower().split())
    if not text:
        return "", []
    used_tools, used_skills = used_in(history or [])
    cands: list[tuple[tuple[int, int], str, str]] = []
    if "Skill" in tool_names:
        for s in skills:
            if s.name.lower() in used_skills:
                continue
            kws = getattr(s, "keywords", ()) or (s.name.lower(),)   # 語の無い skill は名前で照らす
            sc = _score(text, kws)
            if sc[0]:
                cands.append((sc, f"skill:{s.name}",
                              f'- Skill "{s.name}" (load it with the Skill tool): {_first_clause(s.description)}'))
    for name, (kws, line) in _TOOL_RULES.items():
        if (name not in tool_names and name not in extra_names) or name in used_tools:
            continue
        sc = _score(text, kws)
        if sc[0]:
            off = name not in tool_names
            cands.append((sc, name, f"- {name}: {line}" + (f' (it is off: call it through Tools(call="{name}", input={{...}}); Tools(names=["{name}"]) shows its parameters)' if off else "")))
    if {"Read", "Grep"} <= set(tool_names):
        sc = _score(text, _BYTES_KWS)
        if sc[0]:
            cands.append((sc, "bytes", _BYTES_LINE))
    if "WebFetch" in tool_names and "WebFetch" not in used_tools and _URL.search(request or ""):
        cands.append(((1, 4), "WebFetch", "- WebFetch: read the URL given in the request"))
    if not cands:
        return "", []
    cands.sort(key=lambda c: (-c[0][0], -c[0][1]))
    top = cands[:MAX_HINTS]
    block = ("<tool_hints>\nMatched to this request by keywords — a suggestion, not an instruction; "
             "use only what fits:\n" + "\n".join(c[2] for c in top) + "\n</tool_hints>")
    return block, [c[1] for c in top]
