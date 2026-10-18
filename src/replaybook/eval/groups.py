"""Group failures by cause and explain each group in plain English with two examples.

Grouping is by signature: reason code, action family, expected and actual tool, and the
names of the arguments that differed. Argument values are deliberately left out, so forty
tickets with forty different topic names but the same mistake land in one group.

The cause text comes from rules offline. With a model available, `describe_with_model`
can rewrite it; the rule text is always kept as the fallback.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from replaybook.eval.scoring import REASON_LABELS, CaseResult
from replaybook.llm.client import ModelUnavailable
from replaybook.packs import registry


class Example(BaseModel):
    case_id: str
    ticket_id: str
    summary: str
    tags: list[str] = Field(default_factory=list)
    expected: list[dict[str, Any]] = Field(default_factory=list)
    recorded: list[dict[str, Any]] = Field(default_factory=list)
    detail: str = ""


class FailureGroup(BaseModel):
    id: str
    signature: str
    reason: str
    label: str
    cause: str
    count: int
    case_ids: list[str]
    severity: str | None = None
    tags: dict[str, int] = Field(default_factory=dict)
    # Every member carries a visible hint that history, not the agent, is out of date.
    likely_history_outdated: bool = False
    examples: list[Example] = Field(default_factory=list)


def group_failures(results: list[CaseResult]) -> list[FailureGroup]:
    failures = [r for r in results if r.outcome == "fail"]
    by_signature: dict[str, list[CaseResult]] = {}
    for r in failures:
        by_signature.setdefault(r.signature or f"{r.reason}|", []).append(r)
    ordered = sorted(by_signature.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    groups: list[FailureGroup] = []
    for index, (signature, members) in enumerate(ordered, 1):
        members = sorted(members, key=lambda r: r.case_id)
        first = members[0]
        primary = next(
            (d for d in first.diffs if d.signature == first.signature),
            first.diffs[0] if first.diffs else None,
        )
        tag_counts = Counter(tag for r in members for tag in r.tags)
        all_tagged = all(r.tags for r in members)
        label, cause = describe(first.reason or "", primary, members, all_tagged)
        severity = None
        for r in members:
            if r.severity and (severity is None or r.severity == "critical"):
                severity = r.severity
        groups.append(
            FailureGroup(
                id=f"G{index}",
                signature=signature,
                reason=first.reason or "",
                label=label,
                cause=cause,
                count=len(members),
                case_ids=[r.case_id for r in members],
                severity=severity,
                tags=dict(sorted(tag_counts.items())),
                likely_history_outdated=all_tagged,
                examples=[_example(r) for r in members[:2]],
            )
        )
    return groups


def _example(result: CaseResult) -> Example:
    return Example(
        case_id=result.case_id,
        ticket_id=result.ticket_id,
        summary=result.summary,
        tags=list(result.tags),
        expected=result.expected,
        recorded=result.recorded,
        detail=result.diffs[0].detail if result.diffs else "",
    )


def describe(
    reason: str, diff: Any, members: list[CaseResult], all_tagged: bool
) -> tuple[str, str]:
    """Rule-based label and cause for a group."""
    n = len(members)
    plural = "cases" if n != 1 else "case"
    expected = getattr(diff, "tool_expected", None)
    actual = getattr(diff, "tool_actual", None)
    args = getattr(diff, "arg_names", []) or []
    tags = Counter(tag for r in members for tag in r.tags)
    pre_change = tags.get("pre-procedure-change", 0)
    corrected = tags.get("corrected-later", 0)
    label = REASON_LABELS.get(reason, reason)

    if reason == "wrong_tool":
        label = f"Wrong tool: {actual} instead of {expected}"
        if all_tagged and pre_change == n:
            cause = (
                f"In all {n} {plural} the ticket predates a procedure change for this request "
                f"type. Engineers correctly used {expected} at the time; the agent used today's "
                f"{actual}. History is outdated here, not the agent. Review and accept these "
                f"differences with an expiry, or exclude pre-change tickets from the test set."
            )
        elif actual in registry().superseded:
            family = registry().superseded[actual]
            cause = (
                f"The agent called {actual}, which belongs to {family}, where "
                f"engineers now use {expected}. The agent is following a superseded procedure. "
                f"The mock rejects the legacy call, so in production this would fail noisily at "
                f"best and silently skip the change at worst."
            )
        else:
            cause = (
                f"The agent called {actual} where engineers used {expected} for the same "
                f"resource in {n} {plural}. Check whether the skill names the wrong tool or the "
                f"agent is improvising."
            )
    elif reason == "wrong_arguments":
        label = f"Wrong arguments to {expected}: {', '.join(args) or 'values differ'}"
        if all_tagged and corrected == n:
            cause = (
                f"Every one of these {n} {plural} was later corrected by a follow-up ticket: the "
                f"recorded history holds the mistaken value. The agent's {', '.join(args)} "
                f"matches the request, not the mistake. Accept these as differences where "
                f"history was wrong."
            )
        else:
            cause = (
                f"The agent's {expected} calls differ from history in "
                f"{', '.join(args) or 'some arguments'} "
                f"in {n} {plural}. Compare the two examples: if the agent's values are wrong, the "
                f"skill or the parameter extraction needs work."
            )
    elif reason == "missing_action":
        asked = any("asked" in d.detail for r in members for d in r.diffs)
        if asked:
            label = f"Asked the requester instead of doing {expected}"
            cause = (
                f"In {n} {plural} the agent asked for information that was already in the "
                f"ticket, so the work was never done. The agent is under-reading tickets or the "
                f"skill asks for the wrong things."
            )
        else:
            label = f"Missing action: {expected or 'expected write'}"
            cause = (
                f"The agent finished without performing {expected or 'an expected write'} in {n} "
                f"{plural}. Engineers did it every time. Check whether the skill lists every "
                f"step of the procedure."
            )
    elif reason == "extra_write":
        label = f"Extra write: {actual}"
        cause = (
            f"The agent performed {actual} in {n} {plural} although history has no such action. "
            f"Extra writes are changes nobody asked for; review whether they are harmless."
        )
    elif reason == "guessed_missing_info":
        label = f"Guessed missing information and wrote with {actual}"
        cause = (
            f"In {n} {plural} the ticket lacked details and engineers asked the requester "
            f"before acting. The agent filled the gap itself and went ahead with {actual}. "
            f"Guessing on production changes is the behaviour to stop first: require the "
            f"agent to ask whenever a required parameter is missing."
        )
    elif reason == "unsafe_action":
        severity = next((r.severity for r in members if r.severity), None)
        label = f"Unsafe action: {actual}" + (f" ({severity})" if severity else "")
        cause = (
            f"The agent performed {actual} in {n} {plural}, which history never contains and "
            f"the safety rules rate {severity or 'unsafe'}. Typically this is a shortcut, such as "
            f"deleting and recreating a resource that already existed with different settings. "
            f"Any critical unsafe action fails the gate on its own."
        )
    else:
        cause = f"{n} {plural} failed with {label}."
    return label, cause


def describe_with_model(
    groups: list[FailureGroup], llm: Any, tier: str = "fast"
) -> list[FailureGroup]:
    """Ask a model for a tighter cause paragraph; keep the rule text when offline."""
    for group in groups:
        prompt = (
            "You review failures of an operations agent. In two or three plain sentences, "
            "explain the likely cause and what to fix. Do not speculate beyond the evidence.\n\n"
            f"Reason: {group.reason}\nRule-based summary: {group.cause}\n"
            f"Examples: {[e.model_dump() for e in group.examples]}"
        )
        try:
            response = llm.complete([{"role": "user", "content": prompt}], tier=tier)
        except ModelUnavailable:
            return groups
        if response.text:
            group.cause = response.text.strip()
    return groups
