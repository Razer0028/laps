"""NotebookEdit: edit Jupyter .ipynb cells.

Operations (mode):
  - replace: replace a cell's source by index
  - insert:  insert a new code/markdown cell at index
  - delete:  remove a cell at index
"""
from __future__ import annotations
import json
import uuid
from pathlib import Path

from . import ToolSpec
from .atomic import write_text_atomic
from ..policy import authorize_path, PolicyDenied


def _empty_code_cell(src: str) -> dict:
    return {
        "cell_type": "code",
        "id": uuid.uuid4().hex[:8],
        "metadata": {},
        "execution_count": None,
        "outputs": [],
        "source": src.splitlines(keepends=True),
    }


def _empty_md_cell(src: str) -> dict:
    return {
        "cell_type": "markdown",
        "id": uuid.uuid4().hex[:8],
        "metadata": {},
        "source": src.splitlines(keepends=True),
    }


def _run(inp: dict) -> str:
    path = inp.get("notebook_path") or inp.get("file_path")
    mode = (inp.get("mode") or "replace").lower()
    idx = inp.get("cell_index")
    src = inp.get("new_source") or inp.get("source") or ""
    cell_type = (inp.get("cell_type") or "code").lower()

    if not path:
        return "Error: missing 'notebook_path'"
    # ★`int(idx)` は3か所で素だった。モデルが "2" ではなく "二" や "cell 2" を
    #   渡すと ValueError で**道具ごと**落ち、利用者には
    #   "Error in NotebookEdit: invalid literal for int()" しか届かない。
    if idx is not None:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return f"Error: cell_index は整数で指定してください（受け取った値: {idx!r}）"
    p = Path(path)
    # ★実行ゲートを通す（2026-08-21・敵対レビューで判明）。
    #   この道具は policy を **import すらしていなかった**。
    #   `full`（アプリの既定）に入って出荷されているので、
    #   **全モードで、JSON として解ける自己保護ファイル（settings.json 等）を
    #   書き換えられ、監査に1行も残らない**状態だった。
    #   Write / Edit は同じ検査を通っている＝**同じ守りの片側だけが欠けていた**。
    #   読む前に見るのは、存在の有無すら機微な場所があるため（Write と同じ作法）。
    try:
        authorize_path(str(p), write=True, kind="notebook_edit")
    except PolicyDenied as e:
        return f"Error: policy denied: {e}"
    if not p.exists():
        return f"Error: notebook not found: {path}"
    try:
        # ★**符号化を明示する**（2026-09-02）。`.ipynb` は nbformat の仕様で
        #   **常に UTF-8**。ここは読み書きとも `read_text()` / `write_text()`＝
        #   その機械の既定だったので、UTF-8 が既定でない環境（日本語 Windows の
        #   cp932。この製品は Windows 版を出荷している）では
        #   ・日本語を含む notebook が読めない（UnicodeDecodeError）
        #   ・書き戻すと **cp932 の .ipynb** になり、Jupyter でも他の道具でも
        #     開けない壊れたファイルになる（＝仕様違反のファイルを作る）
        #   Read/Write/Edit は既に UTF-8 で揃っている＝**同じ守りの片側が欠けていた**
        #   （この道具は policy ゲートでも同じことが 08-21 に見つかっている）。
        nb = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return f"Error: invalid notebook JSON: {e}"
    except UnicodeDecodeError as e:
        return f"Error: notebook is not valid UTF-8 ({p}): {e}"
    except OSError as e:
        return f"Error: cannot read notebook ({p}): {e}"
    if not isinstance(nb, dict):
        # JSON として解けても notebook ではない（配列など）。`setdefault` が
        # AttributeError で落ちるより、何であるかを言う。
        return (f"Error: {path} は notebook の形ではありません"
                f"（最上位が {type(nb).__name__}。ipynb は object）")
    cells = nb.setdefault("cells", [])
    if not isinstance(cells, list):
        return "Error: notebook の 'cells' が配列ではありません（壊れています）"

    if mode == "insert":
        i = idx if idx is not None else len(cells)
        i = max(0, min(i, len(cells)))
        new_cell = _empty_code_cell(src) if cell_type == "code" else _empty_md_cell(src)
        cells.insert(i, new_cell)
        msg = f"OK: inserted {cell_type} cell at index {i}"
    elif mode == "delete":
        if idx is None:
            return "Error: 'cell_index' required for delete"
        i = idx
        if not (0 <= i < len(cells)):
            return f"Error: cell_index {i} out of range (0..{len(cells)-1})"
        removed = cells.pop(i)
        # 壊れた notebook（セルが dict でない）でも道具ごと落ちない
        _kind = removed.get("cell_type", "?") if isinstance(removed, dict) else "?"
        msg = f"OK: deleted {_kind} cell at index {i}"
    elif mode == "replace":
        if idx is None:
            return "Error: 'cell_index' required for replace"
        i = idx
        if not (0 <= i < len(cells)):
            return f"Error: cell_index {i} out of range (0..{len(cells)-1})"
        if not isinstance(cells[i], dict):
            return (f"Error: cell {i} が object ではありません"
                    f"（{type(cells[i]).__name__}）。notebook が壊れています")
        cells[i]["source"] = src.splitlines(keepends=True)
        if cells[i].get("cell_type") == "code":
            cells[i]["outputs"] = []
            cells[i]["execution_count"] = None
        msg = f"OK: replaced cell {i} ({cells[i].get('cell_type','?')})"
    else:
        return f"Error: unknown mode {mode!r} (use replace|insert|delete)"

    # ★**不可分に書く**（2026-09-02）。`write_text` は開いた瞬間に 0 バイトへ
    #   切り詰めるので、書いている途中で失敗すると **notebook を丸ごと失う**
    #   （tools/atomic.py に再現手順つきで書いてある形。Write / Edit / TaskUpdate は
    #   既にこれを通しており、ここだけが素のままだった＝また片側だけ）。
    try:
        write_text_atomic(p, json.dumps(nb, ensure_ascii=False, indent=1) + "\n")
    except OSError as e:
        return f"Error: notebook を書けません（{p}）: {e}"
    # ★成功を型で名乗る（2026-09-21・他の道具と同じ理由）。
    from . import ToolResult
    return ToolResult(msg + f"  ({len(cells)} cells total)", ok=True, kind="ok")


SPEC = ToolSpec(
    name="NotebookEdit",
    description=(
        "Edit a Jupyter .ipynb. mode=replace|insert|delete, cell_index=int, "
        "new_source=str, cell_type=code|markdown (insert only)."
    ),
    schema={
        "type": "object",
        "properties": {
            "notebook_path": {"type": "string"},
            "mode": {"type": "string", "enum": ["replace", "insert", "delete"]},
            "cell_index": {"type": "integer"},
            "new_source": {"type": "string"},
            "cell_type": {"type": "string", "enum": ["code", "markdown"]},
        },
        "required": ["notebook_path"],
    },
    mutates=True,
    handler=_run,
)
