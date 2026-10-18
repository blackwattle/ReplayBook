"""Effort per ticket, from the source an adapter is configured with.

Every adapter says where effort comes from, because discovery ranks request types by
engineer-hours and the number means something different depending on the source:

- logged: time the engineer recorded on the ticket (the best figure, often missing)
- status_time: time the ticket spent in working statuses (needs status-change history)
- created_to_resolved: wall-clock time from creation to resolution (always available,
  counts waiting time as work)

A ticket whose source yields nothing keeps `time_spent_hours=None`, and discovery falls
back to its activity estimate for that ticket.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, get_args

from replaybook.schema.ticket import Ticket

EffortChoice = Literal["logged", "status_time", "created_to_resolved"]
EFFORT_SOURCES: tuple[str, ...] = get_args(EffortChoice)
WORKING_STATUSES: tuple[str, ...] = ("In Progress", "Reopened")
DESCRIPTIONS = {
    "logged": "time the engineer logged on the ticket",
    "status_time": "time the ticket spent in working statuses",
    "created_to_resolved": "wall-clock time from creation to resolution (includes waiting)",
}


def check_source(source: str) -> EffortChoice:
    if source not in EFFORT_SOURCES:
        raise ValueError(
            f"effort source must be one of {', '.join(EFFORT_SOURCES)}, not '{source}'"
        )
    return source  # type: ignore[return-value]


def status_time_hours(ticket: Ticket, working: Iterable[str] = WORKING_STATUSES) -> float | None:
    """Hours spent in working statuses, from the status-change history. None without one."""
    changes = sorted(ticket.status_changes, key=lambda c: c.at)
    if not changes:
        return None
    working_set = set(working)
    total = 0.0
    for current, following in zip(changes, changes[1:], strict=False):
        if current.to_status in working_set:
            total += (following.at - current.at).total_seconds() / 3600
    last = changes[-1]
    if last.to_status in working_set and ticket.resolved is not None and ticket.resolved > last.at:
        total += (ticket.resolved - last.at).total_seconds() / 3600
    return round(total, 2)


def created_to_resolved_hours(ticket: Ticket) -> float | None:
    if ticket.resolved is None or ticket.resolved < ticket.created:
        return None
    return round((ticket.resolved - ticket.created).total_seconds() / 3600, 2)


def apply_effort(ticket: Ticket, source: str, working: Iterable[str] = WORKING_STATUSES) -> Ticket:
    """Return the ticket with time_spent_hours and effort_source set from `source`."""
    choice = check_source(source)
    if choice == "logged":
        if ticket.time_spent_hours is None:
            return ticket.model_copy(update={"effort_source": None})
        return ticket.model_copy(update={"effort_source": "logged"})
    if choice == "status_time":
        hours = status_time_hours(ticket, working)
    else:
        hours = created_to_resolved_hours(ticket)
    if hours is None:
        return ticket.model_copy(update={"time_spent_hours": None, "effort_source": None})
    return ticket.model_copy(update={"time_spent_hours": hours, "effort_source": choice})


def apply_effort_all(
    tickets: list[Ticket], source: str, working: Iterable[str] = WORKING_STATUSES
) -> list[Ticket]:
    return [apply_effort(t, source, working) for t in tickets]


def describe(source: str, working: Iterable[str] = WORKING_STATUSES) -> str:
    """One line for a data card."""
    text = DESCRIPTIONS.get(source, source)
    if source == "status_time":
        text += f" ({', '.join(working)})"
    return f"{source}: {text}"
