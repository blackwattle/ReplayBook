"""The Public Jira Dataset as a ticket source.

Montgomery, Lüders and Maalej, "An Alternative Issue Tracking Dataset of Public Jira
Repositories" (MSR 2022), Zenodo 10.5281/zenodo.5882881, CC BY 4.0. It is a MongoDB dump:
one collection per public Jira (Apache, Hyperledger, MongoDB, Qt, ...), one document per
issue as the Jira REST API v2 returned it, users already anonymised to UUIDs.

Four ways in, all producing the same canonical tickets:

- the `.archive` file from the dump itself, streamed, one collection at a time (no
  database needed; the whole dump would take about 60 GB inside MongoDB);
- a `.bson` collection file from a plain `mongodump` directory;
- a `.jsonl` or `.jsonl.gz` file of issue documents (what `export` keeps as its raw copy);
- a MongoDB collection restored with the `jira` compose profile
  (mongodb://host:27017/JiraReposAnon?collection=Apache).

`export` keeps one project (Apache INFRA is the ops-style one), redacts at ingest and
writes tickets.jsonl, an empty platform.yaml and a DATASET.md data card. Nothing in the
dataset records what engineers actually did on the platform, so every expectation built
from it is unverified until a person confirms it.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from replaybook.adapters.jira_issue import issue_to_ticket, user_id, workflow_movers
from replaybook.adapters.redact import redact_ticket
from replaybook.schema.ticket import Ticket

SOURCE = "public_jira"
CITATION = (
    "Montgomery L, Lüders C, Maalej W. An Alternative Issue Tracking Dataset of Public Jira "
    "Repositories. MSR 2022. Zenodo 10.5281/zenodo.5882881 (CC BY 4.0)."
)


def iter_issues(
    source: str | Path, project: str | None = None, collection: str | None = None
) -> Iterator[dict[str, Any]]:
    """Issue documents from a mongodump .archive, a .bson file, a .jsonl(.gz) file or a
    MongoDB URI. `project` is a hint: MongoDB filters server-side and the archive reader
    skips other projects without decoding them; plain files are read in full.
    """
    text = str(source)
    if text.startswith("mongodb://") or text.startswith("mongodb+srv://"):
        yield from _iter_mongo(text, project)
        return
    path = Path(source)
    if path.suffix == ".archive":
        from replaybook.adapters.mongo_archive import iter_collection

        if not collection:
            raise ValueError("a mongodump archive needs --collection (for example Apache)")
        prefix = f"{project}-" if project else None
        yield from iter_collection(path, collection, key_prefix=prefix)
        return
    if path.suffix == ".bson":
        yield from _iter_bson(path)
        return
    opener = gzip.open if path.name.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:  # type: ignore[operator]
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _iter_bson(path: Path) -> Iterator[dict[str, Any]]:
    try:
        import bson
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise RuntimeError("reading .bson needs pymongo: run `uv sync --extra jira`") from exc
    with path.open("rb") as handle:
        yield from bson.decode_file_iter(handle)


def _iter_mongo(uri: str, project: str | None = None) -> Iterator[dict[str, Any]]:
    """mongodb://host:27017/JiraRepos?collection=Apache (collection in the query string)."""
    try:
        from pymongo import MongoClient
    except ImportError as exc:  # pragma: no cover - depends on the optional extra
        raise RuntimeError("MongoDB input needs pymongo: run `uv sync --extra jira`") from exc
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(uri)
    collection = (parse_qs(parts.query).get("collection") or [""])[0]
    database = parts.path.strip("/")
    if not database or not collection:
        raise ValueError("MongoDB input needs a database in the path and ?collection=<name>")
    plain = uri.split("?")[0]
    client = MongoClient(plain, serverSelectionTimeoutMS=5000)
    query = {"fields.project.key": project} if project else {}
    yield from client[database][collection].find(query, no_cursor_timeout=True)


def issue_project(issue: dict[str, Any]) -> str:
    fields = issue.get("fields") or {}
    key = str(issue.get("key") or "")
    return str((fields.get("project") or {}).get("key") or key.split("-")[0])


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def extract_project(
    source: str | Path, project: str, out_path: Path, collection: str | None = None
) -> int:
    """Pass 1: copy the raw issue documents of one project to a .jsonl.gz file."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with gzip.open(out_path, "wt", encoding="utf-8") as handle:
        for issue in iter_issues(source, project, collection):
            if issue_project(issue) != project:
                continue
            handle.write(json.dumps(issue, default=_json_default))
            handle.write("\n")
            count += 1
    return count


def convert_project(raw_path: Path, source: str = SOURCE) -> list[Ticket]:
    """Pass 2: canonical, redacted tickets. Engineers are learnt from the whole project."""
    engineers: set[str] = set()
    for issue in iter_issues(raw_path):
        assignee = user_id((issue.get("fields") or {}).get("assignee"))
        if assignee:
            engineers.add(assignee)
        engineers |= workflow_movers(issue)
    tickets: list[Ticket] = []
    for issue in iter_issues(raw_path):
        tickets.append(redact_ticket(issue_to_ticket(issue, source, engineers)))
    tickets.sort(key=lambda t: (t.created, t.id))
    return tickets


def export(
    source: str | Path,
    project: str,
    out_dir: Path,
    collection: str | None = None,
    effort: str = "logged",
) -> dict[str, Any]:
    """Keep one project: raw copy, canonical tickets, platform facts and a data card.
    `effort` is the effort source: logged (Jira's time spent), status_time or
    created_to_resolved."""
    from replaybook.adapters.effort import apply_effort_all

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "raw" / f"{project}.jsonl.gz"
    count = extract_project(source, project, raw_path, collection)
    tickets = apply_effort_all(convert_project(raw_path), effort)
    with (out_dir / "tickets.jsonl").open("w", encoding="utf-8") as handle:
        for ticket in tickets:
            handle.write(ticket.model_dump_json())
            handle.write("\n")
    (out_dir / "platform.yaml").write_text(
        "# No platform facts are known for a public Jira project. Procedure changes, if any,\n"
        "# have to be added by hand (request_type, at, from_version, to_version).\n"
        "procedure_changes: []\n",
        encoding="utf-8",
    )
    stats = summarise(tickets, project, str(source), effort)
    (out_dir / "DATASET.md").write_text(data_card(stats), encoding="utf-8")
    stats["raw_documents"] = count
    return stats


def summarise(
    tickets: list[Ticket], project: str, source: str, effort: str = "logged"
) -> dict[str, Any]:
    statuses = Counter(t.status for t in tickets)
    resolutions = Counter(t.resolution or "(none)" for t in tickets)
    waited = sum(
        any(c.to_status == "Waiting on Requester" for c in t.status_changes) for t in tickets
    )
    return {
        "project": project,
        "source": source,
        "tickets": len(tickets),
        "comments": sum(len(t.comments) for t in tickets),
        "status_changes": sum(len(t.status_changes) for t in tickets),
        "with_code_changes": sum(bool(t.code_changes) for t in tickets),
        "with_links": sum(bool(t.links) for t in tickets),
        "waited_on_requester": waited,
        "with_time_spent": sum(t.time_spent_hours is not None for t in tickets),
        "effort_source": effort,
        "by_status": dict(statuses.most_common()),
        "by_resolution": dict(resolutions.most_common()),
        "oldest": min((t.created for t in tickets), default=None),
        "newest": max((t.created for t in tickets), default=None),
        "engineer_comments": sum(c.role == "engineer" for t in tickets for c in t.comments),
        "bot_comments": sum(c.role == "bot" for t in tickets for c in t.comments),
    }


def _describe_effort(source: str) -> str:
    from replaybook.adapters.effort import describe

    return describe(source)


def data_card(stats: dict[str, Any], title: str = "", provenance: str = "") -> str:
    def table(counts: dict[str, int]) -> str:
        return "\n".join(f"| {name} | {count} |" for name, count in counts.items())

    oldest = str(stats["oldest"])[:10] if stats["oldest"] else "-"
    newest = str(stats["newest"])[:10] if stats["newest"] else "-"
    title = title or f"Public Jira Dataset, project {stats['project']}"
    provenance = provenance or (
        f"Source: {CITATION}\nExported from `{stats['source']}` by `replaybook public-jira "
        "export`. Real tickets, written by real people, anonymised by the dataset authors "
        "(users are UUIDs) and redacted again at ingest (emails, public IP addresses and "
        "secret-looking strings)."
    )
    return f"""# Dataset: {title}

{provenance}

| Measure | Value |
|---|---|
| Tickets | {stats["tickets"]} |
| Date range | {oldest} to {newest} |
| Comments | {stats["comments"]} |
| Comments by engineers / bots | {stats["engineer_comments"]} / {stats["bot_comments"]} |
| Status changes (from the changelog) | {stats["status_changes"]} |
| Tickets that waited on the requester | {stats["waited_on_requester"]} |
| Tickets with linked code changes (from bot comments) | {stats["with_code_changes"]} |
| Tickets with issue links | {stats["with_links"]} |
| Tickets with an effort figure | {stats["with_time_spent"]} |
| Effort source | {_describe_effort(stats.get("effort_source", "logged"))} |

## Status (canonical)

| Status | Tickets |
|---|---|
{table(stats["by_status"])}

## Resolution

| Resolution | Tickets |
|---|---|
{table(stats["by_resolution"])}

## What is and is not here

- There is no audit log. Nothing in Jira records which tool an engineer ran, so tickets
  have no recorded actions and every expectation built from them is **unverified** until a
  person confirms it. `replaybook build-testset --extract rules` (or `model`) derives the
  expected actions from the ticket text.
- Statuses are mapped onto the canonical names (Open, In Progress, Waiting on Requester,
  Resolved, Closed); the ticket keeps unknown names as they are. "Waiting for user" style
  statuses become "Waiting on Requester", which is what makes an "ask" case.
- Comment roles: the reporter is the requester; assignees and people who moved issues
  through the workflow are engineers; known bots are bots; everyone else is "other".
- Platform facts (platform.yaml) are empty: procedure changes are not known for a public
  project and have to be added by hand.
"""
