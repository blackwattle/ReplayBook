"""Unsafe-action rules and severities.

A write that history did not contain is checked here. Each pack lists its dangerous tools
in rules.yaml (destructive tools are critical) and may add an argument-based check in
safety.py for tools that are only unsafe with certain arguments. The core asks every
pack and takes the first verdict.
"""

from __future__ import annotations

from replaybook.eval.normalise import NormalisedWrite
from replaybook.mock_env.world import WorldState
from replaybook.packs import PackRegistry, registry


def unsafe_severity(
    write: NormalisedWrite, world: WorldState, packs: PackRegistry | None = None
) -> tuple[str, str] | None:
    """Return (severity, reason) for an unexpected write that is unsafe, else None."""
    reg = packs if packs is not None else registry()
    verdict = reg.unsafe_severity(write.tool, write.args, world.state)
    if verdict is None:
        return None
    severity, reason = verdict
    return severity, reason or f"{write.tool} is listed as unsafe"


def critical_tools(packs: PackRegistry | None = None) -> set[str]:
    reg = packs if packs is not None else registry()
    return {tool for tool, (severity, _reason) in reg.unsafe.items() if severity == "critical"}
