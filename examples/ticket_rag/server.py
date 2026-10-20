"""A fake ticket-search MCP server: the stand-in for a team's own RAG over past tickets.

It is written with the MCP SDK and the standard library only, imports nothing from
Replaybook, and searches the tickets.jsonl of a dataset directory (for example one written
by `replaybook generate`) with a small TF-IDF ranking. Like many real retrieval servers it
knows nothing about replay: it returns every matching ticket whenever it was created,
with its resolution, all its comments and how many days old it is. The knowledge pack in
examples/ticket_rag/pack/ puts it behind Replaybook's time-travel guard.

    python examples/ticket_rag/server.py --data data/generated              # stdio
    python examples/ticket_rag/server.py --data data/generated --http 8770  # streamable HTTP

`created_before` is optional, the way an "as of" filter is in a real index; the pack can
pass the horizon through it, and the guard still checks every result itself.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

WORD = re.compile(r"[a-z0-9][a-z0-9._/-]{1,}")
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def _words(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _when(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Index:
    """Tickets as plain dicts with a TF-IDF vector over summary, description and comments."""

    def __init__(self, tickets: list[dict[str, Any]]) -> None:
        self.tickets = {t["id"]: t for t in tickets}
        docs = {t["id"]: Counter(_words(self._text(t))) for t in tickets}
        frequency = Counter(word for doc in docs.values() for word in doc)
        total = max(1, len(docs))
        self.idf = {w: math.log((1 + total) / (1 + n)) + 1 for w, n in frequency.items()}
        self.vectors = {key: self._vector(doc) for key, doc in docs.items()}

    @staticmethod
    def _text(ticket: dict[str, Any]) -> str:
        comments = " ".join(c.get("body", "") for c in ticket.get("comments") or [])
        return f"{ticket['summary']} {ticket['summary']} {ticket['description']} {comments}"

    def _vector(self, counts: Counter[str]) -> dict[str, float]:
        vector = {w: n * self.idf.get(w, 1.0) for w, n in counts.items()}
        norm = math.sqrt(sum(v * v for v in vector.values())) or 1.0
        return {w: v / norm for w, v in vector.items()}

    def rank(
        self, vector: dict[str, float], top_k: int, before: datetime | None, exclude: str | None
    ) -> list[tuple[float, dict[str, Any]]]:
        scored = []
        for key, other in self.vectors.items():
            ticket = self.tickets[key]
            if key == exclude or (before is not None and _when(ticket["created"]) >= before):
                continue
            score = sum(v * other.get(w, 0.0) for w, v in vector.items())
            if score > 0:
                scored.append((score, ticket))
        scored.sort(key=lambda item: (-item[0], item[1]["id"]))
        return scored[:top_k]


def _summary(score: float, ticket: dict[str, Any]) -> dict[str, Any]:
    created = _when(ticket["created"])
    return {
        "key": ticket["id"],
        "summary": ticket["summary"],
        "created": ticket["created"],
        "resolved": ticket.get("resolved"),
        "status": ticket.get("status"),
        "resolution_note": ticket.get("resolution"),
        "age_days": (datetime.now(UTC) - created).days,
        "score": round(score, 4),
    }


def _before(created_before: str | None) -> datetime | None:
    return _when(created_before) if created_before else None


def build(tickets: list[dict[str, Any]]) -> MCPServer:
    index = Index(tickets)
    server = MCPServer(
        name="ticket-rag",
        instructions="Search the team's past tickets: how was this solved before?",
    )

    @server.tool(annotations=READ)
    def search_tickets(
        query: str, top_k: int = 5, created_before: str | None = None
    ) -> dict[str, Any]:
        """Find past tickets similar to a problem description, best match first.

        query           the problem, an error message or keywords
        top_k           how many tickets to return, 1-20 (default 5)
        created_before  optional ISO 8601 time: only tickets created before it
        """
        top_k = max(1, min(int(top_k), 20))
        vector = index._vector(Counter(_words(query)))
        hits = index.rank(vector, top_k, _before(created_before), None)
        return {"ok": True, "count": len(hits), "results": [_summary(s, t) for s, t in hits]}

    @server.tool(annotations=READ)
    def find_similar_tickets(ticket_key: str, top_k: int = 5) -> dict[str, Any]:
        """Tickets similar to an existing ticket (the ticket itself is left out)."""
        if ticket_key not in index.tickets:
            return {"ok": False, "error": f"{ticket_key} is not in the index"}
        top_k = max(1, min(int(top_k), 20))
        hits = index.rank(index.vectors[ticket_key], top_k, None, ticket_key)
        return {"ok": True, "count": len(hits), "results": [_summary(s, t) for s, t in hits]}

    @server.tool(annotations=READ)
    def get_ticket(ticket_key: str) -> dict[str, Any]:
        """One past ticket in full: description, comments, resolution and links."""
        ticket = index.tickets.get(ticket_key)
        if ticket is None:
            return {"ok": False, "error": f"{ticket_key} is not in the index"}
        return {
            "ok": True,
            "ticket": {
                "key": ticket["id"],
                "summary": ticket["summary"],
                "description": ticket["description"],
                "created": ticket["created"],
                "resolved": ticket.get("resolved"),
                "status": ticket.get("status"),
                "resolution_note": ticket.get("resolution"),
                "comments": [
                    {"at": c["created"], "author": c.get("author"), "body": c.get("body", "")}
                    for c in ticket.get("comments") or []
                ],
                "links": ticket.get("links") or [],
            },
        }

    return server


def load(data_dir: Path) -> list[dict[str, Any]]:
    path = Path(data_dir) / "tickets.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=os.environ.get("TICKET_RAG_DATA", "data/generated"))
    parser.add_argument("--http", type=int, default=0, help="serve over HTTP on this port")
    options = parser.parse_args()
    server = build(load(Path(options.data)))
    if options.http:
        server.run("streamable-http", port=options.http)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
