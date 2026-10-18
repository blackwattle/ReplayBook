"""Hidden ground truth for synthetic tickets.

The generator writes one record per ticket to the ground truth file. Only
`replaybook.eval.ground_truth` may read that file (a test enforces it), so nothing in the
replay or scoring path can peek at the answers.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionSpec(BaseModel):
    """A tool call that counts as correct: tool name and the arguments that matter."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class GroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str
    request_type: str | None
    noise: bool = False
    params: dict[str, Any] = Field(default_factory=dict)
    # Correct under the procedure in force when the ticket was raised.
    correct_actions: list[ActionSpec] = Field(default_factory=list)
    # Correct under the procedure in force today. Differs from correct_actions only for
    # tickets raised before a procedure change.
    current_actions: list[ActionSpec] = Field(default_factory=list)
    procedure_version: str = "current"
    info_missing: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    # The world state contains the condition that tempts an unsafe shortcut (for example
    # the topic already exists with different settings).
    unsafe_trigger: bool = False
    history_mistake: str | None = None
    corrected_by: str | None = None
    is_duplicate_of: str | None = None
    true_effort_hours: float = 0.0
