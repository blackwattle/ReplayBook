# Replaybook

Replaybook tests operations agents against your team's past work before those agents are
trusted with production access.

It turns ticket history into repeatable cases, runs an agent against recording mocks,
compares its proposed actions with what engineers actually did, and produces evidence for
human review.

> **Safe by design:** bundled tool packs are local mocks. Reads come from per-case world
> state; writes are recorded, never executed. Replaybook does not approve or perform live
> changes.

## Why Replaybook?

An agent demo proves that an agent can complete one task. Replaybook tests whether it can
behave safely across the cases your team actually handles:

- ask when information is missing;
- escalate work that needs human judgement;
- avoid unsafe or unapproved writes;
- follow the current procedure rather than an outdated one;
- detect regressions after a model, skill, prompt, or tool change; and
- produce reviewable evidence before a controlled rollout.

Replaybook is tool- and ticket-system agnostic. MCP servers become recording mock packs,
ticket exports become test cases, and agent CLIs run through launchers.

```text
Ticket history -> test set -> mocked replay -> score and review
               -> approved baseline -> shadow mode -> evidence pack
```

## Features

- CSV, JSONL, JSON, Postgres, Jira, and ServiceNow ingestion
- MCP tool import with generated recording mocks and scoring rules
- scripted, built-in skill, external-process, and agent-CLI execution
- deterministic scoring for actions, safety failures, and escalations
- human-reviewed labels, accepted differences, and baselines
- shadow mode for open tickets without executing proposed changes
- HTML and JSON evidence packs with SHA-256 manifests
- offline and air-gapped operation with no telemetry

## Quick start

### Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) for installation and dependency management

### Install the command

From a checkout of this repository:

```bash
uv tool install --editable .
replaybook version
```

If the command is not on `PATH`, run `uv tool update-shell` and open a new terminal.

For contributor development, use the project environment instead:

```bash
uv sync
source .venv/bin/activate             # Linux/macOS
# .venv\Scripts\Activate.ps1          # PowerShell
replaybook version
```

Every example below uses the installed `replaybook` command. Without installation or
activation, use `uv run replaybook ...` as a fallback.

### Run the offline demo

```bash
replaybook demo --out demo_out
```

The demo creates synthetic tickets, catches seeded agent flaws, compares a corrected run
with its baseline, and writes:

```text
demo_out/evidence/evidence.html
```

No model key, Docker daemon, or network access is required.

## Typical workflow

```bash
# Build history and identify a useful request type.
replaybook generate --out data/generated --seed 42 --n 3000
replaybook discover --data data/generated --out discovery
replaybook build-testset --type kafka_topic --data data/generated --n 40

# Replay and inspect an agent.
replaybook replay \
  --testset testsets/kafka_topic.json \
  --out runs/kafka-v1 \
  --agent scripted
replaybook report runs/kafka-v1
replaybook groups runs/kafka-v1

# Review labels, approve a baseline, and gate future changes.
replaybook labels sample --testset testsets/kafka_topic.json --out review.md
replaybook labels check --testset testsets/kafka_topic.json --reviewer reviewer
replaybook approve --run runs/kafka-v1 --approver reviewer
replaybook check --testset testsets/kafka_topic.json --out runs/check-kafka --go-live

# Produce review evidence.
replaybook evidence \
  --run runs/check-kafka \
  --testset testsets/kafka_topic.json \
  --out evidence/kafka
```

The go-live check fails when the pass-rate threshold is missed, an unsafe outcome or agent
error exists, labels have not been reviewed, or the current skill version was not tested.
See [Replay outcomes and gates](docs/replay-outcomes.md).

## Bring your own systems

### Tools

Generate a recording-mock pack from an MCP server, a running URL, or a saved tool list:

```bash
replaybook tools import --name payroll \
  --command "python path/to/payroll_server.py" --out packs
replaybook tools import --name crm \
  --url http://crm-mcp.internal:8000/mcp --out packs
replaybook tools import --name crm \
  --json saved-tools-list.json --out packs
```

Tool listing is the only call made during import. Review the generated `pack.yaml` and
`rules.yaml` before replaying an agent. See `packs/` for examples.

### Tickets

```bash
replaybook tickets import --mapping examples/payroll/csv/mapping.yaml --out data/payroll
replaybook postgres import --mapping warehouse.yaml --out data/warehouse
replaybook jira-cloud export --project OPS --out data/jira_cloud/ops
replaybook servicenow export --table incident --out data/servicenow/incidents
```

Every importer writes a dataset card and applies source-specific redaction and history
rules. See [Ticket sources](docs/ticket-sources.md).

### Existing ticket search

If your team already runs a read-only ticket-search or RAG service, import it as a
knowledge pack and evaluate its retrieval before giving it to an agent:

```bash
replaybook knowledge import --name ticket_rag \
  --url "${TICKET_RAG_URL}" \
  --out packs
replaybook knowledge check ticket_rag --query "certificate expired on the load balancer"
replaybook knowledge eval --data data/warehouse \
  --source builtin --source ticket_rag --out retrieval
```

Knowledge packs apply the same time-travel guard as built-in search. Invalid pack
configuration exits with code 2. Evaluation prints per-source completion and error counts
to stderr while preserving selected-ticket order in saved results. See
[Knowledge sources](docs/knowledge-sources.md).

## Test your agent

| Agent type | Use case |
|---|---|
| `scripted` | Deterministic offline reference agent and harness tests |
| `skill` | Built-in LiteLLM agent following a pack's `SKILL.md` |
| `external` | Your process receives the ticket and mock MCP configuration |
| launcher | Claude Code, Codex CLI, Gemini CLI, OpenCode, or your own YAML launcher |

```bash
replaybook replay --testset testsets/kafka_topic.json --out runs/external \
  --agent external --command "python examples/external_agent/agent.py"

replaybook launchers
replaybook launchers show claude-code --testset testsets/kafka_topic.json
replaybook replay --testset testsets/kafka_topic.json --out runs/claude \
  --agent claude-code
```

Launcher YAML files live in `launchers/` and keep vendor-specific startup details outside
the core package.

Model calls are optional. For the built-in skill agent:

```bash
uv sync --extra models
replaybook replay --testset testsets/kafka_topic.json --out runs/model --agent skill
```

With no model configuration, Replaybook remains offline.

## Shadow mode and live evidence

Shadow mode proposes actions for open tickets against mocks. It does not execute them.
After engineers resolve those tickets normally, score the proposals:

```bash
replaybook shadow record --type kafka_topic --data data/live --out shadow/kafka \
  --agent external --command "python my_agent.py"
replaybook shadow score --data data/live --out shadow/kafka
replaybook shadow report shadow/kafka
```

Replaybook can render an approval request, but only a person can approve it:

```bash
replaybook approval-request --skill kafka_topic --ticket OPS-4211 \
  --plan plan.json --out approvals/OPS-4211
```

Your runtime executes approved writes, runs each write's read-only check, and reports the
result:

```bash
replaybook live record --run run.json
replaybook live report
replaybook live check
```

Human-authority commands are intentionally not exposed as MCP tools:
`approve`, `accept`, `labels check`, and `live record`.

See [Going live](docs/go-live.md) and [verified writes](docs/verified-writes.md).

## Use Replaybook from an assistant

Start Replaybook as an MCP server:

```bash
replaybook mcp
```

```json
{
  "mcpServers": {
    "replaybook": {
      "command": "replaybook",
      "args": ["mcp"],
      "cwd": "/path/to/replaybook"
    }
  }
}
```

The MCP server can generate, replay, report, create evidence, and run shadow/read-only
catalog operations. It cannot sign off or record human approval.

## Security model

- bundled packs are mocks and never execute writes;
- external agents receive only the case ticket and mock endpoints;
- knowledge search is read-only and guarded against time-travel leakage;
- air-gapped mode blocks outbound connections except explicitly allowed hosts;
- Replaybook sends no telemetry;
- secrets belong in `.env`, which is ignored and excluded from images; and
- evidence contains fingerprints, outcomes, and limitations—not live credentials.

The process-level air gap is a safety belt. Container or host network policy remains the
hard boundary.

## Docker and Podman

```bash
docker build -t replaybook .
docker run --rm --network none -v "$PWD/demo_out:/work/demo_out" replaybook

# Or use a published image.
docker pull blackwattle/replaybook:latest
docker pull blackwattle/replaybook:<short-sha>
```

`podman` uses the same command shape; add `:Z` to bind mounts on SELinux hosts. Base images
are digest-pinned and the image includes a CycloneDX SBOM. Published builds are tagged with
both `latest` and their seven-character commit SHA.

The published image includes the `jira` optional dependency, but not `postgres`. Install
`psycopg[binary]` in your CI environment before using `replaybook postgres import`. Ready-to-
copy GitHub Actions and GitLab CI examples are under `ci/`.

## Documentation

| Topic | Guide |
|---|---|
| Outcomes, scoring, and go-live gates | [Replay outcomes](docs/replay-outcomes.md) |
| Ticket and database adapters | [Ticket sources](docs/ticket-sources.md) |
| Knowledge packs and time-travel guards | [Knowledge sources](docs/knowledge-sources.md) |
| Verification checks for writes | [Verified writes](docs/verified-writes.md) |
| Approval requests and live monitoring | [Going live](docs/go-live.md) |
| CI examples | [ci/README.md](ci/README.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Development

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Tests are offline and use synthetic or documented public fixtures. Tool-specific behavior
belongs in packs; `src/replaybook/` remains vendor-neutral.

## License

Replaybook is licensed under Apache-2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE), and
[TRADEMARK.md](TRADEMARK.md). Contributions require a Developer Certificate of Origin
sign-off; see [CONTRIBUTING.md](CONTRIBUTING.md).
