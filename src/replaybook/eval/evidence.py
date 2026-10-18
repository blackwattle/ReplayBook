"""The evidence pack: one self-contained HTML file, a JSON twin and a hashed manifest.

It covers scope and fingerprints, test-set source and selection, pass rates with Wilson
intervals per request type, failure groups with two side-by-side examples, accepted
differences, changes since the baseline, shadow-mode results when a shadow directory is
given, a sign-off block, limitations, and SHA-256 hashes of every input and output.
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
from replaybook.eval.accepted import AcceptedDifferences
from replaybook.eval.baseline import Baseline, CheckReport
from replaybook.eval.groups import FailureGroup, group_failures
from replaybook.eval.runner import RunResult
from replaybook.eval.scoring import REASON_LABELS
from replaybook.eval.shadow import PROPOSALS_FILE, REPORT_FILE, SCORES_FILE, ShadowReport
from replaybook.schema.testcase import TestSet

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
    if inputs.shadow_dir:
        for name in (PROPOSALS_FILE, SCORES_FILE, REPORT_FILE):
            candidates.append((f"shadow {name}", inputs.shadow_dir / name))
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
    gate = "PASS"
    for b in bundles:
        if b.run.summary.gate != "pass":
            gate = "FAIL"
    active = [a for a in inputs.accepted.accepted if a.active_on(today)]
    expired = [a for a in inputs.accepted.accepted if not a.active_on(today)]
    return {
        "title": inputs.title,
        "generated_at": datetime.now(UTC).isoformat(),
        "replaybook_version": __version__,
        "gate": gate,
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
                "tool_fingerprint": b.run.tool_fingerprint,
                "test_set_sha256": b.run.test_set_sha256,
                "fingerprint": b.run.fingerprint,
                "baseline_fingerprint": b.baseline.fingerprint if b.baseline else None,
                "baseline_approved_by": b.baseline.approved_by if b.baseline else None,
                "baseline_approved_at": b.baseline.approved_at.isoformat() if b.baseline else None,
                "seed": b.run.seed,
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
                "selection": _jsonable(b.test_set.selection),
            }
            for b in bundles
        ],
        "pass_rates": [
            {
                "request_type": b.test_set.request_type,
                **b.run.summary.model_dump(),
            }
            for b in bundles
        ],
        "failure_groups": [
            {"request_type": b.test_set.request_type, "groups": [g.model_dump() for g in b.groups]}
            for b in bundles
        ],
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
                }
                if b.baseline
                else None,
            }
            for b in bundles
        ],
        "shadow": _shadow_json(inputs.shadow),
        "quality": inputs.quality,
        "limitations": _limitations(inputs, today),
        "sign_off": {
            "gate_verdict": gate,
            "recommendation": _recommendation(bundles, gate),
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
        "groups": [g.model_dump() for g in report.groups],
        "agents": report.agents,
        "unverified_expectations": report.unverified_expectations,
    }


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


def _recommendation(bundles: list[RunBundle], gate: str) -> str:
    if gate == "FAIL":
        return (
            "Do not approve. At least one run has a critical unsafe action or an agent error. "
            "Fix the agent and re-run `replaybook check`."
        )
    worst = min((b.run.summary.wilson_low for b in bundles), default=0.0)
    return (
        f"No critical unsafe actions and no agent errors. The lowest 95% confidence bound on the "
        f"pass rate across request types is {worst:.0%}. Approve if that bound meets your "
        f"threshold for this change class; otherwise add cases or fix the remaining failures."
    )


def _limitations(inputs: EvidenceInputs, today: date) -> list[str]:
    items = [
        "The environment is a mock. Reads answer from a small per-case world state and writes "
        "are recorded, never executed; behaviour against a live platform may differ.",
        "Expected actions come from what engineers recorded in past tickets. Where history was "
        "wrong or outdated, the difference has to be accepted explicitly, and acceptances expire.",
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
    unverified = sum(not c.expectation.verified for b in inputs.bundles for c in b.test_set.cases)
    if unverified:
        total = sum(len(b.test_set.cases) for b in inputs.bundles)
        items.append(
            f"{unverified} of {total} expectations were extracted from ticket text (by rules or a "
            "model) and are unverified. Until a person confirms them, a mismatch may mean the "
            "expectation is wrong rather than the agent; this pack is a review aid, not evidence."
        )
    if any(not b.run.environment.get("air_gapped") for b in inputs.bundles):
        items.append(
            "Not every run was made in air-gapped mode (REPLAYBOOK_AIRGAPPED). The tools are "
            "still local mocks, but the agent process was not stopped from reaching the network."
        )
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
        f"<div class='gate {'pass' if gate == 'PASS' else 'fail'} badge'>Gate: {gate}</div>",
        f"<p>{_e(data['sign_off']['recommendation'])}</p>",
        "<p class='toc'>"
        + " ".join(
            f"<a href='#{anchor}'>{label}</a>"
            for anchor, label in [
                ("scope", "1 Scope"),
                ("testsets", "2 Test sets"),
                ("rates", "3 Pass rates"),
                ("groups", "4 Failure groups"),
                ("accepted", "5 Accepted differences"),
                ("changes", "6 Changes since baseline"),
                ("shadow", "7 Shadow mode"),
                ("signoff", "8 Sign-off"),
                ("limits", "9 Limitations"),
                ("manifest", "10 Manifest"),
            ]
        )
        + "</p>",
    ]
    if data.get("agent_description"):
        parts.append(f"<p>{_e(data['agent_description'])}</p>")

    parts.append("<h2 id='scope'>1. Scope and fingerprints</h2>")
    parts.append(
        "<table><tr><th>Request type</th><th>Agent</th><th>Model</th><th>Run</th><th>Fingerprint</th><th>Baseline fingerprint</th><th>Tool schemas</th><th>Test set</th></tr>"
    )
    for s in data["scope"]:
        parts.append(
            f"<tr><td>{_e(s['request_type'])}</td><td><code>{_e(json.dumps(s['agent_config'], sort_keys=True))}</code></td>"
            f"<td>{_e(s['model'])}</td><td>{_e(s['run_id'])}<br><span class='muted'>{_e(s['run_created_at'][:19])}</span>"
            f"{('<br><span class=muted>air-gapped, ' + str(s['refused_connections']) + ' refused</span>') if s['air_gapped'] else ''}</td>"
            f"<td class='hash'>{_e(s['fingerprint'])}</td><td class='hash'>{_e(s['baseline_fingerprint'] or '-')}"
            f"{('<br><span class=muted>approved by ' + _e(s['baseline_approved_by']) + '</span>') if s['baseline_approved_by'] else ''}</td>"
            f"<td class='hash'>{_e(s['tool_fingerprint'])}</td><td class='hash'>{_e(s['test_set_sha256'])}</td></tr>"
        )
    parts.append("</table>")
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
            f"{sel.get('eligible')} eligible; {sel.get('ask_cases')} cases expect the agent to ask for missing information. "
            f"Typed by recorded actions: {sel.get('typed_by', {}).get('actions')}, by text: {sel.get('typed_by', {}).get('text')}. "
            f"Date range {_e(str(sel.get('date_range', {}).get('oldest'))[:10])} to {_e(str(sel.get('date_range', {}).get('newest'))[:10])}.</p>"
            f"<p class='muted'>Excluded: {_e(json.dumps(sel.get('excluded', {})))}. Tagged: {_e(json.dumps(sel.get('tagged', {})))}. "
            f"Source directory: {_e(ts['data_dir'] or '-')}</p></div>"
        )

    parts.append("<h2 id='rates'>3. Pass rate per request type</h2>")
    parts.append(
        "<table><tr><th>Request type</th><th>Cases</th><th>Pass</th><th>Accepted</th><th>Fail</th><th>Error</th><th>Pass rate</th><th>95% Wilson interval</th><th>Critical</th><th>Gate</th><th>Failures by reason</th></tr>"
    )
    for pr in data["pass_rates"]:
        reasons = (
            ", ".join(f"{REASON_LABELS.get(k, k)} {v}" for k, v in pr["by_reason"].items()) or "-"
        )
        parts.append(
            f"<tr><td>{_e(pr['request_type'])}</td><td>{pr['total']}</td><td>{pr['passed']}</td><td>{pr['accepted']}</td>"
            f"<td>{pr['failed']}</td><td>{pr['errors']}</td><td>{pr['pass_rate']:.0%}</td>"
            f"<td>{pr['wilson_low']:.0%} to {pr['wilson_high']:.0%}</td><td>{pr['critical_failures']}</td>"
            f"<td>{_badge('pass' if pr['gate'] == 'pass' else 'fail')}</td><td>{_e(reasons)}</td></tr>"
        )
    parts.append(
        "</table><p class='muted'>Pass rate counts accepted differences as passes. Any critical unsafe action fails the gate whatever the pass rate.</p>"
    )

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

    parts.append("<h2 id='accepted'>5. Accepted differences and outdated history</h2>")
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

    parts.append("<h2 id='changes'>6. Changes since the baseline</h2>")
    for ch in data["changes_since_baseline"]:
        parts.append(f"<div class='card'><h3>{_e(ch['request_type'])}</h3>")
        if ch["baseline"]:
            b = ch["baseline"]
            parts.append(
                f"<p>Baseline run {_e(b['run_id'])}, approved by {_e(b['approved_by'])} on {_e(b['approved_at'][:10])}: "
                f"{b['summary']['passed']} pass, {b['summary']['failed']} fail, {b['summary']['accepted']} accepted of {b['summary']['total']}.</p>"
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

    parts.append("<h2 id='shadow'>7. Shadow mode</h2>")
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
                "<table><tr><th>Scored</th><th>Pass</th><th>Accepted</th><th>Fail</th><th>Error</th><th>Pass rate</th><th>95% Wilson interval</th><th>Critical</th><th>Failures by reason</th></tr>"
                f"<tr><td>{s['total']}</td><td>{s['passed']}</td><td>{s['accepted']}</td><td>{s['failed']}</td><td>{s['errors']}</td>"
                f"<td>{s['pass_rate']:.0%}</td><td>{s['wilson_low']:.0%} to {s['wilson_high']:.0%}</td><td>{s['critical_failures']}</td><td>{_e(reasons)}</td></tr></table>"
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

    parts.append("<h2 id='signoff'>8. Sign-off</h2>")
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

    parts.append("<h2 id='limits'>9. Limitations</h2><ul>")
    parts.extend(f"<li>{_e(item)}</li>" for item in data["limitations"])
    parts.append("</ul>")

    parts.append(
        "<h2 id='manifest'>10. Manifest of inputs</h2><p class='muted'>SHA-256 of every input. The output hashes are in manifest.json next to this file.</p>"
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
