# Going live: approval requests, live runs and the degraded flag

A skill that passed its replay (the go-live gate, `docs/replay-outcomes.md`) can start
working live tickets, but Replaybook never runs it. A team's own runtime executes the
skill, asks a person to approve its writes, makes them, runs each write's check
(`docs/verified-writes.md`) and reports the run back. Replaybook defines those contracts,
writes the approval requests, keeps the records and computes the numbers. Every live write
needs a person's approval; nothing in `replaybook mcp` approves or records an approval.

## Approval requests

Before a person approves a skill's writes on a live ticket, the runtime asks Replaybook for
what the approver should see:

```bash
uv run replaybook approval-request --skill kafka_topic --ticket OPS-4211 --plan plan.json --out approvals/OPS-4211
uv run replaybook approval-request --skill kafka_topic --plan plan.json --out approvals/x --runs runs --evidence evidence --live-dir live
```

`--plan` is the writes the skill wants to make, as JSON (inline, a file, or `-` for stdin):
`[{"tool": "kafka_create_topic", "args": {...}}, ...]`. The command writes
`approval-request.md` and `approval-request.json` into `--out` for the team's approval
channel, with:

| Field | Where it comes from |
|---|---|
| Skill and version | the request type's SKILL.md and its SHA-256 (12 hex characters shown) |
| Replay result and date | the latest run under `--runs` (default `runs`) made with this version, as "passed 46 of 50 past tickets (44 matched, 2 correctly escalated)" |
| Go-live gate | that run's gate, with the label spot-checks in `--labels-file` and `--min-pass-rate` |
| Live verification | the share of the skill's last live runs with writes whose every write was verified, over the `--window`, or "no live runs yet" |
| Planned writes | each write with the check that confirms it, rendered from the skill's `verify:` |
| Evidence pack | the newest pack under `--evidence` (default `evidence`) whose manifest lists that run's results.json by hash |

The request is **BLOCKED**, said at the top in capitals, and the command exits 1 when:

- the skill's current version has not been replayed (edit a skill, and its old replays no
  longer count);
- the replay of this version failed the go-live gate (the reasons are listed);
- the plan holds a write the skill does not cover (no replay assessed it, and it has no
  check).

A skill that fails validation (`replaybook skills validate`) gets no request at all, and
neither does an empty plan or a plan of ticket tools only; the command exits 2. When the
skill is flagged degraded in live use, the request carries a WARNING line at the top but
is not blocked: the approver decides. Replaybook never approves: the decision is made in
the team's channel, the runtime makes the writes only after it, and the decision comes
back with the run (`replaybook live record`). `replaybook mcp` can render requests
(`replaybook_approval_request`) and has no way to approve one.

## The live-run record

The runtime reports each run as JSON:

```bash
uv run replaybook live record --run run.json                 # or --run - to read stdin
uv run replaybook live record --run run.json --live-dir live --discovery discovery/discovery.json
```

```json
{
  "skill": "kafka_topic",
  "skill_version": "5e0d2f4569c1",
  "ticket": "OPS-4211",
  "at": "2026-10-20T09:14:00Z",
  "approval": {"decision": "approved", "by": "ben", "reason": null, "at": "2026-10-20T09:12:30Z"},
  "writes": [
    {"tool": "kafka_create_topic", "args": {"cluster": "kafka-prod", "topic": "payments.events.v1", "partitions": 6}, "verification": "verified"},
    {"tool": "kafka_add_acl", "args": {"cluster": "kafka-prod", "topic": "payments.events.v1", "principal": "svc-payments-prod", "operations": ["WRITE", "DESCRIBE"]}, "verification": "verified"}
  ],
  "escalation": null
}
```

- `skill` is the request type (the same key `approval-request --skill` takes) and
  `skill_version` the SHA-256 of the SKILL.md the runtime ran, at least its first 12 hex
  characters. When it is the start of the current version, the full version is stored.
- `approval` is the person's decision: `approved` or `rejected`, by whom, an optional reason
  and time.
- `writes` lists each write made, with its check's result: `verified`, `verify_failed` or
  `verify_error` (`replaybook checks compare` gives it).
- `escalation` is set when the ticket went back to a person: `{"reason": ..., "by":
  "skill" | "verification" | "person"}`. `by: verification` means a check did not pass and
  the runtime stopped, as the contract requires.
- `at` defaults to the time of recording.

A record that breaks the contract is refused, so the file only ever holds runs that
followed it: writes on a rejected run, an approval without a name, a write that was not
verified without a hand-back, or a request type no loaded pack defines.

Replaybook appends the run to `runs.jsonl` in the live directory (`--live-dir`, else
`REPLAYBOOK_LIVE_DIR`, else `live/`) with what it computed:

- `id` (`LIVE-1`, `LIVE-2`, ...), `recorded_at` and the skill's name;
- `verification` for the run: `verified` when every write was verified, `verify_failed` or
  `verify_error` when one was not, `none` when the run made no writes (rejected, or handed
  back before writing);
- `escalated`;
- `hours_saved` and `hours_basis`.

## Hours saved

A **verified run** (approved, at least one write, every write verified, not handed back)
saves the request type's median engineer-hours from the discovery report
(`replaybook discover` writes `discovery/discovery.json`; `--discovery` points at another).
Every other run saves zero. When the report has no figure for the request type, or there
is no report, the hours are recorded as unknown with the reason and counted apart, never
guessed.

## Rolling rates and the degraded flag

```bash
uv run replaybook live report                   # every skill
uv run replaybook live report --skill kafka_topic --window 50
uv run replaybook live check                    # exit 1 when a skill is degraded
```

Per skill, over its last `--window` runs (`REPLAYBOOK_LIVE_WINDOW`, default 30):

| Rate | Is |
|---|---|
| approval rate | approved runs / runs in the window |
| verification rate | runs whose every write was verified / runs in the window that made writes |
| escalation rate | runs handed back to a person / runs in the window |

The report also gives the hours saved in the window and in all, how many runs in the
window ran the skill's current version, and the time of the last run.

A skill is **degraded** when its approval rate falls below `REPLAYBOOK_MIN_APPROVAL_RATE`
(default 0.8) or its verification rate below `REPLAYBOOK_MIN_VERIFICATION_RATE` (default
0.95); the options `--min-approval-rate` and `--min-verification-rate` override them. A
skill with no runs that made writes has no verification rate yet and is not degraded by
it. Replaybook is air-gapped by default and sends nothing anywhere, so it says so where the
team looks:

- `replaybook live check` exits 1, for a CI job or a scheduler;
- `replaybook live report` and `replaybook status` name the degraded skills and why;
- every change of a skill's state (degraded, or recovered) appends one line to
  `degraded.jsonl` in the live directory, which `live record` and `live check` write and
  an alerting job can follow: `{"at", "skill", "skill_version", "state", "reasons",
  "approval_rate", "verification_rate", "escalation_rate", "window", "runs_in_window"}`;
- `replaybook evidence --live live` adds a "Live runs" section to the evidence pack.

`replaybook mcp` has `replaybook_live_report`; recording a run is not a tool there.

## Moving a degraded skill back to dry-run

Replaybook does not switch skills on or off; the team's runtime does. When a skill is
degraded:

1. Switch the skill off in the runtime, or back to proposing without writing, so nothing
   more is changed on live tickets. Hand its open tickets to a person.
2. Find out why from the records: rejected approvals carry the approver's reason, failed
   checks the differences.
3. Fix the skill (or the agent), replay it until it passes the go-live gate again (a
   changed skill needs a new replay), and run it in shadow mode for a while if the cause
   was not obvious (`replaybook shadow`).
4. Switch it back on. The rolling rates recover as new verified, approved runs replace the
   old ones in the window, and `degraded.jsonl` gets a `recovered` line.
