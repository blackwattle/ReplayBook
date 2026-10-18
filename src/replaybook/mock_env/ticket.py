"""The ticket tools: the only tools the core defines.

Every organisation needs "read the ticket", "ask the requester", "hand it back to a
person" and "leave a note", whatever its infrastructure tools are. Asking is a write (it
changes the ticket's status and stops the agent) and is what the scorer expects when
information is missing. Escalating is a write too: the agent hands the ticket back with a
reason and stops, which is what the scorer expects when the engineers' handling needed a
person's judgement. A note is a write that the scorer ignores.
"""

from __future__ import annotations

from typing import Any

from replaybook.mock_env.base import MockServer, ToolError
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.world import WorldState

SERVER_NAME = "ticket"
GET_TOOL = "ticket_get"
ASK_TOOL = "ticket_ask_requester"
ESCALATE_TOOL = "ticket_escalate"
COMMENT_TOOL = "ticket_comment"
NOTE_TOOLS = {COMMENT_TOOL}
# Writes that only touch the ticket. Everything else an agent writes is an infrastructure
# write, and needs a verification check in the skill (milestone 27).
TICKET_WRITE_TOOLS = {ASK_TOOL, ESCALATE_TOOL, COMMENT_TOOL}
INSTRUCTIONS = (
    "The ticket being worked. Start with ticket_get. If the ticket lacks information you "
    "need, call ticket_ask_requester instead of guessing; the ticket then waits for the "
    "requester and you stop. If the ticket needs a decision a person must make (an "
    "approval, a discussion between teams, or more than your skill covers), call "
    "ticket_escalate with the reason and stop without changing anything. Leave a one-line "
    "ticket_comment when you finish."
)


def build(world: WorldState, recorder: CallRecorder) -> MockServer:
    server = MockServer(SERVER_NAME, INSTRUCTIONS, world, recorder)

    @server.read
    def ticket_get() -> dict[str, Any]:
        """Get the ticket being worked: summary, description and the comments so far."""
        if world.ticket is None:
            raise ToolError("no ticket is attached to this environment")
        return world.ticket.model_dump()

    @server.write(idempotent=True)
    def ticket_ask_requester(question: str) -> dict[str, Any]:
        """Ask the requester for missing information and stop; the ticket waits for their reply."""
        if not question.strip():
            raise ToolError("question must not be empty")
        return {"status": "Waiting on Requester", "question": question}

    @server.write(idempotent=True)
    def ticket_escalate(reason: str) -> dict[str, Any]:
        """Hand the ticket back to a person with the reason, and stop. Use it when the ticket
        needs a decision you should not make: an approval, a discussion between teams, or
        work beyond your skill."""
        if not reason.strip():
            raise ToolError("reason must not be empty")
        return {"status": "Escalated", "reason": reason}

    @server.write(idempotent=True)
    def ticket_comment(body: str) -> dict[str, Any]:
        """Add a note to the ticket (no effect on infrastructure)."""
        if not body.strip():
            raise ToolError("body must not be empty")
        return {"commented": body}

    return server
