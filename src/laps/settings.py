"""User settings at ~/.blackwall/settings.json.

Drives:
- permission policy (analog to Claude Code's fewer-permission-prompts skill)
- statusline customization
- default model / backend
"""
from __future__ import annotations
from . import paths as _lp
import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _settings_path() -> Path:
    home = str(_lp.home_dir())
    return Path(home) / "settings.json"


@dataclass
class Settings:
    auto_yes: bool = False                       # skip destructive prompt
    allow_patterns: list[str] = field(default_factory=list)   # always-allow regex
    deny_patterns: list[str] = field(default_factory=list)    # always-deny regex
    statusline_format: str = "blackwall · {model} · session {sid_short}"
    statusline_color: str = "cyan"
    default_model: str = ""
    default_backend: str = ""

    @classmethod
    def load(cls) -> "Settings":
        p = _settings_path()
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text())
        except json.JSONDecodeError:
            return cls()
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> None:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2))


def render_statusline(s: Settings, *, model: str, sid: str) -> str:
    sid_short = sid[:14]
    return s.statusline_format.format(model=model, sid=sid, sid_short=sid_short)
