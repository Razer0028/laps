"""Pi-4-Q: TodoWrite-style scratchpad (.progress.md).

Persisted per-session at ~/.blackwall/sessions/<id>/.progress.md.
Provides a structured TODO that the model can read and update.
"""
from __future__ import annotations
import re
from dataclasses import dataclass


@dataclass
class TodoItem:
    text: str
    done: bool = False


_TODO_LINE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$", re.MULTILINE)


def parse(scratchpad: str) -> list[TodoItem]:
    items = []
    for m in _TODO_LINE.finditer(scratchpad):
        items.append(TodoItem(text=m.group(2), done=m.group(1).strip().lower() == "x"))
    return items


def render(items: list[TodoItem]) -> str:
    lines = ["# Progress\n"]
    for it in items:
        check = "x" if it.done else " "
        lines.append(f"- [{check}] {it.text}")
    return "\n".join(lines) + "\n"


def update_done(items: list[TodoItem], text: str, done: bool = True) -> list[TodoItem]:
    for it in items:
        if it.text.strip() == text.strip():
            it.done = done
            return items
    items.append(TodoItem(text=text, done=done))
    return items


def system_block(items: list[TodoItem]) -> str:
    """A prompt-injectable scratchpad summary."""
    if not items:
        return ""
    done = sum(1 for i in items if i.done)
    body = render(items)
    return f"\n--- Current TODO ({done}/{len(items)} done) ---\n{body}---\n"
