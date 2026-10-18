"""What an agent under test looks like to Replaybook.

An agent gets a MockEnvironment (the ticket plus mock tools) and does whatever it does.
Replaybook only looks at the calls the environment recorded; the transcript is kept for
the evidence pack and for debugging.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from replaybook.mock_env.registry import MockEnvironment


class AgentRunResult(BaseModel):
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    final_message: str = ""
    # done: finished normally. asked: stopped to ask the requester. max_steps, error,
    # offline: did not finish.
    stopped_reason: str = "done"
    error: str | None = None
    model: str | None = None
    # Model calls the agent made for this case (see llm.client.usage_summary): how many,
    # what they cost where known, how many could not be priced. None for agents without a
    # model (scripted, external).
    usage: dict[str, Any] | None = None


class AgentUnderTest(Protocol):
    name: str

    @property
    def config(self) -> dict[str, Any]:
        """Everything that defines this agent's behaviour, for the fingerprint."""
        ...

    def run(self, env: MockEnvironment) -> AgentRunResult: ...


def ticket_text(env: MockEnvironment) -> str:
    """The text an agent may read: summary, description and comments before the team acted."""
    ticket = env.world.ticket
    if ticket is None:
        return ""
    parts = [ticket.summary, ticket.description]
    parts += [c["body"] for c in ticket.comments]
    return "\n".join(parts)
