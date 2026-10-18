"""Discovery on-ramp: which request types are worth testing first.

Groups past tickets into request types and ranks them by engineer-hours, so a team can
pick the skills to test before writing any. A ticket is typed from its recorded actions
when it has any, from its text otherwise; what neither can type lands in an "untyped"
bucket summarised by its most common words. Hours come from each ticket's effort figure
(logged time, time in working statuses or created-to-resolved, whichever the adapter was
configured with) and from an estimate from engineer activity otherwise; the report says
how much of each.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from statistics import median
from typing import Any

from pydantic import BaseModel, Field

from replaybook.eval.testset import type_of
from replaybook.packs import registry
from replaybook.schema.ticket import Ticket

HOURS_PER_ENGINEER_COMMENT = 0.25
HOURS_PER_STATUS_CHANGE = 0.1
HOURS_PER_ACTION = 0.25
BASE_HOURS = 0.25
ESTIMATE_CAP = 8.0
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "for",
    "from",
    "has",
    "have",
    "i",
    "in",
    "is",
    "it",
    "its",
    "me",
    "my",
    "no",
    "not",
    "of",
    "on",
    "or",
    "our",
    "please",
    "so",
    "that",
    "the",
    "this",
    "to",
    "us",
    "we",
    "when",
    "with",
    "you",
    "your",
    "need",
    "needs",
}


class TypeSummary(BaseModel):
    key: str
    label: str
    tickets: int
    hours: float
    logged_hours: float
    estimated_hours: float
    with_logged_time: int
    median_hours: float
    waited_on_requester: int
    share_of_hours: float
    skill_available: bool
    examples: list[dict[str, str]] = Field(default_factory=list)


class DiscoveryReport(BaseModel):
    tickets: int
    typed: int
    untyped: int
    total_hours: float
    untyped_hours: float
    # Share of hours that came from a recorded effort figure rather than the estimate.
    logged_share: float
    # Tickets per effort source (logged, status_time, created_to_resolved, estimate).
    effort_sources: dict[str, int] = Field(default_factory=dict)
    types: list[TypeSummary] = Field(default_factory=list)
    untyped_words: list[str] = Field(default_factory=list)
    untyped_examples: list[dict[str, str]] = Field(default_factory=list)
    recommendation: list[str] = Field(default_factory=list)
    method: dict[str, Any] = Field(default_factory=dict)


def estimate_hours(ticket: Ticket) -> tuple[float, bool]:
    """(hours, recorded). A recorded effort figure wins; otherwise engineer activity stands
    in for it."""
    if ticket.time_spent_hours is not None:
        return float(ticket.time_spent_hours), True
    engineer_comments = sum(c.role == "engineer" for c in ticket.comments)
    hours = (
        BASE_HOURS
        + HOURS_PER_ENGINEER_COMMENT * engineer_comments
        + HOURS_PER_STATUS_CHANGE * len(ticket.status_changes)
        + HOURS_PER_ACTION * len(ticket.actions)
    )
    return round(min(hours, ESTIMATE_CAP), 2), False


def skill_available(request_type: str) -> bool:
    """Whether a pack ships a sample SKILL.md for the request type."""
    return registry().skill_path(request_type) is not None


def common_words(texts: Iterable[str], limit: int = 12) -> list[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        words = {w for w in re.findall(r"[a-z][a-z0-9-]{2,}", text.lower()) if w not in STOPWORDS}
        counts.update(words)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [word for word, _ in ranked[:limit]]


def effort_source_of(ticket: Ticket) -> str:
    if ticket.time_spent_hours is None:
        return "estimate"
    return ticket.effort_source or "logged"


def discover(tickets: list[Ticket], top: int = 3) -> DiscoveryReport:
    by_type: dict[str, list[tuple[Ticket, float, bool]]] = {}
    untyped: list[tuple[Ticket, float, bool]] = []
    sources: Counter[str] = Counter()
    for ticket in tickets:
        hours, logged = estimate_hours(ticket)
        sources[effort_source_of(ticket)] += 1
        kind, _how = type_of(ticket)
        if kind is None:
            untyped.append((ticket, hours, logged))
        else:
            by_type.setdefault(kind, []).append((ticket, hours, logged))

    total_hours = sum(h for group in by_type.values() for _, h, _ in group) + sum(
        h for _, h, _ in untyped
    )
    logged_hours = sum(h for group in by_type.values() for _, h, lg in group if lg) + sum(
        h for _, h, lg in untyped if lg
    )
    summaries: list[TypeSummary] = []
    for key, group in by_type.items():
        hours = sum(h for _, h, _ in group)
        label = registry().label(key)
        newest = sorted(group, key=lambda item: item[0].created, reverse=True)[:3]
        summaries.append(
            TypeSummary(
                key=key,
                label=label,
                tickets=len(group),
                hours=round(hours, 2),
                logged_hours=round(sum(h for _, h, lg in group if lg), 2),
                estimated_hours=round(sum(h for _, h, lg in group if not lg), 2),
                with_logged_time=sum(lg for _, _, lg in group),
                median_hours=round(median(h for _, h, _ in group), 2),
                waited_on_requester=sum(
                    any(c.to_status == "Waiting on Requester" for c in t.status_changes)
                    for t, _, _ in group
                ),
                share_of_hours=round(hours / total_hours, 4) if total_hours else 0.0,
                skill_available=skill_available(key),
                examples=[{"id": t.id, "summary": t.summary[:90]} for t, _, _ in newest],
            )
        )
    summaries.sort(key=lambda s: (-s.hours, -s.tickets, s.key))
    recommendation = [s.key for s in summaries[:top]]
    newest_untyped = sorted(untyped, key=lambda item: item[0].created, reverse=True)[:5]
    return DiscoveryReport(
        tickets=len(tickets),
        typed=sum(len(g) for g in by_type.values()),
        untyped=len(untyped),
        total_hours=round(total_hours, 2),
        untyped_hours=round(sum(h for _, h, _ in untyped), 2),
        logged_share=round(logged_hours / total_hours, 4) if total_hours else 0.0,
        effort_sources=dict(sorted(sources.items())),
        types=summaries,
        untyped_words=common_words(f"{t.summary} {t.description}" for t, _, _ in untyped),
        untyped_examples=[{"id": t.id, "summary": t.summary[:90]} for t, _, _ in newest_untyped],
        recommendation=recommendation,
        method={
            "typing": "recorded actions first, then the rule-based text classifier",
            "hours": (
                f"the ticket's effort figure where present (sources: "
                f"{', '.join(f'{k} {v}' for k, v in sorted(sources.items()))}); otherwise "
                f"{BASE_HOURS} h + "
                f"{HOURS_PER_ENGINEER_COMMENT} h per engineer comment + "
                f"{HOURS_PER_STATUS_CHANGE} h per status change + {HOURS_PER_ACTION} h per "
                f"recorded action, capped at {ESTIMATE_CAP} h"
            ),
        },
    )


def render_markdown(report: DiscoveryReport) -> str:
    lines = [
        "# Discovery: request types ranked by engineer-hours",
        "",
        f"{report.tickets} tickets, {report.typed} typed, {report.untyped} untyped. "
        f"{report.total_hours} engineer-hours in total, {report.logged_share:.0%} of them from a "
        "recorded effort figure (the rest estimated from engineer activity). Effort sources: "
        + ", ".join(f"{k} {v}" for k, v in report.effort_sources.items())
        + ".",
        "",
        "| Rank | Request type | Tickets | Hours | Share | Logged | Median h | Waited | Skill |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for rank, s in enumerate(report.types, start=1):
        lines.append(
            f"| {rank} | {s.label} (`{s.key}`) | {s.tickets} | {s.hours} | "
            f"{s.share_of_hours:.0%} | {s.with_logged_time} | {s.median_hours} | "
            f"{s.waited_on_requester} | "
            f"{'yes' if s.skill_available else 'no'} |"
        )
    lines += [
        "",
        "## Test first",
        "",
    ]
    for key in report.recommendation:
        s = next(t for t in report.types if t.key == key)
        skill = "reference SKILL.md available" if s.skill_available else "no reference skill yet"
        lines.append(f"- **{s.label}**: {s.hours} h over {s.tickets} tickets; {skill}.")
    if report.untyped:
        lines += [
            "",
            "## Untyped tickets",
            "",
            f"{report.untyped} tickets matched no request type. Most common words: "
            + ", ".join(report.untyped_words)
            + ".",
            "",
        ]
        lines += [f"- {e['id']}: {e['summary']}" for e in report.untyped_examples]
    lines += [
        "",
        "## Method",
        "",
        f"- Typing: {report.method['typing']}.",
        f"- Hours: {report.method['hours']}.",
    ]
    return "\n".join(lines) + "\n"


def write_report(report: DiscoveryReport, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "discovery.json"
    md_path = out_dir / "discovery.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
