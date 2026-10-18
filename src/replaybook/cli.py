"""Command-line entry point. Every command lives here so `replaybook --help` is the map."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from replaybook import __version__

app = typer.Typer(
    help="Test ops agents against your own past tickets in a mock environment.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_DATA_DIR = Path("data/generated")
DEFAULT_ACCEPTED = Path("assurance/accepted.yaml")


@app.callback()
def main() -> None:
    """Replaybook: replay ops agents against past tickets in a mock environment."""
    from replaybook import netguard

    netguard.install_from_env()


@app.command()
def version() -> None:
    """Print the Replaybook version."""
    typer.echo(__version__)


@app.command()
def status() -> None:
    """Show air-gapped mode, allowed hosts, model configuration and telemetry (there is none)."""
    from replaybook import netguard
    from replaybook.llm.client import LLMClient, LLMConfig

    config = LLMConfig.from_env()
    client = LLMClient(config)  # probes the model endpoint; air-gapped mode refuses it
    current = netguard.status()
    typer.echo(f"replaybook {__version__}")
    typer.echo(
        f"air-gapped mode: {'on' if current['air_gapped'] else 'off'} "
        f"(REPLAYBOOK_AIRGAPPED={'true' if current['air_gapped'] else 'false'})"
    )
    typer.echo(
        f"allowed hosts: {', '.join(current['allowed_hosts']) or '(none)'} "
        "(REPLAYBOOK_ALLOWED_HOSTS)"
    )
    state = f"offline: {client.offline_reason}" if client.offline else "online"
    typer.echo(f"models: fast={config.model_fast} strong={config.model_strong}; {state}")
    if current["air_gapped"] and not client.offline:
        from urllib.parse import urlsplit

        endpoint = urlsplit(config.ollama_base).hostname or ""
        if endpoint and not any(h.split(":")[0] == endpoint for h in current["allowed_hosts"]):
            typer.echo(f"warning: model endpoint {endpoint} is not in the allowlist")
    typer.echo(f"connection attempts refused by this process: {current['refused_connections']}")
    typer.echo(
        "telemetry: none. Replaybook sends nothing anywhere; the only network code is the "
        "model client, and air-gapped mode covers it."
    )


@app.command()
def packs(
    tools: Annotated[bool, typer.Option(help="Also list every tool with its kind.")] = False,
) -> None:
    """List the loaded packs, their request types and where they were found."""
    from replaybook.mock_env.registry import describe_all_tools
    from replaybook.packs import registry

    reg = registry()
    typer.echo("search path: " + ", ".join(str(p) for p in reg.search_path))
    for pack in reg.packs:
        kinds = ", ".join(f"{k} ({v.label})" for k, v in pack.request_types.items()) or "none"
        typer.echo(f"{pack.name}: {pack.path}")
        typer.echo(f"  request types: {kinds}")
        typer.echo(
            f"  rules: {len(pack.rules)} tools, {len(pack.unsafe)} unsafe, "
            f"{len(pack.superseded)} superseded"
        )
    if tools:
        for tool in describe_all_tools(reg):
            kind = "read" if tool["annotations"].get("read_only_hint") else "write"
            flag = " destructive" if tool["annotations"].get("destructive_hint") else ""
            typer.echo(f"  {tool['server']:<12} {tool['name']:<44} {kind}{flag}")


@app.command()
def sbom(
    out: Annotated[Path, typer.Option(help="Where to write the CycloneDX JSON.")] = Path(
        "sbom/replaybook.cdx.json"
    ),
) -> None:
    """Write a CycloneDX SBOM of the pinned dependencies (needs `uv sync --group sbom`)."""
    from replaybook.sbom import SbomUnavailable, write_sbom

    try:
        path = write_sbom(out)
    except SbomUnavailable as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Wrote {path}")


@app.command()
def generate(
    out: Annotated[
        Path, typer.Option(help="Directory for the generated dataset.")
    ] = DEFAULT_DATA_DIR,
    seed: Annotated[int, typer.Option(help="Same seed, same data.")] = 42,
    n: Annotated[int, typer.Option(help="Number of tickets (corrections are extra).")] = 3000,
) -> None:
    """Generate synthetic tickets with hidden ground truth."""
    from replaybook.synth import generator

    summary = generator.generate(out, generator.GenerateOptions(seed=seed, n_tickets=n))
    typer.echo(
        f"Wrote {summary['tickets']} tickets to {out} "
        f"({summary['typed']} typed, {summary['by_type'].get('noise', 0)} noise, "
        f"{summary['info_missing']} with missing info)."
    )


@app.command("mock-server")
def mock_server(
    server: Annotated[str, typer.Option(help="ticket, or the name of a loaded pack.")],
    world: Annotated[Path | None, typer.Option(help="World state JSON for this case.")] = None,
    record: Annotated[Path | None, typer.Option(help="JSONL file to append every call to.")] = None,
    knowledge: Annotated[
        Path | None, typer.Option(help="Dataset directory the knowledge server answers from.")
    ] = None,
) -> None:
    """Run one mock MCP server over stdio (for agents in another process)."""
    from replaybook.mock_env.serve import serve_stdio

    serve_stdio(server, world, record, knowledge)


@app.command("build-testset")
def build_testset(
    request_type: Annotated[
        str, typer.Option("--type", help="Request type key from a pack (see `replaybook packs`).")
    ],
    data: Annotated[
        Path, typer.Option(help="Dataset directory with tickets.jsonl.")
    ] = DEFAULT_DATA_DIR,
    n: Annotated[int, typer.Option(help="How many of the newest tickets to take.")] = 40,
    out: Annotated[Path | None, typer.Option(help="Where to write the test set JSON.")] = None,
    extract: Annotated[
        str,
        typer.Option(
            help="For tickets without recorded actions: none, rules or model. Extracted "
            "expectations are unverified until a person confirms them."
        ),
    ] = "none",
) -> None:
    """Build a test set from the newest tickets of one request type."""
    from replaybook.adapters.synthetic import load_platform, load_tickets
    from replaybook.eval.extract import make_extractor
    from replaybook.eval.testset import build_test_set

    tickets = load_tickets(data)
    source = tickets[0].source if tickets else "synthetic"
    test_set = build_test_set(
        tickets,
        load_platform(data),
        request_type,
        n=n,
        source=source,
        data_dir=str(data),
        extractor=make_extractor(extract),
    )
    out = out or Path("testsets") / f"{request_type}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(test_set.model_dump_json(indent=2), encoding="utf-8")
    sel = test_set.selection
    unverified = f", {sel['unverified']} unverified" if sel.get("unverified") else ""
    typer.echo(
        f"{test_set.name}: {sel['selected']} cases ({sel['ask_cases']} expect a question"
        f"{unverified}), from {sel['eligible']} eligible tickets; "
        f"excluded {sel['excluded']} -> {out}"
    )


@app.command()
def sync(
    testset: Annotated[Path, typer.Option(help="Test set JSON to extend in place.")],
    data: Annotated[
        Path | None, typer.Option(help="Dataset directory (default: the set's data_dir).")
    ] = None,
    extract: Annotated[
        str, typer.Option(help="For tickets without recorded actions: none, rules or model.")
    ] = "none",
    max_new: Annotated[
        int | None, typer.Option("--max", help="Add at most this many new cases.")
    ] = None,
) -> None:
    """Add tickets resolved since the last sync to a test set; existing cases stay as they are."""
    from replaybook.adapters.synthetic import load_platform, load_tickets
    from replaybook.eval.extract import make_extractor
    from replaybook.eval.sync import sync_test_set
    from replaybook.schema.testcase import TestSet

    test_set = TestSet.model_validate_json(testset.read_text(encoding="utf-8"))
    data_dir = data if data is not None else Path(test_set.data_dir)
    if not (data_dir / "tickets.jsonl").is_file():
        typer.echo(f"error: no tickets.jsonl in {data_dir}; give --data", err=True)
        raise typer.Exit(code=2)
    updated, entry = sync_test_set(
        test_set,
        load_tickets(data_dir),
        load_platform(data_dir),
        extractor=make_extractor(extract),
        max_new=max_new,
        data_dir=str(data_dir),
    )
    testset.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    skipped = ", ".join(f"{k} {v}" for k, v in entry["skipped"].items()) or "none"
    typer.echo(
        f"{updated.name}: {len(entry['added'])} new cases from {entry['candidates']} tickets "
        f"resolved after {str(entry['through_before'])[:10]} (skipped: {skipped}); "
        f"{len(updated.cases)} cases now, synced through {str(entry['through_after'])[:10]}"
    )
    for case_id in entry["added"]:
        typer.echo(f"  NEW {case_id}")


@app.command()
def discover(
    data: Annotated[
        Path, typer.Option(help="Dataset directory with tickets.jsonl.")
    ] = DEFAULT_DATA_DIR,
    out: Annotated[Path, typer.Option(help="Directory for discovery.json and .md.")] = Path(
        "discovery"
    ),
    top: Annotated[int, typer.Option(help="How many request types to recommend.")] = 3,
) -> None:
    """Group past tickets into request types and rank them by engineer-hours."""
    from replaybook.adapters.synthetic import load_tickets
    from replaybook.discover import discover as run_discovery
    from replaybook.discover import write_report

    report = run_discovery(load_tickets(data), top=top)
    paths = write_report(report, out)
    typer.echo(
        f"{report.tickets} tickets: {report.typed} typed, {report.untyped} untyped; "
        f"{report.total_hours} engineer-hours ({report.logged_share:.0%} logged)"
    )
    for rank, s in enumerate(report.types, start=1):
        skill = "skill" if s.skill_available else "no skill"
        typer.echo(
            f"  {rank:>2}. {s.label:<34} {s.tickets:>5} tickets {s.hours:>9.1f} h "
            f"{s.share_of_hours:>5.0%}  {skill}"
        )
    typer.echo(f"Test first: {', '.join(report.recommendation)} -> {paths['markdown']}")


tools_app = typer.Typer(help="Bring your own tools: import an MCP server's tool list as a pack.")
app.add_typer(tools_app, name="tools")


@tools_app.command("import")
def tools_import(
    name: Annotated[str, typer.Option(help="Pack name, e.g. payroll (letters, digits, _ or -).")],
    command: Annotated[
        str, typer.Option(help="Command that starts the MCP server over stdio (shell-split).")
    ] = "",
    json_file: Annotated[
        Path | None, typer.Option("--json", help="A saved tools/list result instead.")
    ] = None,
    out: Annotated[Path, typer.Option(help="Directory to create the pack in.")] = Path("packs"),
    description: Annotated[str, typer.Option(help="One line for pack.yaml.")] = "",
) -> None:
    """Read tool names, schemas and read/write annotations from an MCP server (or a saved
    JSON of them) and generate a pack with a recording mock and a rules.yaml skeleton."""
    from replaybook.tools_import import import_tools

    try:
        result = import_tools(
            name, out, command=command, json_path=json_file, description=description
        )
    except (ValueError, FileExistsError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"{name}: {result['tools']} tools ({result['reads']} reads, {result['writes']} writes, "
        f"{result['destructive']} destructive) from {result['source']} -> {result['pack']}"
    )
    typer.echo(
        f"Next: edit {result['pack'] / 'rules.yaml'} (which arguments matter, what is unsafe) "
        f"and add request_types to {result['pack'] / 'pack.yaml'}. "
        f"Set REPLAYBOOK_PACKS={out} or run from a directory that holds {out}/."
    )


EFFORT_HELP = "Effort source: logged, status_time or created_to_resolved."

tickets_app = typer.Typer(help="Bring your own ticket system: import tickets from a table.")
app.add_typer(tickets_app, name="tickets")


@tickets_app.command("import")
def tickets_import(
    mapping: Annotated[
        Path, typer.Option(help="YAML column mapping (see docs/ticket-sources.md).")
    ],
    out: Annotated[Path, typer.Option(help="Dataset directory to write.")],
) -> None:
    """Import tickets from a CSV, JSONL or JSON export through a column mapping."""
    from replaybook.adapters.tabular import MappingError, import_tickets

    try:
        stats = import_tickets(mapping, out)
    except (MappingError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"{stats['project']}: {stats['tickets']} tickets, {stats['comments']} comments, "
        f"{stats['with_status_history']} with status history, {stats['with_actions']} with "
        f"recorded actions, {stats['waited_on_requester']} waited on the requester; effort "
        f"from {stats['effort_source']} -> {out}"
    )
    if not stats["with_status_history"]:
        typer.echo(
            "warning: no status-change history. Ask cases cannot be detected and status_time "
            "has nothing to measure; map a history table if the system has one.",
            err=True,
        )
    if stats["unknown_statuses"]:
        typer.echo(
            "warning: statuses not in the canonical set: "
            + ", ".join(stats["unknown_statuses"])
            + " (add them to status_map if they mean Open, In Progress, Waiting on Requester, "
            "Resolved or Closed)",
            err=True,
        )


postgres_app = typer.Typer(help="Tickets from a Postgres table (a RAG store or warehouse).")
app.add_typer(postgres_app, name="postgres")


@postgres_app.command("import")
def postgres_import(
    mapping: Annotated[
        Path, typer.Option(help="YAML mapping: dsn, tables, columns (docs/ticket-sources.md).")
    ],
    out: Annotated[Path, typer.Option(help="Dataset directory to write.")],
    allow_missing_history: Annotated[
        bool,
        typer.Option(
            help="Import even without a status-history table (no ask cases, no status_time)."
        ),
    ] = False,
) -> None:
    """Import tickets from Postgres through a column mapping (needs `uv sync --extra postgres`).

    Status-change history is required; without it the import refuses unless told otherwise.
    """
    from replaybook.adapters.postgres import import_tickets
    from replaybook.adapters.tabular import MappingError

    try:
        stats = import_tickets(mapping, out, allow_missing_history=allow_missing_history)
    except (MappingError, OSError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"{stats['project']}: {stats['tickets']} tickets from {stats['tables']['tickets']}, "
        f"{stats['comments']} comments, {stats['with_status_history']} with status history, "
        f"{stats['with_actions']} with recorded actions; effort from {stats['effort_source']} "
        f"-> {out}"
    )
    if not stats["history_mapped"] or not stats["with_status_history"]:
        typer.echo(
            "WARNING: no status-change history. Ask cases (engineers asked the requester "
            "before acting) cannot be detected and status_time effort has nothing to measure. "
            "Expectations will come from recorded actions or text only. Map the history table "
            "as soon as you can.",
            err=True,
        )
    if stats["unknown_statuses"]:
        typer.echo(
            "warning: statuses not in the canonical set: "
            + ", ".join(stats["unknown_statuses"])
            + " (add them to status_map if they mean Open, In Progress, Waiting on Requester, "
            "Resolved or Closed)",
            err=True,
        )


public_jira_app = typer.Typer(help="The Public Jira Dataset (Zenodo 10.5281/zenodo.5882881).")
app.add_typer(public_jira_app, name="public-jira")


@public_jira_app.command("export")
def public_jira_export(
    input: Annotated[
        str,
        typer.Option(
            help="The dump's .archive file (with --collection), a .bson collection file, "
            "a .jsonl(.gz) of issues, or mongodb://host:27017/JiraReposAnon?collection=Apache"
        ),
    ],
    project: Annotated[str, typer.Option(help="Jira project key to keep, e.g. INFRA.")],
    out: Annotated[Path, typer.Option(help="Dataset directory to write.")],
    collection: Annotated[
        str, typer.Option(help="Collection (one Jira) inside a .archive, e.g. Apache.")
    ] = "",
    effort: Annotated[str, typer.Option(help=EFFORT_HELP)] = "logged",
) -> None:
    """Export one project as canonical, redacted tickets with a DATASET.md data card."""
    from replaybook.adapters.public_jira import export

    stats = export(input, project, out, collection or None, effort=effort)
    typer.echo(
        f"{project}: {stats['tickets']} tickets from {stats['raw_documents']} documents, "
        f"{stats['comments']} comments, {stats['waited_on_requester']} waited on the requester, "
        f"{str(stats['oldest'])[:10]} to {str(stats['newest'])[:10]} -> {out}"
    )


@public_jira_app.command("collections")
def public_jira_collections(
    input: Annotated[str, typer.Option(help="The dump's .archive file.")],
) -> None:
    """List the collections (one per public Jira) inside the dump archive."""
    from replaybook.adapters.mongo_archive import list_collections

    for item in list_collections(Path(input)):
        typer.echo(f"{item['db']}.{item['collection']}")


jira_cloud_app = typer.Typer(help="Jira Cloud (REST API v2 with the changelog).")
app.add_typer(jira_cloud_app, name="jira-cloud")


@jira_cloud_app.command("export")
def jira_cloud_export(
    project: Annotated[str, typer.Option(help="Jira project key to export, e.g. OPS.")],
    out: Annotated[Path, typer.Option(help="Dataset directory to write.")],
    jql: Annotated[str, typer.Option(help="Override the JQL (default: the whole project).")] = "",
    effort: Annotated[str, typer.Option(help=EFFORT_HELP)] = "logged",
) -> None:
    """Export a project from Jira Cloud (JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN in .env)."""
    from replaybook.adapters.jira_cloud import JiraCloud, JiraCloudConfig, JiraCloudError

    try:
        stats = JiraCloud(JiraCloudConfig.from_env()).export_project(
            project, out, jql or None, effort=effort
        )
    except JiraCloudError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"{project}: {stats['tickets']} tickets from {stats['raw_documents']} issues, "
        f"{stats['comments']} comments, {stats['waited_on_requester']} waited on the requester, "
        f"{str(stats['oldest'])[:10]} to {str(stats['newest'])[:10]} -> {out}"
    )


servicenow_app = typer.Typer(help="ServiceNow (Table API: records, journal, audit history).")
app.add_typer(servicenow_app, name="servicenow")


@servicenow_app.command("export")
def servicenow_export(
    out: Annotated[Path, typer.Option(help="Dataset directory to write.")],
    table: Annotated[str, typer.Option(help="incident or sc_req_item.")] = "incident",
    query: Annotated[
        str, typer.Option(help="sysparm_query to narrow the export (default: the whole table).")
    ] = "",
    effort: Annotated[str, typer.Option(help=EFFORT_HELP)] = "status_time",
) -> None:
    """Export a ServiceNow table (SERVICENOW_INSTANCE, SERVICENOW_USER, SERVICENOW_PASSWORD
    in .env) as canonical, redacted tickets with a DATASET.md data card."""
    from replaybook.adapters.servicenow import ServiceNow, ServiceNowConfig, ServiceNowError

    try:
        stats = ServiceNow(ServiceNowConfig.from_env()).export_table(
            table, out, query=query, effort=effort
        )
    except (ServiceNowError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(
        f"{table}: {stats['tickets']} tickets from {stats['raw_documents']} records, "
        f"{stats['comments']} comments and work notes, {stats['with_status_history']} with state "
        f"history, {stats['waited_on_requester']} waited on the caller; effort from "
        f"{stats['effort_source']}; {str(stats['oldest'])[:10]} to {str(stats['newest'])[:10]} "
        f"-> {out}"
    )


@app.command()
def replay(
    testset: Annotated[Path, typer.Option(help="Test set JSON from build-testset.")],
    out: Annotated[Path, typer.Option(help="Run directory for cases.jsonl and results.json.")],
    agent: Annotated[str, typer.Option(help="scripted, skill or external.")] = "scripted",
    flaws: Annotated[str, typer.Option(help="Comma-separated scripted-agent flaws.")] = "",
    command: Annotated[str, typer.Option(help="External agent command (shell-split).")] = "",
    workers: Annotated[int, typer.Option(help="Parallel cases.")] = 4,
    seed: Annotated[int, typer.Option(help="Seed recorded with the run.")] = 0,
    resume: Annotated[bool, typer.Option(help="Reuse finished cases in the run directory.")] = True,
    accepted_file: Annotated[
        Path, typer.Option(help="Accepted differences YAML.")
    ] = DEFAULT_ACCEPTED,
    knowledge: Annotated[Path | None, typer.Option(help=KNOWLEDGE_HELP)] = None,
) -> None:
    """Replay an agent against a test set and score every case."""
    from replaybook.eval.accepted import AcceptedDifferences
    from replaybook.eval.runner import run_replay
    from replaybook.schema.testcase import TestSet

    test_set = TestSet.model_validate_json(testset.read_text(encoding="utf-8"))
    run = run_replay(
        test_set,
        make_agent(agent, flaws, command, test_set.request_type),
        out,
        workers=workers,
        resume=resume,
        seed=seed,
        accepted=AcceptedDifferences.load(accepted_file),
        knowledge=knowledge_for(test_set, knowledge),
    )
    print_summary(run)
    raise typer.Exit(code=0 if run.summary.gate == "pass" else 1)


@app.command()
def report(run_dir: Annotated[Path, typer.Argument(help="Run directory from replay.")]) -> None:
    """Print the summary and the non-passing cases of a finished run."""
    from replaybook.eval.runner import load_run

    run = load_run(run_dir)
    print_summary(run)
    for r in run.results:
        if r.outcome != "pass":
            note = r.accepted_reason or (r.diffs[0].detail if r.diffs else "")
            typer.echo(f"  {r.case_id:<10} {r.short():<42} {note}")


@app.command()
def groups(run_dir: Annotated[Path, typer.Argument(help="Run directory from replay.")]) -> None:
    """Group a run's failures by cause, with two examples each, and save groups.json."""
    from replaybook.eval.groups import group_failures
    from replaybook.eval.runner import load_run

    run = load_run(run_dir)
    found = group_failures(run.results)
    (run_dir / "groups.json").write_text(
        "[" + ",\n".join(g.model_dump_json() for g in found) + "]", encoding="utf-8"
    )
    if not found:
        typer.echo("No failures to group.")
    for g in found:
        flag = "  [history likely outdated]" if g.likely_history_outdated else ""
        typer.echo(f"{g.id} ({g.count}) {g.label}{flag}")
        typer.echo(f"    {g.cause}")
        for ex in g.examples:
            typer.echo(f"    - {ex.case_id}: {ex.summary[:70]}")


@app.command()
def accept(
    reason: Annotated[str, typer.Option(help="Why this difference is acceptable.")] = "",
    approver: Annotated[str, typer.Option(help="Who approved it.")] = "",
    case: Annotated[str, typer.Option(help="Case id to accept.")] = "",
    group: Annotated[str, typer.Option(help="Group id (needs --run) to accept by signature.")] = "",
    run: Annotated[Path | None, typer.Option(help="Run directory to re-score afterwards.")] = None,
    expires: Annotated[str, typer.Option(help="Expiry date YYYY-MM-DD (default: 90 days).")] = "",
    kind: Annotated[
        str, typer.Option(help="accepted_difference or outdated_history.")
    ] = "accepted_difference",
    suggest: Annotated[
        bool, typer.Option(help="List failing cases whose history looks outdated.")
    ] = False,
    accepted_file: Annotated[
        Path, typer.Option(help="Accepted differences YAML.")
    ] = DEFAULT_ACCEPTED,
) -> None:
    """Record an accepted difference or outdated-history marker: reason, approver, expiry."""
    from datetime import date

    from replaybook.eval import accepted as acc
    from replaybook.eval.groups import group_failures
    from replaybook.eval.runner import load_run, reapply_accepted

    store = acc.AcceptedDifferences.load(accepted_file)
    if suggest:
        if run is None:
            raise typer.BadParameter("--suggest needs --run")
        for item in acc.suggest(load_run(run).results):
            typer.echo(
                f"{item['case_id']:<10} {item['reason']:<22} {item['why']} -> --kind {item['kind']}"
            )
        return
    if not case and not group:
        raise typer.BadParameter("give --case or --group")
    if group:
        if run is None:
            raise typer.BadParameter("--group needs --run")
        found = next((g for g in group_failures(load_run(run).results) if g.id == group), None)
        if found is None:
            raise typer.BadParameter(f"no group {group} in {run}")
        scope, match = "signature", found.signature
    else:
        scope, match = "case", case
    if kind not in ("accepted_difference", "outdated_history"):
        raise typer.BadParameter("--kind must be accepted_difference or outdated_history")
    entry = store.add(
        scope=scope,  # type: ignore[arg-type]
        match=match,
        reason=reason,
        approver=approver,
        expires=date.fromisoformat(expires) if expires else None,
        kind=kind,  # type: ignore[arg-type]
        run_id=str(run) if run else None,
    )
    store.save(accepted_file)
    typer.echo(f"Recorded {entry.id} ({entry.kind}, {scope} {match[:60]}) until {entry.expires}")
    if run is not None:
        print_summary(reapply_accepted(run, store))


@app.command()
def approve(
    run: Annotated[Path, typer.Option(help="Run directory to approve as the baseline.")],
    approver: Annotated[str, typer.Option(help="Who approves this run.")],
    baseline_dir: Annotated[Path, typer.Option(help="Where baselines live.")] = Path(
        "assurance/baselines"
    ),
) -> None:
    """Mark a run as the baseline for its test set."""
    from replaybook.eval.baseline import approve as approve_run
    from replaybook.eval.runner import load_run

    loaded = load_run(run)
    baseline = approve_run(loaded, approver, baseline_dir)
    typer.echo(
        f"Approved {loaded.run_id} as baseline for {baseline.test_set_name} "
        f"(fingerprint {baseline.fingerprint[:12]}, {baseline.summary.passed} pass / "
        f"{baseline.summary.total})."
    )


@app.command()
def check(
    testset: Annotated[Path, typer.Option(help="Test set JSON from build-testset.")],
    out: Annotated[Path, typer.Option(help="Run directory for the check run.")],
    agent: Annotated[str, typer.Option(help="scripted, skill or external.")] = "scripted",
    flaws: Annotated[str, typer.Option(help="Comma-separated scripted-agent flaws.")] = "",
    command: Annotated[str, typer.Option(help="External agent command (shell-split).")] = "",
    workers: Annotated[int, typer.Option(help="Parallel cases.")] = 4,
    force: Annotated[
        bool, typer.Option(help="Re-run even if the fingerprint is unchanged.")
    ] = False,
    baseline_dir: Annotated[Path, typer.Option(help="Where baselines live.")] = Path(
        "assurance/baselines"
    ),
    accepted_file: Annotated[
        Path, typer.Option(help="Accepted differences YAML.")
    ] = DEFAULT_ACCEPTED,
    knowledge: Annotated[Path | None, typer.Option(help=KNOWLEDGE_HELP)] = None,
) -> None:
    """Re-run when the fingerprint changed and report only what differs from the baseline.

    Exit codes: 0 pass, 1 regression or critical failure, 2 error.
    """
    from replaybook.eval.accepted import AcceptedDifferences
    from replaybook.eval.baseline import check as run_check
    from replaybook.schema.testcase import TestSet

    test_set = TestSet.model_validate_json(testset.read_text(encoding="utf-8"))
    report_, run_ = run_check(
        test_set,
        make_agent(agent, flaws, command, test_set.request_type),
        out,
        baseline_dir=baseline_dir,
        accepted=AcceptedDifferences.load(accepted_file),
        force=force,
        workers=workers,
        knowledge=knowledge_for(test_set, knowledge),
    )
    typer.echo(f"{report_.test_set_name}: {report_.message}")
    for change in report_.regressions:
        typer.echo(
            f"  REGRESSION {change.case_id}: {change.before} -> {change.after} "
            f"({change.reason_after}) {change.detail}"
        )
    for change in report_.changed:
        typer.echo(
            f"  CHANGED    {change.case_id}: {change.reason_before} -> {change.reason_after}"
        )
    for change in report_.improvements:
        typer.echo(f"  IMPROVED   {change.case_id}: {change.before} -> {change.after}")
    for case_id in report_.new_cases:
        typer.echo(f"  NEW        {case_id}")
    for case_id in report_.removed_cases:
        typer.echo(f"  REMOVED    {case_id}")
    if report_.reran and report_.critical_failures:
        typer.echo(f"  critical failures now: {report_.critical_failures}")
    if run_ is not None:
        print_summary(run_)
    raise typer.Exit(code=report_.exit_code)


@app.command()
def evidence(
    run: Annotated[
        list[Path], typer.Option(help="Run directory (repeat for several request types).")
    ],
    out: Annotated[
        Path, typer.Option(help="Directory for evidence.html, evidence.json and manifest.json.")
    ],
    testset: Annotated[
        list[Path] | None, typer.Option(help="Test set JSON per run (same order).")
    ] = None,
    title: Annotated[str, typer.Option(help="Pack title.")] = "Replaybook evidence pack",
    baseline_dir: Annotated[Path, typer.Option(help="Where baselines live.")] = Path(
        "assurance/baselines"
    ),
    accepted_file: Annotated[
        Path, typer.Option(help="Accepted differences YAML.")
    ] = DEFAULT_ACCEPTED,
    shadow: Annotated[
        Path | None, typer.Option(help="Shadow directory from `replaybook shadow`.")
    ] = None,
) -> None:
    """Write the evidence pack (HTML, JSON and SHA-256 manifest) for one or more runs."""
    from replaybook.eval.accepted import AcceptedDifferences
    from replaybook.eval.baseline import CheckReport, load_baseline
    from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence
    from replaybook.eval.runner import load_run
    from replaybook.eval.shadow import load_report
    from replaybook.schema.testcase import TestSet

    shadow_report = load_report(shadow) if shadow is not None else None
    if shadow is not None and shadow_report is None:
        typer.echo(
            f"error: no shadow.json in {shadow}; run `replaybook shadow record` first", err=True
        )
        raise typer.Exit(code=2)

    bundles = []
    for index, run_dir in enumerate(run):
        loaded = load_run(run_dir)
        if testset and index < len(testset):
            ts_path: Path | None = testset[index]
            ts = TestSet.model_validate_json(ts_path.read_text(encoding="utf-8"))
        else:
            ts_path = None
            ts = TestSet(
                name=loaded.test_set_name,
                request_type=loaded.test_set_request_type,
                source="unknown",
                data_dir="",
                created_at=loaded.created_at,
            )
        check_path = run_dir / "check.json"
        check_report = (
            CheckReport.model_validate_json(check_path.read_text(encoding="utf-8"))
            if check_path.exists()
            else None
        )
        bundles.append(
            RunBundle(
                run=loaded,
                test_set=ts,
                run_dir=run_dir,
                test_set_path=ts_path,
                baseline=load_baseline(loaded.test_set_name, baseline_dir),
                check=check_report,
            )
        )
    paths = build_evidence(
        EvidenceInputs(
            title=title,
            bundles=bundles,
            accepted=AcceptedDifferences.load(accepted_file),
            accepted_path=accepted_file if accepted_file.exists() else None,
            baseline_dir=baseline_dir,
            shadow=shadow_report,
            shadow_dir=shadow,
        ),
        out,
    )
    typer.echo(f"Wrote {paths['html']}, {paths['json']} and {paths['manifest']}")


shadow_app = typer.Typer(
    help="Shadow mode: record what the agent would do on open tickets, score it once they resolve."
)
app.add_typer(shadow_app, name="shadow")


@shadow_app.command("record")
def shadow_record(
    request_type: Annotated[str, typer.Option("--type", help="Request type key from a pack.")],
    out: Annotated[Path, typer.Option(help="Shadow directory (proposals.jsonl, scores.jsonl).")],
    data: Annotated[
        Path, typer.Option(help="Dataset directory with the current tickets.jsonl.")
    ] = DEFAULT_DATA_DIR,
    agent: Annotated[str, typer.Option(help="scripted, skill or external.")] = "scripted",
    flaws: Annotated[str, typer.Option(help="Comma-separated scripted-agent flaws.")] = "",
    command: Annotated[str, typer.Option(help="External agent command (shell-split).")] = "",
    extract: Annotated[
        str, typer.Option(help="Seed empty worlds from the text: none, rules or model.")
    ] = "none",
    knowledge: Annotated[Path | None, typer.Option(help=KNOWLEDGE_HELP)] = None,
) -> None:
    """Run the agent on every OPEN ticket of a request type against the mock and store the
    actions it proposed. Nothing is executed and nothing is written to the ticket system."""
    from replaybook.adapters.synthetic import load_platform, load_tickets
    from replaybook.eval.extract import make_extractor
    from replaybook.eval.shadow import record
    from replaybook.mock_env.knowledge import Corpus

    corpus = (
        None
        if (knowledge and str(knowledge).lower() == "none")
        else Corpus.maybe(knowledge or data)
    )
    stats = record(
        load_tickets(data),
        load_platform(data),
        request_type,
        make_agent(agent, flaws, command, request_type),
        out,
        knowledge=corpus,
        extractor=make_extractor(extract),
    )
    typer.echo(
        f"{request_type}: {stats['open_tickets']} open tickets, {stats['recorded']} proposals "
        f"recorded ({stats['asked']} would ask the requester, {stats['proposed_writes']} proposed "
        f"writes), {stats['already_recorded']} already recorded for this agent -> {out}"
    )
    typer.echo(
        "Nothing was executed. Score them once the tickets are resolved: replaybook shadow score"
    )


@shadow_app.command("score")
def shadow_score(
    out: Annotated[Path, typer.Option(help="Shadow directory from `shadow record`.")],
    data: Annotated[
        Path, typer.Option(help="Dataset directory with the current tickets.jsonl.")
    ] = DEFAULT_DATA_DIR,
    extract: Annotated[
        str,
        typer.Option(help="For resolved tickets without recorded actions: none, rules or model."),
    ] = "none",
) -> None:
    """Score stored proposals whose tickets engineers have since resolved."""
    from replaybook.adapters.synthetic import load_platform, load_tickets
    from replaybook.eval.extract import make_extractor
    from replaybook.eval.shadow import score

    stats = score(load_tickets(data), load_platform(data), out, extractor=make_extractor(extract))
    report = stats["report"]
    s = report.summary
    typer.echo(
        f"scored {stats['scored_now']} new proposal(s); {report.scored} scored in total, "
        f"{report.pending} pending, {report.unscorable} unscorable. "
        f"{s.passed} pass, {s.failed} fail, {s.errors} error (pass rate {s.pass_rate:.0%}, "
        f"95% CI {s.wilson_low:.0%}-{s.wilson_high:.0%}); unsafe proposals caught: "
        f"{len(report.unsafe_caught)}"
    )
    for u in report.unsafe_caught:
        typer.echo(
            f"  UNSAFE {u['ticket_id']}: {', '.join(u['tools'])} ({u['severity']}) {u['detail']}"
        )


@shadow_app.command("report")
def shadow_report_cmd(
    out: Annotated[Path, typer.Argument(help="Shadow directory.")],
) -> None:
    """Print the shadow summary (what the evidence pack shows under Shadow mode)."""
    from replaybook.eval.shadow import load_report

    report = load_report(out)
    if report is None:
        typer.echo(f"error: no shadow.json in {out}", err=True)
        raise typer.Exit(code=2)
    s = report.summary
    typer.echo(
        f"{', '.join(report.request_types) or '-'}: {report.proposals} proposals, {report.scored} "
        f"scored, {report.pending} pending, {report.unscorable} unscorable; pass rate "
        f"{s.pass_rate:.0%} (95% CI {s.wilson_low:.0%}-{s.wilson_high:.0%}), critical "
        f"{s.critical_failures}, unsafe caught {len(report.unsafe_caught)}"
    )
    for g in report.groups:
        typer.echo(f"  {g.id} ({g.count}) {g.label}")


@app.command()
def demo(
    out: Annotated[Path, typer.Option(help="Directory for everything the demo produces.")] = Path(
        "demo_out"
    ),
    seed: Annotated[int, typer.Option(help="Dataset seed.")] = 42,
    n: Annotated[int, typer.Option(help="Number of synthetic tickets.")] = 3000,
    cases: Annotated[int, typer.Option(help="Cases per request type.")] = 40,
    workers: Annotated[int, typer.Option(help="Parallel cases.")] = 4,
) -> None:
    """Run the whole story offline: generate, test sets, flawed replay, groups, accept,
    fix, evidence."""
    from replaybook.demo import run_demo

    outcome = run_demo(out, seed=seed, n_tickets=n, cases=cases, workers=workers, say=typer.echo)
    raise typer.Exit(code=0 if all(code == 0 for code in outcome["exit_codes"]) else 1)


KNOWLEDGE_HELP = (
    "Dataset directory whose tickets the agent may search (behind the time-travel guard). "
    "Default: the directory the test set was built from, when it still exists; 'none' to "
    "attach nothing."
)


def knowledge_for(test_set: Any, option: Path | None) -> Any:
    """The past-ticket corpus for a replay: the option, else the test set's data_dir."""
    from replaybook.mock_env.knowledge import Corpus

    if option is not None and str(option).lower() == "none":
        return None
    return Corpus.maybe(option if option is not None else test_set.data_dir)


def make_agent(kind: str, flaws: str, command: str, request_type: str) -> Any:
    """Build an agent under test from CLI options."""
    if kind == "scripted":
        from replaybook.agents.scripted import ScriptedAgent

        return ScriptedAgent(flaws=[f for f in flaws.split(",") if f])
    if kind == "skill":
        from replaybook.agents.skill_agent import SkillAgent
        from replaybook.llm.client import LLMClient

        return SkillAgent(request_type, LLMClient())
    if kind == "external":
        import shlex

        from replaybook.agents.external import ExternalAgent

        if not command:
            raise typer.BadParameter("--command is required for the external agent")
        return ExternalAgent(shlex.split(command))
    raise typer.BadParameter(f"unknown agent '{kind}'")


def print_summary(run: Any) -> None:
    s = run.summary
    flaws = run.agent_config.get("flaws") or []
    flaw_text = " flaws=" + ",".join(flaws) if flaws else ""
    typer.echo(
        f"{run.test_set_name} / {run.agent_config.get('agent')}{flaw_text}: "
        f"{s.passed} pass, {s.failed} fail, {s.accepted} accepted, {s.errors} error of {s.total} "
        f"(pass rate {s.pass_rate:.0%}, 95% CI {s.wilson_low:.0%}-{s.wilson_high:.0%}); "
        f"critical {s.critical_failures}; gate {s.gate.upper()}"
    )
    if s.by_reason:
        typer.echo("  failures by reason: " + ", ".join(f"{k} {v}" for k, v in s.by_reason.items()))


if __name__ == "__main__":
    app()
