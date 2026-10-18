# Verified writes: every write a skill makes declares the check that confirms it

A skill that changes infrastructure has to say how anyone will know the change took
effect. Each write tool a skill lists in `tools:` names a **read-only check** in the
skill's front matter: the read tool, its arguments, the state it must show afterwards, and
how long to wait. Replaybook validates those checks, lists them for every planned write
during replay, shows them to the approver, and gives a team's runtime the comparison it
needs. Replaybook never runs a check itself; replay is always a dry run against mocks.

## Declaring checks

```yaml
---
name: kafka-topic-acls
request_type: kafka_topic
tools: kafka_cluster_info, kafka_describe_topic, kafka_create_topic, kafka_add_acl, ticket_ask_requester, ticket_escalate, ticket_comment
verify:
  kafka_create_topic:
    tool: kafka_describe_topic
    args: {cluster: "{cluster}", topic: "{topic}"}
    expect: {partitions: "{partitions}", retention_ms: "{retention_ms}"}
    timeout_s: 30
  kafka_add_acl:
    tool: kafka_describe_topic
    args: {cluster: "{cluster}", topic: "{topic}"}
    expect: {acls: [{principal: "{principal}", operations: "{operations}"}]}
---
```

- `tool` is the read the runtime calls after the write. It must exist in a loaded pack and
  be read-only by its MCP annotations (`readOnlyHint`). It does not have to be in `tools:`:
  the runtime runs the check, not the agent.
- `args` and `expect` are templates over the **write's** arguments. `"{name}"` is replaced
  by the write's argument of that name; a string that is only a placeholder keeps the
  value's type, so `"{operations}"` stays a list and `"{partitions}"` a number. In longer
  strings the value is inserted as text (`"svc-{service}"`), and `{{` and `}}` are literal
  braces. An argument the write was not given renders as null, and null parts of `expect`
  are not compared, so one check can cover optional arguments; a rendered check leaves
  them out altogether.
- `timeout_s` is how long the runtime may take to see the expected state, default 30.
- The core ticket tools (`ticket_ask_requester`, `ticket_escalate`, `ticket_comment`) take
  no check: the ticket itself shows their effect.

The three example skills and the payroll overlay in `examples/payroll/` declare checks for
every write. `replaybook tools import` writes `skills/example/SKILL.md` into the new pack
with a placeholder check for every non-destructive write (an unknown read tool and an empty
`expect`), so the skeleton fails validation until a person writes the real checks.

## Validation

```powershell
uv run replaybook skills validate                     # every skill of the loaded packs
uv run replaybook skills validate --type kafka_topic  # one request type
uv run replaybook skills validate --file packs/payroll/skills/example/SKILL.md
```

A skill is rejected when:

- a write tool in `tools:` has no check under `verify:`;
- a check's tool is unknown, or is not read-only;
- a template names an argument the write does not take (by its input schema);
- a check passes the read tool an argument it does not take, or leaves out one it needs;
- a check has no `expect` (it would confirm nothing), or a timeout that is not positive;
- `verify:` has a check for a tool that is not a write in `tools:`, or for a ticket tool;
- `tools:` names a tool no loaded pack serves.

Exit code 1 when any skill is invalid. `replay`, `check` (when it re-runs), `shadow record`
and `approval-request` run the same validation before they start and refuse an invalid
skill with the list of problems. `replaybook mcp` offers it as `replaybook_skills_validate`.

## What replay does with the checks

Nothing is run. For every case, each infrastructure write the agent made against the mocks
is listed on the case result (`planned_checks` in `results.json`) with its check rendered
from the write's arguments:

```json
{"write": {"tool": "kafka_add_acl", "args": {"cluster": "kafka-prod", "topic": "payments.events.v1", "principal": "svc-payments-prod", "operations": ["WRITE", "DESCRIBE"]}},
 "check": {"tool": "kafka_describe_topic", "args": {"cluster": "kafka-prod", "topic": "payments.events.v1"},
           "expect": {"acls": [{"principal": "svc-payments-prod", "operations": ["WRITE", "DESCRIBE"]}]}, "timeout_s": 30.0},
 "note": ""}
```

A write the skill has no check for (a tool outside the skill) is listed with `"check":
null` and a note; the score already counts it against the case. Writes the mock rejected
are left out. Shadow proposals carry the same list. The evidence pack has a "Planned
checks" section: the checks each skill declares and every planned write with its rendered
check.

## The live contract

Replaybook does not execute skills. A team's runtime does, and the contract for each write
is:

1. **Render the check before writing.** `replaybook checks render --skill kafka_topic
   --write write.json` prints the check for that write (`write.json` holds `{"tool": ...,
   "args": {...}}`), or call `replaybook.checks.checks_for(...)[tool].render(args)`.
2. **Make the write**, once a person has approved it (see `docs/go-live.md`).
3. **Run the check within its timeout.** Call the read tool with the rendered arguments,
   polling if the system is eventually consistent, until the state matches or `timeout_s`
   has passed.
4. **Compare and record one result per write**:
   - `verified`: the read shows the expected state;
   - `verify_failed`: the read ran and the state differs;
   - `verify_error`: the check could not run, returned an error, or timed out.

   `replaybook checks compare --check check.json --result read.json` prints the result with
   the differences and exits 0 (verified), 1 (verify_failed) or 2 (verify_error);
   `--expect` takes the expected state directly. The function is
   `replaybook.checks.compare(expect, result)`.
5. **On anything but verified, stop.** Make no further writes for that ticket, hand the
   ticket back to a person with the differences as the reason, and record why. The run's
   overall verification is then `verify_failed` or `verify_error`.
6. **Report the run** to Replaybook with `replaybook live record` (`docs/go-live.md`): the
   approval, each write's verification result and any hand-back. Hours saved count only
   runs whose every write was verified.

## How a result is compared with `expect`

- An object in `expect` must be a subset of the result: each key present, each value
  matching. Keys the result has and `expect` does not mention are ignored.
- A list in `expect` matches when each of its items matches some item of the result's list,
  in any order.
- A single value matches an equal value, or a list that contains one.
- Values compare ignoring case and surrounding space, numbers by value (`"6"` equals `6`),
  and `"true"`/`"false"` as booleans.
- A result with a top-level `error` key (that `expect` does not ask for), or no result at
  all, is a `verify_error`, never a pass.
