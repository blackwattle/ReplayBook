"""Test cases and test sets: one past ticket plus what we expect an agent to do with it."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from replaybook.mock_env.world import WorldState
from replaybook.schema.ground_truth import ActionSpec

ExpectationKind = Literal["actions", "ask"]


class Expectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # actions: the agent should perform these writes. ask: the ticket lacked information
    # and the agent should ask the requester instead of writing anything.
    kind: ExpectationKind
    actions: list[ActionSpec] = Field(default_factory=list)
    # history: taken from what engineers actually did. rules or model: extracted from real
    # ticket text by rules or by a model. manual: written by a person.
    source: Literal["history", "rules", "model", "manual"] = "history"
    # Expectations a model extracts from real data stay unverified until a person confirms.
    verified: bool = True
    notes: str | None = None


class TestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ticket_id: str
    request_type: str
    created: datetime
    summary: str
    world: WorldState
    expectation: Expectation
    # Visible hints that history itself may be outdated or wrong, for the accept workflow:
    # pre-procedure-change, corrected-later.
    tags: list[str] = Field(default_factory=list)


class TestSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    request_type: str
    source: str
    data_dir: str
    created_at: datetime
    selection: dict[str, Any] = Field(default_factory=dict)
    cases: list[TestCase] = Field(default_factory=list)

    def sha256(self) -> str:
        """Hash of what matters for replay: case ids, worlds and expectations."""
        payload = [
            {
                "id": c.id,
                "world": c.world.model_dump(mode="json"),
                "expectation": c.expectation.model_dump(mode="json"),
            }
            for c in self.cases
        ]
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def case(self, case_id: str) -> TestCase:
        for case in self.cases:
            if case.id == case_id:
                return case
        raise KeyError(case_id)
