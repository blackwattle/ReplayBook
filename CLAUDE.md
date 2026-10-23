# Replaybook

Every tool pack in this repo is a local mock and nothing touches real infrastructure.
Reads answer from a small per-case world state; writes are recorded, never executed. The
Kafka, OpenStack, Artifactory and platform packs are examples for the demo. The opt-in
platform-observer and trustgraph-librarian packs under the assurance example are synthetic
fixtures. None is an integration.

## What this is
Replaybook replays an ops agent against a team's past tickets inside a mock
environment, compares the agent's writes with what engineers actually did, groups the
failures by cause and writes an evidence pack that a change board can sign off.

**Bring your own tools and ticket system.** A team points `replaybook tools import` at
its own MCP servers and gets a pack: a recording mock of each tool plus scoring rules in
YAML to edit. It points an adapter at its own ticket history (CSV or JSONL export,
Postgres table, Jira, ServiceNow) and gets canonical tickets. The core knows nothing
about any particular tool; everything tool-specific lives in `packs/<name>/`.

**Shadow mode.** Before an agent is trusted, `replaybook shadow` runs it on open tickets
against the mock environment and keeps the actions it proposed. Nothing is executed. When
engineers resolve a ticket, the proposal is scored against what they did, and the
evidence pack reports the shadow results in their own section.

## Working rules
- Python 3.12 managed by uv. Run things with `uv run ...` so the pinned lock is used.
- `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .` must pass
  before every commit.
- Everything must work in PowerShell on Windows and in Linux CI. No bash-only
  scripts, no Makefiles.
- No network in unit tests: fake LLM, fake embeddings, scripted agent, fixture rows and
  fake fetchers for the database and HTTP adapters.
- Secrets only in `.env` (gitignored). Never in code, logs, evidence packs or commits.
- Develop and test with synthetic data and a throwaway Postgres, never real company data.
- Ground truth for synthetic data (`ground_truth.jsonl`) is read only by
  `replaybook.eval.ground_truth`. A test fails if anything else imports it.
- The core (`src/replaybook/`) never imports or names a specific tool or vendor. A test
  fails if it does. Tool-specific code goes in a pack.
- Boring, readable code. No abstractions for sources that do not exist yet.
- Licence: Apache-2.0 (`LICENSE`, `NOTICE`; `TRADEMARK.md` for the name, `CONTRIBUTING.md`
  for the DCO). No GPL or AGPL dependency in the default install.
- Anything that names an agent harness (launchers) lives outside `src/`, like packs.
- A knowledge pack is the only live source: a team's own past-ticket search server, read-only
  tools only, every result through the time-travel guard. The guard fails closed (no id or
  no date, no result), and its tests use the fake server in `examples/ticket_rag/`.

## Layout
- `packs/<name>/`            one pack per tool family: `pack.yaml` manifest (request types, keywords, identifying tools, skill), `rules.yaml` scoring rules, `server.py` mock tools, `world.py` world slice, `safety.py` argument-based unsafe checks, `synth.py` synthetic request types, `agent.py` scripted procedures and extraction rules, `skills/`
- `launchers/<name>.yaml`    one launcher per agent harness (command template, how the MCP config and the prompt are passed, built-in tools switched off, timeout, limitations); shipped: claude-code, codex, gemini-cli, opencode
- `src/replaybook/packs.py`  pack discovery and loading (REPLAYBOOK_PACKS, then `./packs`, then the repo's own)
- `src/replaybook/workflow.py` the workflow commands as plain functions returning dicts (generate, build_testset, replay, report, groups, check, evidence, shadow_*), shared by the CLI and the MCP server
- `src/replaybook/mcp_server.py` `replaybook mcp`: the workflow as MCP tools over stdio (no accept/approve: sign-off stays with a person)
- `src/replaybook/agents/launchers.py` launcher schema, discovery (REPLAYBOOK_LAUNCHERS, then `./launchers`, then the repo's own), config rendering (json/toml) and the agent that runs a harness; `replaybook launchers` lists, `launchers show` dry-runs
- `src/replaybook/tools_import.py` `replaybook tools import`: an MCP server's tool list -> pack with a recording mock, a rules.yaml skeleton and a skill skeleton whose placeholder checks fail validation
- `src/replaybook/skills.py`, `checks.py` skill front matter and version; the read-only check every write declares (`verify:`), its validation, rendering and the comparison a live runtime uses
- `src/replaybook/approval.py` `replaybook approval-request`: what an approver sees before a live change (skill version, replay result, live verification rate, each write with its check, the evidence pack); blocked without a replay of the current version
- `src/replaybook/schema/`   canonical Ticket (with effort_source), TestCase and result models (pydantic v2)
- `src/replaybook/synth/`    seeded synthetic ticket generator with hidden ground truth; request types come from the packs
- `src/replaybook/adapters/` ticket sources: synthetic, tabular (csv/jsonl/json with a mapping), postgres, public_jira, jira_cloud, servicenow; `effort.py` applies the effort source; `jira_issue.py` is the shared Jira conversion
- `src/replaybook/mock_env/guarded.py`, `remote.py` knowledge packs: a team's own read-only past-ticket search server (`knowledge:` in pack.yaml, tools.json) served behind the time-travel guard; `remote.py` is the one connection to it
- `src/replaybook/knowledge_pack.py` `replaybook knowledge import` (a knowledge pack skeleton from a server's tool list) and `knowledge check` (validate, probe through the guard)
- `src/replaybook/eval/retrieval.py` `replaybook knowledge eval`: scores past-ticket search (builtin, a knowledge pack, or an agent following a research skill with `--agent`) against the earlier tickets each ticket links to or mentions, at its creation time
- `src/replaybook/mock_env/` ticket tools, knowledge tools behind the time-travel guard, pack server hosting, generic recording mock, world state, call recorder, tool fingerprints; `serve.py` serves the mocks over stdio or streamable HTTP (one combined server, `mock-server --http`), `http_tools.py` is the function-calling door (`tools schema`, `tool-endpoint`)
- `src/replaybook/agents/external.py` also writes each case's mock-server settings file (`--env-file`): harnesses start servers with their own minimal environment
- `src/replaybook/agents/`   agents under test: scripted (with flaws, procedures from packs), LiteLLM skill agent, external subprocess adapter (transports stdio, http, tools); `parse.py` generic text helpers
- `src/replaybook/llm/`      LiteLLM wrapper with spend cap, disk cache and offline fallback
- `src/replaybook/eval/`     test-set builder, replay runner, scoring (the outcome classes), failure groups, accepted differences, baselines, sync, shadow, evidence pack; labels.py labels each case act or escalate and records the spot-check, golive.py is the go-live gate, live.py keeps the live runs a team's runtime reports and each skill's rolling rates; extract.py derives unverified expectations from real ticket text
- `src/replaybook/discover.py` discovery on-ramp: request types ranked by engineer-hours, effort sources counted
- `src/replaybook/netguard.py` air-gapped mode: refuses every connection except allowlisted hosts
- `src/replaybook/sbom.py`   CycloneDX SBOM from uv.lock (needs the `sbom` dependency group)
- `examples/payroll/`        a fake payroll MCP server, the overlay a person adds after the import, canonical tickets and their CSV form: the conformance test for a pack the core has never seen
- `examples/external_agent/` a tiny external agent run through the subprocess adapter
- `examples/ticket_rag/`     a fake past-ticket search server that leaks like a real one, the knowledge pack that guards it (with a research skill), and a model-free research agent
- `examples/platform_agents_assurance/` deterministic recording-mock cases, shadow fixture and evidence workflow for platform-observer and trustgraph-librarian; no human approvals
- `docs/ticket-sources.md`   column mappings for the csv/jsonl and postgres adapters (including links), ServiceNow notes, the as-of rule for RAG tables
- `docs/knowledge-sources.md` knowledge packs: the guard rules, the pack.yaml reference, import/check/eval, the worked example
- `docs/public-jira-findings.md` what broke on real tickets (Apache INFRA) and what changed because of it
- `docs/replay-outcomes.md`  the outcomes, the escalation labelling rule, the spot-check and the go-live gate
- `docs/verified-writes.md`  declaring and validating checks, the live contract for a runtime
- `docs/go-live.md`         live-run records, rolling rates, the degraded flag, hours saved, approval requests
- `Dockerfile`, `compose.yaml` self-hosted image: non-root, air-gapped by default, packs at /app/packs, SBOM at /app/sbom.cdx.json
- `tests/`                   pytest, offline only

## Commands
- `uv run replaybook demo` runs the whole story offline and writes `demo_out/`.
- `uv run replaybook packs` lists the loaded packs; `--tools` lists every mock tool.
- `uv run replaybook mcp` serves the workflow as MCP tools over stdio for an assistant.
- `uv run replaybook mock-server --http --port 0` serves every mock tool on one MCP server over streamable HTTP; `tool-endpoint` is the function-calling door; `tools schema` prints the tool list as JSON Schema.
- `uv run replaybook knowledge import|check|eval` makes a knowledge pack from a past-ticket search server, checks it through the guard and scores its search against history.
- `uv run replaybook launchers` lists the launchers; `launchers show <name>` prints the command, config and prompt a launcher would use.
- `uv run replaybook skills validate` checks every skill's verification checks; `labels sample`/`labels check` handle the escalation-label spot-check; `approval-request`, `live record`, `live report` and `live check` cover go-live.
- `uv run replaybook --help` lists every command.
- `docker compose up --build` runs the demo in the image with no network at all.
