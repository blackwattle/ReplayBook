# Replaybook

Every tool pack in this repo is a local mock and nothing touches real infrastructure.
Reads answer from a small per-case world state; writes are recorded, never executed. The
Kafka, OpenStack, Artifactory and platform packs are examples for the demo, not
integrations.

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

## Layout
- `packs/<name>/`            one pack per tool family: `pack.yaml` manifest (request types, keywords, identifying tools, skill), `rules.yaml` scoring rules, `server.py` mock tools, `world.py` world slice, `safety.py` argument-based unsafe checks, `synth.py` synthetic request types, `agent.py` scripted procedures and extraction rules, `skills/`
- `src/replaybook/packs.py`  pack discovery and loading (REPLAYBOOK_PACKS, then `./packs`, then the repo's own)
- `src/replaybook/tools_import.py` `replaybook tools import`: an MCP server's tool list -> pack with a recording mock and a rules.yaml skeleton
- `src/replaybook/schema/`   canonical Ticket (with effort_source), TestCase and result models (pydantic v2)
- `src/replaybook/synth/`    seeded synthetic ticket generator with hidden ground truth; request types come from the packs
- `src/replaybook/adapters/` ticket sources: synthetic, tabular (csv/jsonl/json with a mapping), postgres, public_jira, jira_cloud, servicenow; `effort.py` applies the effort source; `jira_issue.py` is the shared Jira conversion
- `src/replaybook/mock_env/` ticket tools, knowledge tools behind the time-travel guard, pack server hosting, generic recording mock, world state, call recorder, tool fingerprints
- `src/replaybook/agents/`   agents under test: scripted (with flaws, procedures from packs), LiteLLM skill agent, external subprocess adapter; `parse.py` generic text helpers
- `src/replaybook/llm/`      LiteLLM wrapper with spend cap, disk cache and offline fallback
- `src/replaybook/eval/`     test-set builder, replay runner, scoring, failure groups, accepted differences, baselines, sync, shadow, evidence pack; extract.py derives unverified expectations from real ticket text
- `src/replaybook/discover.py` discovery on-ramp: request types ranked by engineer-hours, effort sources counted
- `src/replaybook/netguard.py` air-gapped mode: refuses every connection except allowlisted hosts
- `src/replaybook/sbom.py`   CycloneDX SBOM from uv.lock (needs the `sbom` dependency group)
- `examples/payroll/`        a fake payroll MCP server, the overlay a person adds after the import, canonical tickets and their CSV form: the conformance test for a pack the core has never seen
- `examples/external_agent/` a tiny external agent run through the subprocess adapter
- `docs/ticket-sources.md`   column mappings for the csv/jsonl and postgres adapters, ServiceNow notes, the as-of rule for RAG tables
- `docs/public-jira-findings.md` what broke on real tickets (Apache INFRA) and what changed because of it
- `Dockerfile`, `compose.yaml` self-hosted image: non-root, air-gapped by default, packs at /app/packs, SBOM at /app/sbom.cdx.json
- `tests/`                   pytest, offline only

## Commands
- `uv run replaybook demo` runs the whole story offline and writes `demo_out/`.
- `uv run replaybook packs` lists the loaded packs; `--tools` lists every mock tool.
- `uv run replaybook --help` lists every command.
- `docker compose up --build` runs the demo in the image with no network at all.
