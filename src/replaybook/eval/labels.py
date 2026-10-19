"""Expected outcomes: which past tickets should the agent hand back to a person?

Every test case expects one of two outcomes. `act` means do the work, which includes
asking the requester when information is missing. `escalate` means the engineers' handling
needed a person's judgement, so the agent should call `ticket_escalate` and change nothing.

The rule (docs/replay-outcomes.md) labels a case escalate when the history shows any of:

- recorded engineer actions with a tool the request type's skill does not list in
  `tools:` (a superseded tool for a job the skill does list, such as the pre-migration
  tool, does not count: that is outdated history, not judgement);
- recorded actions beyond the type's usual pattern: a tool family used in fewer than 5% of
  the type's tickets with recorded actions, or more actions than 90% of those tickets
  have (only once the type has at least 20 such tickets);
- a comment by anyone but the requester that mentions an approval, an escalation or a
  second opinion (vendor-neutral keywords, replaceable per request type in pack.yaml under
  `escalation: {keywords: [...]}`);
- a discussion: at least two different engineers commented before the work was done
  (`escalation: {discussion_engineers: N}` in pack.yaml changes the number, 0 turns it
  off).

The requester is the ticket's reporter as well as anyone the adapter gave the requester
role: an adapter that learns its engineers across a whole project (anyone who ever moved
an issue) makes a reporter who closes their own tickets an "engineer" on their request.

Rule labels stay unverified until a person spot-checks them: `replaybook labels check`
records who checked which cases when, and which labels they changed, in
`assurance/labels.yaml`. A spot-check covers at least 10 cases or 10% of the set,
whichever is larger (all of a smaller set), and the go-live gate needs one that still
covers that many of the set's current labels. A spot-check that changed more than one in
ten of the labels it covered says the rule does not fit the set: it does not count, nor
does any check before it, and the gate names the parts of the rule behind the changed
labels until a fresh sample (cases no earlier check covered come first) is checked.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from replaybook.packs import PackRegistry, registry
from replaybook.schema.testcase import Expectation, ExpectedOutcome, TestSet
from replaybook.schema.ticket import Ticket
from replaybook.skills import SkillError, find_skill

DEFAULT_PATH = Path("assurance/labels.yaml")
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "approval",
    "approve",
    "approved",
    "sign-off",
    "sign off",
    "signed off",
    "change advisory",
    "CAB",
    "escalate",
    "escalated",
    "escalating",
    "escalation",
    "second opinion",
)
DISCUSSION_ENGINEERS = 2
# The parts of the rule, as the test-set selection counts them.
RULE_PARTS: dict[str, str] = {
    "skill_tools": "a tool the skill does not list",
    "usual_pattern": "beyond the type's usual pattern",
    "keywords": "an approval, an escalation or a second opinion in a comment",
    "discussion": "a discussion between engineers",
}
RARE_SHARE = 0.05
USUAL_SHARE = 0.90
MIN_PATTERN_TICKETS = 20
MIN_SAMPLE = 10
SAMPLE_SHARE = 0.10
LABELS: tuple[str, ...] = ("act", "escalate")


# --- the labelling rule ---------------------------------------------------------------


@dataclass
class UsualPattern:
    """How a request type is usually handled, from its tickets with recorded actions."""

    tickets: int = 0
    family_tickets: dict[str, int] = field(default_factory=dict)
    max_actions: int = 0

    @property
    def reliable(self) -> bool:
        return self.tickets >= MIN_PATTERN_TICKETS

    def share(self, family: str) -> float:
        return self.family_tickets.get(family, 0) / self.tickets if self.tickets else 0.0


def family_of(tool: str, packs: PackRegistry | None = None) -> str:
    rule = (packs if packs is not None else registry()).rules.get(tool)
    return rule.family if rule is not None else tool


def usual_pattern(tickets: list[Ticket], packs: PackRegistry | None = None) -> UsualPattern:
    """The usual pattern of one request type's tickets (those with recorded actions)."""
    with_actions = [t for t in tickets if t.actions]
    pattern = UsualPattern(tickets=len(with_actions))
    counts = sorted(len(t.actions) for t in with_actions)
    if counts:
        pattern.max_actions = counts[max(0, math.ceil(USUAL_SHARE * len(counts)) - 1)]
    for ticket in with_actions:
        for family in {family_of(a.tool, packs) for a in ticket.actions}:
            pattern.family_tickets[family] = pattern.family_tickets.get(family, 0) + 1
    return pattern


def judgement_reasons(
    ticket: Ticket,
    request_type: str,
    pattern: UsualPattern | None = None,
    packs: PackRegistry | None = None,
) -> list[str]:
    """Why the history says this ticket needed a person's judgement; empty when it did not."""
    return [text for _kind, text in judgement_findings(ticket, request_type, pattern, packs)]


def judgement_findings(
    ticket: Ticket,
    request_type: str,
    pattern: UsualPattern | None = None,
    packs: PackRegistry | None = None,
) -> list[tuple[str, str]]:
    """(rule part, reason) for every part of the rule the ticket meets. The parts are the
    keys of RULE_PARTS."""
    reg = packs if packs is not None else registry()
    info = reg.request_types.get(request_type)
    keywords = DEFAULT_KEYWORDS
    discussion = DISCUSSION_ENGINEERS
    if info is not None and info.escalation_keywords is not None:
        keywords = info.escalation_keywords
    if info is not None and info.discussion_engineers is not None:
        discussion = info.discussion_engineers
    found: list[tuple[str, str]] = []

    try:
        skill = find_skill(request_type, reg)
    except SkillError:
        skill = None
    if skill is not None and skill.tools and ticket.actions:
        listed = set(skill.tools)
        families = {family_of(t, reg) for t in listed}

        def outdated(tool: str) -> bool:
            return tool in reg.superseded and family_of(tool, reg) in families

        outside = sorted(
            {a.tool for a in ticket.actions if a.tool not in listed and not outdated(a.tool)}
        )
        if outside:
            found.append(
                (
                    "skill_tools",
                    f"engineers used {', '.join(outside)}, which the skill {skill.name} does "
                    "not list",
                )
            )

    if pattern is not None and pattern.reliable and ticket.actions:
        for family in sorted({family_of(a.tool, reg) for a in ticket.actions}):
            if pattern.share(family) < RARE_SHARE:
                found.append(
                    (
                        "usual_pattern",
                        f"{family} is rare for this request type "
                        f"({pattern.family_tickets.get(family, 0)} of {pattern.tickets} tickets)",
                    )
                )
        if len(ticket.actions) > pattern.max_actions:
            found.append(
                (
                    "usual_pattern",
                    f"{len(ticket.actions)} recorded actions where this request type usually "
                    f"has at most {pattern.max_actions}",
                )
            )

    comments = sorted(ticket.comments, key=lambda c: c.created)
    team = [c for c in comments if c.role != "requester" and c.author != ticket.reporter]
    for comment in team:
        word = _first_keyword(keywords, comment.body)
        if word:
            snippet = " ".join(comment.body.split())
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "..."
            found.append(
                ("keywords", f"a comment by {comment.author} mentions '{word}': \"{snippet}\"")
            )
            break

    if discussion > 0:
        if ticket.actions:
            done_at = min(a.at for a in ticket.actions)
        else:
            done_at = ticket.resolved or max((c.created for c in comments), default=ticket.created)
        engineers: list[str] = []
        for comment in team:
            if comment.role == "engineer" and comment.created <= done_at:
                if comment.author not in engineers:
                    engineers.append(comment.author)
        if len(engineers) >= discussion:
            found.append(
                (
                    "discussion",
                    f"{len(engineers)} engineers ({', '.join(engineers)}) discussed it before "
                    "the work was done",
                )
            )
    return found


def label(
    expectation: Expectation,
    ticket: Ticket,
    request_type: str,
    pattern: UsualPattern | None = None,
    packs: PackRegistry | None = None,
) -> Expectation:
    """The expectation with its expected outcome set by the rule, the reasons it found and
    the parts of the rule they came from."""
    findings = judgement_findings(ticket, request_type, pattern, packs)
    parts = {part for part, _text in findings}
    return expectation.model_copy(
        update={
            "outcome": "escalate" if findings else "act",
            "outcome_reasons": [text for _part, text in findings],
            "outcome_parts": [part for part in RULE_PARTS if part in parts],
            "outcome_source": "rule",
        }
    )


def _first_keyword(keywords: tuple[str, ...], text: str) -> str | None:
    for word in keywords:
        if re.search(r"(?<![\w-])" + re.escape(word) + r"(?![\w-])", text, re.IGNORECASE):
            return word
    return None


# --- the spot-check -------------------------------------------------------------------


def required_sample(cases: int) -> int:
    """At least 10 cases or 10% of the set, whichever is larger; all of a smaller set."""
    return min(cases, max(MIN_SAMPLE, math.ceil(SAMPLE_SHARE * cases)))


def too_many_changed(changed: int, covered: int) -> bool:
    """A spot-check changed more than one in ten of the labels it covered: the rule does
    not fit the set, so the check does not count for the go-live gate."""
    return changed * 10 > covered


class LabelChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    was: ExpectedOutcome
    now: ExpectedOutcome
    note: str = ""
    # Where the old label came from: the rule, with the parts of it that labelled the case
    # escalate (none when the rule labelled it act), or an earlier spot-check.
    was_source: Literal["rule", "manual"] = "rule"
    rule_parts: list[str] = Field(default_factory=list)


class LabelCheck(BaseModel):
    """One person's spot-check of a test set's labels."""

    model_config = ConfigDict(extra="forbid")

    id: str
    test_set: str
    reviewer: str
    checked_at: datetime
    cases_in_set: int
    required: int
    # case id -> the label the reviewer confirmed or set
    labels: dict[str, ExpectedOutcome]
    changed: list[LabelChange] = Field(default_factory=list)
    # The test set's hash after the changes were applied.
    test_set_sha256: str = ""

    @property
    def counts(self) -> bool:
        """False when the check changed more than one in ten of the labels it covered."""
        return not too_many_changed(len(self.changed), len(self.labels))


class LabelChecks(BaseModel):
    checks: list[LabelCheck] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> LabelChecks:
        path = Path(path)
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def save(self, path: Path = DEFAULT_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Spot-checks of the expected outcomes (act or escalate) on Replaybook test sets.\n"
            "# Each entry says who checked which cases when and which labels they changed.\n"
            "# Written by `replaybook labels check`; the go-live gate reads it.\n"
        )
        body = yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        path.write_text(header + body, encoding="utf-8")

    def next_id(self) -> str:
        numbers = [int(c.id.split("-")[-1]) for c in self.checks if c.id.split("-")[-1].isdigit()]
        return f"LBL-{max(numbers, default=0) + 1}"

    def for_set(self, name: str) -> list[LabelCheck]:
        return sorted((c for c in self.checks if c.test_set == name), key=lambda c: c.checked_at)


def suggest_sample(test_set: TestSet, store: LabelChecks | None = None) -> list[str]:
    """The cases to spot-check: the required number, half of them (or as many as there
    are) escalate cases, the rest act cases, in an order fixed by the set's name and the
    case ids so that the same set always gives the same sample. With the store, cases an
    earlier check of the set covered come last, so a new check gets a fresh sample where
    the set has enough cases."""
    checked: set[str] = set()
    for earlier in store.for_set(test_set.name) if store is not None else []:
        checked.update(earlier.labels)

    def order(case_id: str) -> tuple[bool, str]:
        digest = hashlib.sha256(f"{test_set.name}:{case_id}".encode()).hexdigest()
        return case_id in checked, digest

    escalate = sorted(
        (c.id for c in test_set.cases if c.expectation.outcome == "escalate"), key=order
    )
    act = sorted((c.id for c in test_set.cases if c.expectation.outcome != "escalate"), key=order)
    need = required_sample(len(test_set.cases))
    n_escalate = min(len(escalate), max(math.ceil(need / 2), need - len(act)))
    chosen = escalate[:n_escalate] + act[: need - n_escalate]
    return sorted(chosen)


def record_check(
    test_set: TestSet,
    store: LabelChecks,
    reviewer: str,
    case_ids: list[str] | None = None,
    changes: dict[str, str] | None = None,
    note: str = "",
    now: datetime | None = None,
) -> tuple[TestSet, LabelCheck]:
    """Record a person's spot-check: the cases they looked at (default: the suggested
    sample) and the labels they changed. Returns the test set with those changes applied
    and the entry added to the store; the caller saves both."""
    reviewer = reviewer.strip()
    if not reviewer:
        raise ValueError("a spot-check needs the name of the person who did it (--reviewer)")
    changes = dict(changes or {})
    by_id = {c.id: c for c in test_set.cases}
    ids = list(dict.fromkeys(case_ids or suggest_sample(test_set, store)))
    unknown = sorted({*ids, *changes} - set(by_id))
    if unknown:
        raise ValueError(f"not in test set {test_set.name}: {', '.join(unknown)}")
    for case_id, value in changes.items():
        if value not in LABELS:
            raise ValueError(f"{case_id}: a label is act or escalate, not '{value}'")
        if case_id not in ids:
            ids.append(case_id)
    need = required_sample(len(test_set.cases))
    if len(ids) < need:
        raise ValueError(
            f"a spot-check covers at least {need} of the {len(test_set.cases)} cases "
            f"(10 or 10%, whichever is larger); {len(ids)} given"
        )
    now = now or datetime.now(UTC)
    entry_id = store.next_id()
    changed: list[LabelChange] = []
    cases = []
    for case in test_set.cases:
        new = changes.get(case.id)
        old = case.expectation
        if new is not None and new != old.outcome:
            text = note or f"set to {new} by {reviewer} in spot-check {entry_id}"
            changed.append(
                LabelChange.model_validate(
                    {
                        "case_id": case.id,
                        "was": old.outcome,
                        "now": new,
                        "note": text,
                        "was_source": old.outcome_source,
                        "rule_parts": old.outcome_parts if old.outcome_source == "rule" else [],
                    }
                )
            )
            expectation = old.model_copy(
                update={
                    "outcome": new,
                    "outcome_source": "manual",
                    "outcome_reasons": [text],
                    "outcome_parts": [],
                }
            )
            case = case.model_copy(update={"expectation": expectation})
        cases.append(case)
    updated = test_set.model_copy(update={"cases": cases})
    if changed:
        selection = dict(updated.selection)
        selection["escalate_cases"] = sum(c.expectation.outcome == "escalate" for c in cases)
        updated = updated.model_copy(update={"selection": selection})
    final = {c.id: c for c in updated.cases}
    entry = LabelCheck(
        id=entry_id,
        test_set=test_set.name,
        reviewer=reviewer,
        checked_at=now,
        cases_in_set=len(test_set.cases),
        required=need,
        labels={case_id: final[case_id].expectation.outcome for case_id in sorted(ids)},
        changed=changed,
        test_set_sha256=updated.sha256(),
    )
    store.checks.append(entry)
    return updated, entry


# What can be behind a changed label besides a part of the rule (RULE_PARTS).
OTHER_CAUSES: dict[str, str] = {
    "none": "none (the rule labelled it act)",
    "spot-check": "an earlier spot-check",
    "not recorded": "not recorded (the set predates them)",
}


def change_causes(check: LabelCheck) -> dict[str, int]:
    """For each label a check changed, what gave the old label: the parts of the rule that
    labelled the case escalate, `none` when the rule labelled it act, `spot-check` when an
    earlier check set it. A case escalated by two parts counts for both."""
    counts: dict[str, int] = {}
    for change in check.changed:
        if change.was_source == "manual":
            causes = ["spot-check"]
        elif change.was == "act":
            causes = ["none"]
        else:
            causes = change.rule_parts or ["not recorded"]
        for cause in causes:
            counts[cause] = counts.get(cause, 0) + 1
    return counts


class UncountedCheck(BaseModel):
    """A spot-check that changed more than one in ten of the labels it covered."""

    id: str
    changed: int
    covered: int
    # change_causes() of the check: rule part (or other cause) -> changed labels
    causes: dict[str, int] = Field(default_factory=dict)

    def line(self) -> str:
        named = [(key, OTHER_CAUSES.get(key, key)) for key in [*RULE_PARTS, *OTHER_CAUSES]]
        causes = ", ".join(
            f"{name} {self.causes[key]}" for key, name in named if key in self.causes
        )
        return (
            f"labels spot-check {self.id} changed {self.changed} of the {self.covered} labels it "
            f"covered, more than one in ten, so it does not count (parts of the labelling rule "
            f"behind the changed labels: {causes}); tune `escalation:` in pack.yaml or relabel, "
            f"then check a fresh sample (`replaybook labels sample`)"
        )


def uncounted(check: LabelCheck) -> UncountedCheck:
    return UncountedCheck(
        id=check.id,
        changed=len(check.changed),
        covered=len(check.labels),
        causes=change_causes(check),
    )


class LabelStatus(BaseModel):
    """Whether a set's current labels are covered by spot-checks, for the go-live gate."""

    checked: bool = False
    cases: int = 0
    required: int = 0
    covered: int = 0
    escalate_cases: int = 0
    # Reviewers and date of the checks that count.
    reviewers: list[str] = Field(default_factory=list)
    last_checked_at: datetime | None = None
    # Every check of the set, and those that count: the checks after the last one that
    # changed more than one in ten of its labels (`not_counted`), or all of them.
    check_ids: list[str] = Field(default_factory=list)
    counted_ids: list[str] = Field(default_factory=list)
    changed: int = 0
    not_counted: UncountedCheck | None = None

    def line(self) -> str:
        if self.checked:
            when = self.last_checked_at.date().isoformat() if self.last_checked_at else "-"
            earlier = ""
            if self.not_counted is not None:
                earlier = (
                    f"; {self.not_counted.id} changed {self.not_counted.changed} of "
                    f"{self.not_counted.covered} labels and does not count"
                )
            return (
                f"labels spot-checked ({self.covered} of {self.cases} cases by "
                f"{', '.join(self.reviewers)}, last on {when}{earlier})"
            )
        if self.not_counted is not None and not self.counted_ids:
            return self.not_counted.line()
        if not self.check_ids:
            return (
                f"labels not spot-checked: `replaybook labels check` needs at least "
                f"{self.required} of the {self.cases} cases"
            )
        return (
            f"labels spot-check covers {self.covered} of the {self.required} cases needed "
            f"(the set changed since); run `replaybook labels check` again"
        )


def label_status(test_set: str, labels: dict[str, str], store: LabelChecks) -> LabelStatus:
    """`labels` maps each current case id to its expected outcome. A checked case counts
    only while it still carries the label the reviewer confirmed or set. A check that
    changed more than one in ten of its labels does not count, nor does any check before
    it: the rule needed tuning, so only a fresh check after it shows the labels hold."""
    checks = store.for_set(test_set)
    last_bad = max((i for i, c in enumerate(checks) if not c.counts), default=None)
    counted = checks if last_bad is None else checks[last_bad + 1 :]
    confirmed: dict[str, str] = {}
    for check in counted:
        confirmed.update(check.labels)
    covered = sum(1 for case_id, value in labels.items() if confirmed.get(case_id) == value)
    need = required_sample(len(labels))
    return LabelStatus(
        checked=bool(labels) and covered >= need,
        cases=len(labels),
        required=need,
        covered=covered,
        escalate_cases=sum(value == "escalate" for value in labels.values()),
        reviewers=sorted({c.reviewer for c in counted}),
        last_checked_at=counted[-1].checked_at if counted else None,
        check_ids=[c.id for c in checks],
        counted_ids=[c.id for c in counted],
        changed=sum(len(c.changed) for c in checks),
        not_counted=uncounted(checks[last_bad]) if last_bad is not None else None,
    )


def test_set_labels(test_set: TestSet) -> dict[str, str]:
    return {c.id: c.expectation.outcome for c in test_set.cases}


def review_sheet(test_set: TestSet, case_ids: list[str]) -> str:
    """Markdown for the person doing the spot-check: each sampled case with its label, the
    rule's reasons, the request as filed and what engineers recorded."""
    by_id = {c.id: c for c in test_set.cases}
    need = required_sample(len(test_set.cases))
    lines = [
        f"# Label spot-check: {test_set.name}",
        "",
        f"{len(case_ids)} of {len(test_set.cases)} cases (a spot-check needs at least {need}). "
        "For each case decide whether the agent should act (do the work, or ask the "
        "requester for missing details) or escalate (hand the ticket back because it needs "
        "a person's judgement). Then record it:",
        "",
        "```",
        f"replaybook labels check --testset <this set> --reviewer <your name> "
        f"--cases {','.join(case_ids)} [--set CASE=act|escalate ...]",
        "```",
        "",
    ]
    for case_id in case_ids:
        case = by_id[case_id]
        exp = case.expectation
        lines.append(f"## {case.id}: {case.summary}")
        lines.append("")
        lines.append(f"- Label: **{exp.outcome}** ({exp.outcome_source})")
        for reason in exp.outcome_reasons:
            lines.append(f"  - {reason}")
        actions = ", ".join(a.tool for a in exp.actions) or (
            "asked the requester" if exp.kind == "ask" else "-"
        )
        lines.append(f"- Engineers did: {actions}")
        if case.world.ticket is not None:
            text = " ".join(case.world.ticket.description.split())
            lines.append(f"- Request: {text[:400]}{'...' if len(text) > 400 else ''}")
        lines.append("")
    return "\n".join(lines)
