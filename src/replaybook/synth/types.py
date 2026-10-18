"""What a synthetic request type looks like. The packs define the actual types.

Each type says how to sample realistic parameters, which tool calls are correct under
which procedure version, how requesters phrase the request, what inventory the ticket
sees, what a plausible human mistake looks like and, optionally, what an unusual request
beyond the skill looks like. The generator uses all of it; the request type's key, label
and keywords for classification live in the pack's manifest.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from replaybook.schema.ground_truth import ActionSpec

TIMELINE_START = datetime(2021, 10, 1, tzinfo=UTC)
TIMELINE_END = datetime(2026, 9, 30, tzinfo=UTC)

Params = dict[str, Any]
Mistake = tuple[Params, str] | None


@dataclass(frozen=True)
class Unusual:
    """A request that goes beyond the skill's usual pattern: the sentence the requester adds,
    the extra actions engineers took once someone had decided to do it, and any parameters
    those need (kept in the ground truth)."""

    cue: str
    actions: list[ActionSpec]
    params: Params = field(default_factory=dict)


@dataclass(frozen=True)
class RequestType:
    key: str
    label: str
    weight: float
    missable_fields: tuple[str, ...]
    effort_hours: tuple[float, float]
    summaries: tuple[str, ...]
    intros: tuple[str, ...]
    field_phrases: dict[str, tuple[str, ...]]
    field_labels: dict[str, tuple[str, ...]]
    sample_params: Callable[[random.Random], Params]
    actions: Callable[[Params, str], list[ActionSpec]]
    inventory: Callable[[Params, random.Random], dict[str, Any]]
    mistake: Callable[[Params, random.Random], Mistake]
    components: tuple[str, ...]
    procedure_version: Callable[[datetime], str] = lambda _at: "current"
    unsafe_trigger_field: str | None = None
    # Extra template placeholders derived from the parameters (for example a verb phrase
    # for an access level).
    derived: Callable[[Params], dict[str, Any]] | None = None
    # A request beyond the skill for the "unusual" judgement tickets:
    # unusual(params, procedure_version, rng). Types without one get only the approval and
    # discussion kinds of judgement ticket.
    unusual: Callable[[Params, str, random.Random], Unusual] | None = None

    def version_at(self, at: datetime) -> str:
        return self.procedure_version(at)


def current_version(_at: datetime) -> str:
    return "current"
