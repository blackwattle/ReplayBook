"""The evidence pack: one self-contained HTML file, a JSON twin and a hashed manifest.

It covers scope and fingerprints (with the skill's name and version), test-set source and
selection, the outcome counts and pass rate with a Wilson interval per request type, the
go-live gate, failure groups with two side-by-side examples, the planned checks (each
write the agent planned with the read-only check that would confirm it live; replay never
runs them), accepted differences, changes since the baseline, shadow-mode results when a
shadow directory is given, go-live metrics when live records are given (each skill's
rolling approval, verification and escalation rates and hours saved), a sign-off block,
limitations, and SHA-256 hashes of every input and output.

The verdict at the top is the go-live gate (eval.golive): the minimum pass rate, no unsafe
outcome, no agent error, the escalation labels spot-checked and the skill's current
version. The older gate (no critical unsafe action, no agent error) is shown beside it.
"""

from __future__ import annotations

import hashlib
import html
import json
import platform
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from replaybook import __version__, netguard
from replaybook.checks import checks_for
from replaybook.eval.accepted import AcceptedDifferences
from replaybook.eval.baseline import Baseline, CheckReport
from replaybook.eval.golive import GoLiveGate, gate_dict, gate_for_run, min_pass_rate
from replaybook.eval.groups import FailureGroup, group_failures
from replaybook.eval.labels import LabelChecks
from replaybook.eval.live import DEGRADED_FILE, RUNS_FILE, LiveReport
from replaybook.eval.runner import RunResult, failure_line, result_line, usefulness_line
from replaybook.eval.scoring import OUTCOME_LABELS, OUTCOMES, REASON_LABELS
from replaybook.eval.shadow import PROPOSALS_FILE, REPORT_FILE, SCORES_FILE, ShadowReport
from replaybook.schema.testcase import TestSet
from replaybook.skills import short

HTML_FILE = "evidence.html"
JSON_FILE = "evidence.json"
MANIFEST_FILE = "manifest.json"


@dataclass
class RunBundle:
    """Everything the pack knows about one request type."""

    run: RunResult
    test_set: TestSet
    run_dir: Path | None = None
    test_set_path: Path | None = None
    baseline: Baseline | None = None
    check: CheckReport | None = None
    groups: list[FailureGroup] = field(default_factory=list)
    gate: GoLiveGate | None = None


@dataclass
class EvidenceInputs:
    title: str
    bundles: list[RunBundle]
    accepted: AcceptedDifferences = field(default_factory=AcceptedDifferences)
    accepted_path: Path | None = None
    baseline_dir: Path | None = None
    data_dir: Path | None = None
    data_card: str | None = None
    quality: dict[str, Any] = field(default_factory=dict)
    agent_description: str = ""
    extra_limitations: list[str] = field(default_factory=list)
    today: date | None = None
    # Shadow mode: the report and the directory its files live in (hashed into the manifest).
    shadow: ShadowReport | None = None
    shadow_dir: Path | None = None
    # The label spot-checks the go-live gate reads, the file they came from (hashed into the
    # manifest) and the minimum pass rate (None: REPLAYBOOK_MIN_PASS_RATE or 0.9).
    label_checks: LabelChecks = field(default_factory=LabelChecks)
    labels_path: Path | None = None
    min_pass_rate: float | None = None
    # Live runs after go-live (eval.live): the report and its directory (hashed into the
    # manifest), when given.
    live: LiveReport | None = None
    live_dir: Path | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_evidence(inputs: EvidenceInputs, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for bundle in inputs.bundles:
        if not bundle.groups:
            bundle.groups = group_failures(bundle.run.results)
        bundle.gate = gate_for_run(bundle.run, inputs.label_checks, inputs.min_pass_rate)
    today = inputs.today or date.today()
    manifest_inputs = _input_files(inputs)
    data = _as_json(inputs, manifest_inputs, today)
    html_text = render_html(data)
    html_path = out_dir / HTML_FILE
    json_path = out_dir / JSON_FILE
    html_path.write_text(html_text, encoding="utf-8")
    json_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "replaybook_version": __version__,
        "algorithm": "sha256",
        "inputs": manifest_inputs,
        "outputs": [
            {
                "path": HTML_FILE,
                "sha256": sha256_file(html_path),
                "bytes": html_path.stat().st_size,
            },
            {
                "path": JSON_FILE,
                "sha256": sha256_file(json_path),
                "bytes": json_path.stat().st_size,
            },
        ],
    }
    manifest_path = out_dir / MANIFEST_FILE
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"html": html_path, "json": json_path, "manifest": manifest_path}


def verify_manifest(out_dir: Path) -> list[str]:
    """Return the paths whose hash no longer matches the manifest (empty means intact)."""
    out_dir = Path(out_dir)
    manifest = json.loads((out_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    broken = []
    for entry in manifest["outputs"]:
        path = out_dir / entry["path"]
        if not path.exists() or sha256_file(path) != entry["sha256"]:
            broken.append(entry["path"])
    for entry in manifest["inputs"]:
        path = Path(entry["path"])
        if path.exists() and sha256_file(path) != entry["sha256"]:
            broken.append(entry["path"])
    return broken


# --- data ----------------------------------------------------------------------------


def _input_files(inputs: EvidenceInputs) -> list[dict[str, Any]]:
    candidates: list[tuple[str, Path | None]] = []
    for bundle in inputs.bundles:
        candidates.append((f"test set {bundle.test_set.name}", bundle.test_set_path))
        if bundle.run_dir:
            candidates.append((f"run results {bundle.run.run_id}", bundle.run_dir / "results.json"))
            candidates.append((f"check report {bundle.run.run_id}", bundle.run_dir / "check.json"))
        if inputs.baseline_dir:
            candidates.append(
                (
                    f"baseline {bundle.test_set.name}",
                    inputs.baseline_dir / f"{bundle.test_set.name}.json",
                )
            )
    candidates.append(("accepted differences", inputs.accepted_path))
    candidates.append(("label spot-checks", inputs.labels_path))
    if inputs.shadow_dir:
        for name in (PROPOSALS_FILE, SCORES_FILE, REPORT_FILE):
            candidates.append((f"shadow {name}", inputs.shadow_dir / name))
    if inputs.live_dir:
        for name in (RUNS_FILE, DEGRADED_FILE):
            candidates.append((f"live {name}", inputs.live_dir / name))
    if inputs.data_dir:
        for name in ("tickets.jsonl", "platform.yaml", "DATASET.md"):
            candidates.append((f"data {name}", inputs.data_dir / name))
    files = []
    seen: set[str] = set()
    for role, path in candidates:
        if path is None or not Path(path).exists() or str(path) in seen:
            continue
        seen.add(str(path))
        files.append(
            {
                "role": role,
                "path": str(path),
                "sha256": sha256_file(path),
                "bytes": Path(path).stat().st_size,
            }
        )
    return files


def _as_json(
    inputs: EvidenceInputs, manifest_inputs: list[dict[str, Any]], today: date
) -> dict[str, Any]:
    bundles = inputs.bundles
    gate = "PASS" if all(b.gate is not None and b.gate.passed for b in bundles) else "FAIL"
    critical_gate = "PASS" if all(b.run.summary.gate == "pass" for b in bundles) else "FAIL"
    active = [a for a in inputs.accepted.accepted if a.active_on(today)]
    expired = [a for a in inputs.accepted.accepted if not a.active_on(today)]
    return {
        "title": inputs.title,
        "generated_at": datetime.now(UTC).isoformat(),
        "replaybook_version": __version__,
        # The go-live gate across every request type; critical_gate is the older one (no
        # critical unsafe action, no agent error) that `check` bases its exit codes on.
        "gate": gate,
        "critical_gate": critical_gate,
        "min_pass_rate": min_pass_rate(inputs.min_pass_rate),
        "go_live": [
            {"request_type": b.test_set.request_type, **gate_dict(b.gate)}
            for b in bundles
            if b.gate is not None
        ],
        "agent_description": inputs.agent_description,
        "scope": [
            {
                "request_type": b.test_set.request_type,
                "test_set": b.test_set.name,
                "run_id": b.run.run_id,
                "run_created_at": b.run.created_at.isoformat(),
                "agent_config": b.run.agent_config,
                "model": b.run.agent_config.get("model", "none (deterministic scripted agent)"),
                "skill_sha256": b.run.agent_config.get("skill_sha256"),
                "skill": b.run.skill,
                "skill_version": b.run.skill_version,
                "skill_version_short": short(b.run.skill_version),
                "tool_fingerprint": b.run.tool_fingerprint,
                "test_set_sha256": b.run.test_set_sha256,
                "fingerprint": b.run.fingerprint,
                "baseline_fingerprint": b.baseline.fingerprint if b.baseline else None,
                "baseline_approved_by": b.baseline.approved_by if b.baseline else None,
                "baseline_approved_at": b.baseline.approved_at.isoformat() if b.baseline else None,
                "seed": b.run.seed,
                "model_usage": b.run.model_usage,
                "model_cost": _model_cost_sentence(b.run.model_usage),
                "air_gapped": bool(b.run.environment.get("air_gapped", False)),
                "allowed_hosts": list(b.run.environment.get("allowed_hosts", [])),
                "refused_connections": int(b.run.environment.get("refused_connections", 0)),
            }
            for b in bundles
        ],
        "environment": _environment(bundles),
        "test_sets": [
            {
                "name": b.test_set.name,
                "request_type": b.test_set.request_type,
                "source": b.test_set.source,
                "data_dir": b.test_set.data_dir,
                "cases": len(b.test_set.cases),
                "unverified_expectations": sum(
                    not c.expectation.verified for c in b.test_set.cases
                ),
                "expectation_sources": _jsonable(
                    b.test_set.selection.get("expectation_sources", {})
                ),
                "escalate_cases": b.run.summary.escalate_cases,
                "labels": b.gate.labels.model_dump(mode="json") if b.gate else None,
                "labels_line": b.gate.labels.line() if b.gate else "",
                "selection": _jsonable(b.test_set.selection),
            }
            for b in bundles
        ],
        "pass_rates": [
            {
                "request_type": b.test_set.request_type,
                **b.run.summary.model_dump(),
                "result_line": result_line(b.run.summary, "past tickets"),
                "failure_line": failure_line(b.run.summary),
                "usefulness_line": usefulness_line(b.run.summary),
            }
            for b in bundles
        ],
        "agent_errors": [
            {"request_type": b.test_set.request_type, "case_id": r.case_id, "error": r.agent_error}
            for b in bundles
            for r in b.run.results
            if r.outcome == "error"
        ],
        "failure_groups": [
            {"request_type": b.test_set.request_type, "groups": [g.model_dump() for g in b.groups]}
            for b in bundles
        ],
        "planned_checks": [_planned_checks_json(b) for b in bundles],
        "accepted": {
            "active": [a.model_dump(mode="json") for a in active],
            "expired": [a.model_dump(mode="json") for a in expired],
            "applied": [
                {
                    "request_type": b.test_set.request_type,
                    "case_id": r.case_id,
                    "accepted_by": r.accepted_by,
                    "reason": r.accepted_reason,
                    "tags": r.tags,
                }
                for b in bundles
                for r in b.run.results
                if r.outcome == "accepted"
            ],
        },
        "changes_since_baseline": [
            {
                "request_type": b.test_set.request_type,
                "check": b.check.model_dump(mode="json") if b.check else None,
                "baseline": {
                    "run_id": b.baseline.run_id,
                    "approved_by": b.baseline.approved_by,
                    "approved_at": b.baseline.approved_at.isoformat(),
                    "summary": b.baseline.summary.model_dump(),
                    "result_line": result_line(b.baseline.summary)
                    if b.baseline.summary.outcomes
                    else (
                        f"{b.baseline.summary.passed} pass, {b.baseline.summary.failed} fail, "
                        f"{b.baseline.summary.accepted} accepted of {b.baseline.summary.total}"
                    ),
                    "skill_version": b.baseline.skill_version,
                }
                if b.baseline
                else None,
            }
            for b in bundles
        ],
        "shadow": _shadow_json(inputs.shadow),
        "live": _live_json(inputs.live, [b.test_set.request_type for b in bundles]),
        "quality": inputs.quality,
        "limitations": _limitations(inputs, today),
        "sign_off": {
            "gate_verdict": gate,
            "recommendation": _recommendation(bundles, gate, critical_gate),
            "fields": [
                "Decision (approve / reject / approve with conditions)",
                "Name",
                "Role",
                "Date",
                "Signature",
            ],
        },
        "manifest_inputs": manifest_inputs,
        "data_card": inputs.data_card,
    }


def _planned_checks_json(bundle: RunBundle) -> dict[str, Any]:
    """The skill's declared checks and every write the agent planned in the run, with the
    check rendered for it. Replay is a dry run: none of these was executed."""
    try:
        declared = [c.as_dict() for c in checks_for(bundle.test_set.request_type).values()]
    except ValueError:
        declared = []
    rows = [
        {"case_id": r.case_id, **planned}
        for r in bundle.run.results
        for planned in r.planned_checks
    ]
    return {
        "request_type": bundle.test_set.request_type,
        "skill": bundle.run.skill,
        "declared": declared,
        "planned_writes": len(rows),
        "with_check": sum(1 for row in rows if row["check"] is not None),
        "rows": rows,
    }


def _live_json(report: LiveReport | None, request_types: list[str]) -> dict[str, Any] | None:
    """The go-live metrics of the pack's request types, when live records were given."""
    if report is None:
        return None
    skills = [s for s in report.skills if s.skill in request_types]
    return {
        "live_dir": report.live_dir,
        "window": report.window,
        "min_approval_rate": report.min_approval_rate,
        "min_verification_rate": report.min_verification_rate,
        "skills": [s.model_dump(mode="json") | {"line": s.line()} for s in skills],
        "without_runs": sorted(set(request_types) - {s.skill for s in skills}),
    }


def _shadow_json(report: ShadowReport | None) -> dict[str, Any] | None:
    if report is None:
        return None
    return {
        "shadow_dir": report.shadow_dir,
        "generated_at": report.generated_at.isoformat(),
        "request_types": report.request_types,
        "proposals": report.proposals,
        "scored": report.scored,
        "pending": report.pending,
        "unscorable": report.unscorable,
        "summary": report.summary.model_dump(),
        "unsafe_caught": report.unsafe_caught,
        "result_line": result_line(report.summary, "scored proposals"),
        "failure_line": failure_line(report.summary),
        "groups": [g.model_dump() for g in report.groups],
        "agents": report.agents,
        "unverified_expectations": report.unverified_expectations,
    }


def _model_cost_sentence(usage: dict[str, Any] | None) -> str:
    """How much the run's model calls cost and who says so, or that no model was used."""
    if not usage or not usage.get("calls"):
        return "No model calls: the agent under test used no model."
    sources = list(usage.get("cost_sources") or [])
    if "gateway" in sources and "measured" not in sources:
        measured = "reported by the gateway, not measured by Replaybook"
    elif "gateway" in sources:
        measured = "partly measured by Replaybook (LiteLLM pricing), partly reported by the gateway"
    else:
        measured = "measured by Replaybook (LiteLLM pricing)"
    text = (
        f"{usage['calls']} model call(s) ({usage.get('cached_calls', 0)} answered from the cache); "
        f"cost {usage.get('cost_usd', 0.0):.4f} USD, {measured}."
    )
    unknown = int(usage.get("unknown_cost_calls", 0))
    if unknown:
        text += (
            f" {unknown} call(s) could not be priced: the cost is unknown, not zero, so the "
            "figure is a lower bound."
        )
    return text


def _environment(bundles: list[RunBundle]) -> dict[str, Any]:
    """Air-gap facts for the runs and for the process writing this pack. Telemetry: none."""
    current = netguard.status()
    return {
        "runs_air_gapped": sum(1 for b in bundles if b.run.environment.get("air_gapped")),
        "runs_total": len(bundles),
        "pack_air_gapped": current["air_gapped"],
        "pack_allowed_hosts": current["allowed_hosts"],
        "pack_refused_connections": current["refused_connections"],
        "telemetry": "none",
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _recommendation(bundles: list[RunBundle], gate: str, critical_gate: str = "PASS") -> str:
    if critical_gate == "FAIL":
        return (
            "Do not approve. At least one run has a critical unsafe action or an agent error. "
            "Fix the agent and re-run `replaybook check`."
        )
    if gate == "FAIL":
        failing = "; ".join(
            f"{b.test_set.request_type}: {', '.join(b.gate.reasons)}"
            for b in bundles
            if b.gate is not None and not b.gate.passed
        )
        return (
            f"Do not approve for go-live yet. The go-live gate fails ({failing}). Fix what it "
            "names and re-run, or record the missing label spot-check with `replaybook labels "
            "check`."
        )
    worst = min((b.run.summary.wilson_low for b in bundles), default=0.0)
    return (
        f"The go-live gate passes for every request type: the pass rate meets the minimum, with "
        f"no unsafe outcome, no agent error and the escalation labels spot-checked. The lowest "
        f"95% confidence bound on the pass rate across request types is {worst:.0%}. Approve "
        f"if that bound meets your threshold for this change class; otherwise add cases or fix "
        f"the remaining failures."
    )


def knowledge_limitations(environments: list[dict[str, Any]]) -> list[str]:
    """One line per knowledge pack the runs could read: the one live source a replay may
    use. Pinned answers (cache: replay) are reproducible and say which cache they came from;
    anything else read a live index that may have changed since."""
    seen: dict[str, dict[str, Any]] = {}
    for environment in environments:
        for name, pack in (environment.get("knowledge_packs") or {}).items():
            seen.setdefault(name, pack)
            if pack.get("cache") != "replay":
                seen[name] = pack  # one live run makes the pack live in this pack of evidence
    items = []
    for name, pack in sorted(seen.items()):
        where = f"{pack.get('server')} over {pack.get('transport')}"
        if pack.get("cache") == "replay":
            items.append(
                f"The agent could search past tickets through the knowledge pack '{name}' "
                f"({where}). Every answer came from recorded responses (cache sha256 "
                f"{str(pack.get('cache_sha256'))[:12]}), behind the time-travel guard, so a "
                "re-run with the same cache sees the same results. The server's ranking was "
                "recorded, not checked: an index built after the tickets may rank with text "
                "written later."
            )
        else:
            items.append(
                f"The agent could search past tickets through the knowledge pack '{name}' "
                f"({where}), a live index read behind the time-travel guard. Only tickets "
                "created before each case's ticket were returned, but the index may have changed "
                "since, so a re-run can see different search results; set `cache: replay` in the "
                "pack to pin them. The server's ranking may use text written after the horizon."
            )
    return items


def _limitations(inputs: EvidenceInputs, today: date) -> list[str]:
    items = [
        "The environment is a mock. Reads answer from a small per-case world state and writes "
        "are recorded, never executed; behaviour against a live platform may differ.",
        "Expected actions come from what engineers recorded in past tickets. Where history was "
        "wrong or outdated, the difference has to be accepted explicitly, and acceptances expire.",
        "Replay is a dry run: the verification checks under Planned checks were rendered from the "
        "skill, never run. Whether they pass on the live platform is known only from live runs.",
    ]
    sources = {b.test_set.source for b in inputs.bundles}
    if "synthetic" in sources:
        items.append(
            "The tickets are synthetic. They are generated from a seed with hidden ground truth "
            "used only to check Replaybook itself; results say how well the harness catches "
            "known flaws, not how the agent performs on your real history."
        )
    covered = sorted({b.test_set.request_type for b in inputs.bundles})
    items.append(
        f"Only these request types were replayed: {', '.join(covered)}. Nothing here is evidence "
        "about other request types."
    )
    smallest = min((len(b.test_set.cases) for b in inputs.bundles), default=0)
    items.append(
        f"The smallest test set has {smallest} cases; the Wilson intervals show how much the pass "
        "rates could move with more data."
    )
    if any("flaws" in b.run.agent_config for b in inputs.bundles):
        items.append(
            "The agent under test is Replaybook's deterministic scripted reference agent, not a "
            "model-based agent. Use it to validate the harness, then replay your own agent."
        )
    for b in inputs.bundles:
        if b.run.summary.accepted:
            soon = [
                a
                for a in inputs.accepted.accepted
                if a.active_on(today) and (a.expires - today).days <= 30
            ]
            if soon:
                items.append(
                    f"{len(soon)} acceptance(s) expire within 30 days; the affected cases will fail again."
                )
            break
    unchecked = [
        b.test_set.request_type for b in inputs.bundles if b.gate and not b.gate.labels.checked
    ]
    if unchecked:
        items.append(
            "The expected outcomes (act or escalate) of "
            + ", ".join(unchecked)
            + " come from the labelling rule and no person has spot-checked them yet; an "
            "escalation result can be wrong because the label is."
        )
    automated = sorted(
        {r for b in inputs.bundles if b.gate for r in b.gate.labels.reviewers if "automated" in r}
    )
    if automated:
        items.append(
            "The label spot-check in this pack was recorded by an automated stand-in ("
            + "; ".join(automated)
            + "), not by a person. In real use a person checks the labels."
        )
    unverified = sum(not c.expectation.verified for b in inputs.bundles for c in b.test_set.cases)
    if unverified:
        total = sum(len(b.test_set.cases) for b in inputs.bundles)
        items.append(
            f"{unverified} of {total} expectations were extracted from ticket text (by rules or a "
            "model) and are unverified. Until a person confirms them, a mismatch may mean the "
            "expectation is wrong rather than the agent; this pack is a review aid, not evidence."
        )
    launchers = sorted(
        {
            str(b.run.agent_config.get("launcher"))
            for b in inputs.bundles
            if b.run.agent_config.get("agent") == "launcher"
        }
    )
    if launchers:
        items.append(
            f"The agent under test ran through the {', '.join(launchers)} launcher(s): an "
            "off-the-shelf harness with its built-in tools switched off as the launcher file "
            "describes. Replaybook records only calls to the mock servers; anything the harness "
            "did outside them (its model choice, a tool the launcher could not disable) is not in "
            "this pack."
        )
    if any(not b.run.environment.get("air_gapped") for b in inputs.bundles):
        items.append(
            "Not every run was made in air-gapped mode (REPLAYBOOK_AIRGAPPED). The tools are "
            "still local mocks, but the agent process was not stopped from reaching the network."
        )
    unknown_cost = sum(
        int((b.run.model_usage or {}).get("unknown_cost_calls", 0)) for b in inputs.bundles
    )
    if unknown_cost:
        items.append(
            f"{unknown_cost} model call(s) could not be priced (a gateway or a model LiteLLM has "
            "no price for). Their cost is recorded as unknown, not zero; the spend figures are "
            "lower bounds and the spend cap could not see those calls."
        )
    if any("gateway" in (b.run.model_usage or {}).get("cost_sources", []) for b in inputs.bundles):
        items.append(
            "Some model costs were reported by the gateway rather than measured by Replaybook; "
            "the gateway's own accounting is the authority for them."
        )
    items.extend(knowledge_limitations([b.run.environment for b in inputs.bundles]))
    if inputs.shadow is not None:
        items.append(
            "Shadow results score proposals the agent made against the mock environment at the "
            "time, against what engineers later did. Proposals on tickets that are still open "
            "are pending and say nothing yet; the agent never acted on any ticket."
        )
    items.extend(inputs.extra_limitations)
    return items


# --- html ----------------------------------------------------------------------------

CSS = """
:root { --ok:#1b7f3b; --bad:#b3261e; --warn:#8a6d00; --muted:#666; --line:#ddd; }
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 0; color:#111; }
main { max-width: 1100px; margin: 0 auto; padding: 24px; }
h1 { font-size: 26px; margin-bottom: 4px; } h2 { margin-top: 36px; border-bottom: 2px solid var(--line); padding-bottom: 4px; }
h3 { margin-bottom: 4px; } .muted { color: var(--muted); } table { border-collapse: collapse; width: 100%; margin: 8px 0 16px; font-size: 14px; }
th, td { border: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; } th { background: #f4f4f4; }
.badge { display:inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; color: #fff; }
.pass { background: var(--ok); } .fail { background: var(--bad); } .accepted { background: var(--warn); } .error { background: #555; }
.gate { font-size: 20px; padding: 8px 14px; border-radius: 6px; display: inline-block; margin: 8px 0; }
.card { border: 1px solid var(--line); border-radius: 8px; padding: 12px 16px; margin: 12px 0; }
.pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; } .pair h4 { margin: 6px 0; font-size: 13px; }
pre { background: #f7f7f7; padding: 8px; border-radius: 6px; overflow-x: auto; font-size: 12px; margin: 0; white-space: pre-wrap; }
code { font-size: 12px; } .hash { font-family: ui-monospace, Consolas, monospace; font-size: 11px; word-break: break-all; }
.sign td { height: 34px; } .toc a { margin-right: 14px; } ul { margin-top: 4px; }
"""


def _e(value: Any) -> str:
    return html.escape(str(value))


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _badge(outcome: str) -> str:
    return f'<span class="badge {_e(outcome)}">{_e(outcome)}</span>'


def _actions_table(actions: list[dict[str, Any]]) -> str:
    if not actions:
        return '<p class="muted">no writes</p>'
    rows = "".join(
        f"<tr><td><code>{_e(a['tool'])}</code>{' <span class=muted>(rejected by mock)</span>' if a.get('attempted_only') else ''}</td>"
        f"<td><pre>{_e(json.dumps(a['args'], indent=1, sort_keys=True))}</pre></td></tr>"
        for a in actions
    )
    return f"<table><tr><th>tool</th><th>arguments</th></tr>{rows}</table>"


def render_html(data: dict[str, Any]) -> str:
    gate = data["gate"]
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        f"<title>{_e(data['title'])}</title><style>{CSS}</style></head><body><main>",
        f"<h1>{_e(data['title'])}</h1>",
        f"<p class='muted'>Generated {_e(data['generated_at'])} by Replaybook {_e(data['replaybook_version'])}. "
        "Every tool used here is a local mock from a Replaybook pack; nothing touched real infrastructure.</p>",
        f"<div class='gate {'pass' if gate == 'PASS' else 'fail'} badge'>Go-live gate: {gate}</div>",
        f"<p>{_e(data['sign_off']['recommendation'])}</p>",
        f"<p class='muted'>Minimum pass rate {data['min_pass_rate']:.0%}; no unsafe outcome, no agent error, "
        f"escalation labels spot-checked, the skill's current version. Critical gate (no critical unsafe action, "
        f"no agent error): {_e(data['critical_gate'])}.</p>",
        "<p class='toc'>"
        + " ".join(
            f"<a href='#{anchor}'>{label}</a>"
            for anchor, label in [
                ("scope", "1 Scope"),
                ("testsets", "2 Test sets"),
                ("rates", "3 Outcomes and the go-live gate"),
                ("groups", "4 Failure groups"),
                ("checks", "5 Planned checks"),
                ("accepted", "6 Accepted differences"),
                ("changes", "7 Changes since baseline"),
                ("shadow", "8 Shadow mode"),
                ("live", "9 Live runs"),
                ("signoff", "10 Sign-off"),
                ("limits", "11 Limitations"),
                ("manifest", "12 Manifest"),
            ]
        )
        + "</p>",
    ]
    if data.get("agent_description"):
        parts.append(f"<p>{_e(data['agent_description'])}</p>")

    parts.append("<h2 id='scope'>1. Scope and fingerprints</h2>")
    parts.append(
        "<table><tr><th>Request type</th><th>Skill and version</th><th>Agent</th><th>Model</th><th>Run</th><th>Fingerprint</th><th>Baseline fingerprint</th><th>Tool schemas</th><th>Test set</th></tr>"
    )
    for s in data["scope"]:
        skill_cell = (
            f"{_e(s['skill'])}<br><span class='hash'>{_e(s['skill_version_short'])}</span>"
            if s.get("skill")
            else "<span class='muted'>no skill file</span>"
        )
        parts.append(
            f"<tr><td>{_e(s['request_type'])}</td><td>{skill_cell}</td><td><code>{_e(json.dumps(s['agent_config'], sort_keys=True))}</code></td>"
            f"<td>{_e(s['model'])}</td><td>{_e(s['run_id'])}<br><span class='muted'>{_e(s['run_created_at'][:19])}</span>"
            f"{('<br><span class=muted>air-gapped, ' + str(s['refused_connections']) + ' refused</span>') if s['air_gapped'] else ''}</td>"
            f"<td class='hash'>{_e(s['fingerprint'])}</td><td class='hash'>{_e(s['baseline_fingerprint'] or '-')}"
            f"{('<br><span class=muted>approved by ' + _e(s['baseline_approved_by']) + '</span>') if s['baseline_approved_by'] else ''}</td>"
            f"<td class='hash'>{_e(s['tool_fingerprint'])}</td><td class='hash'>{_e(s['test_set_sha256'])}</td></tr>"
        )
    parts.append("</table>")
    for s in data["scope"]:
        if s.get("model_usage"):
            parts.append(
                f"<p><b>Model usage, {_e(s['request_type'])}.</b> {_e(s['model_cost'])} "
                f"Models: {_e(', '.join(s['model_usage'].get('models') or []) or '-')}.</p>"
            )
    env = data["environment"]
    pack_hosts = ", ".join(env["pack_allowed_hosts"]) or "none"
    parts.append(
        f"<p><b>Environment.</b> {env['runs_air_gapped']} of {env['runs_total']} runs were made in air-gapped mode, "
        "where the process refuses every connection except an allowlisted model endpoint. This pack was written "
        f"{'in' if env['pack_air_gapped'] else 'outside'} air-gapped mode (allowed hosts: {_e(pack_hosts)}; "
        f"{env['pack_refused_connections']} connection attempts refused). Replaybook sends no telemetry. "
        f"Python {_e(env['python'])} on {_e(env['platform'])}.</p>"
    )

    parts.append("<h2 id='testsets'>2. Test-set source and selection</h2>")
    for ts in data["test_sets"]:
        sel = ts["selection"]
        parts.append(
            f"<div class='card'><h3>{_e(ts['name'])} <span class='muted'>({_e(ts['source'])}, {ts['cases']} cases"
            f"{(', ' + str(ts['unverified_expectations']) + ' unverified expectations') if ts.get('unverified_expectations') else ''})</span></h3>"
            f"<p>{_e(sel.get('method', ''))}: requested {sel.get('requested')}, selected {sel.get('selected')} of "
            f"{sel.get('eligible')} eligible; {sel.get('ask_cases')} cases expect the agent to ask for missing information; "
            f"{ts.get('escalate_cases', 0)} expect it to hand the ticket back (escalate). {_e(ts.get('labels_line', '').capitalize())}. "
            f"Typed by recorded actions: {sel.get('typed_by', {}).get('actions')}, by text: {sel.get('typed_by', {}).get('text')}. "
            f"Date range {_e(str(sel.get('date_range', {}).get('oldest'))[:10])} to {_e(str(sel.get('date_range', {}).get('newest'))[:10])}.</p>"
            f"<p class='muted'>Excluded: {_e(json.dumps(sel.get('excluded', {})))}. Tagged: {_e(json.dumps(sel.get('tagged', {})))}. "
            f"Source directory: {_e(ts['data_dir'] or '-')}</p></div>"
        )

    parts.append("<h2 id='rates'>3. Outcomes and the go-live gate</h2>")
    gates = {g["request_type"]: g for g in data.get("go_live", [])}
    for pr in data["pass_rates"]:
        g = gates.get(pr["request_type"])
        verdict = _badge("pass" if g and g["verdict"] == "PASS" else "fail") if g else ""
        parts.append(
            f"<p><b>{_e(pr['request_type'])}</b>: {_e(pr['result_line'])}; {_e(pr['failure_line'])}. "
            f"Pass rate {pr['pass_rate']:.0%} (95% Wilson interval {pr['wilson_low']:.0%} to {pr['wilson_high']:.0%}); "
            f"{_e(pr['usefulness_line'])}.<br>{verdict} {_e(g['line'] if g else '')}</p>"
        )
    header = "".join(f"<th>{_e(OUTCOME_LABELS[k].capitalize())}</th>" for k in OUTCOMES)
    parts.append(
        f"<table><tr><th>Request type</th><th>Cases</th>{header}<th>Pass rate</th><th>Unneeded escalations</th><th>Critical</th><th>Failures by reason</th></tr>"
    )
    for pr in data["pass_rates"]:
        reasons = (
            ", ".join(f"{REASON_LABELS.get(k, k)} {v}" for k, v in pr["by_reason"].items()) or "-"
        )
        counts = "".join(f"<td>{pr['outcomes'].get(k, 0)}</td>" for k in OUTCOMES)
        parts.append(
            f"<tr><td>{_e(pr['request_type'])}</td><td>{pr['total']}</td>{counts}<td>{pr['pass_rate']:.0%}</td>"
            f"<td>{pr['unneeded_escalation_rate']:.0%}</td><td>{pr['critical_failures']}</td><td>{_e(reasons)}</td></tr>"
        )
    parts.append(
        "</table><p class='muted'>A case passes when it matched what engineers did, correctly handed back a ticket that "
        "needed a person's judgement, or is an accepted difference. Unsafe covers destructive actions and any "
        "infrastructure write the engineers did not make. The unneeded-escalation rate is the share of cases that "
        "expected the agent to act which it handed back anyway: a usefulness measure, beside the pass rate. "
        "docs/replay-outcomes.md defines every outcome.</p>"
    )
    if data["agent_errors"]:
        parts.append(
            "<p>Agent errors: these cases could not be judged, and any error fails the gate.</p>"
            "<table><tr><th>Request type</th><th>Case</th><th>What went wrong</th></tr>"
        )
        for err in data["agent_errors"]:
            parts.append(
                f"<tr><td>{_e(err['request_type'])}</td><td>{_e(err['case_id'])}</td><td>{_e(err['error'])}</td></tr>"
            )
        parts.append("</table>")

    parts.append("<h2 id='groups'>4. Failure groups</h2>")
    any_groups = False
    for fg in data["failure_groups"]:
        for g in fg["groups"]:
            any_groups = True
            flag = (
                " <span class='badge accepted'>history likely outdated</span>"
                if g["likely_history_outdated"]
                else ""
            )
            sev = (
                f" <span class='badge fail'>{_e(g['severity'])}</span>" if g.get("severity") else ""
            )
            parts.append(
                f"<div class='card'><h3>{_e(fg['request_type'])} {_e(g['id'])}: {_e(g['label'])} "
                f"<span class='muted'>({g['count']} cases)</span>{sev}{flag}</h3><p>{_e(g['cause'])}</p>"
                f"<p class='muted'>Cases: {_e(', '.join(g['case_ids']))}</p><div class='pair'>"
            )
            for ex in g["examples"]:
                parts.append(
                    f"<div><h4>{_e(ex['case_id'])}: {_e(ex['summary'])}</h4>"
                    f"<p class='muted'>{_e(ex['detail'])}{(' Tags: ' + _e(', '.join(ex['tags']))) if ex['tags'] else ''}</p>"
                    f"<h4>Engineers did (history)</h4>{_actions_table(ex['expected'])}"
                    f"<h4>Agent did (recorded writes)</h4>{_actions_table(ex['recorded'])}</div>"
                )
            parts.append("</div></div>")
    if not any_groups:
        parts.append("<p>No failures to group.</p>")

    parts.append("<h2 id='checks'>5. Planned checks (dry run)</h2>")
    parts.append(
        "<p>Every write a skill makes declares a read-only check that confirms it. Replay never runs "
        "a check: it lists each write the agent planned with the check a live runtime would run after "
        "it (docs/verified-writes.md), so an approver sees how each change will be confirmed.</p>"
    )
    for pc in data.get("planned_checks", []):
        parts.append(
            f"<div class='card'><h3>{_e(pc['request_type'])} <span class='muted'>({_e(pc['skill'] or 'no skill file')}; "
            f"{pc['planned_writes']} planned write(s), {pc['with_check']} with a check)</span></h3>"
        )
        if pc["declared"]:
            parts.append(
                "<table><tr><th>Write</th><th>Check tool</th><th>Arguments</th><th>Must show</th><th>Timeout</th></tr>"
            )
            for c in pc["declared"]:
                parts.append(
                    f"<tr><td><code>{_e(c['write'])}</code></td><td><code>{_e(c['tool'])}</code></td>"
                    f"<td><pre>{_e(json.dumps(c['args'], sort_keys=True))}</pre></td>"
                    f"<td><pre>{_e(json.dumps(c['expect'], sort_keys=True))}</pre></td><td>{c['timeout_s']:.0f} s</td></tr>"
                )
            parts.append("</table>")
        else:
            parts.append(
                "<p class='muted'>The request type has no skill file, so no checks are declared.</p>"
            )
        if pc["rows"]:
            parts.append(
                f"<details><summary>Every planned write with its rendered check ({len(pc['rows'])})</summary>"
                "<table><tr><th>Case</th><th>Planned write</th><th>Check</th></tr>"
            )
            for row in pc["rows"]:
                check = row["check"]
                check_cell = (
                    f"<code>{_e(check['tool'])}</code> <pre>{_e(json.dumps(check['args'], sort_keys=True))}</pre>"
                    f"must show <pre>{_e(json.dumps(check['expect'], sort_keys=True))}</pre>within {check['timeout_s']:.0f} s"
                    if check
                    else f"<span class='muted'>{_e(row['note'])}</span>"
                )
                parts.append(
                    f"<tr><td>{_e(row['case_id'])}</td><td><code>{_e(row['write']['tool'])}</code> "
                    f"<pre>{_e(json.dumps(row['write']['args'], sort_keys=True))}</pre></td><td>{check_cell}</td></tr>"
                )
            parts.append("</table></details>")
        parts.append("</div>")

    parts.append("<h2 id='accepted'>6. Accepted differences and outdated history</h2>")
    acc = data["accepted"]
    if acc["active"] or acc["expired"]:
        parts.append(
            "<table><tr><th>Id</th><th>Kind</th><th>Scope</th><th>Match</th><th>Reason</th><th>Approver</th><th>Approved</th><th>Expires</th><th>Status</th></tr>"
        )
        for a in acc["active"] + acc["expired"]:
            status = "active" if a in acc["active"] else "expired"
            parts.append(
                f"<tr><td>{_e(a['id'])}</td><td>{_e(a['kind'])}</td><td>{_e(a['scope'])}</td><td class='hash'>{_e(a['match'])}</td>"
                f"<td>{_e(a['reason'])}</td><td>{_e(a['approver'])}</td><td>{_e(a['approved_at'])}</td><td>{_e(a['expires'])}</td>"
                f"<td>{_badge('accepted' if status == 'active' else 'fail')} {status}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<p>No accepted differences recorded.</p>")
    if acc["applied"]:
        parts.append(
            f"<p>Applied to {len(acc['applied'])} case(s) in these runs: "
            + _e(", ".join(f"{x['request_type']}/{x['case_id']}" for x in acc["applied"]))
            + "</p>"
        )

    parts.append("<h2 id='changes'>7. Changes since the baseline</h2>")
    for ch in data["changes_since_baseline"]:
        parts.append(f"<div class='card'><h3>{_e(ch['request_type'])}</h3>")
        if ch["baseline"]:
            b = ch["baseline"]
            parts.append(
                f"<p>Baseline run {_e(b['run_id'])}, approved by {_e(b['approved_by'])} on {_e(b['approved_at'][:10])}: "
                f"{_e(b['result_line'])}.</p>"
            )
        else:
            parts.append("<p class='muted'>No approved baseline for this test set.</p>")
        if ch["check"]:
            c = ch["check"]
            parts.append(f"<p>{_e(c['message'])}</p>")
            for label, items in (
                ("Regressions", c["regressions"]),
                ("Changed failures", c["changed"]),
                ("Improvements", c["improvements"]),
            ):
                if items:
                    parts.append(f"<h4>{label} ({len(items)})</h4><ul>")
                    for it in items:
                        parts.append(
                            f"<li>{_e(it['case_id'])}: {_e(it['before'])} &rarr; {_e(it['after'])}"
                            f"{(' (' + _e(it['reason_after']) + ')') if it.get('reason_after') else ''} {_e(it.get('detail', ''))}</li>"
                        )
                    parts.append("</ul>")
            if c["new_cases"] or c["removed_cases"]:
                parts.append(
                    f"<p class='muted'>New cases: {_e(', '.join(c['new_cases']) or '-')}. Removed: {_e(', '.join(c['removed_cases']) or '-')}.</p>"
                )
            if not (c["regressions"] or c["changed"] or c["improvements"]):
                parts.append("<p>No outcome differs from the baseline.</p>")
        parts.append("</div>")

    if data.get("quality"):
        parts.append(
            "<h2>Harness quality checks (synthetic data only)</h2><table><tr><th>Check</th><th>Result</th></tr>"
        )
        for k, v in data["quality"].items():
            parts.append(f"<tr><td>{_e(k)}</td><td>{_e(v)}</td></tr>")
        parts.append("</table>")

    parts.append("<h2 id='shadow'>8. Shadow mode</h2>")
    sh = data.get("shadow")
    if not sh:
        parts.append(
            "<p class='muted'>No shadow results were given (<code>replaybook evidence --shadow DIR</code>). "
            "Shadow mode records what the agent proposes on open tickets, without executing anything, and "
            "scores the proposals once engineers resolve the tickets.</p>"
        )
    else:
        s = sh["summary"]
        parts.append(
            f"<p>The agent was run in shadow on <b>{sh['proposals']}</b> open ticket(s) of {_e(', '.join(sh['request_types']) or '-')}; "
            f"nothing was executed. <b>{sh['scored']}</b> proposal(s) have since been scored against what engineers did, "
            f"<b>{sh['pending']}</b> are still waiting for the ticket to be resolved and {sh['unscorable']} could not be scored "
            f"(the ticket was a duplicate, was not done, or no expectation could be built).</p>"
        )
        if sh["scored"]:
            reasons = (
                ", ".join(f"{REASON_LABELS.get(k, k)} {v}" for k, v in s["by_reason"].items())
                or "-"
            )
            parts.append(
                f"<p>{_e(sh.get('result_line', ''))}; {_e(sh.get('failure_line', ''))}. Pass rate {s['pass_rate']:.0%} "
                f"(95% Wilson interval {s['wilson_low']:.0%} to {s['wilson_high']:.0%}); critical {s['critical_failures']}; "
                f"failures by reason: {_e(reasons)}.</p>"
            )
        if sh["unsafe_caught"]:
            parts.append(
                f"<h3>Unsafe proposals caught before the agent had any access ({len(sh['unsafe_caught'])})</h3>"
                "<table><tr><th>Ticket</th><th>Severity</th><th>Tools</th><th>Why</th><th>Ticket resolved</th></tr>"
            )
            for u in sh["unsafe_caught"]:
                parts.append(
                    f"<tr><td>{_e(u['ticket_id'])}</td><td>{_badge('fail')} {_e(u['severity'])}</td><td><code>{_e(', '.join(u['tools']))}</code></td>"
                    f"<td>{_e(u['detail'])}</td><td>{_e(u['resolved_at'][:10])}</td></tr>"
                )
            parts.append("</table>")
        for g in sh["groups"]:
            parts.append(
                f"<div class='card'><h3>Shadow {_e(g['id'])}: {_e(g['label'])} <span class='muted'>({g['count']} cases)</span></h3>"
                f"<p>{_e(g['cause'])}</p><p class='muted'>Tickets: {_e(', '.join(g['case_ids']))}</p><div class='pair'>"
            )
            for ex in g["examples"]:
                parts.append(
                    f"<div><h4>{_e(ex['case_id'])}: {_e(ex['summary'])}</h4><p class='muted'>{_e(ex['detail'])}</p>"
                    f"<h4>Engineers did (history)</h4>{_actions_table(ex['expected'])}"
                    f"<h4>Agent proposed (never executed)</h4>{_actions_table(ex['recorded'])}</div>"
                )
            parts.append("</div></div>")
        if sh["agents"]:
            parts.append(
                "<p class='muted'>Agents in shadow: "
                + "; ".join(
                    f"{_e(json.dumps(a['agent_config'], sort_keys=True))} ({a['proposals']} proposals, fingerprint {_e(a['fingerprint'][:12])})"
                    for a in sh["agents"]
                )
                + "</p>"
            )
        if sh.get("unverified_expectations"):
            parts.append(
                f"<p class='muted'>{sh['unverified_expectations']} of the scored expectations were extracted from ticket text and are unverified.</p>"
            )

    parts.append("<h2 id='live'>9. Live runs after go-live</h2>")
    lv = data.get("live")
    if not lv:
        parts.append(
            "<p class='muted'>No live records were given (<code>replaybook evidence --live DIR</code>). "
            "Once a skill is live, a team's runtime reports each run with <code>replaybook live record</code>; "
            "this section then shows each skill's rolling approval, verification and escalation rates and "
            "the hours saved.</p>"
        )
    else:
        parts.append(
            f"<p>Rates over each skill's last {lv['window']} live runs. A skill is degraded when its approval "
            f"rate falls below {lv['min_approval_rate']:.0%} or its verification rate below "
            f"{lv['min_verification_rate']:.0%}; hours saved count only runs whose every write was verified.</p>"
            "<table><tr><th>Skill</th><th>Runs</th><th>On this version</th><th>Approval</th><th>Verification</th>"
            "<th>Escalation</th><th>Hours saved (window / all)</th><th>State</th></tr>"
        )
        for sk in lv["skills"]:
            state = (
                f"{_badge('fail')} degraded: {_e('; '.join(sk['reasons']))}"
                if sk["degraded"]
                else _badge("pass")
            )
            parts.append(
                f"<tr><td>{_e(sk['skill'])}<br><span class='hash'>{_e(short(sk['current_version']))}</span></td>"
                f"<td>{sk['runs_in_window']} of {sk['runs_total']}</td><td>{sk['runs_on_current_version']}</td>"
                f"<td>{_rate(sk['approval_rate'])}</td><td>{_rate(sk['verification_rate'])} of {sk['with_writes']}</td>"
                f"<td>{_rate(sk['escalation_rate'])}</td><td>{sk['hours_saved_window']:.1f} / {sk['hours_saved_total']:.1f}"
                f"{(' (' + str(sk['hours_unknown']) + ' unknown)') if sk['hours_unknown'] else ''}</td><td>{state}</td></tr>"
            )
        parts.append("</table>")
        if lv["without_runs"]:
            parts.append(
                f"<p class='muted'>No live runs yet for {_e(', '.join(lv['without_runs']))}.</p>"
            )

    parts.append("<h2 id='signoff'>10. Sign-off</h2>")
    parts.append(
        f"<p>Gate verdict computed by Replaybook: <strong>{_e(gate)}</strong>. {_e(data['sign_off']['recommendation'])}</p>"
    )
    parts.append(
        "<table class='sign'>"
        + "".join(
            f"<tr><th style='width:40%'>{_e(f)}</th><td></td></tr>"
            for f in data["sign_off"]["fields"]
        )
        + "</table>"
    )

    parts.append("<h2 id='limits'>11. Limitations</h2><ul>")
    parts.extend(f"<li>{_e(item)}</li>" for item in data["limitations"])
    parts.append("</ul>")

    parts.append(
        "<h2 id='manifest'>12. Manifest of inputs</h2><p class='muted'>SHA-256 of every input. The output hashes are in manifest.json next to this file.</p>"
    )
    parts.append("<table><tr><th>Role</th><th>Path</th><th>Bytes</th><th>SHA-256</th></tr>")
    for f in data["manifest_inputs"]:
        parts.append(
            f"<tr><td>{_e(f['role'])}</td><td>{_e(f['path'])}</td><td>{f['bytes']}</td><td class='hash'>{_e(f['sha256'])}</td></tr>"
        )
    parts.append("</table>")
    if data.get("data_card"):
        parts.append("<h2>Appendix: data card</h2><pre>" + _e(data["data_card"]) + "</pre>")
    parts.append("</main></body></html>")
    return "\n".join(parts)
