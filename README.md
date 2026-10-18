# Replaybook

Replaybook tests ops agents before they are trusted with production. It replays an agent
against a team's own past tickets in a mock environment, compares what the agent did with
what engineers actually did, groups the failures by cause, and produces an evidence pack a
change board or risk team can sign off. When the model, skill, tools or agent change, it
re-runs and shows only what changed.

It is for platform and risk teams in regulated organisations who want agents doing ops
work but must prove it is safe first. Everything runs on your own machines and nothing
leaves your network.

**Every tool pack in this repo is a local mock and nothing touches real infrastructure.**
Reads answer from a small per-case world state; writes are recorded, never executed.

## Bring your own tools and ticket system

Replaybook does not care which tools your agent uses or where your tickets live.

- **Tools.** Point `replaybook tools import` at any MCP server (or a saved JSON of its tool
  list) and you get a *pack*: a recording mock of every tool, with reads answered from a
  small per-case world state and writes recorded instead of executed, plus a scoring-rules
  file in YAML that says which arguments matter and which tools are dangerous. Edit the
  rules, drop the pack into `packs/`, and the agent can be tested against it. The Kafka,
  OpenStack, Artifactory and platform packs in this repo are only examples for the demo;
  the core knows nothing about any particular tool.
- **Tickets.** An adapter turns your ticket history into Replaybook's canonical ticket: a
  CSV or JSONL export with a column mapping you write once (`docs/ticket-sources.md`), a
  Postgres table your RAG or warehouse already has, Jira (the public dataset or Jira Cloud),
  or ServiceNow. Each adapter says where effort comes from (logged time, time in working
  statuses, or created-to-resolved), so discovery can rank request types by the hours they
  really cost you.

## Shadow mode

Before anyone trusts the agent with a live queue, `replaybook shadow` runs it on *open*
tickets against the mock environment and keeps the actions it proposed. Nothing is
executed. When your engineers resolve a ticket the normal way, Replaybook scores the
agent's proposal against what they did. The evidence pack gets a shadow section: how many
proposals were made, how many could be scored, the pass rate, and the unsafe proposals
that were caught before the agent had any access.

```powershell
uv run replaybook shadow record --type kafka_topic --data data/live --out shadow/kafka --agent external --command "python my_agent.py"
# ... days later, after engineers resolved some of those tickets and the dataset was re-exported:
uv run replaybook shadow score --data data/live --out shadow/kafka
uv run replaybook shadow report shadow/kafka
uv run replaybook evidence --run runs/check-kafka --testset testsets/kafka_topic.json --shadow shadow/kafka --out evidence/kafka
```

Proposals are stored per ticket and per agent fingerprint, so re-recording with the same
agent adds nothing and a changed agent sits next to the old one. Tickets that end as
duplicates or "won't do" are unscorable; tickets still open are pending.

## The ten-minute demo

```powershell
uv sync
uv run replaybook demo --out demo_out
```

It runs offline (no API key, no Docker) in well under a minute and prints the story as it
goes:

1. **Generates about 3,000 synthetic tickets** over five years with hidden ground truth:
   ten recurring request types (Kafka topics, quota increases, Artifactory repos, DNS,
   certificates, access, CI fixes and more), a quarter one-off noise, typos, missing
   information, waiting-on-requester periods, reopens, duplicates, resolutions that just
   say "done", linked code changes, two procedure changes partway through the timeline
   and a few human mistakes that were later corrected.
2. **Builds test sets for three request types** from the newest tickets of each. A ticket
   where engineers had to ask the requester for details becomes a case where the agent is
   expected to ask too.
3. **Replays a reference agent seeded with three flaws** and catches all of them: it
   guesses when information is missing, it follows the superseded ZooKeeper and
   per-service quota procedures, and it deletes and recreates a Kafka topic that already
   existed. That last one is a critical unsafe action and fails the gate on its own.
4. **Groups the failures by cause**, in plain English, with two side-by-side examples
   each (what engineers did against what the agent did).
5. **Accepts the cases where history itself is outdated**, with a reason, an approver and
   a 90-day expiry. Tickets that predate a procedure change are marked as outdated
   history; the same mechanism handles historical mistakes.
6. **Approves the first run as the baseline, fixes the flaws and re-runs.** Only changed
   outcomes are shown: improvements, no regressions, exit code 0.
7. **Writes the evidence pack**: one self-contained HTML file plus JSON and a manifest of
   SHA-256 hashes for every input and output. Open `demo_out/evidence/evidence.html`.

Quality bars checked against the hidden ground truth on every demo run: zero false passes
on seeded wrong or unsafe actions, failure groups that match the seeded flaw types with
purity of 0.9 or better, and identical results for identical inputs and seed.

## Using it with your own agent

Three kinds of agent can be put under test:

- **Scripted reference agent** with switchable flaws (`guess_missing_info`,
  `superseded_procedure`, `unsafe_write`). Deterministic and offline; it is what the demo
  uses and what proves the harness works.
- **Built-in LiteLLM agent** that follows a `SKILL.md`. Each pack ships sample skills
  (`packs/<name>/skills/`): Kafka topics and ACLs, Artifactory repos and permissions,
  OpenStack quota increases. Needs a model: put a key in `.env` or run Ollama locally and
  install the extra with `uv sync --extra models`.
- **Any external agent** run as a subprocess against the mock MCP servers. Replaybook
  sets `REPLAYBOOK_MCP_CONFIG` (a standard `mcpServers` JSON) and `REPLAYBOOK_TICKET`, and
  collects the writes the servers recorded. `examples/external_agent/agent.py` is a tiny
  working example.

```powershell
uv run replaybook generate --out data/generated --seed 42 --n 3000
uv run replaybook build-testset --type kafka_topic --data data/generated --n 40
uv run replaybook replay --testset testsets/kafka_topic.json --out runs/kafka-v1 --agent scripted --flaws guess_missing_info,superseded_procedure,unsafe_write
uv run replaybook replay --testset testsets/kafka_topic.json --out runs/kafka-ext --agent external --command "python examples/external_agent/agent.py"
uv run replaybook report runs/kafka-v1
uv run replaybook groups runs/kafka-v1
```

## Start with discovery

Before writing any skill, find out which request types cost the most engineer time:

```powershell
uv run replaybook discover --data data/generated --out discovery
```

It types every past ticket (recorded actions first, then the text classifier), adds up
engineer-hours per request type (logged time where it exists, otherwise an estimate from
engineer comments, status changes and recorded actions, marked as such) and writes
`discovery.md` and `discovery.json`: the ranking, each type's share of hours, how often it
waited on the requester, whether a reference skill exists, and three recent examples.
Tickets that match no type are summarised by their most common words so you can see what
you are missing. On synthetic data the ranking is checked against the hidden true effort.

## The assurance workflow

```powershell
uv run replaybook accept --suggest --run runs/kafka-v2
uv run replaybook accept --group G1 --run runs/kafka-v2 --reason "History predates the KRaft move" --approver ben --kind outdated_history
uv run replaybook approve --run runs/kafka-v2 --approver ben
uv run replaybook check --testset testsets/kafka_topic.json --out runs/check-kafka   # exit 0 pass, 1 regression or critical, 2 error
uv run replaybook evidence --run runs/check-kafka --testset testsets/kafka_topic.json --out evidence/kafka
```

- **Accepted differences** live in `assurance/accepted.yaml` with reason, approver, approval
  date and expiry. Expired ones come back as failures.
- **Baselines** live in `assurance/baselines/` and carry the run's fingerprint: a hash of
  the model id, skill text, tool schemas, agent config and test set.
- **`check`** re-runs only when the fingerprint changed and prints only the outcomes that
  differ from the baseline. Ready-to-copy CI jobs are in `ci/`.
- **`sync`** keeps a test set growing: `uv run replaybook sync --testset testsets/kafka_topic.json`
  adds the tickets of that request type resolved since the last sync as new cases, built
  the same way, and leaves every existing case untouched so baselines and acceptances
  still apply. The set records each sync (what was added, what was skipped and why), and
  the next `check` lists the new cases as NEW.
- **The evidence pack** covers scope and fingerprints, test-set source and selection, pass
  rate with a Wilson interval per request type, failure groups with examples, accepted
  differences, changes since the baseline, a sign-off block, limitations and the manifest.

## Your own tickets: any CSV, JSONL or JSON export

The universal fallback is a table. Write a column mapping once (`docs/ticket-sources.md`
documents every field, with the payroll example as a worked case) and import:

```powershell
uv run replaybook tickets import --mapping examples/payroll/csv/mapping.yaml --out data/payroll
```

Companion tables carry comments, status history and an audit log of tool calls when the
system has them. The mapping also chooses the **effort source** that discovery ranks by:
`logged` time, `status_time` (time in working statuses) or `created_to_resolved`. The data
card names it, every ticket carries it, and the import warns when there is no status
history (no ask cases, nothing for `status_time` to measure). The Jira exports take the
same `--effort` option.

ServiceNow queues come in through the Table API (`replaybook servicenow export --table
incident --out data/snow`): records, journal comments and work notes, and the state audit
trail, with On Hold / Awaiting Caller mapped to the waiting status. It is tested against
fixtures built from the documented response shapes; a live run needs a developer instance.

Tickets already in a Postgres table (a RAG store or a warehouse) come in the same way with
`replaybook postgres import --mapping warehouse.yaml --out data/warehouse` and
`uv sync --extra postgres`. That adapter requires a status-history table and refuses
without one unless you pass `--allow-missing-history`, because without transitions there
are no ask cases and an agent that never asks would look fine.

## Real tickets: the Public Jira Dataset and Jira Cloud

Synthetic history proves the harness; real history shows what breaks. Two adapters turn
Jira issues into canonical tickets through one shared conversion (`jira_issue.py`):

```powershell
# The Public Jira Dataset (Zenodo 10.5281/zenodo.5882881, CC BY 4.0): restore only the
# collection you need into a local MongoDB with the compose `jira` profile, then export
# one ops-style project. A .bson collection file or a .jsonl of issues works without a database.
docker compose --profile jira up -d mongo
$env:JIRA_DUMP_DIR = "D:\datasets\ThePublicJiraDataset. DataDump"
docker compose --profile jira run --rm jira-restore
uv run replaybook public-jira export --input "mongodb://127.0.0.1:27017/JiraReposAnon?collection=Apache" --project INFRA --out data/public_jira/infra

# Jira Cloud (REST API v2 with the changelog). JIRA_SITE, JIRA_EMAIL and JIRA_API_TOKEN in .env.
uv run replaybook jira-cloud export --project OPS --out data/jira_cloud/ops

# Real tickets have no audit log, so expectations are extracted from the text and stay
# unverified until a person confirms them. The evidence pack says how many are unverified.
uv run replaybook build-testset --type dns_change --data data/public_jira/infra --n 40 --extract rules
```

Both exports redact at ingest (emails, public IP addresses, secret-looking strings), reduce
user objects to account ids before anything is written, map Jira workflows onto the
canonical statuses, and write a `DATASET.md` data card. `docs/public-jira-findings.md`
records what happened when Apache INFRA (15,777 real tickets) went through the whole
pipeline: what the discovery ranking looked like, which assumptions from synthetic history
broke (a waiting status is not an ask, empty worlds, noisy rules, keyword typing) and what
was changed because of it.

## Searching past tickets without cheating: the time-travel guard

An agent that can search old tickets is useful ("how did we do this last time?") and
dangerous during replay: the corpus contains the very ticket being replayed, with its
answer, and everything that happened after it. So Replaybook serves the knowledge tools
itself. `knowledge_search_tickets` and `knowledge_get_ticket` answer from the dataset the
test set came from (or `--knowledge DIR`), with a horizon at the test ticket's creation
time: only tickets created before it, with comments, status changes and recorded actions
cut at that instant, the status as it was then, and never the ticket under test. A test
seeds a leak (a later ticket quoting the answer, the test ticket's own resolution, a
long-running ticket whose answer arrived later) and proves none of it comes back. Point
your own RAG at the same rule: filter on creation time, strictly before the ticket's.

## How scoring works

Recorded writes are compared with the actions engineers took, after per-tool
normalisation from the pack's `rules.yaml` (which arguments matter, case, list order,
equivalent values such as `7d` and `604800000` ms). Outcomes are pass; fail with a reason
code (wrong tool, wrong arguments, missing action, extra write, guessed missing
information, unsafe action); or accepted difference. Tools a pack lists as unsafe (every
destructive tool, plus argument-based checks such as internet-open firewall rules on admin
ports or privileged group membership) are critical and fail the gate whatever the pass
rate.

## Packs: what a tool family looks like to Replaybook

```
packs/kafka/
  pack.yaml      name, description, request types (label, keywords, identifying tools, sample skill)
  rules.yaml     scoring rules: normalisation per tool, unsafe tools, superseded tools
  server.py      the mock MCP server (reads from the world slice, writes recorded)
  world.py       the pack's slice of world state from a ticket's inventory
  safety.py      optional argument-based unsafe checks
  synth.py       optional synthetic request types for the generator
  agent.py       optional scripted procedures, text parsers and extraction rules
  skills/        sample SKILL.md files
```

To make a pack from your own MCP server, let Replaybook list its tools and generate the
recording mock and a scoring skeleton, then edit the two YAML files:

```powershell
uv run replaybook tools import --name payroll --command "python path/to/payroll_server.py" --out packs
uv run replaybook tools import --name payroll --json saved-tools-list.json --out packs   # from a saved tools/list result
```

Listing tools is the only call the import makes. The generated `rules.yaml` compares every
argument and treats every destructive-annotated tool as critical until you say otherwise;
`pack.yaml` needs the request types (label, keywords, identifying tools) filled in.

`examples/payroll/` is the proof that this works for a tool family the core has never
heard of: a fake payroll MCP server, the overlay a person adds after the import (request
type, a scripted procedure, a sample skill) and a dozen tickets. The conformance test
imports it, builds a test set, replays, groups and writes an evidence pack, and checks that
no file under `src/` changed.

`replaybook packs` lists what is loaded and from where (`REPLAYBOOK_PACKS`, then `./packs`,
then the repo's own). The only tools the core defines are `ticket_get`,
`ticket_ask_requester` and `ticket_comment`; a test fails if a vendor name appears under
`src/`.

## Models and offline mode

All model calls go through LiteLLM and are optional. Models come from `.env`
(`REPLAYBOOK_MODEL_FAST`, `REPLAYBOOK_MODEL_STRONG`; see `.env.example`). With no key and
no local Ollama server everything runs offline: template text, rule-based labels and the
scripted agent. A hard spend cap (`REPLAYBOOK_MAX_SPEND_USD`, default 20) switches to
offline mode when reached, and every response is cached on disk by hash.

Any OpenAI-compatible gateway works today (a LiteLLM proxy, OpenRouter, a cloud API
gateway or an internal model endpoint): use the `openai/` prefix with the model name the
gateway knows, put the gateway's key in `OPENAI_API_KEY` and its URL in
`OPENAI_BASE_URL`, and add the host to `REPLAYBOOK_ALLOWED_HOSTS` in air-gapped mode.
Behind a gateway LiteLLM usually cannot price a call, so the gateway's own budget takes
over from the spend cap and the evidence pack's spend figure reads zero for those runs.

## Self-hosted deployment

Replaybook is meant to run inside your network. The image is offline by default, air-gapped
by default, sends no telemetry, runs as a non-root user and ships its own software bill of
materials.

```powershell
docker build -t replaybook .
docker run --rm --network none -v "${PWD}/demo_out:/work/demo_out" replaybook   # the demo
docker run --rm --network none replaybook status                                # what it can reach
docker compose up --build                                                       # the demo through compose
```

A prebuilt image is published to Docker Hub by CI on every green build of `main`:
`docker pull blackwattle/replaybook:latest` (each build is also tagged with its short commit).
Build it yourself, as above, when you want to verify the SBOM against your own checkout.

The image runs as a non-root user (uid 1000). On a Linux host, create the mounted directory
first and make it writable to that user (`mkdir -p demo_out && chmod a+rwx demo_out`, or
`chown 1000 demo_out`); Docker would otherwise create it owned by root and the container could
not write into it. Docker Desktop and rootless podman map the ownership for you.

`podman` works the same way (`podman build`, `podman run`; add `:Z` to the volume on SELinux
hosts, and `podman compose` needs the docker-compose or podman-compose provider). Base images
are pinned by digest in the `Dockerfile`, so a rebuild uses the same bases until you change
them on purpose.

- **Air-gapped mode.** `REPLAYBOOK_AIRGAPPED=true` (the image default) makes the process
  refuse every outbound connection and name lookup except the hosts in
  `REPLAYBOOK_ALLOWED_HOSTS` (for example `ollama:11434`). Refusals are counted, and the
  evidence pack says how many runs were made in air-gapped mode. It is a process-level
  seatbelt; the hard boundary is the container network. The default compose service has
  `network_mode: none`, and the optional `models` profile puts Replaybook and a local Ollama
  on an internal network with nothing else on it. `tests/test_airgap.py` proves the guard on
  the loopback interface.
- **No telemetry.** Replaybook sends nothing anywhere. The only network-capable code is the
  model client, a test fails if any other module imports a network library, and LiteLLM's
  own telemetry is switched off. The image also sets `DO_NOT_TRACK=1` for third-party
  libraries that honour it.
- **SBOM.** `uv run --group sbom replaybook sbom` writes a CycloneDX 1.6 JSON SBOM
  (`sbom/replaybook.cdx.json`) of exactly the pinned dependencies in `uv.lock`, with their
  hashes, reproducibly. The image carries the one for its own build at `/app/sbom.cdx.json`,
  and CI publishes one next to every evidence pack.
- **Secrets** stay in `.env`, which the image never contains: `.dockerignore` keeps
  everything but the source out. Mount your `assurance/` directory and test sets into
  `/work`.

## Development

```powershell
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Python 3.12, uv, typer, pydantic v2, the official MCP Python SDK, plain files (JSONL,
YAML, HTML) for storage. No network in tests. `CLAUDE.md` has the working rules.

## Licence

Apache-2.0; see `LICENSE` and `NOTICE`. The name "Replaybook" is a trademark of the
author and is not licensed; `TRADEMARK.md` says what is fine without asking.
Contributions are accepted under the Developer Certificate of Origin (`CONTRIBUTING.md`).

What the licence does not reach: the packs, rules, skills and recording mocks a team
writes or imports for its own tools are that team's configuration, and the test sets,
evidence packs and synthetic tickets Replaybook produces are that team's documents.
Nothing in Apache-2.0 asks for either to be shared.

Every runtime dependency is MIT, BSD or Apache-2.0 licensed except the Postgres driver
(`psycopg`, LGPL-3.0), which sits behind the optional `postgres` extra and is not in the
Docker image. The SBOM lists all of them.
