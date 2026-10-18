"""Sample SKILL.md files the built-in agent follows, one per request type, from the packs."""

from __future__ import annotations

from pathlib import Path

from replaybook.packs import PackRegistry, registry


def skill_path(request_type: str, packs: PackRegistry | None = None) -> Path:
    reg = packs if packs is not None else registry()
    found = reg.skill_path(request_type)
    if found is None:
        available = ", ".join(sorted(reg.skills())) or "none"
        raise KeyError(f"no sample skill for request type '{request_type}'. Available: {available}")
    return found


def load_skill(request_type: str, packs: PackRegistry | None = None) -> str:
    return skill_path(request_type, packs).read_text(encoding="utf-8")
