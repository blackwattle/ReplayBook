"""Approval requests: what a person sees before approving a skill's writes on a live ticket.

`replaybook approval-request --skill <type> --plan <planned writes> --out <dir>` writes
`approval-request.md` and `approval-request.json` for a team's approval channel:

- the skill's name and version (the SHA-256 of its SKILL.md);
- the replay result and date for that version: the latest run under `--runs` made with it,
  as "passed 46 of 50 past tickets (44 matched, 2 correctly escalated)", and its go-live
  gate;
- the live verification rate over the window (eval.live), or "no live runs yet";
- each planned write with the read-only check that will confirm it (replaybook.checks);
- the evidence pack of that replay: the newest pack under `--evidence` whose manifest holds
  the run's results.json by hash.

The request is BLOCKED, said at the top in capitals, and the command exits 1 when the
current version has no replay, when its replay failed the go-live gate, or when the plan
holds a write the skill does not cover (no replay assessed it). A skill that fails
validation gets no request at all. Replaybook never approves anything: the decision is
made in the team's channel and comes back with the live run (`replaybook live record`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from replaybook import __version__
from replaybook.checks import checks_for, require_valid_skill
from replaybook.eval.golive import gate_dict, gate_for_run
from replaybook.eval.labels import LabelChecks
from replaybook.eval.live import RUNS_FILE, build_report
from replaybook.eval.runner import RESULTS_FILE, RunResult, load_run, result_line
from replaybook.mock_env.registry import TICKET_WRITE_TOOLS
from replaybook.skills import find_skill, short

MARKDOWN_FILE = "approval-request.md"
JSON_FILE = "approval-request.json"


class ApprovalError(ValueError):
    """No request can be written: no skill, an invalid skill or an unreadable plan."""


class PlannedWrite(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


def parse_plan(data: Any) -> list[PlannedWrite]:
    """The planned writes: a list of {"tool", "args"} or {"writes": [...]}."""
    if isinstance(data, dict) and "writes" in data:
        data = data["writes"]
    if not isinstance(data, list) or not data:
        raise ApprovalError('the plan is a non-empty list of {"tool": ..., "args": {...}}')
    try:
        writes = [PlannedWrite.model_validate(item) for item in data]
    except ValueError as exc:
        raise ApprovalError(f"the plan is not a list of writes: {exc}") from exc
    ticket_tools = [w.tool for w in writes if w.tool in TICKET_WRITE_TOOLS]
    if ticket_tools:
        raise ApprovalError(
            f"{', '.join(ticket_tools)} only touch the ticket and need no approval; plan the "
            "infrastructure writes"
        )
    return writes


def find_replay(runs_dir: Path, request_type: str, version: str) -> tuple[Path, RunResult] | None:
    """The latest run under `runs_dir` of this request type made with this skill version."""
    found: list[tuple[datetime, Path, RunResult]] = []
    root = Path(runs_dir)
    if not root.is_dir():
        return None
    for path in root.rglob(RESULTS_FILE):
        try:
            run = load_run(path.parent)
        except (OSError, ValueError):
            continue  # not a run directory
        if run.test_set_request_type == request_type and run.skill_version == version:
            found.append((run.created_at, path.parent, run))
    if not found:
        return None
    _at, run_dir, run = max(found, key=lambda item: (item[0], str(item[1])))
    return run_dir, run


def find_evidence(evidence_dir: Path, run_dir: Path) -> Path | None:
    """The newest evidence pack under `evidence_dir` whose manifest lists this run's
    results.json with the same SHA-256."""
    results = Path(run_dir) / RESULTS_FILE
    root = Path(evidence_dir)
    if not results.is_file() or not root.is_dir():
        return None
    digest = hashlib.sha256(results.read_bytes()).hexdigest()
    matches: list[tuple[str, Path]] = []
    for manifest in root.rglob("manifest.json"):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if any(entry.get("sha256") == digest for entry in data.get("inputs", [])):
            html = manifest.parent / "evidence.html"
            if html.is_file():
                matches.append((str(data.get("created_at", "")), html))
    return max(matches)[1] if matches else None


def build_request(
    request_type: str,
    plan: list[PlannedWrite],
    ticket: str | None = None,
    runs_dir: Path = Path("runs"),
    evidence_dir: Path = Path("evidence"),
    live_dir: Path = Path("live"),
    label_checks: LabelChecks | None = None,
    min_pass_rate: float | None = None,
    window: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Everything the approver sees, as JSON. Raises ApprovalError when there is no skill or
    it fails validation (SkillInvalid is a ValueError too)."""
    skill = find_skill(request_type)
    if skill is None:
        raise ApprovalError(f"the request type {request_type} has no skill; nothing to approve")
    require_valid_skill(request_type)
    version = skill.version
    checks = checks_for(request_type)

    blocks: list[str] = []
    replay = find_replay(runs_dir, request_type, version)
    replay_json: dict[str, Any] | None = None
    evidence: Path | None = None
    if replay is None:
        blocks.append(f"VERSION {short(version)} OF THIS SKILL HAS NOT BEEN REPLAYED")
    else:
        run_dir, run = replay
        gate = gate_for_run(run, label_checks or LabelChecks(), min_pass_rate)
        replay_json = {
            "run_dir": str(run_dir),
            "run_id": run.run_id,
            "created_at": run.created_at.isoformat(),
            "result_line": result_line(run.summary, "past tickets"),
            "summary": run.summary.model_dump(),
            "go_live": gate_dict(gate),
        }
        if not gate.passed:
            blocks.append(
                f"THE REPLAY OF VERSION {short(version)} FAILED THE GO-LIVE GATE: "
                + "; ".join(gate.reasons).upper()
            )
        evidence = find_evidence(evidence_dir, run_dir)

    planned = []
    uncovered = []
    for write in plan:
        check = checks.get(write.tool)
        if check is None:
            uncovered.append(write.tool)
        planned.append(
            {
                "write": write.model_dump(),
                "check": check.render(write.args) if check is not None else None,
            }
        )
    if uncovered:
        blocks.append(
            "THE PLAN INCLUDES WRITES THE SKILL DOES NOT COVER: "
            + ", ".join(sorted(set(uncovered)))
        )

    live_json: dict[str, Any] | None = None
    warnings: list[str] = []
    if (Path(live_dir) / RUNS_FILE).is_file():
        report = build_report(Path(live_dir), window, skills=[request_type])
        if report.skills:
            item = report.skills[0]
            live_json = item.model_dump(mode="json") | {"line": item.line()}
            if item.degraded:
                warnings.append(
                    "WARNING: THIS SKILL IS FLAGGED DEGRADED IN LIVE USE: "
                    + "; ".join(item.reasons).upper()
                )
    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "replaybook_version": __version__,
        "request_type": request_type,
        "skill": skill.name,
        "skill_version": version,
        "skill_version_short": short(version),
        "skill_path": str(skill.path),
        "ticket": ticket,
        "blocked": bool(blocks),
        "blocks": blocks,
        "warnings": warnings,
        "replay": replay_json,
        "live": live_json,
        "live_line": _live_line(live_json),
        "evidence_pack": str(evidence) if evidence else None,
        "plan": planned,
    }


def _live_line(live: dict[str, Any] | None) -> str:
    if not live or live.get("verification_rate") is None:
        return "no live runs yet" if not live else "no live runs with writes yet"
    return (
        f"{live['verification_rate']:.0%} of the last {live['with_writes']} live runs with writes "
        f"verified (window {live['window']}, {live['runs_on_current_version']} of "
        f"{live['runs_in_window']} runs on this version)"
    )


def render_markdown(request: dict[str, Any]) -> str:
    on = f" on {request['ticket']}" if request.get("ticket") else ""
    lines = [f"# Approval request: {request['skill']}{on}", ""]
    for block in request["blocks"]:
        lines += [f"> **BLOCKED: {block}. DO NOT APPROVE.**", ""]
    for warning in request["warnings"]:
        lines += [f"> **{warning}**", ""]
    replay = request["replay"]
    if replay:
        when = replay["created_at"][:10]
        replay_text = f"{replay['result_line']} on {when} (run `{replay['run_dir']}`)"
        gate = replay["go_live"]["line"]
    else:
        replay_text = "none for this version"
        gate = "-"
    evidence = (
        f"`{request['evidence_pack']}`"
        if request["evidence_pack"]
        else "none found for this replay; write one with `replaybook evidence --run <run dir>`"
    )
    lines += [
        "| | |",
        "|---|---|",
        f"| Skill | {request['skill']} (request type `{request['request_type']}`) |",
        f"| Version | `{request['skill_version_short']}` (SHA-256 of `{request['skill_path']}`) |",
        f"| Ticket | {request.get('ticket') or '-'} |",
        f"| Replay | {replay_text} |",
        f"| Go-live gate | {gate} |",
        f"| Live verification | {request['live_line']} |",
        f"| Evidence pack | {evidence} |",
        "",
        "## Planned writes and their checks",
        "",
    ]
    for index, item in enumerate(request["plan"], 1):
        write = item["write"]
        lines.append(f"{index}. `{write['tool']}` `{json.dumps(write['args'], sort_keys=True)}`")
        check = item["check"]
        if check:
            lines.append(
                f"   - Check after the write: `{check['tool']}` "
                f"`{json.dumps(check['args'], sort_keys=True)}` must show "
                f"`{json.dumps(check['expect'], sort_keys=True)}` within {check['timeout_s']:.0f} s"
            )
        else:
            lines.append("   - **No check: the skill does not cover this write.**")
    lines += [
        "",
        "## Decision",
        "",
        "Approve or reject in your approval channel. Replaybook never approves: the runtime "
        "makes the writes only after a person approves, runs each check, and reports the "
        "decision and the results with `replaybook live record`. If a check does not pass, "
        "the runtime stops and hands the ticket back.",
        "",
        f"Generated by Replaybook {request['replaybook_version']} at {request['generated_at']}.",
        "",
    ]
    return "\n".join(lines)


def write_request(request: dict[str, Any], out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / MARKDOWN_FILE
    js = out_dir / JSON_FILE
    md.write_text(render_markdown(request), encoding="utf-8")
    js.write_text(json.dumps(request, indent=2, default=str), encoding="utf-8")
    return {"markdown": md, "json": js}
