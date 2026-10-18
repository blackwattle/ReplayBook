"""Replay a test set against an agent: parallel, resumable, deterministic in its output.

Each case gets a fresh copy of its world and its own recorder. Results are appended to
`cases.jsonl` as they finish so an interrupted run can resume, and the final `results.json`
is sorted by case id so thread scheduling never changes the output.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from replaybook import netguard
from replaybook.agents.base import AgentRunResult, AgentUnderTest
from replaybook.eval.accepted import AcceptedDifferences, apply_accepted
from replaybook.eval.scoring import CaseResult, score_case
from replaybook.eval.stats import wilson_interval
from replaybook.mock_env.knowledge import Corpus
from replaybook.mock_env.registry import (
    MockEnvironment,
    describe_all_tools,
    tool_schema_fingerprint,
)
from replaybook.schema.testcase import TestCase, TestSet

CASES_FILE = "cases.jsonl"
RESULTS_FILE = "results.json"


class RunSummary(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    accepted: int = 0
    errors: int = 0
    pass_rate: float = 0.0
    wilson_low: float = 0.0
    wilson_high: float = 0.0
    critical_failures: int = 0
    gate: str = "pass"
    by_reason: dict[str, int] = Field(default_factory=dict)


class RunResult(BaseModel):
    run_id: str
    created_at: datetime
    agent_config: dict[str, Any]
    test_set_name: str
    test_set_request_type: str
    test_set_sha256: str
    tool_fingerprint: str
    fingerprint: str
    seed: int = 0
    workers: int = 1
    # Air-gapped mode state of the process that made the run (see replaybook.netguard).
    environment: dict[str, Any] = Field(default_factory=dict)
    results: list[CaseResult] = Field(default_factory=list)
    summary: RunSummary = Field(default_factory=RunSummary)

    def result(self, case_id: str) -> CaseResult:
        for r in self.results:
            if r.case_id == case_id:
                return r
        raise KeyError(case_id)


def compute_fingerprint(
    agent_config: dict[str, Any], test_set_sha: str, tool_fingerprint: str
) -> str:
    """Hashes model id, skill text, tool schemas, agent config and test set (via their hashes)."""
    payload = json.dumps(
        {"agent": agent_config, "test_set": test_set_sha, "tools": tool_fingerprint},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def summarise(results: Iterable[CaseResult]) -> RunSummary:
    items = list(results)
    summary = RunSummary(total=len(items))
    for r in items:
        if r.outcome == "pass":
            summary.passed += 1
        elif r.outcome == "accepted":
            summary.accepted += 1
        elif r.outcome == "error":
            summary.errors += 1
        else:
            summary.failed += 1
        if r.outcome in ("fail", "error") and r.reason:
            summary.by_reason[r.reason] = summary.by_reason.get(r.reason, 0) + 1
        if r.is_critical:
            summary.critical_failures += 1
    ok = summary.passed + summary.accepted
    summary.pass_rate = ok / summary.total if summary.total else 0.0
    summary.wilson_low, summary.wilson_high = wilson_interval(ok, summary.total)
    summary.gate = "fail" if summary.critical_failures or summary.errors else "pass"
    summary.by_reason = dict(sorted(summary.by_reason.items()))
    return summary


def replay_case(
    case: TestCase, agent: AgentUnderTest, knowledge: Corpus | None = None
) -> CaseResult:
    env = MockEnvironment(case.world.model_copy(deep=True), knowledge=knowledge)
    started = time.perf_counter()
    error: str | None = None
    try:
        outcome: AgentRunResult = agent.run(env)
        stopped = outcome.stopped_reason
        transcript = outcome.transcript
        if outcome.stopped_reason in ("error", "offline") and outcome.error:
            error = outcome.error
    except Exception as exc:  # the agent is someone else's code; never let it kill the run
        stopped = "error"
        transcript = []
        error = f"{type(exc).__name__}: {exc}"
    result = score_case(
        case,
        env.recorder.writes,
        reads=len(env.recorder.reads),
        agent_stopped_reason=stopped,
        agent_error=error,
    )
    result.transcript = transcript
    result.duration_s = round(time.perf_counter() - started, 4)
    return result


def run_replay(
    test_set: TestSet,
    agent: AgentUnderTest,
    out_dir: Path,
    workers: int = 4,
    resume: bool = True,
    seed: int = 0,
    run_id: str | None = None,
    accepted: AcceptedDifferences | None = None,
    today: date | None = None,
    knowledge: Corpus | None = None,
) -> RunResult:
    """`knowledge` is the past-ticket corpus the agent may search, served behind the
    time-travel guard (only items created before each case's ticket)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases_path = out_dir / CASES_FILE
    if hasattr(agent, "set_seed"):
        agent.set_seed(seed)

    done: dict[str, CaseResult] = {}
    if resume and cases_path.exists():
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = CaseResult.model_validate_json(line)
                done[r.case_id] = r
    elif cases_path.exists():
        cases_path.unlink()

    todo = [c for c in test_set.cases if c.id not in done]
    lock = threading.Lock()

    def work(case: TestCase) -> CaseResult:
        result = replay_case(case, agent, knowledge)
        with lock, cases_path.open("a", encoding="utf-8") as handle:
            handle.write(result.model_dump_json())
            handle.write("\n")
        return result

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(work, case) for case in todo]
            for future in as_completed(futures):
                result = future.result()
                done[result.case_id] = result

    results = [done[c.id] for c in sorted(test_set.cases, key=lambda c: c.id)]
    if accepted is not None:
        results = apply_accepted(results, accepted, today)
    tool_fingerprint = tool_schema_fingerprint(describe_all_tools())
    test_set_sha = test_set.sha256()
    environment = netguard.summary()
    environment["knowledge"] = str(knowledge.path) if knowledge and knowledge.path else None
    run = RunResult(
        run_id=run_id or out_dir.name,
        created_at=datetime.now(UTC),
        agent_config=dict(agent.config),
        test_set_name=test_set.name,
        test_set_request_type=test_set.request_type,
        test_set_sha256=test_set_sha,
        tool_fingerprint=tool_fingerprint,
        fingerprint=compute_fingerprint(dict(agent.config), test_set_sha, tool_fingerprint),
        seed=seed,
        workers=workers,
        environment=environment,
        results=results,
        summary=summarise(results),
    )
    (out_dir / RESULTS_FILE).write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return run


def load_run(out_dir: Path) -> RunResult:
    return RunResult.model_validate_json((Path(out_dir) / RESULTS_FILE).read_text(encoding="utf-8"))


def reapply_accepted(
    out_dir: Path, accepted: AcceptedDifferences, today: date | None = None
) -> RunResult:
    """Re-read a finished run, apply the current acceptances and rewrite results.json."""
    run = load_run(out_dir)
    run.results = apply_accepted(run.results, accepted, today)
    run.summary = summarise(run.results)
    (Path(out_dir) / RESULTS_FILE).write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return run
