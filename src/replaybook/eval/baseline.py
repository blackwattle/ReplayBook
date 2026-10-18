"""Baselines, fingerprints and regression-only checks.

`approve` stores an accepted run as the baseline for its test set. `check` recomputes the
fingerprint (model id, skill text, tool schemas, agent config and test set, all via their
hashes), re-runs when it changed and reports only the outcomes that differ from the
baseline. Exit codes: 0 pass, 1 regression or critical failure, 2 error.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from replaybook.agents.base import AgentUnderTest
from replaybook.eval.accepted import AcceptedDifferences
from replaybook.eval.runner import (
    RunResult,
    RunSummary,
    compute_fingerprint,
    run_replay,
)
from replaybook.mock_env.registry import describe_all_tools, tool_schema_fingerprint
from replaybook.schema.testcase import TestSet

BASELINE_DIR = Path("assurance/baselines")
GOOD = {"pass", "accepted"}


class Baseline(BaseModel):
    test_set_name: str
    run_id: str
    approved_by: str
    approved_at: datetime
    fingerprint: str
    agent_config: dict[str, Any]
    test_set_sha256: str
    tool_fingerprint: str
    summary: RunSummary
    outcomes: dict[str, dict[str, Any]]


class Change(BaseModel):
    case_id: str
    before: str
    after: str
    reason_before: str | None = None
    reason_after: str | None = None
    detail: str = ""


class CheckReport(BaseModel):
    test_set_name: str
    checked_at: datetime
    fingerprint_baseline: str | None
    fingerprint_now: str
    fingerprint_changed: bool
    reran: bool
    message: str
    regressions: list[Change] = Field(default_factory=list)
    improvements: list[Change] = Field(default_factory=list)
    changed: list[Change] = Field(default_factory=list)
    new_cases: list[str] = Field(default_factory=list)
    removed_cases: list[str] = Field(default_factory=list)
    critical_failures: int = 0
    errors: int = 0
    summary_now: RunSummary | None = None
    summary_baseline: RunSummary | None = None
    exit_code: int = 0

    @property
    def has_differences(self) -> bool:
        return bool(
            self.regressions
            or self.improvements
            or self.changed
            or self.new_cases
            or self.removed_cases
        )


def baseline_path(test_set_name: str, baseline_dir: Path = BASELINE_DIR) -> Path:
    return Path(baseline_dir) / f"{test_set_name}.json"


def approve(run: RunResult, approver: str, baseline_dir: Path = BASELINE_DIR) -> Baseline:
    if not approver.strip():
        raise ValueError("an approval needs an approver")
    baseline = Baseline(
        test_set_name=run.test_set_name,
        run_id=run.run_id,
        approved_by=approver.strip(),
        approved_at=datetime.now(UTC),
        fingerprint=run.fingerprint,
        agent_config=run.agent_config,
        test_set_sha256=run.test_set_sha256,
        tool_fingerprint=run.tool_fingerprint,
        summary=run.summary,
        outcomes={
            r.case_id: {
                "outcome": r.outcome,
                "reason": r.reason,
                "signature": r.signature,
                "severity": r.severity,
            }
            for r in run.results
        },
    )
    path = baseline_path(run.test_set_name, baseline_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
    return baseline


def load_baseline(test_set_name: str, baseline_dir: Path = BASELINE_DIR) -> Baseline | None:
    path = baseline_path(test_set_name, baseline_dir)
    if not path.exists():
        return None
    return Baseline.model_validate_json(path.read_text(encoding="utf-8"))


def compare(
    baseline: Baseline, run: RunResult
) -> tuple[list[Change], list[Change], list[Change], list[str], list[str]]:
    """Only what differs: regressions, improvements, changed failures, new and removed cases."""
    regressions: list[Change] = []
    improvements: list[Change] = []
    changed: list[Change] = []
    now = {r.case_id: r for r in run.results}
    for case_id, before in sorted(baseline.outcomes.items()):
        after = now.get(case_id)
        if after is None:
            continue
        was_good = before["outcome"] in GOOD
        is_good = after.outcome in GOOD
        detail = after.diffs[0].detail if after.diffs else (after.agent_error or "")
        change = Change(
            case_id=case_id,
            before=before["outcome"],
            after=after.outcome,
            reason_before=before.get("reason"),
            reason_after=after.reason,
            detail=detail,
        )
        if was_good and not is_good:
            regressions.append(change)
        elif not was_good and is_good:
            improvements.append(change)
        elif (
            not was_good
            and not is_good
            and (before.get("reason") != after.reason or before.get("signature") != after.signature)
        ):
            changed.append(change)
    new_cases = sorted(set(now) - set(baseline.outcomes))
    removed = sorted(set(baseline.outcomes) - set(now))
    return regressions, improvements, changed, new_cases, removed


def check(
    test_set: TestSet,
    agent: AgentUnderTest,
    out_dir: Path,
    baseline_dir: Path = BASELINE_DIR,
    accepted: AcceptedDifferences | None = None,
    force: bool = False,
    workers: int = 4,
    today: date | None = None,
    knowledge: Any = None,
) -> tuple[CheckReport, RunResult | None]:
    baseline = load_baseline(test_set.name, baseline_dir)
    tool_fp = tool_schema_fingerprint(describe_all_tools())
    fingerprint_now = compute_fingerprint(dict(agent.config), test_set.sha256(), tool_fp)
    checked_at = datetime.now(UTC)
    if baseline is None:
        return (
            CheckReport(
                test_set_name=test_set.name,
                checked_at=checked_at,
                fingerprint_baseline=None,
                fingerprint_now=fingerprint_now,
                fingerprint_changed=True,
                reran=False,
                message=f"no baseline for '{test_set.name}'; run `replaybook approve` first",
                exit_code=2,
            ),
            None,
        )
    fingerprint_changed = fingerprint_now != baseline.fingerprint
    if not fingerprint_changed and not force:
        return (
            CheckReport(
                test_set_name=test_set.name,
                checked_at=checked_at,
                fingerprint_baseline=baseline.fingerprint,
                fingerprint_now=fingerprint_now,
                fingerprint_changed=False,
                reran=False,
                message="fingerprint unchanged since the approved baseline; nothing to re-run",
                summary_baseline=baseline.summary,
                critical_failures=baseline.summary.critical_failures,
                exit_code=1 if baseline.summary.critical_failures else 0,
            ),
            None,
        )
    run = run_replay(
        test_set,
        agent,
        out_dir,
        workers=workers,
        resume=False,
        accepted=accepted,
        today=today,
        knowledge=knowledge,
    )
    regressions, improvements, changed, new_cases, removed = compare(baseline, run)
    what_changed = _what_changed(baseline, run)
    if run.summary.errors:
        exit_code = 2
    elif regressions or run.summary.critical_failures:
        exit_code = 1
    else:
        exit_code = 0
    report = CheckReport(
        test_set_name=test_set.name,
        checked_at=checked_at,
        fingerprint_baseline=baseline.fingerprint,
        fingerprint_now=fingerprint_now,
        fingerprint_changed=fingerprint_changed,
        reran=True,
        message=(
            f"fingerprint changed ({', '.join(what_changed) or 'forced re-run'}); re-ran "
            f"{run.summary.total} cases: {len(regressions)} regressions, "
            f"{len(improvements)} improvements, {len(changed)} changed failures"
        ),
        regressions=regressions,
        improvements=improvements,
        changed=changed,
        new_cases=new_cases,
        removed_cases=removed,
        critical_failures=run.summary.critical_failures,
        errors=run.summary.errors,
        summary_now=run.summary,
        summary_baseline=baseline.summary,
        exit_code=exit_code,
    )
    (Path(out_dir) / "check.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report, run


def _what_changed(baseline: Baseline, run: RunResult) -> list[str]:
    parts = []
    if baseline.agent_config != run.agent_config:
        keys = sorted(
            k
            for k in set(baseline.agent_config) | set(run.agent_config)
            if baseline.agent_config.get(k) != run.agent_config.get(k)
        )
        parts.append("agent config: " + ", ".join(keys))
    if baseline.test_set_sha256 != run.test_set_sha256:
        parts.append("test set")
    if baseline.tool_fingerprint != run.tool_fingerprint:
        parts.append("tool schemas")
    return parts
