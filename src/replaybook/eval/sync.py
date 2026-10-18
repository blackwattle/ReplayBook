"""`replaybook sync`: add newly resolved tickets to an existing test set.

A test set is a snapshot of history. As engineers resolve more tickets of its request
type, sync appends them as new cases, built the same way as the originals, and leaves
every existing case untouched (same ids, worlds and expectations), so baselines and
accepted differences still apply to them. The set remembers how far it has synced
(`selection["synced_through"]`, the latest resolution time it has seen) and every sync
run is recorded under `selection["sync"]` with what was added and why anything was
skipped. New cases show up as NEW in `replaybook check`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from replaybook.eval.extract import Extractor
from replaybook.eval.testset import build_test_set, exclusion_reason, type_of
from replaybook.schema.testcase import TestSet
from replaybook.schema.ticket import Ticket


def synced_through(test_set: TestSet) -> datetime | None:
    """The latest resolution time the set has seen, from the selection or its cases."""
    recorded = test_set.selection.get("synced_through")
    if recorded:
        at = datetime.fromisoformat(str(recorded))
        return at if at.tzinfo else at.replace(tzinfo=UTC)
    if not test_set.cases:
        return None
    return max(c.created for c in test_set.cases)


def sync_test_set(
    test_set: TestSet,
    tickets: list[Ticket],
    platform: dict[str, Any],
    extractor: Extractor | None = None,
    max_new: int | None = None,
    now: datetime | None = None,
    data_dir: str = "",
) -> tuple[TestSet, dict[str, Any]]:
    """Return the updated set and the sync entry that was recorded in it."""
    now = now or datetime.now(UTC)
    through = synced_through(test_set)
    known = {c.id for c in test_set.cases}
    # Tickets of this type that resolved after the watermark and are not in the set yet.
    fresh: list[Ticket] = []
    for ticket in tickets:
        if ticket.id in known or ticket.resolved is None:
            continue
        if through is not None and ticket.resolved <= through:
            continue
        if type_of(ticket)[0] != test_set.request_type:
            continue
        fresh.append(ticket)
    fresh.sort(key=lambda t: (t.resolved, t.id))  # type: ignore[arg-type,return-value]

    skipped: dict[str, int] = {}
    eligible: list[Ticket] = []
    for ticket in fresh:
        reason = exclusion_reason(ticket, allow_unrecorded=extractor is not None)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            eligible.append(ticket)
    if max_new is not None and len(eligible) > max_new:
        skipped["beyond --max"] = len(eligible) - max_new
        eligible = eligible[:max_new]

    # Build the new cases with the same builder, on the fresh tickets plus the whole
    # corpus (links and procedure tags need it), then keep only the fresh ones.
    built = build_test_set(
        tickets,
        platform,
        test_set.request_type,
        n=len(tickets) + 1,
        name=test_set.name,
        source=test_set.source,
        data_dir=data_dir or test_set.data_dir,
        extractor=extractor,
    )
    wanted = {t.id for t in eligible}
    new_cases = [c for c in built.cases if c.id in wanted]
    missing = wanted - {c.id for c in new_cases}
    if missing:
        skipped["no extractable expectation"] = skipped.get("no extractable expectation", 0) + len(
            missing
        )

    latest_resolved = max((t.resolved for t in fresh if t.resolved), default=through)
    entry = {
        "at": now.isoformat(),
        "data_dir": data_dir or test_set.data_dir,
        "through_before": through.isoformat() if through else None,
        "through_after": latest_resolved.isoformat() if latest_resolved else None,
        "candidates": len(fresh),
        "added": [c.id for c in new_cases],
        "skipped": dict(sorted(skipped.items())),
        "extractor": extractor.name if extractor else "none",
    }
    cases = sorted([*test_set.cases, *new_cases], key=lambda c: c.id)
    selection = dict(test_set.selection)
    selection["selected"] = len(cases)
    selection["synced_through"] = entry["through_after"]
    selection["sync"] = [*selection.get("sync", []), entry]
    selection["ask_cases"] = sum(c.expectation.kind == "ask" for c in cases)
    selection["unverified"] = sum(not c.expectation.verified for c in cases)
    updated = test_set.model_copy(
        update={
            "cases": cases,
            "selection": selection,
            "created_at": max((c.created for c in cases), default=test_set.created_at),
        }
    )
    return updated, entry
