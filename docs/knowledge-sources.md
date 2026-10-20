# Knowledge sources: your own past-ticket search behind the time-travel guard

Many teams already have a retrieval server over their ticket history: a RAG index behind
an MCP server that answers "has this happened before, and how was it solved?". An agent
under test should use that server, with its real tool names, descriptions and ranking,
because that is what it will use in production. Two things stand in the way:

1. **The server cheats during replay.** Its index holds the very ticket being replayed,
   with its resolution, and everything that happened afterwards: later tickets quoting
   the answer, comments added to old tickets since, resolutions that arrived later.
2. **Nobody knows how good its search is.** An agent that fails because the search handed
   it the wrong tickets looks the same as an agent that ignored the right ones.

A **knowledge pack** fixes the first: the server's tools are served behind Replaybook's
time-travel guard. **`replaybook knowledge eval`** fixes the second: it scores the search on
its own, against what engineers actually linked, before any agent is involved.

## The guard

Every case of a replay has a horizon: the creation time of the ticket under test. For each
call an agent makes to a knowledge pack's tools, Replaybook forwards the call to your
server and then, before the agent sees anything:

| Rule | Why |
|---|---|
| Only tickets created strictly before the horizon come back | nothing later existed then |
| Never the ticket under test, whatever the case of its id | its own resolution is the answer |
| A ticket with no id or no parseable creation time is dropped | the guard fails closed |
| `until_resolved` fields (resolution text, final status) and the resolution time are removed unless the ticket was resolved before the horizon | the answer may have come later |
| `timed` lists inside a ticket (comments) keep only items before the horizon | comments added later |
| `remove` fields are always removed | they reveal the present (age in days, ranking scores) |
| A tool that takes a ticket id answers only for a visible ticket, checked through the `lookup` tool | "find similar to T-123" must not reach the future |
| Results are over-fetched (`max_limit`) and trimmed to what the agent asked for | filtering does not leave the agent short |
| An `as_of_arg` is set to the horizon and hidden from the agent's schema | the server filters too, so recall holds; the agent cannot move the horizon |

Every call is recorded as a read in the case's call record, with the result exactly as the
agent saw it. Only tools your server annotates read-only (or that the pack marks
`read_only: true` deliberately) can be in a knowledge pack: a knowledge pack never writes.

What the guard cannot do: change how your server **ranked** its results. If the index was
built today, a ticket's embedding includes comments written after the horizon, and those
can lift it in the ranking even though the comments themselves are cut from the result. A
date-only creation field also means tickets from the horizon's own day are dropped. The report `knowledge eval` writes states the ranking
limitation next to its numbers.

## Making a knowledge pack

```bash
# Over streamable HTTP; secrets as ${VARIABLES} from .env, never values:
uv run replaybook knowledge import --name ticket_rag --url '${TICKET_RAG_URL}' \
    --header 'Authorization=Bearer ${TICKET_RAG_TOKEN}' --out packs
# Or a server you start over stdio:
uv run replaybook knowledge import --name ticket_rag --command "python rag_server.py" --out packs
```

The import lists the server's tools (the only call it makes) and writes
`packs/ticket_rag/tools.json` with the read-only tools and `packs/ticket_rag/pack.yaml` with
a guard section guessed from the argument names. It cannot know where your server keeps
the ticket id or the dates, so those say `CHANGE_ME`. Run a check with a query to see a
real result's fields:

```bash
uv run replaybook knowledge check ticket_rag --query "certificate expired on the load balancer"
```

It prints what the server returned, what the guard removed and why, the fields of the first
raw result, and every problem (a CHANGE_ME left, an argument the tool does not have, a tool
not annotated read-only); it exits 1 while there are problems. `--at 2025-06-01T09:00:00Z`
probes at an earlier horizon; `--refresh` lists the tools again after the server changed.

### pack.yaml reference

```yaml
name: ticket_rag
description: Past tickets from the team's own search server
knowledge:
  server:
    url: ${TICKET_RAG_URL}            # streamable HTTP, or
    command: [python, server.py]      # stdio, started in the pack directory
    headers: {Authorization: "Bearer ${TICKET_RAG_TOKEN}"}
    env: {}                           # extra environment for a stdio server
    timeout_s: 60
  search: search_tickets              # the tool `knowledge eval` scores (needs query_arg)
  lookup: get_ticket                  # one ticket by id: decides whether an id is visible
  tools:
    search_tickets:
      results: results                # the key holding the list of tickets (dotted path)
      id: key                         # field of each ticket: its id
      created: created                # field: creation time (ISO 8601, or a date)
      resolved: resolved              # field: resolution time, if the server has one
      until_resolved: [resolution_note, status]
      remove: [age_days, score]
      query_arg: query
      limit_arg: top_k                # the agent's limit is honoured after guarding
      max_limit: 20                   # what is asked of the server
      as_of_arg: created_before       # optional: the server's own "before" filter
      as_of_format: datetime          # datetime (ISO 8601) or date (YYYY-MM-DD)
      count_field: count              # rewritten to the number actually returned
    find_similar_tickets:
      ticket_arg: ticket_key          # checked through the lookup tool first
      results: results
      id: key
      created: created
    get_ticket:
      ticket_arg: ticket_key
      result: ticket                  # the key holding the one ticket ("" = the result itself)
      id: key
      created: created
      resolved: resolved
      until_resolved: [resolution_note, status]
      timed: {comments: at}           # list inside the ticket -> its time field
      remove: [links]
```

Tool errors your server returns as data (`{"ok": false, "error": ...}`) reach the agent as
an error; a server that cannot be reached is an error for that call, never a crash.

**Ticket ids must match.** The guard compares the ids your server returns with the id of
the ticket under test, so the test set must come from the same ticket system as the index
(import it with `tickets import`, `postgres import` or `jira-cloud export`).

### Give your server an "as of" filter

The guard works without one, but a search over the whole index fills its top results with
later tickets that the guard then throws away. If you own the server, add an optional
argument that keeps only tickets created before a given time, apply it inside the search
(before ranking, not after), and compute any recency weighting relative to it instead of
today. Name it in `as_of_arg`; Replaybook sets it and hides it from the agent.

## Scoring the search: `knowledge eval`

```bash
uv run replaybook knowledge eval --data data/warehouse --source builtin --source ticket_rag --n 200 --k 10 --out retrieval/
```

- **Cases**: the newest `--n` tickets (optionally `--since`/`--until`) that are linked to,
  or mention in a comment, an earlier ticket of the dataset. Links count in both directions
  (`duplicates` weighs 2, everything else 1). Import links with your tickets
  (`links:` in the mapping, docs/ticket-sources.md); Jira exports carry them already.
- **Query**: the ticket as filed, summary and description (`--query-chars`, default 1000).
- **Search**: at the ticket's creation time, through the guard. `builtin` is Replaybook's
  own keyword search over the dataset: the baseline to beat.
- **Scores** at k: hit rate (any related ticket in the top k), recall, MRR, nDCG, and how
  often a known duplicate was found; latency per search; and, per knowledge pack, the
  leaks the guard removed (the ticket itself, later tickets, undated results).

It writes `retrieval.md` (the table above, side by side for every source), `retrieval.json`
and `cases_<source>.jsonl` with every ticket's related tickets, what came back and what was
found. Links are never complete, so absolute numbers are lower bounds: compare sources, or
one source before and after a change (a new embedding model, a recency setting), on the
same cases. The same eval is the `replaybook_knowledge_eval` tool of `replaybook mcp`.

## Replaying an agent with it

A knowledge pack is a pack like any other: put its directory on `REPLAYBOOK_PACKS` (next
to your tool packs) and every door sees it.

- External agents and launchers get one more MCP server in their config (the pack's name);
  `replaybook mock-server --server ticket_rag` serves it over stdio or `--http`.
- The built-in skill agent is offered every knowledge pack's tools beside the built-in
  `knowledge_search_tickets`.
- The tool fingerprint covers the pack's tools, so adding or changing a knowledge pack
  changes the baseline, as it should.

Your server is live: its index changes daily, so two replays a week apart can see different
search results for the same case. The call record keeps exactly what the agent saw in each
run. In air-gapped mode the server's host must be in `REPLAYBOOK_ALLOWED_HOSTS`; a stdio
server is a child process and is governed by the container's network instead.

## Worked example

`examples/ticket_rag/server.py` is a fake ticket-search server (MCP SDK and the standard
library only) over any dataset directory. Like most real retrieval servers it knows nothing
about replay: it returns every match whenever it was created, with resolutions, comments and
age. `examples/ticket_rag/packs/ticket_rag/` is the knowledge pack for it.

```bash
uv run replaybook generate --out data/generated
export TICKET_RAG_DATA=data/generated
export REPLAYBOOK_PACKS=packs:examples/ticket_rag/packs
uv run replaybook packs
uv run replaybook knowledge check ticket_rag --query "kafka topic for payments" --at 2024-06-01T00:00:00Z
uv run replaybook knowledge eval --data data/generated --source builtin --source ticket_rag --out retrieval
```

`tests/test_knowledge_packs.py` seeds the leaks of `tests/test_time_travel.py` (a later
ticket quoting the answer, the ticket's own resolution, a long-running ticket whose answer
arrived later) into this server and proves none of them reaches the agent, with and without
the server's own as-of filter, in-process and through `mock-server` over stdio.
