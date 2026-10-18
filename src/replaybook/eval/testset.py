"""Build test sets from history: the newest tickets of a request type.

Only visible data is used. The request type comes from the recorded engineer actions when
there are any (an audit log is more reliable than prose) and from the text classifier
otherwise. A ticket where the engineer asked the requester before doing anything, and the
ticket waited on the requester, becomes an "ask" case: the agent should ask too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from replaybook.classify import classify
from replaybook.eval.extract import Extractor
from replaybook.mock_env.world import WorldState
from replaybook.packs import registry
from replaybook.schema.ground_truth import ActionSpec
from replaybook.schema.testcase import Expectation, TestCase, TestSet
from replaybook.schema.ticket import Ticket

WAITING_STATUS = "Waiting on Requester"
# A question posted up to this long after the status change still counts as the reason.
ASK_GRACE = timedelta(days=1)


def type_of(ticket: Ticket) -> tuple[str | None, str]:
    """(request type, how we know): 'actions' from recorded tools, else 'text'."""
    tool_types = registry().tool_types
    from_actions = {tool_types.get(a.tool) for a in ticket.actions}
    from_actions.discard(None)
    if len(from_actions) == 1:
        return from_actions.pop(), "actions"
    return classify(ticket)[0], "text"


def expects_ask(ticket: Ticket) -> bool:
    """Engineers asked for more information before acting, and the ticket waited for it.

    With an audit log the wait has to precede the first recorded action. Without one (real
    history) a wait is not enough: tickets also wait on the requester after the work, for
    "please verify" and "can this be closed", so an engineer must have asked a question
    (a comment with a question mark) by the time the wait began.
    """
    waited = [c.at for c in ticket.status_changes if c.to_status == WAITING_STATUS]
    if not waited:
        return False
    first_wait = min(waited)
    if ticket.actions:
        return first_wait < min(a.at for a in ticket.actions)
    return any(
        c.role == "engineer" and "?" in c.body and c.created <= first_wait + ASK_GRACE
        for c in ticket.comments
    )


def visible_tags(
    ticket: Ticket, all_tickets: dict[str, Ticket], platform: dict[str, Any], request_type: str
) -> list[str]:
    tags: list[str] = []
    for change in platform.get("procedure_changes", []):
        if change.get("request_type") == request_type:
            at = datetime.fromisoformat(str(change["at"]))
            if at.tzinfo is None:
                at = at.replace(tzinfo=UTC)
            if ticket.created < at:
                tags.append("pre-procedure-change")
    for link in ticket.links:
        other = all_tickets.get(link.target)
        if other and any(x.type == "fixes" and x.target == ticket.id for x in other.links):
            tags.append("corrected-later")
    return sorted(set(tags))


def build_test_set(
    tickets: list[Ticket],
    platform: dict[str, Any],
    request_type: str,
    n: int = 40,
    name: str | None = None,
    source: str = "synthetic",
    data_dir: str = "",
    extractor: Extractor | None = None,
) -> TestSet:
    """With an extractor, tickets without recorded actions (real history) get expectations
    extracted from their text, labelled with the extractor's name and unverified."""
    by_id = {t.id: t for t in tickets}
    excluded: dict[str, int] = {}
    candidates: list[tuple[Ticket, str, Expectation]] = []
    for ticket in tickets:
        kind, how = type_of(ticket)
        if kind != request_type:
            continue
        reason = exclusion_reason(ticket, allow_unrecorded=extractor is not None)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        expectation = expectation_for(ticket, request_type, extractor)
        if expectation is None:
            reason = "no extractable expectation"
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        if not ticket.inventory and extractor is not None:
            # Real tickets carry no inventory snapshot. Seed the world with the things the
            # text names (the certificate, zone or group), for ask cases too, so an agent
            # that asks does so because information is missing, not because the mock is
            # empty. The seed is recorded on the case for review.
            found = extractor.extract(ticket, request_type)
            seed = seed_inventory(found.actions if found else [])
            if seed:
                ticket = ticket.model_copy(update={"inventory": seed})
        candidates.append((ticket, how, expectation))

    candidates.sort(key=lambda item: (item[0].created, item[0].id), reverse=True)
    chosen = candidates[:n]
    cases: list[TestCase] = []
    for ticket, _how, expectation in chosen:
        cases.append(
            TestCase(
                id=ticket.id,
                ticket_id=ticket.id,
                request_type=request_type,
                created=ticket.created,
                summary=ticket.summary,
                world=WorldState.from_ticket(ticket, platform),
                expectation=expectation,
                tags=visible_tags(ticket, by_id, platform, request_type),
            )
        )
    cases.sort(key=lambda c: c.id)
    return TestSet(
        name=name or request_type,
        request_type=request_type,
        source=source,
        data_dir=data_dir,
        created_at=datetime(2000, 1, 1, tzinfo=UTC) if not cases else max(c.created for c in cases),
        selection={
            "method": "newest tickets of the request type",
            "requested": n,
            "selected": len(cases),
            "eligible": len(candidates),
            "typed_by": {
                "actions": sum(how == "actions" for _, how, _e in chosen),
                "text": sum(how == "text" for _, how, _e in chosen),
            },
            "unverified": sum(not c.expectation.verified for c in cases),
            "expectation_sources": _count(c.expectation.source for c in cases),
            "excluded": dict(sorted(excluded.items())),
            "date_range": {
                "oldest": min((c.created for c in cases), default=None),
                "newest": max((c.created for c in cases), default=None),
            },
            "ask_cases": sum(c.expectation.kind == "ask" for c in cases),
            "tagged": {
                tag: sum(tag in c.tags for c in cases)
                for tag in ("pre-procedure-change", "corrected-later")
            },
        },
        cases=cases,
    )


def expectation_for(
    ticket: Ticket, request_type: str, extractor: Extractor | None
) -> Expectation | None:
    if expects_ask(ticket):
        return Expectation(kind="ask", notes="engineers asked the requester first")
    if ticket.actions:
        return Expectation(
            kind="actions",
            actions=[ActionSpec(tool=a.tool, args=dict(a.args)) for a in ticket.actions],
        )
    if extractor is None:
        return None
    found = extractor.extract(ticket, request_type)
    if found is None or not found.actions:
        return None
    return Expectation(
        kind="actions",
        actions=found.actions,
        source=found.source,
        verified=False,
        notes=found.notes,
    )


def seed_inventory(actions: list[ActionSpec]) -> dict[str, Any]:
    """A minimal inventory in which the named targets exist, from expected actions. Each
    pack seeds the targets its own tools name."""
    return registry().seed_inventory(actions)


def _count(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def exclusion_reason(ticket: Ticket, allow_unrecorded: bool = False) -> str | None:
    if ticket.resolution == "Duplicate":
        return "duplicate"
    if ticket.status not in ("Resolved", "Closed"):
        return "not resolved"
    if ticket.resolution not in (None, "Done", "Fixed"):
        return f"resolution {ticket.resolution}"
    if any(link.type == "fixes" for link in ticket.links):
        return "follow-up correction ticket"
    if not ticket.actions and not allow_unrecorded:
        return "no recorded actions"
    return None
