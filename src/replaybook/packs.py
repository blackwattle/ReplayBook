"""Packs: everything Replaybook knows about a tool family, loaded from `packs/<name>/`.

The core never imports or names a specific tool (a test enforces it). A pack is a
directory with:

- pack.yaml      name, description and the request types it answers (label, keywords,
                 components, the tools that identify the type, the sample skill)
- rules.yaml     scoring rules: per-tool normalisation (family, target, which arguments
                 matter, defaults, unordered lists, case), unsafe tools with a severity,
                 and superseded tools with a plain-English label
- __init__.py    makes the directory importable; the other modules use relative imports
- server.py      build(world, recorder) -> MockServer with the pack's mock tools
- world.py       from_inventory(inventory, facts) -> dict, the pack's slice of world state
- safety.py      optional unsafe_severity(tool, args, state) for argument-based checks
- synth.py       optional REQUEST_TYPES, PROCEDURE_CHANGES, CHANGE_TICKETS and
                 platform_facts() for the synthetic generator
- agent.py       optional PROCEDURES (scripted reference procedures), EXTRACTORS (rules
                 that read expected actions from ticket text) and seed_inventory()
- tools.json     for imported packs without a server.py: the MCP tool list a generic
                 recording mock is built from
- skills/        sample SKILL.md files

Packs are found through REPLAYBOOK_PACKS (directories separated by the OS path separator),
otherwise `./packs` in the working directory, otherwise the `packs/` directory of this
checkout. `registry()` caches the result; `reset()` forgets it.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

ENV_VAR = "REPLAYBOOK_PACKS"
REPO_PACKS = Path(__file__).resolve().parents[2] / "packs"
MODULE_PREFIX = "replaybook_packs"


class PackError(Exception):
    """A pack directory is malformed."""


@dataclass(frozen=True)
class ToolRule:
    """How two calls of one tool are compared. Tools in the same family do the same job in
    different ways (a legacy and a current tool), which is how a wrong tool is told apart
    from a completely different action."""

    family: str
    target: tuple[str, ...]
    matters: tuple[str, ...]
    defaults: dict[str, Any] = field(default_factory=dict)
    unordered: tuple[str, ...] = ()
    keep_case: tuple[str, ...] = ()
    # argument name -> name of a core value normaliser (see eval.normalise.EQUIVALENTS)
    equivalents: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RequestTypeInfo:
    key: str
    label: str
    pack: str
    keywords: tuple[str, ...] = ()
    strong_keywords: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    anchor_keywords: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    skill: Path | None = None


@dataclass(frozen=True)
class ProcedureChange:
    request_type: str
    at: datetime
    from_version: str
    to_version: str
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_type": self.request_type,
            "at": self.at.isoformat(),
            "from_version": self.from_version,
            "to_version": self.to_version,
            "summary": self.summary,
        }


class Pack:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        manifest_path = self.path / "pack.yaml"
        if not manifest_path.is_file():
            raise PackError(f"{self.path} has no pack.yaml")
        self.manifest: dict[str, Any] = yaml.safe_load(manifest_path.read_text("utf-8")) or {}
        self.name = str(self.manifest.get("name") or self.path.name)
        if not self.name.replace("_", "").replace("-", "").isalnum():
            raise PackError(f"pack name '{self.name}' must be letters, digits, _ or -")
        self.description = str(self.manifest.get("description") or "")
        self.request_types = self._request_types()
        self.rules, self.unsafe, self.superseded = self._rules()
        self._modules: dict[str, ModuleType | None] = {}
        self._lock = threading.Lock()

    # --- manifest ----------------------------------------------------------------------

    def _request_types(self) -> dict[str, RequestTypeInfo]:
        found: dict[str, RequestTypeInfo] = {}
        for key, spec in (self.manifest.get("request_types") or {}).items():
            spec = spec or {}
            skill = spec.get("skill")
            found[key] = RequestTypeInfo(
                key=key,
                label=str(spec.get("label") or key),
                pack=self.name,
                keywords=tuple(spec.get("keywords") or ()),
                strong_keywords=tuple(spec.get("strong_keywords") or ()),
                components=tuple(spec.get("components") or ()),
                exclude_keywords=tuple(spec.get("exclude_keywords") or ()),
                anchor_keywords=tuple(spec.get("anchor_keywords") or ()),
                tools=tuple(spec.get("tools") or ()),
                skill=(self.path / skill) if skill else None,
            )
        return found

    def _rules(self) -> tuple[dict[str, ToolRule], dict[str, tuple[str, str]], dict[str, str]]:
        path = self.path / "rules.yaml"
        data = yaml.safe_load(path.read_text("utf-8")) if path.is_file() else {}
        data = data or {}
        rules: dict[str, ToolRule] = {}
        for tool, spec in (data.get("tools") or {}).items():
            spec = spec or {}
            matters = tuple(spec.get("matters") or ())
            rules[tool] = ToolRule(
                family=str(spec.get("family") or tool),
                target=tuple(spec.get("target") or ()),
                matters=matters,
                defaults=dict(spec.get("defaults") or {}),
                unordered=tuple(spec.get("unordered") or ()),
                keep_case=tuple(spec.get("keep_case") or ()),
                equivalents=dict(spec.get("equivalents") or {}),
            )
        unsafe: dict[str, tuple[str, str]] = {}
        for tool, spec in (data.get("unsafe") or {}).items():
            if isinstance(spec, str):
                unsafe[tool] = ("critical", spec)
            else:
                spec = spec or {}
                unsafe[tool] = (
                    str(spec.get("severity") or "critical"),
                    str(spec.get("reason") or ""),
                )
        superseded = {k: str(v) for k, v in (data.get("superseded") or {}).items()}
        return rules, unsafe, superseded

    # --- python modules ----------------------------------------------------------------

    def module(self, name: str) -> ModuleType | None:
        """Import `<pack>/<name>.py` as a submodule of the pack package, or None if absent."""
        with self._lock:
            if name in self._modules:
                return self._modules[name]
            if not (self.path / f"{name}.py").is_file():
                self._modules[name] = None
                return None
            package = f"{MODULE_PREFIX}.{self.name.replace('-', '_')}"
            if package not in sys.modules:
                init = self.path / "__init__.py"
                if not init.is_file():
                    raise PackError(
                        f"{self.path} needs an __init__.py so its modules can import each other"
                    )
                spec = importlib.util.spec_from_file_location(
                    package, init, submodule_search_locations=[str(self.path)]
                )
                assert spec is not None and spec.loader is not None
                module = importlib.util.module_from_spec(spec)
                sys.modules[package] = module
                spec.loader.exec_module(module)
            loaded = importlib.import_module(f"{package}.{name}")
            self._modules[name] = loaded
            return loaded

    @property
    def tools_json(self) -> Path | None:
        path = self.path / "tools.json"
        return path if path.is_file() else None

    def build_server(self, world: Any, recorder: Any) -> Any:
        server = self.module("server")
        if server is not None:
            return server.build(world, recorder)
        if self.tools_json is not None:
            from replaybook.mock_env.generic import build_recording_server

            return build_recording_server(self, world, recorder)
        raise PackError(f"pack '{self.name}' has neither server.py nor tools.json")

    def from_inventory(self, inventory: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
        """The pack's slice of world state for a ticket. Default: the inventory key named
        after the pack, as it is."""
        world = self.module("world")
        if world is not None and hasattr(world, "from_inventory"):
            return world.from_inventory(inventory or {}, facts or {})
        return dict((inventory or {}).get(self.name) or {})

    def unsafe_severity(
        self, tool: str, args: dict[str, Any], state: dict[str, Any]
    ) -> tuple[str, str] | None:
        """Severity and reason for an unexpected write: the rules.yaml list first, then the
        pack's optional argument-based check."""
        if tool in self.unsafe:
            return self.unsafe[tool]
        safety = self.module("safety")
        if safety is not None and hasattr(safety, "unsafe_severity"):
            return safety.unsafe_severity(tool, args, state)
        return None

    # --- synthetic data ----------------------------------------------------------------

    def synth_types(self) -> list[Any]:
        synth = self.module("synth")
        return list(getattr(synth, "REQUEST_TYPES", ())) if synth else []

    def procedure_changes(self) -> list[ProcedureChange]:
        synth = self.module("synth")
        found: list[ProcedureChange] = []
        for change in getattr(synth, "PROCEDURE_CHANGES", ()) if synth else ():
            at = change["at"]
            if isinstance(at, str):
                at = datetime.fromisoformat(at)
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            found.append(
                ProcedureChange(
                    request_type=str(change["request_type"]),
                    at=at,
                    from_version=str(change["from_version"]),
                    to_version=str(change["to_version"]),
                    summary=str(change.get("summary", "")),
                )
            )
        return found

    def change_tickets(self) -> list[dict[str, Any]]:
        synth = self.module("synth")
        return list(getattr(synth, "CHANGE_TICKETS", ())) if synth else []

    def platform_facts(self) -> dict[str, Any]:
        synth = self.module("synth")
        if synth is not None and hasattr(synth, "platform_facts"):
            return dict(synth.platform_facts())
        return {}

    # --- agents and extraction ---------------------------------------------------------

    def procedures(self) -> dict[str, Callable[..., Any]]:
        agent = self.module("agent")
        return dict(getattr(agent, "PROCEDURES", {})) if agent else {}

    def extract_rules(self) -> dict[str, Callable[[str], list[Any]]]:
        agent = self.module("agent")
        return dict(getattr(agent, "EXTRACTORS", {})) if agent else {}

    def seed_inventory(self, actions: list[Any]) -> dict[str, Any]:
        agent = self.module("agent")
        if agent is not None and hasattr(agent, "seed_inventory"):
            return dict(agent.seed_inventory(actions))
        return {}

    def __repr__(self) -> str:
        return f"Pack({self.name!r}, {str(self.path)!r})"


class PackRegistry:
    """Every loaded pack and the merged views the core works from."""

    def __init__(self, packs: list[Pack], search_path: list[Path]) -> None:
        self.packs = sorted(packs, key=lambda p: p.name)
        self.search_path = search_path
        self.by_name = {p.name: p for p in self.packs}
        if len(self.by_name) != len(self.packs):
            names = [p.name for p in self.packs]
            raise PackError(
                f"duplicate pack names: {sorted(n for n in names if names.count(n) > 1)}"
            )
        self.request_types: dict[str, RequestTypeInfo] = {}
        self.rules: dict[str, ToolRule] = {}
        self.unsafe: dict[str, tuple[str, str]] = {}
        self.superseded: dict[str, str] = {}
        self.tool_types: dict[str, str] = {}
        for pack in self.packs:
            for key, info in pack.request_types.items():
                if key in self.request_types:
                    raise PackError(
                        f"request type '{key}' is defined by both "
                        f"{self.request_types[key].pack} and {pack.name}"
                    )
                self.request_types[key] = info
                for tool in info.tools:
                    self.tool_types[tool] = key
            for tool, rule in pack.rules.items():
                if tool in self.rules:
                    raise PackError(f"tool '{tool}' has rules in more than one pack")
                self.rules[tool] = rule
            self.unsafe.update(pack.unsafe)
            self.superseded.update(pack.superseded)

    def pack(self, name: str) -> Pack:
        try:
            return self.by_name[name]
        except KeyError as exc:
            raise PackError(
                f"no pack '{name}'. Loaded: {', '.join(self.by_name) or 'none'}"
            ) from exc

    def pack_for_type(self, request_type: str) -> Pack | None:
        info = self.request_types.get(request_type)
        return self.by_name[info.pack] if info else None

    def label(self, request_type: str) -> str:
        info = self.request_types.get(request_type)
        return info.label if info else request_type

    def skill_path(self, request_type: str) -> Path | None:
        info = self.request_types.get(request_type)
        return info.skill if info and info.skill and info.skill.is_file() else None

    def skills(self) -> dict[str, Path]:
        return {k: v.skill for k, v in self.request_types.items() if v.skill and v.skill.is_file()}

    def unsafe_severity(
        self, tool: str, args: dict[str, Any], state: dict[str, dict[str, Any]]
    ) -> tuple[str, str] | None:
        for pack in self.packs:
            verdict = pack.unsafe_severity(tool, args, state.get(pack.name) or {})
            if verdict is not None:
                return verdict
        return None

    def synth_types(self) -> list[Any]:
        return [rt for pack in self.packs for rt in pack.synth_types()]

    def procedure_changes(self) -> list[ProcedureChange]:
        return [c for pack in self.packs for c in pack.procedure_changes()]

    def current_versions(self) -> set[str]:
        """Procedure versions that are current today ("current" plus every to_version)."""
        return {"current"} | {c.to_version for c in self.procedure_changes()}

    def change_tickets(self) -> list[dict[str, Any]]:
        return [t for pack in self.packs for t in pack.change_tickets()]

    def platform_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {}
        for pack in self.packs:
            facts.update(pack.platform_facts())
        return facts

    def procedures(self) -> dict[str, Callable[..., Any]]:
        merged: dict[str, Callable[..., Any]] = {}
        for pack in self.packs:
            merged.update(pack.procedures())
        return merged

    def extract_rules(self) -> dict[str, Callable[[str], list[Any]]]:
        merged: dict[str, Callable[[str], list[Any]]] = {}
        for pack in self.packs:
            merged.update(pack.extract_rules())
        return merged

    def seed_inventory(self, actions: list[Any]) -> dict[str, Any]:
        seed: dict[str, Any] = {}
        for pack in self.packs:
            seed.update(pack.seed_inventory(actions))
        return seed

    def summary(self) -> dict[str, Any]:
        return {
            "search_path": [str(p) for p in self.search_path],
            "packs": {
                p.name: {
                    "path": str(p.path),
                    "request_types": sorted(p.request_types),
                    "tools_with_rules": len(p.rules),
                }
                for p in self.packs
            },
        }


def search_path() -> list[Path]:
    configured = os.environ.get(ENV_VAR, "").strip()
    if configured:
        return [Path(p).expanduser() for p in configured.split(os.pathsep) if p.strip()]
    local = Path.cwd() / "packs"
    if local.is_dir() and any((d / "pack.yaml").is_file() for d in local.iterdir()):
        return [local]
    return [REPO_PACKS]


def load_packs(directories: list[Path] | None = None) -> PackRegistry:
    dirs = list(directories) if directories is not None else search_path()
    packs: list[Pack] = []
    for directory in dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if child.is_dir() and (child / "pack.yaml").is_file():
                packs.append(Pack(child))
    return PackRegistry(packs, dirs)


_registry: PackRegistry | None = None
_registry_lock = threading.Lock()


def registry() -> PackRegistry:
    """The packs on the search path, loaded once per process (see `reset`)."""
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = load_packs()
        return _registry


def reset() -> None:
    """Forget the cached registry and the imported pack modules, so a changed
    REPLAYBOOK_PACKS takes effect (tests use this)."""
    global _registry
    with _registry_lock:
        _registry = None
        for name in [m for m in sys.modules if m.startswith(MODULE_PREFIX)]:
            del sys.modules[name]


def use(reg: PackRegistry) -> None:
    """Make an explicitly loaded registry the process-wide one."""
    global _registry
    with _registry_lock:
        _registry = reg
