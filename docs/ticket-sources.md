# Ticket sources: bringing your own ticket system

Replaybook works from canonical tickets (`tickets.jsonl` in a dataset directory). An
adapter produces them from wherever your history lives:

| Source | Command | Effort source default |
|---|---|---|
| CSV, JSONL or JSON export of any system | `replaybook tickets import --mapping mapping.yaml --out data/x` | whatever the mapping says (`logged` if unset) |
| Postgres table (RAG or warehouse) | `replaybook postgres import --mapping mapping.yaml --out data/x` | whatever the mapping says |
| Public Jira Dataset | `replaybook public-jira export ... --effort logged` | `logged` |
| Jira Cloud | `replaybook jira-cloud export ... --effort logged` | `logged` |
| ServiceNow | `replaybook servicenow export ... --effort status_time` | `status_time` |
| Synthetic | `replaybook generate` | `logged` where the generator logged time |

Every adapter redacts at ingest (emails, public IP addresses, secret-looking strings) and
writes a `DATASET.md` data card that names the effort source.

## Effort sources

Discovery ranks request types by engineer-hours, so each adapter says what its hours mean.
Every ticket carries `time_spent_hours` and `effort_source`:

- `logged`: time the engineer recorded on the ticket. The best figure and the one most
  often missing. Tickets without it fall back to the activity estimate.
- `status_time`: time the ticket spent in working statuses (default `In Progress` and
  `Reopened`; configurable). Needs status-change history.
- `created_to_resolved`: wall-clock time from creation to resolution. Always available,
  counts waiting time as work, so it overstates requests that waited on the requester.
- `estimate`: not an adapter choice. Discovery marks a ticket `estimate` when its source
  yielded nothing and derives hours from engineer comments, status changes and actions.

The discovery report shows how many tickets fell into each source.

## The csv/jsonl mapping

The mapping is a YAML file next to the export. Paths are relative to the mapping file.

```yaml
source: servicedesk            # recorded on every ticket as its source
project: OPS

tickets:
  file: tickets.csv            # .csv, .jsonl or .json
  columns:                     # canonical field -> column name in the file
    id: Issue key              # required
    summary: Summary           # required
    description: Description   # required
    reporter: Reporter         # required
    created: Created           # required
    status: Status             # required
    resolved: Resolved
    resolution: Resolution
    assignee: Assignee
    priority: Priority
    labels: Labels             # split on list_separator, or a JSON list
    components: Components
    time_spent_hours: Time spent (h)   # or time_spent_minutes / time_spent_seconds
    comments: Comments         # a JSON list in the cell, instead of a companion table
    status_changes: History    # same
    actions: Actions           # same
    links: Links               # same: a JSON list of {type, target}
  list_separator: ";"
  datetime_format: null        # ISO 8601 when null; otherwise a strptime format such as "%d/%m/%Y %H:%M"

comments:                      # optional companion table, one row per comment
  file: comments.csv
  columns:
    ticket_id: Issue key
    author: Author
    created: Created
    body: Body
    role: Role                 # optional: requester, engineer, bot or other

status_changes:                # optional companion table, one row per transition
  file: history.csv
  columns:
    ticket_id: Issue key
    at: Changed
    from_status: From
    to_status: To

actions:                       # optional: an audit log of tool calls (JSONL is easiest)
  file: actions.jsonl
  columns:
    ticket_id: ticket_id
    tool: tool
    args: args                 # a JSON object (or a JSON string in a CSV cell)
    at: at

links:                         # optional companion table, one row per link between tickets
  file: links.csv
  columns:
    ticket_id: Issue key
    target: Linked issue
    type: Link type
link_types:                    # optional: your link names -> duplicates, relates or fixes
  "Problem/Incident": fixes

status_map:                    # your status names -> the canonical ones
  "Pending customer": "Waiting on Requester"
  "Done": "Resolved"

engineers: [alice, bob]        # comment authors treated as engineers (the assignee always is)

effort:
  source: status_time          # logged | status_time | created_to_resolved
  working_statuses: [In Progress, Reopened]
```

What the fields mean:

- **Canonical statuses** are `Open`, `In Progress`, `Waiting on Requester`, `Resolved` and
  `Closed`. Unknown names are kept as they are and listed in the data card. A status that
  means "waiting for the person who asked" must map to `Waiting on Requester`: that is how
  a ticket becomes an "ask" case (the agent is expected to ask instead of acting).
- **Comment roles**: the reporter is the requester; the assignee and anyone in `engineers`
  is an engineer; a `role` column overrides both. Ask cases without an audit log need an
  engineer comment with a question mark before the waiting period.
- **Status-change history** matters. Without it there are no ask cases and `status_time`
  has nothing to measure; the import warns clearly. With only `created` and `resolved`, the
  adapter writes a two-step history (Open, then the final status).
- **Actions** are the audit log of what engineers did, as tool calls, when a system has
  one. Tool names must match the pack's mock tools. Tickets without actions get their
  expectations from the text (`build-testset --extract rules|model`), labelled unverified.
- **Links** tie a ticket to others (duplicates, relates, fixes). They are the ground truth
  `replaybook knowledge eval` scores past-ticket search against (docs/knowledge-sources.md).
  Link names are mapped through `link_types`; otherwise a name containing "dup" means
  duplicates, "fix" means fixes and anything else relates. A link may point at a ticket
  outside the export; a link to the ticket itself is dropped.
- **Datetimes** are ISO 8601 unless `datetime_format` says otherwise; naive values are
  taken as UTC; numbers are Unix timestamps.

## Worked example: the payroll tickets as a CSV export

`examples/payroll/csv/` holds the conformance tickets as a ticket system would export
them: `tickets.csv`, `comments.csv`, `history.csv`, `actions.jsonl` and `mapping.yaml`.

```powershell
uv run replaybook tickets import --mapping examples/payroll/csv/mapping.yaml --out data/payroll
uv run replaybook discover --data data/payroll --out discovery/payroll
```

The test suite checks that the import produces the same canonical tickets as
`examples/payroll/tickets.jsonl`.

## ServiceNow

```powershell
# SERVICENOW_INSTANCE, SERVICENOW_USER, SERVICENOW_PASSWORD in .env
uv run replaybook servicenow export --table incident --out data/servicenow/incident
uv run replaybook servicenow export --table sc_req_item --query "assignment_group.name=Platform" --out data/servicenow/platform-requests --effort logged
```

The adapter reads the Table API: the records (`incident` or `sc_req_item`), their
comments and work notes from `sys_journal_field`, and their state transitions from
`sys_audit`. States map onto the canonical names: New and Pending are Open; In Progress and
Work in Progress are In Progress; Resolved and Closed Complete are Resolved; Closed, Closed
Incomplete, Closed Skipped and Canceled are Closed. **On Hold counts as Waiting on Requester
only with hold reason Awaiting Caller**; awaiting a vendor, a change or a problem is the
team's wait, not the requester's. Work notes are engineer comments; customer-visible
comments are the caller's when the user name matches the caller, the assignee's when it
matches the assignee, "other" otherwise.

User references are reduced to sys_ids before the raw copy is written (user names are used
in memory for the comment roles only). The default effort source is `status_time`, because
`time_worked` is rarely used; `--effort logged` reads it when it is. A live export needs a
developer instance; the tests run against fixtures built from the
documented response shapes.

## The Postgres mapping

Many teams already have their tickets in a Postgres table: a RAG pipeline's document store
or a warehouse. The Postgres adapter reads them with the same canonical fields; the mapping
names tables and columns instead of files. Install the driver with
`uv sync --extra postgres`.

```yaml
dsn: ${POSTGRES_DSN}             # a connection string, or the name of a variable holding one;
                                 # POSTGRES_DSN / REPLAYBOOK_POSTGRES_DSN are read when unset
source: warehouse
project: OPS

tickets:
  table: analytics.tickets       # table or schema.table (identifiers only; no SQL)
  columns:                       # canonical field -> column, as for csv/jsonl
    id: ticket_key
    summary: title
    description: body
    reporter: requester
    created: opened_at
    status: state
    resolved: closed_at
    time_spent_minutes: minutes
  where: "project = 'OPS'"       # optional SQL fragment appended to the ticket query
  limit: 50000                   # optional

comments:
  table: analytics.ticket_comments
  columns: {ticket_id: ticket_key, author: author, created: posted_at, body: body}

status_changes:                  # REQUIRED (see below)
  table: analytics.ticket_status_history
  columns: {ticket_id: ticket_key, at: changed_at, from_status: old_state, to_status: new_state}

actions:                         # optional audit log of tool calls (args as JSON/jsonb)
  table: analytics.ticket_actions
  columns: {ticket_id: ticket_key, tool: tool, args: args, at: at}

links:                           # optional links between tickets
  table: analytics.ticket_links
  columns: {ticket_id: ticket_key, target: linked_key, type: link_type}

status_map: {"Pending customer": "Waiting on Requester", "Done": "Resolved"}
engineers: [alice]
effort: {source: status_time, working_statuses: [In Progress, Reopened]}
```

```powershell
uv run replaybook postgres import --mapping warehouse.yaml --out data/warehouse
```

**Status-change history is required.** A RAG table often holds only the final text of a
ticket. Without the transitions Replaybook cannot tell the tickets where engineers asked
the requester before acting (so there are no ask cases, and an agent that never asks looks
fine), and `status_time` has nothing to measure. The import therefore refuses when no
`status_changes` table is mapped. `--allow-missing-history` (or
`allow_missing_history: true` in the mapping) imports anyway, prints a warning in capitals
and records it in the data card; expectations then come only from recorded actions or
from the text.

Two more things to know:

- Companion tables are read whole and joined on the ticket id in memory; filter the ticket
  table with `where` and `limit`, and keep companion tables to the same project.
- Replaybook's air-gapped mode intercepts Python sockets. The Postgres driver connects
  through libpq, which it cannot see, so the database host is governed by your network
  (the container's, in the image), not by `REPLAYBOOK_ALLOWED_HOSTS`.

**A RAG table during replay.** If the agent under test searches the same table through its
own retrieval tool, nothing stops it reading the ticket it is being tested on, or later
ones. Either route the agent through Replaybook's `knowledge_search_tickets` (served from
the imported dataset with the time-travel guard: only items created strictly before the
ticket under test, cut at that instant, never the ticket itself), or serve the agent's own
retrieval server as a knowledge pack, which puts the same guard around its tools
(docs/knowledge-sources.md). Embeddings computed over a whole table leak too, because the
vector of a later ticket is still a neighbour; give your server an as-of filter on the
creation column so it ranks only what existed then.

Develop and test against a throwaway Postgres (for example `docker run --rm -e
POSTGRES_PASSWORD=x -p 5432:5432 postgres:16` with a few rows), never against real company
data. The test suite uses a fake fetcher and never opens a connection.
