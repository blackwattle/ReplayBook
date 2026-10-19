"""Command-line entry point. Every command lives here so `replaybook --help` is the map.

The workflow commands (generate, build-testset, replay, report, groups, check, evidence,
shadow) call `replaybook.workflow`, which `replaybook mcp` exposes as MCP tools too, so the
command line and an assistant do the same work on the same files.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from replaybook import __version__
from replaybook.workflow import (
    DEFAULT_ACCEPTED,
    DEFAULT_BASELINES,
    DEFAULT_DATA_DIR,
    DEFAULT_LABELS,
)

app = typer.Typer(
    help="Test ops agents against your own past tickets in a mock environment.",
    no_args_is_help=True,
    add_completion=False,
)

KNOWLEDGE_HELP = (
    "Dataset directory whose tickets the agent may search (behind the time-travel guard). "
    "Default: the directory the test set was built from, when it still exists; 'none' to "
    "attach nothing."
)
COMMAND_HELP = (
    "External agent command (shell-split); with a launcher, the harness executable to run "
    "instead of the one in the launcher file."
)
TRANSPORT_HELP = (
    "How an external agent reaches the mock tools: stdio (one MCP server process per pack, "
    "REPLAYBOOK_MCP_CONFIG), http (one MCP server over streamable HTTP, REPLAYBOOK_MCP_URL) or "
    "tools (the function-calling endpoint, REPLAYBOOK_TOOLS_URL)."
)
AGENT_HELP = (
    "scripted, skill, external, or the name of a launcher (see `replaybook launchers`) that "
    "runs an off-the-shelf agent harness."
)
EFFORT_HELP = "Effort source: logged, status_time or created_to_resolved."
LABELS_HELP = "Label spot-checks YAML (written by `replaybook labels check`)."
MIN_PASS_RATE_HELP = (
    "Minimum pass rate for the go-live gate, 0 to 1 (default: REPLAYBOOK_MIN_PASS_RATE, else 0.9)."
)


def run_workflow(fn: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Call a workflow function and print its message; bad arguments become usage errors."""
    from replaybook.workflow import WorkflowError

    try:
        result = fn(*args, **kwargs)
    except WorkflowError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(result["message"])
    return result


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
    if config.api_base:
        typer.echo(
            f"gateway: every model call goes to {config.api_base} (REPLAYBOOK_MODEL_API_BASE"
            f"{', with REPLAYBOOK_MODEL_API_KEY' if config.api_key else ''}); costs the gateway "
            "cannot report are recorded as unknown, not zero"
        )
    typer.echo(client.spend_line())
    if current["air_gapped"] and not client.offline:
        from urllib.parse import urlsplit

        endpoint = urlsplit(config.api_base or config.ollama_base).hostname or ""
        if endpoint and not any(h.split(":")[0] == endpoint for h in current["allowed_hosts"]):
            typer.echo(f"warning: model endpoint {endpoint} is not in the allowlist")
    typer.echo(f"connection attempts refused by this process: {current['refused_connections']}")
    typer.echo(
        "telemetry: none. Replaybook sends nothing anywhere; the only network code is the "
        "model client, and air-gapped mode covers it."
    )
    from replaybook.eval.live import RUNS_FILE, build_report, live_dir

    directory = live_dir()
    if (directory / RUNS_FILE).exists():
        try:
            report = build_report(directory)
        except ValueError as exc:
            typer.echo(f"live runs: {exc}")
        else:
            degraded = report.degraded
            typer.echo(
                f"live runs: {len(report.skills)} skill(s) in {directory}; "
                + (
                    "DEGRADED: "
                    + "; ".join(f"{s.skill} ({', '.join(s.reasons)})" for s in degraded)
                    if degraded
                    else "none degraded"
                )
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


launchers_app = typer.Typer(
    help="Launchers: run an off-the-shelf agent harness as the agent under test "
    "(launchers/<name>.yaml).",
    invoke_without_command=True,
)
app.add_typer(launchers_app, name="launchers")


@launchers_app.callback()
def launchers_list(ctx: typer.Context) -> None:
    """List the launchers found on the search path (REPLAYBOOK_LAUNCHERS, ./launchers, the
    repo's own) and whether each harness executable is on PATH."""
    if ctx.invoked_subcommand is not None:
        return
    import shutil

    from replaybook.agents.launchers import LauncherError, launchers

    try:
        reg = launchers()
    except LauncherError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo("search path: " + ", ".join(str(p) for p in reg.search_path))
    if not reg.launchers:
        typer.echo("no launchers found")
    for launcher in reg.launchers:
        found = shutil.which(launcher.executable)
        where = f"found at {found}" if found else "not on PATH"
        typer.echo(f"{launcher.name}: {launcher.description}")
        typer.echo(
            f"  runs `{launcher.executable}` ({where}); MCP config passed as "
            f"{launcher.mcp_config.pass_as} ({launcher.mcp_config.format}), prompt as "
            f"{launcher.prompt.pass_as}; {len(launcher.disabled_tools)} built-in tools "
            f"switched off; timeout {launcher.timeout_s:.0f}s; checked {launcher.checked or '-'}"
        )
    typer.echo(
        "Use one with: replaybook replay --testset <set> --out <run> --agent <launcher>; "
        "`replaybook launchers show <name>` prints the exact command."
    )


@launchers_app.command("show")
def launchers_show(
    name: Annotated[str, typer.Argument(help="Launcher name (see `replaybook launchers`).")],
    testset: Annotated[
        Path | None, typer.Option(help="Render for the first case of this test set.")
    ] = None,
) -> None:
    """Dry run: print the command, environment, config file and prompt the launcher would use
    for one case, without starting the harness."""
    from replaybook.agents.external import case_files
    from replaybook.agents.launchers import LauncherAgent, LauncherError, launchers
    from replaybook.mock_env.registry import MockEnvironment
    from replaybook.mock_env.world import TicketContext, WorldState
    from replaybook.schema.testcase import TestSet

    try:
        launcher = launchers().launcher(name)
    except LauncherError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    request_type: str | None = None
    if testset is not None:
        test_set = TestSet.model_validate_json(testset.read_text(encoding="utf-8"))
        if not test_set.cases:
            typer.echo("error: the test set has no cases", err=True)
            raise typer.Exit(code=2)
        world = test_set.cases[0].world.model_copy(deep=True)
        request_type = test_set.request_type
    else:
        world = WorldState.from_inventory({}, {})
        world.ticket = TicketContext(
            id="T-0001",
            summary="Example ticket for a launcher dry run",
            description="This stands in for a real ticket; use --testset to render a real case.",
            reporter="requester",
            created="2026-01-01T00:00:00+00:00",
        )
    env = MockEnvironment(world)
    agent = LauncherAgent(launcher, request_type)
    with case_files(env) as files:
        invocation = agent.invocation(env, files)
        typer.echo(f"{launcher.name}: {launcher.description}")
        typer.echo(f"file: {launcher.path}")
        if launcher.docs:
            typer.echo(f"docs: {launcher.docs} (checked {launcher.checked or '-'})")
        typer.echo("\ncommand (one argument per line):")
        for item in invocation.command:
            typer.echo(f"  {item!r}")
        typer.echo(f"\nworking directory: {invocation.cwd}")
        extra = {k: v for k, v in invocation.env.items() if k not in os.environ}
        typer.echo("environment added: " + (", ".join(f"{k}={v}" for k, v in extra.items()) or "-"))
        typer.echo(f"prompt passed as: {launcher.prompt.pass_as}")
        if invocation.config_path:
            typer.echo(f"\nconfig file {invocation.config_path}:")
            typer.echo(invocation.config_text or "")
        typer.echo("\nprompt:")
        typer.echo(invocation.prompt)
        typer.echo(
            f"built-in tools switched off: {', '.join(launcher.disabled_tools) or 'none declared'}"
        )
        if launcher.limitations:
            typer.echo(f"limitations: {' '.join(launcher.limitations.split())}")
    typer.echo("\n(The temporary files above were removed; nothing was started.)")


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
def mcp() -> None:
    """Serve the workflow (generate, build-testset, replay, report, groups, check, evidence,
    shadow) as MCP tools over stdio, so an assistant can operate Replaybook. Same arguments
    as the CLI, same files on disk, relative to the current directory."""
    from replaybook.mcp_server import serve

    serve()


@app.command()
def generate(
    out: Annotated[
        Path, typer.Option(help="Directory for the generated dataset.")
    ] = DEFAULT_DATA_DIR,
    seed: Annotated[int, typer.Option(help="Same seed, same data.")] = 42,
    n: Annotated[int, typer.Option(help="Number of tickets (corrections are extra).")] = 3000,
) -> None:
    """Generate synthetic tickets with hidden ground truth."""
    from replaybook import workflow

    run_workflow(workflow.generate, out, seed, n)


@app.command("mock-server")
def mock_server(
    server: Annotated[
        str,
        typer.Option(
            help="ticket, knowledge, the name of a loaded pack, or all (every tool on one server)."
        ),
    ] = "all",
    world: Annotated[Path | None, typer.Option(help="World state JSON for this case.")] = None,
    record: Annotated[Path | None, typer.Option(help="JSONL file to append every call to.")] = None,
    knowledge: Annotated[
        Path | None, typer.Option(help="Dataset directory the knowledge server answers from.")
    ] = None,
    http: Annotated[
        bool, typer.Option(help="Serve over streamable HTTP at http://HOST:PORT/mcp, not stdio.")
    ] = False,
    host: Annotated[str, typer.Option(help="Bind address for --http.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port for --http; 0 takes a free one.")] = 0,
) -> None:
    """Run the mock MCP servers for an agent in another process: over stdio (default) or over
    streamable HTTP (--http). With --http the URL is printed as `REPLAYBOOK_LISTENING <url>`
    once the port is bound. In air-gapped mode the client side must allowlist the host."""
    from replaybook.mock_env.serve import serve_http, serve_stdio

    if http:
        serve_http([server], world, record, knowledge, host=host, port=port, announce=typer.echo)
    else:
        serve_stdio(server, world, record, knowledge)


@app.command("tool-endpoint")
def tool_endpoint(
    world: Annotated[Path | None, typer.Option(help="World state JSON for this case.")] = None,
    record: Annotated[Path | None, typer.Option(help="JSONL file to append every call to.")] = None,
    knowledge: Annotated[
        Path | None, typer.Option(help="Dataset directory the knowledge server answers from.")
    ] = None,
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port; 0 takes a free one.")] = 0,
) -> None:
    """Serve the mock tools to frameworks that use provider-native function calling instead of
    MCP: GET /tools lists them as JSON Schema, POST /tools/<name> calls one. Every call is
    recorded exactly like an MCP call. Prints `REPLAYBOOK_LISTENING <url>` once bound."""
    from replaybook.mock_env.http_tools import serve_tools
    from replaybook.mock_env.serve import build_environment

    env = build_environment(None, world, record, knowledge)
    serve_tools(env, host=host, port=port, announce=typer.echo)


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
    from replaybook import workflow

    run_workflow(workflow.build_testset, request_type, data, n, out, extract)


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
    url: Annotated[
        str, typer.Option(help="URL of an MCP server over streamable HTTP, e.g. http://h:8000/mcp.")
    ] = "",
    out: Annotated[Path, typer.Option(help="Directory to create the pack in.")] = Path("packs"),
    description: Annotated[str, typer.Option(help="One line for pack.yaml.")] = "",
) -> None:
    """Read tool names, schemas and read/write annotations from an MCP server (over stdio or
    streamable HTTP, or a saved JSON of them) and generate a pack with a recording mock and a
    rules.yaml skeleton."""
    from replaybook.tools_import import import_tools

    try:
        result = import_tools(
            name, out, command=command, json_path=json_file, url=url, description=description
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


@tools_app.command("schema")
def tools_schema(
    shape: Annotated[
        str, typer.Option(help="openai (the function-calling shape) or mcp (name, inputSchema).")
    ] = "openai",
    out: Annotated[Path | None, typer.Option(help="Write to this file instead of stdout.")] = None,
) -> None:
    """Print the mock tools as JSON Schema for frameworks that use provider-native function
    calling. The same list is served by `replaybook tool-endpoint` at GET /tools."""
    from replaybook.mock_env.http_tools import write_schema
    from replaybook.mock_env.registry import describe_all_tools

    try:
        text = write_schema(describe_all_tools(), shape, out)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if out is None:
        typer.echo(text, nl=False)
    else:
        typer.echo(f"Wrote {out}")


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
    agent: Annotated[str, typer.Option(help=AGENT_HELP)] = "scripted",
    flaws: Annotated[str, typer.Option(help="Comma-separated scripted-agent flaws.")] = "",
    command: Annotated[str, typer.Option(help=COMMAND_HELP)] = "",
    transport: Annotated[str, typer.Option(help=TRANSPORT_HELP)] = "stdio",
    workers: Annotated[int, typer.Option(help="Parallel cases.")] = 4,
    seed: Annotated[int, typer.Option(help="Seed recorded with the run.")] = 0,
    resume: Annotated[bool, typer.Option(help="Reuse finished cases in the run directory.")] = True,
    accepted_file: Annotated[
        Path, typer.Option(help="Accepted differences YAML.")
    ] = DEFAULT_ACCEPTED,
    knowledge: Annotated[Path | None, typer.Option(help=KNOWLEDGE_HELP)] = None,
    labels_file: Annotated[Path, typer.Option(help=LABELS_HELP)] = DEFAULT_LABELS,
    min_pass_rate: Annotated[float | None, typer.Option(help=MIN_PASS_RATE_HELP)] = None,
) -> None:
    """Replay an agent against a test set and score every case. Exit code 1 on a critical
    unsafe action or an agent error; the go-live gate is printed beside it."""
    from replaybook import workflow

    result = run_workflow(
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
        transport,
        labels_file,
        min_pass_rate,
    )
    raise typer.Exit(code=result["exit_code"])


@app.command()
def report(
    run_dir: Annotated[Path, typer.Argument(help="Run directory from replay.")],
    labels_file: Annotated[Path, typer.Option(help=LABELS_HELP)] = DEFAULT_LABELS,
    min_pass_rate: Annotated[float | None, typer.Option(help=MIN_PASS_RATE_HELP)] = None,
) -> None:
    """Print the result line, the go-live gate and the cases that did not pass (accepted
    differences included) of a finished run."""
    from replaybook import workflow

    result = run_workflow(workflow.report, run_dir, labels_file, min_pass_rate)
    for r in result["non_passing"]:
        typer.echo(f"  {r['case_id']:<10} {r['short']:<42} {r['note']}")
    for r in result["correct_escalations"]:
        typer.echo(f"  {r['case_id']:<10} {'correct_escalation':<42} handed back: {r['reason']}")


@app.command()
def groups(
    run_dir: Annotated[Path, typer.Argument(help="Run directory from replay.")],
    model: Annotated[
        bool,
        typer.Option(
            "--model",
            help="Have the fast model (REPLAYBOOK_MODEL_FAST) write each group's cause. The "
            "rule text stays when the model is offline or at the spend cap; each group says "
            "which wrote it.",
        ),
    ] = False,
) -> None:
    """Group a run's failures by cause, with two examples each, and save groups.json."""
    from replaybook import workflow

    result = run_workflow(workflow.groups, run_dir, model)
    for g in result["groups"]:
        flag = "  [history likely outdated]" if g["likely_history_outdated"] else ""
        typer.echo(f"{g['id']} ({g['count']}) {g['label']}{flag}")
        typer.echo(f"    {g['cause']}")
        if g["cause_source"] == "model":
            typer.echo(f"    (cause written by {g['cause_model']})")
        elif model:
            typer.echo(f"    (rule text kept: {g['cause_note'] or 'no reason recorded'})")
        for ex in g["examples"]:
            typer.echo(f"    - {ex['case_id']}: {ex['summary'][:70]}")


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
    from replaybook.workflow import summary_line

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
        typer.echo(summary_line(reapply_accepted(run, store)))


@app.command()
def approve(
    run: Annotated[Path, typer.Option(help="Run directory to approve as the baseline.")],
    approver: Annotated[str, typer.Option(help="Who approves this run.")],
    baseline_dir: Annotated[Path, typer.Option(help="Where baselines live.")] = DEFAULT_BASELINES,
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
    agent: Annotated[str, typer.Option(help=AGENT_HELP)] = "scripted",
    flaws: Annotated[str, typer.Option(help="Comma-separated scripted-agent flaws.")] = "",
    command: Annotated[str, typer.Option(help=COMMAND_HELP)] = "",
    transport: Annotated[str, typer.Option(help=TRANSPORT_HELP)] = "stdio",
    workers: Annotated[int, typer.Option(help="Parallel cases.")] = 4,
    force: Annotated[
        bool, typer.Option(help="Re-run even if the fingerprint is unchanged.")
    ] = False,
    baseline_dir: Annotated[Path, typer.Option(help="Where baselines live.")] = DEFAULT_BASELINES,
    accepted_file: Annotated[
        Path, typer.Option(help="Accepted differences YAML.")
    ] = DEFAULT_ACCEPTED,
    knowledge: Annotated[Path | None, typer.Option(help=KNOWLEDGE_HELP)] = None,
    go_live: Annotated[
        bool,
        typer.Option(
            "--go-live",
            help="Also require the go-live gate (minimum pass rate, no unsafe outcome, no "
            "agent error, labels spot-checked, current skill version); exit 1 if it fails.",
        ),
    ] = False,
    labels_file: Annotated[Path, typer.Option(help=LABELS_HELP)] = DEFAULT_LABELS,
    min_pass_rate: Annotated[float | None, typer.Option(help=MIN_PASS_RATE_HELP)] = None,
) -> None:
    """Re-run when the fingerprint changed and report only what differs from the baseline.

    Exit codes: 0 pass, 1 regression or critical failure (or, with --go-live, a failed
    go-live gate), 2 error.
    """
    from replaybook import workflow

    result = run_workflow(
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
        transport,
        go_live,
        labels_file,
        min_pass_rate,
    )
    raise typer.Exit(code=result["exit_code"])


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
    baseline_dir: Annotated[Path, typer.Option(help="Where baselines live.")] = DEFAULT_BASELINES,
    accepted_file: Annotated[
        Path, typer.Option(help="Accepted differences YAML.")
    ] = DEFAULT_ACCEPTED,
    shadow: Annotated[
        Path | None, typer.Option(help="Shadow directory from `replaybook shadow`.")
    ] = None,
    labels_file: Annotated[Path, typer.Option(help=LABELS_HELP)] = DEFAULT_LABELS,
    min_pass_rate: Annotated[float | None, typer.Option(help=MIN_PASS_RATE_HELP)] = None,
    live: Annotated[
        Path | None,
        typer.Option(help="Live-runs directory from `replaybook live record` (go-live metrics)."),
    ] = None,
) -> None:
    """Write the evidence pack (HTML, JSON and SHA-256 manifest) for one or more runs. Its
    verdict is the go-live gate."""
    from replaybook import workflow

    run_workflow(
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


skills_app = typer.Typer(help="Skills: the SKILL.md files agents follow, and their checks.")
app.add_typer(skills_app, name="skills")


@skills_app.command("validate")
def skills_validate(
    request_type: Annotated[
        str, typer.Option("--type", help="One request type (default: every skill of the packs).")
    ] = "",
    file: Annotated[
        Path | None, typer.Option(help="A SKILL.md file, e.g. one a pack does not list yet.")
    ] = None,
) -> None:
    """Check that every write a skill lists has a read-only check (`verify:` in its front
    matter) whose templates name the write's own arguments. Exit 1 when any skill is
    invalid; replay, shadow record and approval requests refuse an invalid skill."""
    from replaybook import workflow

    result = run_workflow(workflow.skills_validate, request_type, file)
    raise typer.Exit(code=0 if result["valid"] else 1)


checks_app = typer.Typer(
    help="Verification checks for live runtimes: render a write's check, compare a read's "
    "result with it. Replaybook itself never runs a check."
)
app.add_typer(checks_app, name="checks")


def _json_input(value: str, what: str) -> object:
    """A JSON value given inline, as a file path, or as - for stdin."""
    import json
    import sys

    try:
        if value == "-":
            return json.loads(sys.stdin.read())
        if value.lstrip().startswith(("{", "[")):
            return json.loads(value)
        return json.loads(Path(value).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(f"{what}: not JSON, a JSON file or - ({exc})") from exc


@checks_app.command("render")
def checks_render(
    skill: Annotated[str, typer.Option("--skill", help="The request type whose skill to use.")],
    write: Annotated[
        str,
        typer.Option(help='The write as JSON {"tool": ..., "args": {...}}, a JSON file, or -.'),
    ],
) -> None:
    """Print the check that confirms one write: the read tool, its arguments, the state it
    must show and the timeout, rendered from the skill's `verify:`. Exit 2 when the skill
    has no check for that write."""
    import json

    from replaybook.checks import checks_for, require_valid_skill

    data = _json_input(write, "--write")
    if not isinstance(data, dict) or not isinstance(data.get("tool"), str):
        raise typer.BadParameter('--write must be {"tool": ..., "args": {...}}')
    try:
        require_valid_skill(skill)
        checks = checks_for(skill)
    except (KeyError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    check = checks.get(data["tool"])
    if check is None:
        typer.echo(f"error: the {skill} skill has no check for {data['tool']}", err=True)
        raise typer.Exit(code=2)
    typer.echo(json.dumps(check.render(dict(data.get("args") or {})), indent=2))


@checks_app.command("compare")
def checks_compare(
    result: Annotated[
        str, typer.Option(help="The read's result as JSON, a JSON file, or - for stdin.")
    ],
    expect: Annotated[str, typer.Option(help="The rendered `expect` as JSON or a JSON file.")] = "",
    check: Annotated[
        str,
        typer.Option(help="A check from `checks render` (JSON or a file); its expect is used."),
    ] = "",
) -> None:
    """Compare a read's result with a check's expected state and print verified,
    verify_failed or verify_error with the differences. Exit 0 verified, 1 verify_failed,
    2 verify_error."""
    import json

    from replaybook.checks import compare

    if bool(expect) == bool(check):
        raise typer.BadParameter("give exactly one of --expect or --check")
    if check:
        rendered = _json_input(check, "--check")
        if not isinstance(rendered, dict) or "expect" not in rendered:
            raise typer.BadParameter("--check must be a check from `replaybook checks render`")
        wanted = rendered["expect"]
    else:
        wanted = _json_input(expect, "--expect")
    try:
        actual = _json_input(result, "--result")
    except typer.BadParameter as exc:
        outcome = {"status": "verify_error", "differences": [f"no readable result: {exc}"]}
        typer.echo(json.dumps(outcome, indent=2))
        raise typer.Exit(code=2) from exc
    found = compare(wanted, actual)
    typer.echo(json.dumps(found.as_dict(), indent=2))
    raise typer.Exit(code={"verified": 0, "verify_failed": 1}.get(found.status, 2))


@app.command("approval-request")
def approval_request(
    skill: Annotated[str, typer.Option("--skill", help="The request type whose skill would act.")],
    plan: Annotated[
        str,
        typer.Option(
            help='Planned writes as JSON [{"tool": ..., "args": {...}}], a JSON file, or -.'
        ),
    ],
    out: Annotated[Path, typer.Option(help="Directory for approval-request.md and .json.")],
    ticket: Annotated[str, typer.Option(help="The live ticket the writes are for.")] = "",
    runs: Annotated[
        Path, typer.Option(help="Where to look for the replay of this skill version.")
    ] = Path("runs"),
    evidence: Annotated[
        Path, typer.Option(help="Where to look for that replay's evidence pack.")
    ] = Path("evidence"),
    live_dir: Annotated[
        Path | None,
        typer.Option(help="Live-runs directory (default: REPLAYBOOK_LIVE_DIR, else live)."),
    ] = None,
    labels_file: Annotated[Path, typer.Option(help=LABELS_HELP)] = DEFAULT_LABELS,
    min_pass_rate: Annotated[float | None, typer.Option(help=MIN_PASS_RATE_HELP)] = None,
    window: Annotated[
        int | None,
        typer.Option(
            help="Live runs the verification rate covers (default: REPLAYBOOK_LIVE_WINDOW, "
            "else 30)."
        ),
    ] = None,
) -> None:
    """Write what an approver sees before approving a skill's writes on a live ticket: the
    skill and its version, the replay result and date for that version, the live
    verification rate, each planned write with its check, and the replay's evidence pack.
    Exit 1, BLOCKED at the top, when the version has no replay, its replay failed the
    go-live gate, or the plan holds a write the skill does not cover. Nothing is approved."""
    from replaybook import workflow

    result = run_workflow(
        workflow.approval_request,
        skill,
        _json_input(plan, "--plan"),
        out,
        ticket or None,
        runs,
        evidence,
        live_dir,
        labels_file,
        min_pass_rate,
        window,
    )
    raise typer.Exit(code=result["exit_code"])


live_app = typer.Typer(
    help="After go-live: record the live runs a team's runtime reports, and watch each skill's "
    "approval, verification and escalation rates."
)
app.add_typer(live_app, name="live")
LIVE_DIR_HELP = (
    "Directory of runs.jsonl and degraded.jsonl (default: REPLAYBOOK_LIVE_DIR, else live)."
)
WINDOW_HELP = (
    "How many of each skill's latest runs the rates cover (default: REPLAYBOOK_LIVE_WINDOW, "
    "else 30)."
)
MIN_APPROVAL_HELP = (
    "Approval rate below which a skill is degraded (default: REPLAYBOOK_MIN_APPROVAL_RATE, "
    "else 0.8)."
)
MIN_VERIFICATION_HELP = (
    "Verification rate below which a skill is degraded (default: "
    "REPLAYBOOK_MIN_VERIFICATION_RATE, else 0.95)."
)


@live_app.command("record")
def live_record(
    run: Annotated[
        str,
        typer.Option(
            help="The run as JSON (skill, skill_version, ticket, approval, writes, ...), a "
            "file, or -."
        ),
    ],
    live_dir: Annotated[Path | None, typer.Option(help=LIVE_DIR_HELP)] = None,
    discovery: Annotated[
        Path | None,
        typer.Option(help="Discovery report for hours saved (default: discovery/discovery.json)."),
    ] = None,
) -> None:
    """Append one live run, as a team's runtime reports it, to runs.jsonl: the approval, each
    write's verification result, any hand-back, and the hours saved Replaybook computes. A
    record that breaks the live contract (writes without approval, a failed check without a
    hand-back) is refused."""
    from replaybook import workflow

    run_workflow(workflow.live_record, _json_input(run, "--run"), live_dir, discovery)


@live_app.command("report")
def live_report_cmd(
    live_dir: Annotated[Path | None, typer.Option(help=LIVE_DIR_HELP)] = None,
    window: Annotated[int | None, typer.Option(help=WINDOW_HELP)] = None,
    skill: Annotated[str, typer.Option(help="One request type (default: every skill).")] = "",
    min_approval_rate: Annotated[float | None, typer.Option(help=MIN_APPROVAL_HELP)] = None,
    min_verification_rate: Annotated[float | None, typer.Option(help=MIN_VERIFICATION_HELP)] = None,
) -> None:
    """Per skill: the rolling approval, verification and escalation rates, the hours saved,
    and DEGRADED when a rate falls below its threshold."""
    from replaybook import workflow

    run_workflow(
        workflow.live_report, live_dir, window, skill, min_approval_rate, min_verification_rate
    )


@live_app.command("check")
def live_check_cmd(
    live_dir: Annotated[Path | None, typer.Option(help=LIVE_DIR_HELP)] = None,
    window: Annotated[int | None, typer.Option(help=WINDOW_HELP)] = None,
    min_approval_rate: Annotated[float | None, typer.Option(help=MIN_APPROVAL_HELP)] = None,
    min_verification_rate: Annotated[float | None, typer.Option(help=MIN_VERIFICATION_HELP)] = None,
) -> None:
    """For CI or a scheduler: exit 1 when any skill is degraded, 0 otherwise. Each change of a
    skill's state is appended to degraded.jsonl."""
    from replaybook import workflow

    result = run_workflow(
        workflow.live_check, live_dir, window, min_approval_rate, min_verification_rate
    )
    raise typer.Exit(code=result["exit_code"])


labels_app = typer.Typer(
    help="Expected outcomes (act or escalate): the labelling rule's sample and a person's "
    "spot-check."
)
app.add_typer(labels_app, name="labels")


@labels_app.command("sample")
def labels_sample(
    testset: Annotated[Path, typer.Option(help="Test set JSON.")],
    out: Annotated[
        Path | None, typer.Option(help="Write the review sheet (Markdown) here.")
    ] = None,
    labels_file: Annotated[Path, typer.Option(help=LABELS_HELP)] = DEFAULT_LABELS,
) -> None:
    """List the cases to spot-check (at least 10 or 10% of the set, whichever is larger) with
    each label and why the rule gave it; --out writes a review sheet. Cases an earlier
    check covered come last, so the sample is a fresh one where the set allows."""
    from replaybook import workflow

    result = run_workflow(workflow.labels_sample, testset, out, labels_file)
    if out is None:
        typer.echo("")
        typer.echo(result["sheet"])


@labels_app.command("check")
def labels_check(
    testset: Annotated[Path, typer.Option(help="Test set JSON; changed labels are written back.")],
    reviewer: Annotated[str, typer.Option(help="Who did the spot-check.")],
    cases: Annotated[
        str,
        typer.Option(
            help="Comma-separated case ids you checked (default: the suggested sample, see "
            "`labels sample`)."
        ),
    ] = "",
    set_label: Annotated[
        list[str] | None,
        typer.Option("--set", help="CASE=act or CASE=escalate for a label you changed (repeat)."),
    ] = None,
    note: Annotated[str, typer.Option(help="Why you changed labels, kept with the change.")] = "",
    labels_file: Annotated[Path, typer.Option(help=LABELS_HELP)] = DEFAULT_LABELS,
) -> None:
    """Record a person's spot-check of the escalation labels: who, when, which cases and
    which labels changed. The go-live gate needs one covering at least 10 cases or 10% of
    the set; a check that changed more than one in ten of its labels does not count. Not
    available through `replaybook mcp`: a person signs this."""
    from replaybook.eval.labels import LabelChecks, record_check, uncounted
    from replaybook.schema.testcase import TestSet

    if not testset.is_file():
        raise typer.BadParameter(f"no test set at {testset}")
    changes: dict[str, str] = {}
    for item in set_label or []:
        case_id, sep, value = item.partition("=")
        if not sep or not case_id.strip():
            raise typer.BadParameter(f"--set takes CASE=act or CASE=escalate, not '{item}'")
        changes[case_id.strip()] = value.strip()
    test_set = TestSet.model_validate_json(testset.read_text(encoding="utf-8"))
    store = LabelChecks.load(labels_file)
    ids = [c.strip() for c in cases.split(",") if c.strip()] or None
    try:
        updated, entry = record_check(test_set, store, reviewer, ids, changes, note)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if entry.changed:
        testset.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    store.save(labels_file)
    escalate = sum(1 for v in entry.labels.values() if v == "escalate")
    typer.echo(
        f"{entry.id}: {entry.reviewer} checked {len(entry.labels)} of {entry.cases_in_set} labels "
        f"on {entry.test_set} ({escalate} escalate, {len(entry.labels) - escalate} act) -> "
        f"{labels_file}"
    )
    for change in entry.changed:
        typer.echo(f"  CHANGED {change.case_id}: {change.was} -> {change.now} ({testset} updated)")
    if not entry.counts:
        typer.echo(f"  NOTE {uncounted(entry).line()}")


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
    agent: Annotated[str, typer.Option(help=AGENT_HELP)] = "scripted",
    flaws: Annotated[str, typer.Option(help="Comma-separated scripted-agent flaws.")] = "",
    command: Annotated[str, typer.Option(help=COMMAND_HELP)] = "",
    transport: Annotated[str, typer.Option(help=TRANSPORT_HELP)] = "stdio",
    extract: Annotated[
        str, typer.Option(help="Seed empty worlds from the text: none, rules or model.")
    ] = "none",
    knowledge: Annotated[Path | None, typer.Option(help=KNOWLEDGE_HELP)] = None,
) -> None:
    """Run the agent on every OPEN ticket of a request type against the mock and store the
    actions it proposed. Nothing is executed and nothing is written to the ticket system."""
    from replaybook import workflow

    run_workflow(
        workflow.shadow_record,
        request_type,
        out,
        data,
        agent,
        flaws,
        command,
        extract,
        knowledge,
        transport,
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
    from replaybook import workflow

    run_workflow(workflow.shadow_score, out, data, extract)


@shadow_app.command("report")
def shadow_report_cmd(
    out: Annotated[Path, typer.Argument(help="Shadow directory.")],
) -> None:
    """Print the shadow summary (what the evidence pack shows under Shadow mode)."""
    from replaybook import workflow

    run_workflow(workflow.shadow_report, out)


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


if __name__ == "__main__":
    app()
