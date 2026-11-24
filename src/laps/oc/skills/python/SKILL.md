---
name: python
description: Python projects done right - environments (venv, uv), pyproject packaging, project layout, typing and dataclasses/Pydantic, error handling, logging, CLIs, subprocess and file/encoding pitfalls (especially on Windows), async, testing with pytest, linting (ruff, mypy), and performance. Use for any Python code or tooling task.
keywords: pyproject, pyproject.toml, venv, 仮想環境, pytest, poetry, uv run, パッケージ化, django, flask, fastapi, python+プロジェクト, python+環境
---

# Python

## Environment
- One virtual environment per project: `uv venv` + `uv pip install -r requirements.txt` (or `uv sync` with pyproject), or `python -m venv .venv`. Run tools through the venv's interpreter: `.venv/bin/python -m pytest` (Windows `.venv\Scripts\python -m pytest`).
- On Windows use the `py` launcher to pick versions (`py -3.12 -m venv .venv`, `py -0p` lists installs). `python` may be the Store stub.
- Never `pip install` into the system Python on Linux (PEP 668 "externally managed"); use a venv or `pipx` for tools.

## Project layout
```
pyproject.toml      # [project] name, version, dependencies, requires-python; [project.scripts] for CLIs
src/pkg/__init__.py
src/pkg/cli.py
tests/test_x.py
```
Pin direct dependencies with lower bounds in pyproject; lock exact versions for apps (`uv lock`, `pip-tools`).

## Code
- Type hints everywhere public; `from __future__ import annotations` for forward refs on older versions. `dataclasses` for plain records, Pydantic for validated external data (JSON, env, API bodies).
- `pathlib.Path` for paths; always pass `encoding="utf-8"` to `open`/`read_text`/`write_text` (the default is the locale encoding - CP932 on Japanese Windows). Japanese legacy files: `encoding="cp932"`; unknown: try utf-8, fall back, or `errors="replace"`.
- `subprocess.run([...], check=True, capture_output=True, text=True, encoding="utf-8", timeout=...)` with an argument list (no `shell=True` with user input). On Windows, console programs may print in CP932: decode with `errors="replace"`.
- Exceptions: catch specific types, add context (`raise ToolError("...") from e`), never bare `except:`. Use `logging` (module-level `log = logging.getLogger(__name__)`) not print for libraries.
- CLIs: `argparse` (stdlib) or Typer/Click; exit codes 0/1/2; `if __name__ == "__main__": raise SystemExit(main())`.
- Concurrency: threads/`concurrent.futures` for I/O-bound work, processes for CPU-bound; asyncio only when the stack is async (httpx, FastAPI) - never call blocking I/O inside `async def`.
- Atomic writes: write to `path.with_suffix(".tmp")`, then `os.replace(tmp, path)`.

## Testing and quality
- pytest: plain `assert`, fixtures, `tmp_path`, `monkeypatch`, `pytest.mark.parametrize`; run `python -m pytest -q -x`.
- `ruff check --fix .` and `ruff format .` (lint + format), `mypy src` or `pyright` for types. Follow whatever the project configures in pyproject.

## Performance
Measure first: `python -X importtime`, `cProfile` (`python -m cProfile -s cumtime script.py | head -30`), `py-spy top --pid N`. Usual fixes: better algorithm/data structure (dict/set lookups), avoid repeated work in loops, vectorize with NumPy/Polars, batch I/O, cache (`functools.lru_cache`).

## Packaging and distribution
- Build: `python -m build` (sdist + wheel); non-Python files need `[tool.setuptools.package-data]` or they silently drop out of the wheel.
- Single-file tools for users without Python: PyInstaller (`pyinstaller --onefile cli.py`) or ship with the embeddable Python on Windows.
