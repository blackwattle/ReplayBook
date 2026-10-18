"""Scoring after go-live: the live runs a team's runtime reports, per skill.

Replaybook never executes a skill. A team's runtime runs the approved skill on a live
ticket, makes each write once a person has approved it, runs each write's check
(docs/verified-writes.md) and reports the run with `replaybook live record`. Replaybook
appends one record per run to `live/runs.jsonl` (the directory from `--live-dir`, else
REPLAYBOOK_LIVE_DIR, else `live`), computes the hours saved, and keeps each skill's rolling
rates over the last runs:

- approval rate: approved runs over all runs in the window;
- verification rate: runs whose every write was verified, over the runs that made writes;
- escalation rate: runs handed back to a person, over all runs in the window.

The window is `--window`, else REPLAYBOOK_LIVE_WINDOW, else the last 30 runs of the skill.
A skill is degraded when its approval rate falls below REPLAYBOOK_MIN_APPROVAL_RATE (0.8)
or its verification rate below REPLAYBOOK_MIN_VERIFICATION_RATE (0.95). Nothing here sends
anything anywhere: `replaybook live check` exits 1, `live report` and `replaybook status`
say so, and every change of a skill's state appends a line to `live/degraded.jsonl` for a
team's CI or scheduler to alert on.

Hours saved: a verified run (approved, at least one write, every write verified, not handed
back) saves the request type's median engineer-hours from the discovery report
(`discovery/discovery.json` unless told otherwise); every other run saves zero. When the
report has no figure for the type the hours are unknown, never guessed.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator

from replaybook.packs import registry
from replaybook.skills import short, skill_identity

RUNS_FILE = "runs.jsonl"
DEGRADED_FILE = "degraded.jsonl"
DEFAULT_LIVE_DIR = Path("live")
DEFAULT_DISCOVERY = Path("discovery/discovery.json")
ENV_LIVE_DIR = "REPLAYBOOK_LIVE_DIR"
ENV_WINDOW = "REPLAYBOOK_LIVE_WINDOW"
ENV_MIN_APPROVAL = "REPLAYBOOK_MIN_APPROVAL_RATE"
ENV_MIN_VERIFICATION = "REPLAYBOOK_MIN_VERIFICATION_RATE"
DEFAULT_WINDOW = 30
DEFAULT_MIN_APPROVAL = 0.8
DEFAULT_MIN_VERIFICATION = 0.95
RunVerification = Literal["verified", "verify_failed", "verify_error", "none"]


# --- settings -------------------------------------------------------------------------


def live_dir(value: Path | str | None = None) -> Path:
    if value:
        return Path(value)
    load_dotenv(override=False)
    return Path(os.environ.get(ENV_LIVE_DIR, "").strip() or DEFAULT_LIVE_DIR)


def window(value: int | None = None) -> int:
    if value is None:
        load_dotenv(override=False)
        raw = os.environ.get(ENV_WINDOW, "").strip()
        try:
            value = int(raw) if raw else DEFAULT_WINDOW
        except ValueError as exc:
            raise ValueError(f"{ENV_WINDOW} must be a whole number of runs") from exc
    if value < 1:
        raise ValueError("the window is at least one run")
    return value


def threshold(name: str, default: float, value: float | None = None) -> float:
    if value is None:
        load_dotenv(override=False)
        raw = os.environ.get(name, "").strip()
        try:
            value = float(raw) if raw else default
        except ValueError as exc:
            raise ValueError(f"{name} must be a number between 0 and 1") from exc
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} is a share between 0 and 1")
    return value


# --- the record -----------------------------------------------------------------------


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "rejected"]
    by: str
    reason: str | None = None
    at: datetime | None = None


class LiveWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    verification: Literal["verified", "verify_failed", "verify_error"]
    detail: str = ""


class Escalation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    # skill: the skill handed the ticket back; verification: a check failed and the runtime
    # stopped; person: someone took the ticket over.
    by: Literal["skill", "verification", "person"] = "skill"


class LiveRunReport(BaseModel):
    """What a team's runtime reports about one live run."""

    model_config = ConfigDict(extra="forbid")

    skill: str  # the request type, as in `approval-request --skill`
    skill_version: str
    ticket: str
    at: datetime | None = None
    approval: Approval
    writes: list[LiveWrite] = Field(default_factory=list)
    escalation: Escalation | None = None

    @field_validator("skill_version")
    @classmethod
    def _hex(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) < 12 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("skill_version is the skill's SHA-256, at least 12 hex characters")
        return value


class LiveRun(LiveRunReport):
    """One recorded live run, with what Replaybook computed from the report."""

    id: str
    recorded_at: datetime
    skill_name: str | None = None
    verification: RunVerification
    escalated: bool
    hours_saved: float | None
    hours_basis: str


def check_report(report: LiveRunReport) -> None:
    """The record must say what the contract says happened, or it is refused."""
    if not report.approval.by.strip():
        raise ValueError("approval.by names the person who approved or rejected the run")
    if report.approval.decision == "rejected" and report.writes:
        raise ValueError("a rejected run makes no writes; report the writes of approved runs only")
    unverified = [w for w in report.writes if w.verification != "verified"]
    if unverified and report.escalation is None:
        raise ValueError(
            f"{unverified[0].tool} was {unverified[0].verification}: the live contract stops the "
            "skill and hands the ticket back to a person, so report the escalation too"
        )
    if report.skill not in registry().request_types:
        known = ", ".join(sorted(registry().request_types)) or "none"
        raise ValueError(f"no request type '{report.skill}' in the loaded packs ({known})")


def overall_verification(writes: list[LiveWrite]) -> RunVerification:
    if not writes:
        return "none"
    statuses = {w.verification for w in writes}
    for status in ("verify_failed", "verify_error"):
        if status in statuses:
            return status  # type: ignore[return-value]
    return "verified"


def saved_hours(
    request_type: str, verified: bool, discovery: Path | None
) -> tuple[float | None, str]:
    """(hours, where the figure came from): the type's median hours for a verified run."""
    if not verified:
        return 0.0, "not a verified run"
    path = Path(discovery) if discovery is not None else DEFAULT_DISCOVERY
    if not path.is_file():
        return None, f"unknown: no discovery report at {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data.get("types", []):
        if item.get("key") == request_type:
            return float(item["median_hours"]), (
                f"median of {item.get('tickets')} past tickets in {path}"
            )
    return None, f"unknown: {path} has no figure for {request_type}"


def load_runs(directory: Path) -> list[LiveRun]:
    path = Path(directory) / RUNS_FILE
    if not path.exists():
        return []
    return [
        LiveRun.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def record_run(
    report: LiveRunReport,
    directory: Path,
    discovery: Path | None = None,
    now: datetime | None = None,
) -> LiveRun:
    """Append one live run to runs.jsonl and note any change of the skill's state."""
    check_report(report)
    now = now or datetime.now(UTC)
    directory = Path(directory)
    current = skill_identity(report.skill)
    version = report.skill_version
    if current["skill_version"] and current["skill_version"].startswith(version):
        version = current["skill_version"]
    verification = overall_verification(report.writes)
    escalated = report.escalation is not None
    verified = (
        report.approval.decision == "approved" and verification == "verified" and not escalated
    )
    hours, basis = saved_hours(report.skill, verified, discovery)
    runs = load_runs(directory)
    run = LiveRun(
        **report.model_dump(exclude={"skill_version"}),
        skill_version=version,
        id=f"LIVE-{len(runs) + 1}",
        recorded_at=now,
        skill_name=current["skill"],
        verification=verification,
        escalated=escalated,
        hours_saved=hours,
        hours_basis=basis,
    )
    if run.at is None:
        run.at = now
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / RUNS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(run.model_dump_json())
        handle.write("\n")
    note_transitions(directory, build_report(directory, skills=[report.skill], now=now))
    return run


# --- the report -----------------------------------------------------------------------


class SkillLive(BaseModel):
    skill: str
    skill_name: str | None = None
    current_version: str | None = None
    window: int
    runs_total: int = 0
    runs_in_window: int = 0
    runs_on_current_version: int = 0
    approved: int = 0
    rejected: int = 0
    approval_rate: float | None = None
    with_writes: int = 0
    verified: int = 0
    verify_failed: int = 0
    verify_error: int = 0
    verification_rate: float | None = None
    escalated: int = 0
    escalation_rate: float | None = None
    hours_saved_window: float = 0.0
    hours_saved_total: float = 0.0
    # Verified runs whose hours are unknown (no discovery figure for the type).
    hours_unknown: int = 0
    degraded: bool = False
    reasons: list[str] = Field(default_factory=list)
    last_run_at: datetime | None = None

    def line(self) -> str:
        rate = _pct
        text = (
            f"{self.skill} ({self.skill_name or '-'} {short(self.current_version)}): "
            f"{self.runs_in_window} of the last {self.window} runs ({self.runs_on_current_version} "
            f"on this version); approval {rate(self.approval_rate)}, verification "
            f"{rate(self.verification_rate)} of {self.with_writes} with writes, escalation "
            f"{rate(self.escalation_rate)}; hours saved {self.hours_saved_window:.1f} in the "
            f"window, {self.hours_saved_total:.1f} in all"
        )
        if self.hours_unknown:
            text += f" ({self.hours_unknown} verified run(s) with unknown hours)"
        if self.degraded:
            text += "; DEGRADED: " + "; ".join(self.reasons)
        return text


class LiveReport(BaseModel):
    live_dir: str
    generated_at: datetime
    window: int
    min_approval_rate: float
    min_verification_rate: float
    skills: list[SkillLive] = Field(default_factory=list)

    @property
    def degraded(self) -> list[SkillLive]:
        return [s for s in self.skills if s.degraded]


def build_report(
    directory: Path,
    window_runs: int | None = None,
    min_approval: float | None = None,
    min_verification: float | None = None,
    skills: list[str] | None = None,
    now: datetime | None = None,
) -> LiveReport:
    """Per skill, the rolling rates over its last `window_runs` runs and the hours saved."""
    size = window(window_runs)
    approval_floor = threshold(ENV_MIN_APPROVAL, DEFAULT_MIN_APPROVAL, min_approval)
    verification_floor = threshold(ENV_MIN_VERIFICATION, DEFAULT_MIN_VERIFICATION, min_verification)
    runs = load_runs(directory)
    by_skill: dict[str, list[LiveRun]] = {}
    for run in runs:
        by_skill.setdefault(run.skill, []).append(run)
    out = []
    for skill in sorted(by_skill):
        if skills is not None and skill not in skills:
            continue
        ordered = sorted(by_skill[skill], key=lambda r: (r.at or r.recorded_at, r.id))
        last = ordered[-size:]
        current = skill_identity(skill)
        item = SkillLive(
            skill=skill,
            skill_name=current["skill"] or ordered[-1].skill_name,
            current_version=current["skill_version"],
            window=size,
            runs_total=len(ordered),
            runs_in_window=len(last),
            runs_on_current_version=sum(r.skill_version == current["skill_version"] for r in last),
            approved=sum(r.approval.decision == "approved" for r in last),
            rejected=sum(r.approval.decision == "rejected" for r in last),
            with_writes=sum(bool(r.writes) for r in last),
            verified=sum(r.verification == "verified" for r in last),
            verify_failed=sum(r.verification == "verify_failed" for r in last),
            verify_error=sum(r.verification == "verify_error" for r in last),
            escalated=sum(r.escalated for r in last),
            hours_saved_window=round(sum(r.hours_saved or 0.0 for r in last), 2),
            hours_saved_total=round(sum(r.hours_saved or 0.0 for r in ordered), 2),
            hours_unknown=sum(r.hours_saved is None for r in ordered),
            last_run_at=ordered[-1].at or ordered[-1].recorded_at,
        )
        item.approval_rate = item.approved / len(last) if last else None
        item.verification_rate = item.verified / item.with_writes if item.with_writes else None
        item.escalation_rate = item.escalated / len(last) if last else None
        if item.approval_rate is not None and item.approval_rate < approval_floor:
            item.reasons.append(
                f"approval rate {item.approval_rate:.0%} is below {approval_floor:.0%}"
            )
        if item.verification_rate is not None and item.verification_rate < verification_floor:
            item.reasons.append(
                f"verification rate {item.verification_rate:.0%} is below {verification_floor:.0%}"
            )
        item.degraded = bool(item.reasons)
        out.append(item)
    return LiveReport(
        live_dir=str(directory),
        generated_at=now or datetime.now(UTC),
        window=size,
        min_approval_rate=approval_floor,
        min_verification_rate=verification_floor,
        skills=out,
    )


def load_transitions(directory: Path) -> list[dict[str, Any]]:
    path = Path(directory) / DEGRADED_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def note_transitions(directory: Path, report: LiveReport) -> list[dict[str, Any]]:
    """Append a line to degraded.jsonl whenever a skill becomes degraded or recovers; a
    skill that stays in the same state adds nothing. Returns the lines written."""
    last: dict[str, str] = {}
    for line in load_transitions(directory):
        last[line["skill"]] = line["state"]
    written = []
    for item in report.skills:
        state = "degraded" if item.degraded else "recovered"
        if last.get(item.skill, "recovered") == state:
            continue
        entry = {
            "at": report.generated_at.isoformat(),
            "skill": item.skill,
            "skill_version": item.current_version,
            "state": state,
            "reasons": list(item.reasons),
            "approval_rate": item.approval_rate,
            "verification_rate": item.verification_rate,
            "escalation_rate": item.escalation_rate,
            "window": item.window,
            "runs_in_window": item.runs_in_window,
        }
        written.append(entry)
    if written:
        Path(directory).mkdir(parents=True, exist_ok=True)
        with (Path(directory) / DEGRADED_FILE).open("a", encoding="utf-8") as handle:
            for entry in written:
                handle.write(json.dumps(entry, sort_keys=True))
                handle.write("\n")
    return written


def verification_for(
    directory: Path, skill: str, window_runs: int | None = None
) -> SkillLive | None:
    """The live figures of one skill, or None when it has no live runs yet."""
    report = build_report(directory, window_runs, skills=[skill])
    return report.skills[0] if report.skills else None


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"
