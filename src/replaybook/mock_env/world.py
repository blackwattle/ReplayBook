"""World state: the small, per-case slice of infrastructure the mock servers answer from.

Everything is a local mock. Reads return this state; writes change an in-memory copy
and are recorded, never executed anywhere real. A world holds the ticket context plus
one JSON object per pack, built by the pack from the ticket's inventory snapshot
overlaid with today's platform facts (platform.yaml), because an agent is assessed for
use on today's platform. The core only copies, hashes and serialises those objects; each
pack validates its own slice into typed models when its server is built.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from replaybook.packs import PackRegistry, registry
from replaybook.schema.ticket import Ticket


class TicketContext(BaseModel):
    """What the agent is allowed to see of the ticket: as filed, before the team acted."""

    id: str
    summary: str
    description: str
    reporter: str
    created: str
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    comments: list[dict[str, str]] = Field(default_factory=list)

    @classmethod
    def from_ticket(cls, ticket: Ticket) -> TicketContext:
        shown = ticket.comments_before_first_engineer_reply()
        return cls(
            id=ticket.id,
            summary=ticket.summary,
            description=ticket.description,
            reporter=ticket.reporter,
            created=ticket.created.isoformat(),
            labels=list(ticket.labels),
            components=list(ticket.components),
            comments=[
                {
                    "author": c.author,
                    "role": c.role,
                    "created": c.created.isoformat(),
                    "body": c.body,
                }
                for c in shown
            ],
        )


class WorldState(BaseModel):
    ticket: TicketContext | None = None
    # pack name -> that pack's slice of the world, as plain JSON
    state: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def slice(self, pack: str) -> dict[str, Any]:
        """The pack's slice, created empty if the world has none."""
        return self.state.setdefault(pack, {})

    @classmethod
    def from_ticket(
        cls,
        ticket: Ticket,
        platform_facts: dict[str, Any] | None = None,
        packs: PackRegistry | None = None,
    ) -> WorldState:
        world = cls.from_inventory(ticket.inventory, platform_facts, packs)
        world.ticket = TicketContext.from_ticket(ticket)
        return world

    @classmethod
    def from_inventory(
        cls,
        inventory: dict[str, Any],
        platform_facts: dict[str, Any] | None = None,
        packs: PackRegistry | None = None,
    ) -> WorldState:
        """Build a world from a ticket's inventory snapshot plus today's platform facts."""
        reg = packs if packs is not None else registry()
        facts = platform_facts or {}
        inv = inventory or {}
        return cls(state={pack.name: pack.from_inventory(inv, facts) for pack in reg.packs})
