"""Knowledge tools with the time-travel guard.

An agent working a ticket may want to search past tickets ("how did we do this last
time?"). During replay that is dangerous: the corpus contains the very ticket being
replayed, with its resolution, and everything that happened after it. So Replaybook serves
the knowledge tools itself, from the ticket corpus, with a horizon: the creation time of
the ticket under test. Only tickets created before the horizon come back, with comments,
status changes, actions and code changes cut at that instant, the status as it was then,
and never the ticket under test itself. The same rule applies to any RAG table: filter on
the item's creation time, strictly before the ticket's.

The server is always present so the tool surface (and the fingerprint) is stable; with no
corpus attached the search says so and returns nothing.
"""

from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from replaybook.mock_env.base import MockServer, ToolError
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.world import WorldState
from replaybook.schema.ticket import Ticket

SERVER_NAME = "knowledge"
SEARCH_TOOL = "knowledge_search_tickets"
GET_TOOL = "knowledge_get_ticket"
INSTRUCTIONS = (
    "Past tickets of this team, as they stood when the current ticket was raised. Search "
    "them for how similar requests were handled. Nothing created after the current ticket "
    "is visible, and the current ticket itself is not in here."
)
WORD = re.compile(r"[a-z0-9][a-z0-9._/-]{1,}")
_cache: dict[Path, Corpus] = {}
_cache_lock = threading.Lock()


class Corpus:
    """The tickets a knowledge server answers from, loaded once per dataset directory."""

    def __init__(self, tickets: list[Ticket], path: Path | None = None) -> None:
        self.tickets = sorted(tickets, key=lambda t: (t.created, t.id))
        self.by_id = {t.id: t for t in self.tickets}
        self.path = Path(path) if path else None

    @classmethod
    def load(cls, data_dir: Path) -> Corpus:
        """tickets.jsonl from a dataset directory, cached process-wide by path."""
        from replaybook.adapters.synthetic import load_tickets

        key = Path(data_dir).resolve()
        with _cache_lock:
            if key not in _cache:
                _cache[key] = cls(load_tickets(key), key)
            return _cache[key]

    @classmethod
    def maybe(cls, data_dir: str | Path | None) -> Corpus | None:
        """A corpus when the directory holds tickets.jsonl, else None."""
        if not data_dir:
            return None
        path = Path(data_dir)
        return cls.load(path) if (path / "tickets.jsonl").is_file() else None

    def visible(self, horizon: datetime, exclude: str | None) -> list[Ticket]:
        return [cut(t, horizon) for t in self.tickets if t.created < horizon and t.id != exclude]


def cut(ticket: Ticket, horizon: datetime) -> Ticket:
    """The ticket as it stood strictly before `horizon`."""
    changes = [c for c in ticket.status_changes if c.at < horizon]
    status = changes[-1].to_status if changes else "Open"
    resolved = (
        ticket.resolved if ticket.resolved is not None and ticket.resolved < horizon else None
    )
    return ticket.model_copy(
        update={
            "comments": [c for c in ticket.comments if c.created < horizon],
            "status_changes": changes,
            "actions": [a for a in ticket.actions if a.at < horizon],
            "code_changes": [
                c for c in ticket.code_changes if c.merged_at and c.merged_at < horizon
            ],
            "status": status,
            "resolved": resolved,
            "resolution": ticket.resolution if resolved is not None else None,
            "time_spent_hours": ticket.time_spent_hours if resolved is not None else None,
            "effort_source": ticket.effort_source if resolved is not None else None,
        }
    )


def _words(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def score(ticket: Ticket, query_words: set[str]) -> int:
    if not query_words:
        return 0
    summary = _words(ticket.summary)
    body = _words(ticket.description) | {w for c in ticket.comments for w in _words(c.body)}
    return 2 * len(query_words & summary) + len(query_words & body)


def _snippet(ticket: Ticket, limit: int = 160) -> str:
    text = " ".join(ticket.description.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _summary_of(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "created": ticket.created.isoformat(),
        "summary": ticket.summary,
        "status": ticket.status,
        "resolution": ticket.resolution,
        "snippet": _snippet(ticket),
    }


def _full(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "created": ticket.created.isoformat(),
        "resolved": ticket.resolved.isoformat() if ticket.resolved else None,
        "status": ticket.status,
        "resolution": ticket.resolution,
        "summary": ticket.summary,
        "description": ticket.description,
        "labels": list(ticket.labels),
        "components": list(ticket.components),
        "comments": [
            {"author": c.author, "role": c.role, "created": c.created.isoformat(), "body": c.body}
            for c in ticket.comments
        ],
        "status_changes": [
            {"at": c.at.isoformat(), "from": c.from_status, "to": c.to_status}
            for c in ticket.status_changes
        ],
        "actions": [
            {"tool": a.tool, "args": dict(a.args), "at": a.at.isoformat()} for a in ticket.actions
        ],
    }


def horizon_of(world: WorldState) -> tuple[datetime | None, str | None]:
    if world.ticket is None:
        return None, None
    return datetime.fromisoformat(world.ticket.created), world.ticket.id


def build(world: WorldState, recorder: CallRecorder, corpus: Corpus | None) -> MockServer:
    server = MockServer(SERVER_NAME, INSTRUCTIONS, world, recorder)
    horizon, exclude = horizon_of(world)

    def available() -> list[Ticket]:
        if corpus is None or horizon is None:
            return []
        return corpus.visible(horizon, exclude)

    @server.read
    def knowledge_search_tickets(query: str, limit: int = 5) -> dict[str, Any]:
        """Search past tickets (created before the current one) by keywords. Returns the
        best matches with id, date, summary, status at the time and a snippet."""
        if corpus is None:
            return {"query": query, "results": [], "note": "no knowledge corpus is attached"}
        if horizon is None:
            raise ToolError("no ticket is attached to this environment, so there is no horizon")
        words = _words(query)
        ranked = sorted(
            ((score(t, words), t) for t in available()),
            key=lambda item: (-item[0], item[1].created, item[1].id),
        )
        hits = [t for s, t in ranked if s > 0][: max(1, min(int(limit), 25))]
        return {
            "query": query,
            "horizon": horizon.isoformat(),
            "results": [_summary_of(t) for t in hits],
        }

    @server.read
    def knowledge_get_ticket(ticket_id: str) -> dict[str, Any]:
        """Read one past ticket as it stood when the current ticket was raised."""
        if corpus is None:
            raise ToolError("no knowledge corpus is attached")
        if horizon is None:
            raise ToolError("no ticket is attached to this environment, so there is no horizon")
        found = corpus.by_id.get(ticket_id)
        if found is None or found.id == exclude or not found.created < horizon:
            raise ToolError(f"ticket '{ticket_id}' is not available")
        return _full(cut(found, horizon))

    return server
