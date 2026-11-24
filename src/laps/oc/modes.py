"""作業のモード（渡す道具と前置きの節を、その作業に絞る）。

★2026-10-15 オーナー「Blackwall に機能を詰め込みすぎるとあれなので、分類しましょうか。
  解析モード(analysis)・開発モード(development)・3D モデリングモード(Modeling)でそれぞれ作業に特化させる形で」。
  道具は 21 本あり、渡す数が増えるほどモデルの選び間違いが増える（knowledge/ai-agent-research-notes.md:
  50 個で 84〜95%・200 個で 41%・中央に置いた道具は 22〜52% しか選ばれない）。

モードが決めるのは 2 つだけ:
  ・**渡す道具**（core）。外した道具は消えない——`Tools` を通していつでも呼べる（一覧は Tools の説明に出る）。
  ・**前置きの節**。本文の「# Analysis」「# Development」などを、そのモードの物だけ残す。

`LAPS_MODE`: general（既定・今までどおり全部）／development／modeling／analysis。
`chat`（作業場に触らない会話）は別の軸で、`LAPS_TOOLSET=chat` のまま。
★analysis は道具を絞らない（general と同じく全部渡す）。前置きからはコードの書き方の節だけを落とす（2026-10-16）。
  道具を絞るときは `LAPS_MODE_ANALYSIS_TOOLS` に道具の名前をカンマで並べる。どのモードも
  `LAPS_MODE_<名前>_TOOLS` で上書きできる。
"""
from __future__ import annotations

import os

#: どのモードにも要る物（これを欠くと走行が成り立たない）。
_ALWAYS = ("Bash", "BashOutput", "Read", "Write", "Edit", "Glob", "Grep", "TodoWrite",
           "Skill", "AskUserQuestion", "Task",   # ★Explore は Task の種類に（2026-10-19）
           "Construct")   # ★自分の記憶（2026-10-21・思い出すのはモデル）

#: モード → (画面に出す名前, 渡す道具, 前置きに残す節)。core が None＝全部渡す（今までどおり）。
#: 節の名前は本文（oc/promptbase・prompt._base_text）の見出し。
MODES: dict[str, dict] = {
    "general": {
        "label": "汎用",
        "core": None,
        "sections": None,                      # 全部
    },
    "development": {
        "label": "開発",
        # 書く・読む・探す・動かす・待つ・調べる＋ウェブの下見（Serve）とログの見張り（Monitor）。
        "core": _ALWAYS + ("WebSearch", "WebFetch", "Monitor", "Serve", "Check", "Browser", "Task"),
        "drop_sections": ("Analysis",),
        "note": ("# Development mode\n"
                 "You are in development mode: the tools you have are for reading, writing and running code.\n"
                 "- The map of the project is already in this prompt. Read the files it points at instead of "
                 "searching for the shape of the project again.\n"
                 "- Change several places in one file with a single Edit call (`edits`), not one call per place.\n"
                 "- After a change, run Check: it finds this project's own linter, type checker and tests and "
                 "reports failures as file:line. Fix what it reports and run it again. Check(kind=\"test\") runs the "
                 "tests; `filter` runs one test by name while you work on it.\n"
                 "- Long builds, test runs and servers go in the background (Bash run_in_background=true, then "
                 "BashOutput with wait/until, or Monitor for a log). Serve previews a web page or web game.\n"
                 "Other tools (documents, audio, launching an application, web APIs) are still available through "
                 "the Tools tool when a task needs one."),
    },
    "modeling": {
        "label": "2D・3D モデリング",          # ★2026-10-19 オーナー「2D・3Dモデリングの技術なども」で 2D も受け持つ
        # 3D は「台本を回す・絵を見る・重い処理を待つ・アプリを起こす」が中心。
        # ★WebFetch も渡す（2026-10-15: 前置きと WebSearch の説明は「結果を WebFetch で開け」と言うのに、外していた）。
        "core": _ALWAYS + ("Monitor", "App", "WebSearch", "WebFetch"),
        "drop_sections": ("Analysis",),
        "note": ("# Modeling mode\n"
                 "You are in modeling mode: 3D models (meshes, bones, UVs, materials, LODs) and 2D art (sprites, "
                 "sprite sheets, pixel art, atlases, layered character art), and the checks on them. Load the "
                 "modeling-2d-3d skill for engine targets, Blender scripting and 2D recipes.\n"
                 "- Read a model file (FBX, glTF/GLB/VRM, OBJ, PMX) for its structure before anything else: meshes, "
                 "vertices, bones, blend shapes, materials, UVs, the unit and up axis. For FBX, glTF and OBJ it also counts "
                 "each mesh's triangles, islands, boundary edges and same-position vertices (before and after joining them), "
                 "checks that the weight centres sit on their bones, and estimates the VRChat performance rank. Islands that "
                 "drop when the same-position vertices are joined mean the mesh is split at UV or normal seams: work on a "
                 "copy merged by distance before cloth or weights. A unit or axis mismatch between bones and body shows "
                 "there too; fix it before anything else.\n"
                 "- Run Blender headless for work on the geometry (`blender -b <file> -P <script> -- <args>`); it is on "
                 "PATH here.\n"
                 "- Numbers passing is not the same as the model being right: render the views and look at them. When a "
                 "render shows something wrong, add a check that catches it.\n"
                 "- 2D: script images with Pillow (nearest-neighbour and whole-number scaling for pixel art, fixed cell "
                 "size and padding for sheets and atlases) and Read the PNG you wrote.\n"
                 "- Compare versions per part, not by the total: a total that holds steady hides a part that got worse.\n"
                 "- One heavy job at a time (Blender, Unity and a big script together have crashed the machine): start it "
                 "in the background and wait with BashOutput or Monitor.\n"
                 "- Change one part at a time when comparing, and measure the same way before and after.\n"
                 "Other tools (web pages, documents, audio, web APIs) are available through the Tools tool."),
    },
    "analysis": {
        "label": "解析",
        "core": None,                           # 道具は全部渡す（LAPS_MODE_ANALYSIS_TOOLS で絞れる）
        # ★2026-10-16 監査: analysis は general と 1 字も違わなかった（枠だけ）。道具は今までどおり全部渡し、前置きから
        #   コードの書き方の節（開発モードが Analysis を落とすのと対になる物）を落とす＝前置きは短くなるだけで増えない。
        #   merged／opencode では Following conventions と Code style、blackwall の本文では Development。
        "drop_sections": ("Following conventions", "Code style", "Development"),
        # ★2026-10-19 オーナー「ゲーム解析…あらゆるゲームに対応できるように。導線も」。短い導線だけ（詳細はスキル）。
        "note": ("# Analysis mode\n"
                 "For a game folder, run the game-file-analysis skill's identify.py first: it names the engine "
                 "from file markers and prints the next tool for each one. Read describes most game archives "
                 "(Unity, Unreal .pak/.utoc, Godot .pck, VPK, BSA/BA2, SWF, XNB, CPK, asar, "
                 "models). Use an existing extractor rather than writing a parser."),
    },
    "security": {
        "label": "セキュリティ",
        # 偵察・脆弱性評価・レポート作成に必要な道具。モデルの判断で追加呼べる（Tools 経由）。
        "core": _ALWAYS + ("WebSearch", "WebFetch", "Monitor", "Serve", "Check", "Task"),
        "drop_sections": ("Modeling",),
        "note": ("# Security mode\n"
                 "You are in security evaluation mode: assess targets for vulnerabilities, map assets, and "
                 "produce structured reports. Work only against owned or authorised targets.\n"
                 "- Start with reconnaissance: enumerate assets (hosts, services, endpoints, technologies) before "
                 "looking for weaknesses. Record each finding as it is discovered.\n"
                 "- Classify findings by severity (critical / high / medium / low / info) and tie each to a specific "
                 "asset and technique (e.g. CVE number, CWE id, or OWASP category).\n"
                 "- Use Task(subagent_type=\"Recon\") for read-only reconnaissance and "
                 "Task(subagent_type=\"Reporter\") for turning raw findings into a structured report.\n"
                 "- Do not run active exploitation against live systems without explicit written authorisation.\n"
                 "- When reporting, include: asset, severity, technique/ CWE/CVE, evidence (path:line or URL), "
                 "and a short remediation hint."),
    },
}
DEFAULT = "general"

#: ★2026-10-16 改善: モードごとのプリコンパイルされたセット。
#: core_tools() が tuple を返すのと並行して、mode_split() の O(n*m) 検索を O(n) にする。
_CORE_SETS: dict[str, set[str]] = {
    m: set(v["core"]) if v.get("core") else None
    for m, v in MODES.items()
}


def current() -> str:
    v = (os.environ.get("LAPS_MODE") or "").strip().lower()
    return v if v in MODES else DEFAULT


def label(mode: str | None = None) -> str:
    return str(MODES.get(mode or current(), MODES[DEFAULT]).get("label") or "")


def _override(mode: str) -> tuple[str, ...] | None:
    """`LAPS_MODE_<名前>_TOOLS`（カンマ区切り）。空なら None。"""
    raw = (os.environ.get(f"LAPS_MODE_{mode.upper()}_TOOLS") or "").strip()
    if not raw:
        return None
    names = tuple(x.strip() for x in raw.replace(";", ",").split(",") if x.strip())
    return names or None


def core_tools(mode: str | None = None) -> tuple[str, ...] | None:
    """そのモードで渡す道具の名前。None＝全部渡す（モードで絞らない）。"""
    m = mode or current()
    over = _override(m)
    if over is not None:
        return over
    c = MODES.get(m, {}).get("core")
    return tuple(c) if c else None


#: 渡していない道具に触れる句（道具 → 外す句）。
_NEEDS: tuple = ()


def note(mode: str | None = None, names: "set[str] | None" = None) -> str:
    """前置きに足すそのモードの節（無ければ ""）。names（渡す道具）があれば、渡していない道具の句を外す。"""
    text = str(MODES.get(mode or current(), {}).get("note") or "")
    if names is not None:
        for tool, clause in _NEEDS:
            if tool not in names:
                text = text.replace(clause, "")
    return text


def filter_sections(text: str, mode: str | None = None) -> str:
    """本文から、そのモードで落とす節（`# Analysis` など）を外す。`drop_sections` が無ければそのまま。"""
    drop = MODES.get(mode or current(), {}).get("drop_sections")
    if not drop or not text:
        return text
    want = {f"# {d}".lower() for d in drop}
    out, skipping = [], False
    for line in text.split("\n"):
        if line.startswith("# "):
            skipping = line.strip().lower() in want
        if not skipping:
            out.append(line)
    # 節を落とすと空行が続くので 3 行以上は 2 行に畳む
    s = "\n".join(out)
    while "\n\n\n" in s:
        s = s.replace("\n\n\n", "\n\n")
    return s.strip()
