"""Seeded synthetic ticket generator.

Structure comes first: for each ticket we sample the hidden truth (type, parameters,
timeline, correct actions, missing information, effort), then render the human text
from templates. The same seed always gives the same files, byte for byte.

Outputs in the data directory:
- tickets.jsonl        canonical tickets, the visible data a team would have
- ground_truth.jsonl   hidden answers, read only by the eval module
- platform.yaml        visible platform facts: procedure changes, dates, and each pack's facts
- DATASET.md           a data card with counts
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from replaybook.packs import PackRegistry, registry
from replaybook.schema.ground_truth import ActionSpec, GroundTruth
from replaybook.schema.ticket import (
    CodeChange,
    Comment,
    EngineerAction,
    StatusChange,
    Ticket,
    TicketLink,
)
from replaybook.synth import names, render
from replaybook.synth.names import pick, weighted
from replaybook.synth.noise import NOISE_TEMPLATES
from replaybook.synth.types import TIMELINE_END, TIMELINE_START, RequestType

TICKETS_FILE = "tickets.jsonl"
GROUND_TRUTH_FILE = "ground_truth.jsonl"
PLATFORM_FILE = "platform.yaml"
DATASET_CARD = "DATASET.md"


@dataclass(frozen=True)
class GenerateOptions:
    seed: int = 42
    n_tickets: int = 3000
    project: str = "OPS"
    noise_share: float = 0.25
    info_missing_share: float = 0.20
    duplicate_share: float = 0.03
    mistake_share: float = 0.02
    reopen_share: float = 0.05
    code_change_share: float = 0.40
    unknown_actions_share: float = 0.12
    logged_time_share: float = 0.60


@dataclass
class _Draft:
    """A ticket before ids are assigned. `key` is a temporary handle for links."""

    key: int
    created: datetime
    ticket_fields: dict[str, Any]
    truth_fields: dict[str, Any]
    links: list[tuple[str, int]] = field(default_factory=list)


def request_types(packs: PackRegistry | None = None) -> dict[str, RequestType]:
    """The synthetic request types every loaded pack contributes, in pack order."""
    reg = packs if packs is not None else registry()
    found: dict[str, RequestType] = {}
    for rt in reg.synth_types():
        if rt.key in found:
            raise ValueError(f"synthetic request type '{rt.key}' is defined twice")
        if rt.key not in reg.request_types:
            raise ValueError(f"synthetic request type '{rt.key}' is missing from its pack.yaml")
        found[rt.key] = rt
    return found


def generate(
    out_dir: Path, options: GenerateOptions | None = None, packs: PackRegistry | None = None
) -> dict[str, Any]:
    options = options or GenerateOptions()
    reg = packs if packs is not None else registry()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(options.seed)

    drafts = _build_drafts(rng, options, reg)
    drafts.sort(key=lambda d: (d.created, d.key))
    ids = {d.key: f"{options.project}-{1000 + i}" for i, d in enumerate(drafts)}

    tickets: list[Ticket] = []
    truths: list[GroundTruth] = []
    for draft in drafts:
        ticket_id = ids[draft.key]
        links = [TicketLink(type=t, target=ids[k]) for t, k in draft.links]
        ticket = Ticket(
            id=ticket_id,
            source="synthetic",
            project=options.project,
            links=links,
            **_resolve_refs(draft.ticket_fields, ids),
        )
        truth_fields = dict(draft.truth_fields)
        for ref in ("corrected_by", "is_duplicate_of"):
            if truth_fields.get(ref) is not None:
                truth_fields[ref] = ids[truth_fields[ref]]
        tickets.append(ticket)
        truths.append(GroundTruth(ticket_id=ticket_id, **truth_fields))

    _write_jsonl(out_dir / TICKETS_FILE, (t.model_dump(mode="json") for t in tickets))
    _write_jsonl(out_dir / GROUND_TRUTH_FILE, (t.model_dump(mode="json") for t in truths))
    (out_dir / PLATFORM_FILE).write_text(
        yaml.safe_dump(platform_facts(options, reg), sort_keys=False), encoding="utf-8"
    )
    summary = summarise(truths, tickets, options, reg)
    (out_dir / DATASET_CARD).write_text(data_card(summary, reg), encoding="utf-8")
    return summary


# --- sampling --------------------------------------------------------------------------


def _build_drafts(rng: random.Random, options: GenerateOptions, reg: PackRegistry) -> list[_Draft]:
    span = TIMELINE_END - TIMELINE_START
    created_times = sorted(
        _business_time(rng, TIMELINE_START, span) for _ in range(options.n_tickets)
    )
    types = request_types(reg)
    if not types:
        raise ValueError("no loaded pack defines synthetic request types")

    drafts: list[_Draft] = []
    recent_by_type: dict[str, list[_Draft]] = {k: [] for k in types}
    type_weights = [(rt, rt.weight) for rt in types.values()]
    next_key = 0

    for created in created_times:
        key = next_key
        next_key += 1
        if rng.random() < options.noise_share:
            drafts.append(_noise_draft(rng, key, created))
            continue
        rt = weighted(rng, type_weights)
        draft, correction = _typed_draft(
            rng, key, created, rt, options, recent_by_type[rt.key], next_key, types
        )
        drafts.append(draft)
        recent_by_type[rt.key].append(draft)
        if correction is not None:
            next_key += 1
            drafts.append(correction)

    for change in reg.change_tickets():
        drafts.append(_platform_change_draft(rng, next_key, change))
        next_key += 1
    return drafts


def _business_time(rng: random.Random, start: datetime, span: timedelta) -> datetime:
    at = start + timedelta(seconds=rng.random() * span.total_seconds())
    at = at.replace(minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0)
    if rng.random() < 0.85:
        at = at.replace(hour=rng.randint(8, 17))
        if at.weekday() >= 5:
            at -= timedelta(days=at.weekday() - 4)
    return at


def _typed_draft(
    rng: random.Random,
    key: int,
    created: datetime,
    rt: RequestType,
    options: GenerateOptions,
    earlier: list[_Draft],
    correction_key: int,
    types: dict[str, RequestType],
) -> tuple[_Draft, _Draft | None]:
    version = rt.version_at(created)
    reporter = pick(rng, names.REQUESTERS)
    engineer = pick(rng, names.ENGINEERS)
    params = rt.sample_params(rng)

    duplicate_of: _Draft | None = None
    candidates = [d for d in earlier[-20:] if (created - d.created).days <= 90]
    if candidates and rng.random() < options.duplicate_share:
        duplicate_of = pick(rng, candidates)
        params = dict(duplicate_of.truth_fields["params"])

    missing: list[str] = []
    if duplicate_of is None and rng.random() < options.info_missing_share:
        missing = [pick(rng, rt.missable_fields)]

    mistake = None
    if duplicate_of is None and not missing and rng.random() < options.mistake_share:
        mistake = rt.mistake(params, rng)

    correct_actions = rt.actions(params, version)
    current_actions = rt.actions(params, rt.version_at(TIMELINE_END))
    recorded_params = mistake[0] if mistake else params
    recorded_actions = rt.actions(recorded_params, version)

    effort = rng.uniform(*rt.effort_hours) * (1.5 if missing else 1.0)
    summary = render.render_summary(rt, params, missing, rng)
    description = render.render_description(rt, params, missing, rng)

    comments: list[Comment] = []
    changes: list[StatusChange] = [StatusChange(at=created, from_status=None, to_status="Open")]
    actions: list[EngineerAction] = []
    links: list[tuple[str, int]] = []
    code_changes: list[CodeChange] = []
    now = created + _hours(rng, 0.25, 30)
    changes.append(StatusChange(at=now, from_status="Open", to_status="In Progress"))

    if duplicate_of is not None:
        now += _hours(rng, 0.1, 2)
        comments.append(
            Comment(
                author=engineer,
                role="engineer",
                created=now,
                body=f"Duplicate of {{{duplicate_of.key}}}, closing.",
            )
        )
        changes.append(StatusChange(at=now, from_status="In Progress", to_status="Closed"))
        links.append(("duplicates", duplicate_of.key))
        ticket_fields = _ticket_fields(
            rng,
            rt,
            params,
            summary,
            description,
            reporter,
            engineer,
            created,
            now,
            "Closed",
            "Duplicate",
            comments,
            changes,
            [],
            [],
            None,
            options,
            types,
        )
        truth = {
            "request_type": rt.key,
            "params": params,
            "correct_actions": [],
            "current_actions": [],
            "procedure_version": version,
            "true_effort_hours": round(0.1, 2),
            "is_duplicate_of": duplicate_of.key,
        }
        return _Draft(key, created, ticket_fields, truth, links), None

    if missing:
        label = rt.field_labels[missing[0]][0]
        now += _hours(rng, 0.1, 3)
        comments.append(
            Comment(
                author=engineer,
                role="engineer",
                created=now,
                body=render.render_ask(reporter, label, rng),
            )
        )
        changes.append(
            StatusChange(at=now, from_status="In Progress", to_status="Waiting on Requester")
        )
        now += _hours(rng, 1, 120)
        comments.append(
            Comment(
                author=reporter,
                role="requester",
                created=now,
                body=render.render_reply(label, params[missing[0]], rng),
            )
        )
        changes.append(
            StatusChange(at=now, from_status="Waiting on Requester", to_status="In Progress")
        )

    if rng.random() >= options.unknown_actions_share:
        for spec in recorded_actions:
            now += _hours(rng, 0.1, effort)
            actions.append(EngineerAction(tool=spec.tool, args=dict(spec.args), at=now))
    else:
        now += _hours(rng, 0.5, effort * 2)

    if rng.random() < options.code_change_share:
        component = rt.components[0]
        code_changes.append(
            CodeChange(
                repo=f"platform/{component}-config",
                ref=f"!{rng.randint(100, 9999)}",
                title=f"{rt.label}: {summary[:60]}",
                merged_at=now,
            )
        )

    now += _hours(rng, 0.05, 1)
    comments.append(
        Comment(
            author=engineer,
            role="engineer",
            created=now,
            body=render.render_done(rt, recorded_params, rng),
        )
    )
    changes.append(StatusChange(at=now, from_status="In Progress", to_status="Resolved"))
    resolved = now

    if rng.random() < options.reopen_share:
        now += _hours(rng, 1, 72)
        comments.append(
            Comment(
                author=reporter,
                role="requester",
                created=now,
                body=pick(rng, render.REOPEN_COMMENTS),
            )
        )
        changes.append(StatusChange(at=now, from_status="Resolved", to_status="Reopened"))
        now += _hours(rng, 0.5, 24)
        comments.append(
            Comment(
                author=engineer,
                role="engineer",
                created=now,
                body=pick(rng, render.REOPEN_FIX_COMMENTS),
            )
        )
        changes.append(StatusChange(at=now, from_status="Reopened", to_status="Resolved"))
        resolved = now
        effort *= 1.3

    status = "Resolved"
    if rng.random() < 0.5:
        now += timedelta(days=rng.randint(7, 14))
        changes.append(StatusChange(at=now, from_status="Resolved", to_status="Closed"))
        status = "Closed"

    ticket_fields = _ticket_fields(
        rng,
        rt,
        params,
        summary,
        description,
        reporter,
        engineer,
        created,
        resolved,
        status,
        "Done",
        comments,
        changes,
        actions,
        code_changes,
        effort,
        options,
        types,
    )
    truth = {
        "request_type": rt.key,
        "params": params,
        "correct_actions": correct_actions,
        "current_actions": current_actions,
        "procedure_version": version,
        "info_missing": bool(missing),
        "missing_fields": missing,
        "unsafe_trigger": bool(rt.unsafe_trigger_field and params.get(rt.unsafe_trigger_field)),
        "true_effort_hours": round(effort, 2),
    }
    draft = _Draft(key, created, ticket_fields, truth, links)

    correction = None
    if mistake is not None:
        truth["history_mistake"] = mistake[1]
        correction = _correction_draft(rng, draft, rt, params, mistake[1], resolved, version)
        correction.key = correction_key
        truth["corrected_by"] = correction.key
        draft.links.append(("relates", correction.key))
    return draft, correction


def _correction_draft(
    rng: random.Random,
    original: _Draft,
    rt: RequestType,
    params: dict[str, Any],
    what: str,
    after: datetime,
    version: str,
) -> _Draft:
    created = after + timedelta(days=rng.randint(3, 30), hours=rng.randint(0, 8))
    engineer = pick(rng, names.ENGINEERS)
    reporter = original.ticket_fields["reporter"]
    specs = rt.actions(params, version)
    now = created + _hours(rng, 0.5, 24)
    actions = [EngineerAction(tool=s.tool, args=dict(s.args), at=now) for s in specs]
    comments = [
        Comment(
            author=engineer,
            role="engineer",
            created=now + _hours(rng, 0.1, 1),
            body="Re-applied with the correct values, sorry about that.",
        ),
    ]
    changes = [
        StatusChange(at=created, from_status=None, to_status="Open"),
        StatusChange(at=created + _hours(rng, 0.1, 8), from_status="Open", to_status="In Progress"),
        StatusChange(at=comments[-1].created, from_status="In Progress", to_status="Resolved"),
    ]
    fields = {
        "summary": f"Fix: {what.split(' was ')[0]} from {{{original.key}}}",
        "description": f"Follow-up to {{{original.key}}}: {what}. Please correct it.",
        "reporter": reporter,
        "assignee": engineer,
        "created": created,
        "resolved": comments[-1].created,
        "status": "Resolved",
        "resolution": "Done",
        "priority": "High",
        "labels": [*rt.components, "correction"],
        "components": list(rt.components),
        "comments": comments,
        "status_changes": changes,
        "code_changes": [],
        "actions": actions,
        "time_spent_hours": None,
        "inventory": {},
    }
    truth = {
        "request_type": None,
        "noise": False,
        "params": {"corrects": original.key, "what": what},
        "correct_actions": specs,
        "current_actions": specs,
        "procedure_version": version,
        "true_effort_hours": 0.5,
    }
    draft = _Draft(-1, created, fields, truth, [("fixes", original.key)])
    return draft


def _ticket_fields(
    rng: random.Random,
    rt: RequestType,
    params: dict[str, Any],
    summary: str,
    description: str,
    reporter: str,
    engineer: str,
    created: datetime,
    resolved: datetime,
    status: str,
    resolution: str,
    comments: list[Comment],
    changes: list[StatusChange],
    actions: list[EngineerAction],
    code_changes: list[CodeChange],
    effort: float | None,
    options: GenerateOptions,
    types: dict[str, RequestType],
) -> dict[str, Any]:
    labels = list(rt.components)
    roll = rng.random()
    if roll < 0.10:
        labels = []
    elif roll < 0.18 and len(types) > 1:
        other = pick(rng, [k for k in types if k != rt.key])
        labels = list(types[other].components)
    elif roll < 0.6:
        labels.append("request")
    logged = None
    if effort is not None and rng.random() < options.logged_time_share:
        logged = round(effort * rng.uniform(0.6, 1.5) * 4) / 4
    return {
        "summary": summary,
        "description": description,
        "reporter": reporter,
        "assignee": engineer,
        "created": created,
        "resolved": resolved,
        "status": status,
        "resolution": resolution,
        "priority": weighted(rng, names.PRIORITIES),
        "labels": labels,
        "components": list(rt.components) if rng.random() < 0.7 else [],
        "comments": comments,
        "status_changes": changes,
        "code_changes": code_changes,
        "actions": actions,
        "time_spent_hours": logged,
        "effort_source": "logged" if logged is not None else None,
        "inventory": rt.inventory(params, rng),
    }


def _noise_draft(rng: random.Random, key: int, created: datetime) -> _Draft:
    summary_t, description_t, labels = pick(rng, NOISE_TEMPLATES)
    params = {
        "service": pick(rng, names.SERVICES),
        "env": weighted(rng, names.ENVS),
        "team": pick(rng, names.TEAMS),
        "n": rng.randint(100, 9999),
        "email": pick(rng, names.NOISE_PEOPLE_EMAILS),
        "public_ip": f"203.0.113.{rng.randint(1, 254)}",
    }
    reporter = pick(rng, names.REQUESTERS)
    engineer = pick(rng, names.ENGINEERS)
    comments: list[Comment] = []
    changes = [StatusChange(at=created, from_status=None, to_status="Open")]
    now = created + _hours(rng, 1, 72)
    outcome = pick(rng, ("Done", "Done", "Won't Do", "Cannot Reproduce"))
    if rng.random() < 0.7:
        comments.append(
            Comment(
                author=engineer,
                role="engineer",
                created=now,
                body=pick(
                    rng, ("Looking into it.", "Taking this one.", "Can you add more detail?")
                ),
            )
        )
        changes.append(StatusChange(at=now, from_status="Open", to_status="In Progress"))
        now += _hours(rng, 1, 96)
    if rng.random() < 0.3:
        comments.append(
            Comment(
                author=reporter,
                role="requester",
                created=now,
                body=pick(
                    rng, ("any update?", "Still an issue for us.", "Thanks, no longer needed.")
                ),
            )
        )
        now += _hours(rng, 1, 48)
    comments.append(
        Comment(
            author=engineer,
            role="engineer",
            created=now,
            body=pick(
                rng,
                (
                    "done",
                    "Closing.",
                    "Resolved offline.",
                    "Not something we can fix from here, closing.",
                ),
            ),
        )
    )
    changes.append(StatusChange(at=now, from_status=changes[-1].to_status, to_status="Resolved"))
    effort = rng.uniform(0.25, 4.0)
    fields = {
        "summary": render.fmt(summary_t, params),
        "description": render.mess_up(render.fmt(description_t, params), rng),
        "reporter": reporter,
        "assignee": engineer if rng.random() < 0.8 else None,
        "created": created,
        "resolved": now,
        "status": "Resolved",
        "resolution": outcome,
        "priority": weighted(rng, names.PRIORITIES),
        "labels": list(labels) if rng.random() < 0.7 else [],
        "components": [],
        "comments": comments,
        "status_changes": changes,
        "code_changes": [],
        "actions": [],
        "time_spent_hours": round(effort * 4) / 4 if rng.random() < 0.5 else None,
        "inventory": {},
    }
    fields["effort_source"] = "logged" if fields["time_spent_hours"] is not None else None
    truth = {"request_type": None, "noise": True, "true_effort_hours": round(effort, 2)}
    return _Draft(key, created, fields, truth)


def _platform_change_draft(rng: random.Random, key: int, change: dict[str, Any]) -> _Draft:
    at = change["at"]
    if isinstance(at, str):
        at = datetime.fromisoformat(at)
    created: datetime = at - timedelta(days=1, hours=3)
    engineer = pick(rng, names.ENGINEERS)
    fields = {
        "summary": change["summary"],
        "description": change["description"],
        "reporter": engineer,
        "assignee": engineer,
        "created": created,
        "resolved": at,
        "status": "Closed",
        "resolution": "Done",
        "priority": "High",
        "labels": list(change["labels"]),
        "components": [change["labels"][0]],
        "comments": [],
        "status_changes": [
            StatusChange(at=created, from_status=None, to_status="Open"),
            StatusChange(at=at, from_status="Open", to_status="Closed"),
        ],
        "code_changes": [],
        "actions": [],
        "time_spent_hours": None,
        "inventory": {},
    }
    truth = {"request_type": None, "noise": True, "true_effort_hours": 8.0}
    return _Draft(key, created, fields, truth)


def _hours(rng: random.Random, low: float, high: float) -> timedelta:
    return timedelta(hours=rng.uniform(low, max(low, high)))


# --- outputs ---------------------------------------------------------------------------


def platform_facts(options: GenerateOptions, reg: PackRegistry | None = None) -> dict[str, Any]:
    """Visible facts about the platform today. Not ground truth: a real team knows these.
    Each pack adds its own facts (modes, clusters, privileged groups) under its own keys."""
    reg = reg if reg is not None else registry()
    changes = reg.procedure_changes()
    current = {c.request_type: c.to_version for c in changes}
    return {
        "generated_with_seed": options.seed,
        "timeline": {"start": TIMELINE_START.isoformat(), "end": TIMELINE_END.isoformat()},
        "procedure_changes": [c.as_dict() for c in changes],
        "current_procedures": current,
        **reg.platform_facts(),
        "request_types": {k: reg.label(k) for k in request_types(reg)},
    }


def summarise(
    truths: list[GroundTruth],
    tickets: list[Ticket],
    options: GenerateOptions,
    reg: PackRegistry | None = None,
) -> dict[str, Any]:
    current_versions = (reg if reg is not None else registry()).current_versions()
    by_type: dict[str, int] = {}
    for t in truths:
        key = t.request_type or ("noise" if t.noise else "correction")
        by_type[key] = by_type.get(key, 0) + 1
    typed = [t for t in truths if t.request_type]
    return {
        "seed": options.seed,
        "tickets": len(tickets),
        "by_type": dict(sorted(by_type.items())),
        "typed": len(typed),
        "info_missing": sum(t.info_missing for t in typed),
        "duplicates": sum(t.is_duplicate_of is not None for t in typed),
        "history_mistakes": sum(t.history_mistake is not None for t in typed),
        "unsafe_triggers": sum(t.unsafe_trigger for t in typed),
        "pre_drift": sum(t.procedure_version not in current_versions for t in typed),
        "with_code_changes": sum(bool(t.code_changes) for t in tickets),
        "with_recorded_actions": sum(bool(t.actions) for t in tickets),
        "reopened": sum(any(c.to_status == "Reopened" for c in t.status_changes) for t in tickets),
        "timeline": {
            "start": TIMELINE_START.date().isoformat(),
            "end": TIMELINE_END.date().isoformat(),
        },
    }


def data_card(summary: dict[str, Any], reg: PackRegistry | None = None) -> str:
    changes = (reg if reg is not None else registry()).procedure_changes()
    lines = [
        "# Synthetic ticket dataset",
        "",
        "Generated by `replaybook generate`. Everything here is made up; names, teams and",
        "services are fictional. `tickets.jsonl` is the visible history a team would have.",
        "`ground_truth.jsonl` holds the hidden answers and is read only by the eval module.",
        "",
        f"- Seed: {summary['seed']}",
        f"- Tickets: {summary['tickets']} "
        f"({summary['timeline']['start']} to {summary['timeline']['end']})",
        f"- Typed requests: {summary['typed']}; with missing info: {summary['info_missing']}; "
        f"duplicates: {summary['duplicates']}; historical mistakes later corrected: "
        f"{summary['history_mistakes']}; unsafe-shortcut temptations: "
        f"{summary['unsafe_triggers']}; raised before a procedure change: "
        f"{summary['pre_drift']}",
        f"- Tickets with linked code changes: {summary['with_code_changes']}; with recorded "
        f"engineer actions: {summary['with_recorded_actions']}; reopened: {summary['reopened']}",
        "",
        "## Tickets by type",
        "",
        "| Type | Count |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in summary["by_type"].items()]
    lines += [
        "",
        "## Procedure changes",
        "",
    ]
    lines += [
        f"- {c.request_type}: {c.from_version} -> {c.to_version} on {c.at.date().isoformat()}"
        for c in changes
    ]
    return "\n".join(lines) + "\n"


_REF = re.compile(r"\{(\d+)\}")


def _resolve_refs(fields: dict[str, Any], ids: dict[int, str]) -> dict[str, Any]:
    """Replace {draft-key} placeholders in text with the final ticket ids."""

    def sub(text: str) -> str:
        return _REF.sub(lambda m: ids.get(int(m.group(1)), m.group(0)), text)

    out = dict(fields)
    out["summary"] = sub(fields["summary"])
    out["description"] = sub(fields["description"])
    out["comments"] = [c.model_copy(update={"body": sub(c.body)}) for c in fields["comments"]]
    return out


def _write_jsonl(path: Path, rows: Any) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def spec_to_dict(spec: ActionSpec) -> dict[str, Any]:
    return spec.model_dump(mode="json")
