"""`replaybook mcp`: the workflow as MCP tools over stdio, so any assistant can operate
Replaybook.

Every tool here calls the same function the CLI command of that name calls
(`replaybook.workflow`), with the same arguments, and reads and writes the same files on
disk relative to the directory the server was started in. The tools cover the workflow:
generate, build a test set, replay, report, groups, check, evidence, shadow record, score
and report, and the past-ticket search eval, plus read-only tools: the packs and
launchers, skill validation, the label sample to spot-check and, after go-live, each
skill's live rates. Results carry the result
line ("passed X of N (A matched, B correctly escalated, C accepted)") and the go-live gate.
Recording a live run is the team's runtime's job, not a tool here. Sign-off actions
(`accept`, `approve`, `labels check`) are deliberately not tools: an accepted difference,
an approved baseline and a label spot-check carry a person's name, and a person records
them at the CLI.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from replaybook import __version__, workflow
from replaybook.workflow import (
    DEFAULT_ACCEPTED,
    DEFAULT_BASELINES,
    DEFAULT_DATA_DIR,
    DEFAULT_LABELS,
)

SERVER_NAME = "replaybook"
INSTRUCTIONS = (
    "Replaybook tests an ops agent against a team's past tickets in a mock environment. "
    "The usual order: replaybook_list_packs to see the request types, replaybook_generate (or an "
    "imported dataset) for tickets, replaybook_build_testset per request type, "
    "replaybook_replay for an agent, replaybook_report and replaybook_groups to read the "
    "failures, replaybook_check against the approved baseline, replaybook_evidence for the "
    "pack a change board signs. Shadow mode: replaybook_shadow_record on open tickets, "
    "replaybook_shadow_score once they resolve, replaybook_shadow_report. Results say "
    "'passed X of N (A matched, B correctly escalated)' and give the go-live gate, which "
    "needs the escalation labels spot-checked: replaybook_labels_sample lists the cases a "
    "person should check. Paths are relative to the directory the server was started in. "
    "Every tool in the mock environment is a local mock; nothing touches real "
    "infrastructure. Accepting differences, approving a baseline and recording a label "
    "spot-check are done by a person at the command line, not here."
)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
READS = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def _guard(fn: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Run a workflow function; bad arguments and missing files come back to the client as
    an error result with the reason, not as a crash."""
    try:
        return fn(*args, **kwargs)
    except (workflow.WorkflowError, OSError, ValueError) as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def build_server() -> MCPServer:
    server = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS, version=__version__)

    @server.tool(annotations=READS)
    def replaybook_list_packs() -> dict[str, Any]:
        """List the loaded packs and the request types they define (the keys that
        replaybook_build_testset and replaybook_shadow_record accept)."""
        return _guard(workflow.packs)

    @server.tool(annotations=READS)
    def replaybook_list_launchers() -> dict[str, Any]:
        """List the launchers (off-the-shelf agent harnesses) that can be the agent under
        test, by name, with what each switches off."""
        from replaybook.agents.launchers import launchers

        reg = launchers()
        return {
            "message": "launchers: " + ", ".join(reg.by_name) if reg.launchers else "no launchers",
            "launchers": [
                {
                    "name": launcher.name,
                    "description": launcher.description,
                    "executable": launcher.executable,
                    "disabled_tools": list(launcher.disabled_tools),
                    "limitations": " ".join(launcher.limitations.split()),
                    "checked": launcher.checked,
                }
                for launcher in reg.launchers
            ],
        }

    @server.tool(annotations=WRITES)
    def replaybook_generate(
        out: str = str(DEFAULT_DATA_DIR), seed: int = 42, n: int = 3000
    ) -> dict[str, Any]:
        """Generate synthetic tickets with hidden ground truth into a dataset directory
        (tickets.jsonl, platform.yaml, DATASET.md). Same seed, same data."""
        return _guard(workflow.generate, out, seed, n)

    @server.tool(annotations=WRITES)
    def replaybook_build_testset(
        request_type: str,
        data: str = str(DEFAULT_DATA_DIR),
        n: int = 40,
        out: str | None = None,
        extract: str = "none",
    ) -> dict[str, Any]:
        """Build a test set from the newest tickets of one request type. `extract` (none,
        rules, model) derives unverified expectations for tickets without recorded actions.
        Default output: testsets/<request_type>.json."""
        return _guard(workflow.build_testset, request_type, data, n, out, extract)

    @server.tool(annotations=WRITES)
    def replaybook_replay(
        testset: str,
        out: str,
        agent: str = "scripted",
        flaws: str = "",
        command: str = "",
        workers: int = 4,
        seed: int = 0,
        resume: bool = True,
        accepted_file: str = str(DEFAULT_ACCEPTED),
        knowledge: str | None = None,
        labels_file: str = str(DEFAULT_LABELS),
        min_pass_rate: float | None = None,
    ) -> dict[str, Any]:
        """Replay an agent against a test set and score every case into a run directory.
        `agent` is scripted (with comma-separated `flaws`), skill, external (with `command`)
        or a launcher name. Returns the result line, the outcome counts, the critical gate,
        the go-live gate and the exit code the CLI would give."""
        return _guard(
            workflow.replay,
            testset,
            out,
            agent,
            flaws,
            command,
            workers,
            seed,
            resume,
            accepted_file,
            knowledge,
            "stdio",
            labels_file,
            min_pass_rate,
        )

    @server.tool(annotations=READS)
    def replaybook_report(
        run_dir: str, labels_file: str = str(DEFAULT_LABELS), min_pass_rate: float | None = None
    ) -> dict[str, Any]:
        """The result line and go-live gate of a finished run, every case that did not pass
        with its outcome and reason, and the correct escalations."""
        return _guard(workflow.report, run_dir, labels_file, min_pass_rate)

    @server.tool(annotations=WRITES)
    def replaybook_knowledge_eval(
        data: str = str(DEFAULT_DATA_DIR),
        sources: list[str] | None = None,
        out: str = "retrieval",
        n: int = 200,
        k: int = 10,
        since: str = "",
        until: str = "",
        query_chars: int = 1000,
        workers: int = 4,
    ) -> dict[str, Any]:
        """Score past-ticket search against history before any agent uses it: for the newest
        n tickets with links to (or mentions of) earlier tickets, search with the ticket as
        filed at its creation time, behind the time-travel guard, and report hit rate,
        recall, MRR and nDCG at k. sources: "builtin" (Replaybook's keyword search) and/or
        knowledge packs by name, compared side by side; a knowledge pack also reports the
        leaked results the guard removed. Writes retrieval.md and retrieval.json to out."""
        return _guard(
            workflow.knowledge_eval,
            data,
            sources,
            out,
            n,
            k,
            since,
            until,
            query_chars,
            workers,
        )

    @server.tool(annotations=READS)
    def replaybook_skills_validate(request_type: str = "") -> dict[str, Any]:
        """Validate the skills of the loaded packs (or one request type): every write a skill
        lists must have a read-only verification check whose templates name the write's own
        arguments. Replay, shadow record and approval requests refuse an invalid skill."""
        return _guard(workflow.skills_validate, request_type, None)

    @server.tool(annotations=READS)
    def replaybook_labels_sample(
        testset: str, labels_file: str = str(DEFAULT_LABELS)
    ) -> dict[str, Any]:
        """The cases of a test set a person should spot-check (at least 10 or 10%), with each
        expected outcome (act or escalate) and why the labelling rule gave it; cases an
        earlier spot-check covered come last. Recording the spot-check is `replaybook labels
        check`, by a person at the command line."""
        return _guard(workflow.labels_sample, testset, None, labels_file)

    @server.tool(annotations=WRITES)
    def replaybook_groups(run_dir: str, model: bool = False) -> dict[str, Any]:
        """Group a run's failures by cause, with two examples each; writes groups.json into
        the run directory. `model` has the fast model write each cause (within the spend
        cap); the rule text stays when it is offline, and each group says which wrote it."""
        return _guard(workflow.groups, run_dir, model)

    @server.tool(annotations=WRITES)
    def replaybook_check(
        testset: str,
        out: str,
        agent: str = "scripted",
        flaws: str = "",
        command: str = "",
        workers: int = 4,
        force: bool = False,
        baseline_dir: str = str(DEFAULT_BASELINES),
        accepted_file: str = str(DEFAULT_ACCEPTED),
        knowledge: str | None = None,
        go_live: bool = False,
        labels_file: str = str(DEFAULT_LABELS),
        min_pass_rate: float | None = None,
    ) -> dict[str, Any]:
        """Re-run against the approved baseline when the fingerprint changed and report only
        what differs (regressions, improvements, changed reasons, new and removed cases).
        exit_code: 0 pass, 1 regression or critical failure (or a failed go-live gate when
        `go_live`), 2 error (for example no baseline)."""
        return _guard(
            workflow.check,
            testset,
            out,
            agent,
            flaws,
            command,
            workers,
            force,
            baseline_dir,
            accepted_file,
            knowledge,
            "stdio",
            go_live,
            labels_file,
            min_pass_rate,
        )

    @server.tool(annotations=WRITES)
    def replaybook_evidence(
        run: list[str],
        out: str,
        testset: list[str] | None = None,
        title: str = "Replaybook evidence pack",
        baseline_dir: str = str(DEFAULT_BASELINES),
        accepted_file: str = str(DEFAULT_ACCEPTED),
        shadow: str | None = None,
        labels_file: str = str(DEFAULT_LABELS),
        min_pass_rate: float | None = None,
        live: str | None = None,
    ) -> dict[str, Any]:
        """Write the evidence pack (evidence.html, evidence.json, manifest.json) for one or
        more run directories, with the matching test sets in the same order and, optionally,
        a shadow directory and a live-runs directory. `gate` in the result is the go-live
        gate."""
        return _guard(
            workflow.evidence,
            run,
            out,
            testset,
            title,
            baseline_dir,
            accepted_file,
            shadow,
            labels_file,
            min_pass_rate,
            live,
        )

    @server.tool(annotations=WRITES)
    def replaybook_approval_request(
        skill: str,
        plan: list[dict[str, Any]],
        out: str,
        ticket: str | None = None,
        runs: str = "runs",
        evidence: str = "evidence",
        live_dir: str | None = None,
        labels_file: str = str(DEFAULT_LABELS),
        min_pass_rate: float | None = None,
        window: int | None = None,
    ) -> dict[str, Any]:
        """Render an approval request (approval-request.md and .json in `out`) for a skill's
        planned writes on a live ticket: version, replay result, go-live gate, live
        verification rate, each write with its check, the evidence pack. `blocked` is true
        when the version has no replay, its replay failed the gate, or the plan holds a
        write the skill does not cover. This never approves anything: a person decides."""
        return _guard(
            workflow.approval_request,
            skill,
            plan,
            out,
            ticket,
            runs,
            evidence,
            live_dir,
            labels_file,
            min_pass_rate,
            window,
        )

    @server.tool(annotations=READS)
    def replaybook_live_report(
        live_dir: str | None = None, window: int | None = None, skill: str = ""
    ) -> dict[str, Any]:
        """After go-live: each skill's rolling approval, verification and escalation rates
        over its last live runs, the hours saved, and which skills are degraded. Read-only:
        recording a live run is `replaybook live record`, done by the team's runtime."""
        return _guard(workflow.live_report, live_dir, window, skill)

    @server.tool(annotations=WRITES)
    def replaybook_shadow_record(
        request_type: str,
        out: str,
        data: str = str(DEFAULT_DATA_DIR),
        agent: str = "scripted",
        flaws: str = "",
        command: str = "",
        extract: str = "none",
        knowledge: str | None = None,
    ) -> dict[str, Any]:
        """Shadow mode: run the agent on every OPEN ticket of a request type against the
        mock and store what it proposed. Nothing is executed and nothing is written to the
        ticket system."""
        return _guard(
            workflow.shadow_record,
            request_type,
            out,
            data,
            agent,
            flaws,
            command,
            extract,
            knowledge,
        )

    @server.tool(annotations=WRITES)
    def replaybook_shadow_score(
        out: str, data: str = str(DEFAULT_DATA_DIR), extract: str = "none"
    ) -> dict[str, Any]:
        """Score stored shadow proposals whose tickets engineers have since resolved, from
        the current dataset."""
        return _guard(workflow.shadow_score, out, data, extract)

    @server.tool(annotations=READS)
    def replaybook_shadow_report(out: str) -> dict[str, Any]:
        """The shadow summary: proposals, scored, pending, unscorable, pass rate, unsafe
        proposals caught, failure groups."""
        return _guard(workflow.shadow_report, out)

    return server


def serve() -> None:
    """Run the server over stdio (what `replaybook mcp` does)."""
    build_server().run("stdio")
