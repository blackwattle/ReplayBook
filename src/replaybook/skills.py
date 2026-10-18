"""Skills: the SKILL.md files an agent follows, one per request type, from the packs.

A skill is Markdown with YAML front matter: `name`, `description`, `request_type`,
`tools` (the tools it may call, comma-separated or a list) and `verify` (the read-only
check for each write, see eval.checks). Its version is the SHA-256 of the file's text, so
any edit, checks included, makes a new version, and a replay counts only for the version
it ran (`skill_version` on every run, baseline and shadow proposal).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from replaybook.packs import PackRegistry, registry

SHORT_VERSION = 12


class SkillError(ValueError):
    """A SKILL.md file cannot be read: no front matter, or front matter that is not YAML."""


@dataclass(frozen=True)
class Skill:
    request_type: str
    path: Path
    text: str
    name: str
    description: str
    tools: tuple[str, ...]
    verify: dict[str, Any] = field(default_factory=dict)
    front_matter: dict[str, Any] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return skill_version(self.text)

    @property
    def short_version(self) -> str:
        return self.version[:SHORT_VERSION]


def skill_version(text: str) -> str:
    """The SHA-256 of a skill's text (as read, so line endings do not change it)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short(version: str | None) -> str:
    """A skill version as people read it: the first 12 hex characters."""
    return version[:SHORT_VERSION] if version else "-"


def front_matter(text: str) -> tuple[dict[str, Any], str]:
    """(front matter, body) of a SKILL.md. The front matter sits between two `---` lines."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("a SKILL.md starts with a front matter block between two --- lines")
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise SkillError("the front matter block is not closed with a --- line") from exc
    try:
        data = yaml.safe_load("\n".join(lines[1:end])) or {}
    except yaml.YAMLError as exc:
        raise SkillError(f"the front matter is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillError("the front matter must be a mapping of keys to values")
    return data, "\n".join(lines[end + 1 :])


def parse_skill(text: str, path: Path, request_type: str = "") -> Skill:
    data, _body = front_matter(text)
    tools = data.get("tools") or []
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",")]
    verify = data.get("verify") or {}
    return Skill(
        request_type=str(data.get("request_type") or request_type),
        path=Path(path),
        text=text,
        name=str(data.get("name") or Path(path).parent.name),
        description=str(data.get("description") or ""),
        tools=tuple(str(t) for t in tools if str(t).strip()),
        verify=dict(verify) if isinstance(verify, dict) else {"_invalid": verify},
        front_matter=data,
    )


def skill_path(request_type: str, packs: PackRegistry | None = None) -> Path:
    reg = packs if packs is not None else registry()
    found = reg.skill_path(request_type)
    if found is None:
        available = ", ".join(sorted(reg.skills())) or "none"
        raise KeyError(f"no sample skill for request type '{request_type}'. Available: {available}")
    return found


def load_skill(request_type: str, packs: PackRegistry | None = None) -> str:
    return skill_path(request_type, packs).read_text(encoding="utf-8")


def find_skill(request_type: str, packs: PackRegistry | None = None) -> Skill | None:
    """The request type's skill from its pack, or None when the pack ships none."""
    reg = packs if packs is not None else registry()
    path = reg.skill_path(request_type)
    if path is None:
        return None
    return parse_skill(path.read_text(encoding="utf-8"), path, request_type)


def skill_identity(request_type: str, packs: PackRegistry | None = None) -> dict[str, Any]:
    """What a run records about the skill: its name and version, or None for both when the
    request type has no skill. A skill whose front matter cannot be read still has a
    version (the hash of its text), so the record never depends on it parsing."""
    reg = packs if packs is not None else registry()
    path = reg.skill_path(request_type)
    if path is None:
        return {"skill": None, "skill_version": None}
    text = path.read_text(encoding="utf-8")
    try:
        name = parse_skill(text, path, request_type).name
    except SkillError:
        name = path.parent.name
    return {"skill": name, "skill_version": skill_version(text)}
