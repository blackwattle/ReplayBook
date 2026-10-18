"""`replaybook shadow`: what would the agent have done on the tickets that are open now?

Shadow mode runs the agent against the mock environment for each OPEN ticket of a request
type and stores the actions it proposed. Nothing is executed and nothing is written back
to the ticket system: the proposal lives in a JSONL file. Later, once engineers have
resolved a ticket the normal way, `score` builds the expectation from what they did (the
same rules as a test set: an ask case, recorded actions, or an extracted expectation) and
scores the stored proposal against it. The evidence pack gets a shadow section: how many
proposals, how many could be scored, the pass rate with its interval, and the unsafe
proposals that were caught before the agent had any access.

Files in the shadow directory:
- proposals.jsonl   one line per (ticket, agent fingerprint): the writes the agent proposed
- scores.jsonl      one line per scored proposal, with the resolution time
- shadow.json       the latest report (also what the evidence pack reads)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from replaybook.agents.base import AgentUnderTest
from replaybook.classify import classify
from replaybook.eval.extract import Extractor
from replaybook.eval.groups import FailureGroup, group_failures
from replaybook.eval.runner import RunSummary, compute_fingerprint, summarise
from replaybook.eval.scoring import CaseResult, score_case
from replaybook.eval.testset import expectation_for, seed_inventory, type_of, visible_tags
from replaybook.mock_env.knowledge import Corpus
from replaybook.mock_env.recorder import RecordedCall
from replaybook.mock_env.registry import (
    MockEnvironment,
    describe_all_tools,
    tool_schema_fingerprint,
)
from replaybook.mock_env.world import WorldState
from replaybook.schema.testcase import TestCase
from replaybook.schema.ticket import Ticket

PROPOSALS_FILE = "proposals.jsonl"
SCORES_FILE = "scores.jsonl"
REPORT_FILE = "shadow.json"
OPEN_STATUSES_EXCLUDED = ("Resolved", "Closed")


class Proposal(BaseModel):
    ticket_id: str
    request_type: str
    summary: str
    ticket_created: datetime
    recorded_at: datetime
    agent_config: dict[str, Any]
    tool_fingerprint: str
    fingerprint: str
    world: WorldState
    writes: list[RecordedCall] = Field(default_factory=list)
    reads: int = 0
    stopped_reason: str = "done"
    agent_error: str | None = None
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    knowledge: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.ticket_id, self.fingerprint


class Score(BaseModel):
    ticket_id: str
    fingerprint: str
    resolved_at: datetime
    scored_at: datetime
    expectation_source: str
    expectation_verified: bool
    result: CaseResult


class ShadowReport(BaseModel):
    shadow_dir: str
    generated_at: datetime
    request_types: list[str] = Field(default_factory=list)
    proposals: int = 0
    scored: int = 0
    pending: int = 0
    unscorable: int = 0
    summary: RunSummary = Field(default_factory=RunSummary)
    unsafe_caught: list[dict[str, Any]] = Field(default_factory=list)
    groups: list[FailureGroup] = Field(default_factory=list)
    agents: list[dict[str, Any]] = Field(default_factory=list)
    unverified_expectations: int = 0


# --- storage -----------------------------------------------------------------------------


def _read_jsonl(path: Path, model: Any) -> list[Any]:
    if not path.exists():
        return []
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _append_jsonl(path: Path, rows: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(row.model_dump_json())
            handle.write("\n")


def load_proposals(shadow_dir: Path) -> list[Proposal]:
    return _read_jsonl(Path(shadow_dir) / PROPOSALS_FILE, Proposal)


def load_scores(shadow_dir: Path) -> list[Score]:
    return _read_jsonl(Path(shadow_dir) / SCORES_FILE, Score)


# --- record ------------------------------------------------------------------------------


def is_open(ticket: Ticket) -> bool:
    return ticket.status not in OPEN_STATUSES_EXCLUDED and ticket.resolved is None


def open_tickets(tickets: list[Ticket], request_type: str) -> list[Ticket]:
    """Open tickets of a request type. Open tickets rarely have recorded actions, so the
    text classifier does most of the typing."""
    found = []
    for ticket in tickets:
        if not is_open(ticket):
            continue
        kind = type_of(ticket)[0] or classify(ticket)[0]
        if kind == request_type:
            found.append(ticket)
    return sorted(found, key=lambda t: (t.created, t.id))


def record(
    tickets: list[Ticket],
    platform: dict[str, Any],
    request_type: str,
    agent: AgentUnderTest,
    shadow_dir: Path,
    knowledge: Corpus | None = None,
    extractor: Extractor | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the agent on every open ticket of the type and store what it proposed.

    One proposal per ticket per agent fingerprint: re-running with the same agent and
    tools adds nothing; a changed agent records a new proposal next to the old one."""
    shadow_dir = Path(shadow_dir)
    now = now or datetime.now(UTC)
    tool_fp = tool_schema_fingerprint(describe_all_tools())
    fingerprint = compute_fingerprint(dict(agent.config), "shadow", tool_fp)
    existing = {p.key for p in load_proposals(shadow_dir)}
    candidates = open_tickets(tickets, request_type)
    new: list[Proposal] = []
    skipped = 0
    for ticket in candidates:
        if (ticket.id, fingerprint) in existing:
            skipped += 1
            continue
        if not ticket.inventory and extractor is not None:
            found = extractor.extract(ticket, request_type)
            seed = seed_inventory(found.actions if found else [])
            if seed:
                ticket = ticket.model_copy(update={"inventory": seed})
        world = WorldState.from_ticket(ticket, platform)
        env = MockEnvironment(world.model_copy(deep=True), knowledge=knowledge)
        error: str | None = None
        try:
            outcome = agent.run(env)
            stopped = outcome.stopped_reason
            transcript = outcome.transcript
            if outcome.stopped_reason in ("error", "offline") and outcome.error:
                error = outcome.error
        except Exception as exc:  # the agent is someone else's code
            stopped, transcript, error = "error", [], f"{type(exc).__name__}: {exc}"
        new.append(
            Proposal(
                ticket_id=ticket.id,
                request_type=request_type,
                summary=ticket.summary,
                ticket_created=ticket.created,
                recorded_at=now,
                agent_config=dict(agent.config),
                tool_fingerprint=tool_fp,
                fingerprint=fingerprint,
                world=world,
                writes=env.recorder.writes,
                reads=len(env.recorder.reads),
                stopped_reason=stopped,
                agent_error=error,
                transcript=transcript,
                knowledge=str(knowledge.path) if knowledge and knowledge.path else None,
            )
        )
    _append_jsonl(shadow_dir / PROPOSALS_FILE, new)
    write_report(shadow_dir, tickets=tickets)
    return {
        "request_type": request_type,
        "open_tickets": len(candidates),
        "recorded": len(new),
        "already_recorded": skipped,
        "fingerprint": fingerprint,
        "proposed_writes": sum(len(p.writes) for p in new),
        "asked": sum(p.stopped_reason == "asked" for p in new),
    }


# --- score -------------------------------------------------------------------------------


def score(
    tickets: list[Ticket],
    platform: dict[str, Any],
    shadow_dir: Path,
    extractor: Extractor | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score every stored proposal whose ticket has since been resolved."""
    shadow_dir = Path(shadow_dir)
    now = now or datetime.now(UTC)
    by_id = {t.id: t for t in tickets}
    done = {(s.ticket_id, s.fingerprint) for s in load_scores(shadow_dir)}
    new: list[Score] = []
    pending = unscorable = 0
    for proposal in load_proposals(shadow_dir):
        if proposal.key in done:
            continue
        ticket = by_id.get(proposal.ticket_id)
        if ticket is None or is_open(ticket):
            pending += 1
            continue
        if ticket.resolution not in (None, "Done", "Fixed"):
            unscorable += 1  # duplicates, won't-do: history holds no procedure to compare with
            continue
        expectation = expectation_for(ticket, proposal.request_type, extractor)
        if expectation is None:
            unscorable += 1
            continue
        case = TestCase(
            id=ticket.id,
            ticket_id=ticket.id,
            request_type=proposal.request_type,
            created=ticket.created,
            summary=ticket.summary,
            world=proposal.world,
            expectation=expectation,
            tags=visible_tags(ticket, by_id, platform, proposal.request_type),
        )
        result = score_case(
            case,
            proposal.writes,
            reads=proposal.reads,
            agent_stopped_reason=proposal.stopped_reason,
            agent_error=proposal.agent_error,
        )
        result.transcript = proposal.transcript
        new.append(
            Score(
                ticket_id=ticket.id,
                fingerprint=proposal.fingerprint,
                resolved_at=ticket.resolved or now,
                scored_at=now,
                expectation_source=expectation.source,
                expectation_verified=expectation.verified,
                result=result,
            )
        )
    _append_jsonl(shadow_dir / SCORES_FILE, new)
    report = write_report(shadow_dir, tickets=tickets)
    return {
        "scored_now": len(new),
        "pending": pending,
        "unscorable": unscorable,
        "report": report,
    }


# --- report ------------------------------------------------------------------------------


def build_report(shadow_dir: Path, tickets: list[Ticket] | None = None) -> ShadowReport:
    shadow_dir = Path(shadow_dir)
    proposals = load_proposals(shadow_dir)
    scores = load_scores(shadow_dir)
    scored_keys = {(s.ticket_id, s.fingerprint) for s in scores}
    by_id = {t.id: t for t in tickets} if tickets is not None else None
    pending = unscorable = 0
    for proposal in proposals:
        if proposal.key in scored_keys:
            continue
        ticket = by_id.get(proposal.ticket_id) if by_id is not None else None
        if ticket is None or is_open(ticket):
            pending += 1
        else:
            unscorable += 1
    results = [s.result for s in scores]
    unsafe = [
        {
            "ticket_id": s.ticket_id,
            "severity": s.result.severity,
            "tools": sorted(
                {
                    d.tool_actual
                    for d in s.result.diffs
                    if d.kind == "unsafe_action" and d.tool_actual
                }
            ),
            "detail": next((d.detail for d in s.result.diffs if d.kind == "unsafe_action"), ""),
            "resolved_at": s.resolved_at.isoformat(),
        }
        for s in scores
        if any(d.kind == "unsafe_action" for d in s.result.diffs)
    ]
    agents: dict[str, dict[str, Any]] = {}
    for p in proposals:
        agents.setdefault(
            p.fingerprint,
            {"fingerprint": p.fingerprint, "agent_config": p.agent_config, "proposals": 0},
        )
        agents[p.fingerprint]["proposals"] += 1
    return ShadowReport(
        shadow_dir=str(shadow_dir),
        generated_at=datetime.now(UTC),
        request_types=sorted({p.request_type for p in proposals}),
        proposals=len(proposals),
        scored=len(scores),
        pending=pending,
        unscorable=unscorable,
        summary=summarise(results),
        unsafe_caught=unsafe,
        groups=group_failures(results),
        agents=list(agents.values()),
        unverified_expectations=sum(not s.expectation_verified for s in scores),
    )


def write_report(shadow_dir: Path, tickets: list[Ticket] | None = None) -> ShadowReport:
    report = build_report(shadow_dir, tickets)
    (Path(shadow_dir) / REPORT_FILE).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


def load_report(shadow_dir: Path) -> ShadowReport | None:
    path = Path(shadow_dir) / REPORT_FILE
    if not path.exists():
        return None
    return ShadowReport.model_validate_json(path.read_text(encoding="utf-8"))
