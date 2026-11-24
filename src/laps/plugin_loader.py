"""MCP-lite plugin loader.

Discovers Python files under ~/.blackwall/plugins/*.py. Each plugin must
expose a top-level `SPEC: ToolSpec` matching laps.tools.ToolSpec.

Example plugin (~/.blackwall/plugins/weather.py):

    from laps.tools import ToolSpec
    def _run(inp):
        return f"It's sunny in {inp.get('city', 'Tokyo')}"
    SPEC = ToolSpec(
        name="Weather",
        description="Mock weather query",
        schema={"type":"object","properties":{"city":{"type":"string"}}},
        handler=_run,
    )
"""
from __future__ import annotations
import importlib.util
import logging
from pathlib import Path

from .session import state_dir
from .tools import ToolSpec


log = logging.getLogger(__name__)


def discover_plugins() -> list[ToolSpec]:
    plugins_dir = state_dir() / "plugins"
    if not plugins_dir.exists():
        return []
    specs: list[ToolSpec] = []
    for p in sorted(plugins_dir.glob("*.py")):
        if p.name.startswith("_"):
            continue
        spec_obj = _load_module_spec(p)
        if spec_obj:
            specs.append(spec_obj)
    return specs


def _load_module_spec(path: Path) -> ToolSpec | None:
    try:
        mod_spec = importlib.util.spec_from_file_location(path.stem, path)
        if not mod_spec or not mod_spec.loader:
            return None
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)
        tool_spec = getattr(mod, "SPEC", None)
        if not isinstance(tool_spec, ToolSpec):
            log.warning("plugin %s has no SPEC: ToolSpec", path)
            return None
        return tool_spec
    except Exception as e:
        log.warning("plugin %s load failed: %s", path, e)
        return None
