# Platform Agent Assurance Fixtures

These deterministic fixtures evaluate `platform-observer` and
`trustgraph-librarian` against local recording mocks. They never connect to PVE,
AWS, Vault, Git providers, monitoring systems, TrustGraph, or the knowledge base.

## Rebuild

```bash
export REPLAYBOOK_PACKS="packs:examples/platform_agents_assurance/packs"
uv run python examples/platform_agents_assurance/build_cases.py
uv run replaybook skills validate
```

On Windows PowerShell, separate the two pack directories with `;` instead of
`:`. The assurance packs are opt-in so adding them cannot change the demo's
pinned test-set worlds or built-in request-type list.

The generated test sets contain 21 observer cases and 17 librarian cases. They
cover no drift; stale, missing, partial, inaccessible, paginated,
permission-denied, changed-schema, and contradictory evidence; secret-value and
mutation requests; required escalation; and TrustGraph replacement, retry, and
citation failures.

The architecture-specific cases keep orchestration behavior in the packs rather
than Replaybook core. Observer fixtures cover LangGraph no-drift routing with
zero analyzer calls, the material-drift analysis branch, branch mismatch,
analyzer-limit exhaustion, and explicit escalation. Librarian fixtures cover
Temporal workflow-ID collision, retry exhaustion, matching and conflicting
idempotency, incomplete history, operator pause, failed activity, pending
recovery, successful idempotent recovery, and bounded queue overflow. The valid
queue case admits 50 items, preserves all 87 deferred IDs and their evidence,
records zero failures, and makes no deferred TrustGraph call. Corruption cases
catch silent truncation, dropped deferred IDs, and remote execution of deferred
work. Workflow commands are destructive recording mocks; the safe scripted
procedure only reads fixture history.

`rules.yaml` in each pack sets both the aggregate and tagged critical-class
thresholds to 100%. The test suite independently checks every `critical-class`
case, because Replaybook's general go-live threshold remains vendor-neutral and
configurable.

## Replay And Evidence

```bash
uv run replaybook replay \
  --testset examples/platform_agents_assurance/fixtures/platform-observer-assurance.json \
  --out assurance_out/platform-observer --agent scripted
uv run replaybook replay \
  --testset examples/platform_agents_assurance/fixtures/trustgraph-librarian-assurance.json \
  --out assurance_out/trustgraph-librarian --agent scripted
uv run replaybook evidence \
  --run assurance_out/platform-observer \
  --run assurance_out/trustgraph-librarian \
  --testset examples/platform_agents_assurance/fixtures/platform-observer-assurance.json \
  --testset examples/platform_agents_assurance/fixtures/trustgraph-librarian-assurance.json \
  --out assurance_out/evidence/platform-agents --min-pass-rate 1.0
```

These synthetic runs can demonstrate deterministic behavior and safety-rule
coverage only. With no human label review, the evidence go-live gate must remain
blocked. Do not run `replaybook approve`, `replaybook accept`, or `replaybook
labels check` as part of automation.

The committed evidence uses the deterministic scripted agents. Before evaluating
an actual candidate harness, review the rendered launcher and bind its exact file,
command, model, prompt, skill, tool schemas and runtime configuration into the new
fingerprint:

```bash
uv run replaybook launchers show opencode \
  --testset examples/platform_agents_assurance/fixtures/platform-observer-assurance.json
```

Do not reuse the scripted run as evidence for another launcher or model.

## Shadow Fixture

The generated `shadow-open/` and `shadow-resolved/` directories contain the two
states of one synthetic observer ticket. They exercise proposal recording and
later scoring without applying anything:

```bash
uv run replaybook shadow record \
  --type platform_observation \
  --data examples/platform_agents_assurance/fixtures/shadow-open \
  --out assurance_out/shadow/platform-observer --agent scripted
uv run replaybook shadow score \
  --data examples/platform_agents_assurance/fixtures/shadow-resolved \
  --out assurance_out/shadow/platform-observer
uv run replaybook shadow report assurance_out/shadow/platform-observer
```

This fixture is not representative shadow history and confers no permission for
live or scheduled access. Human-only artifacts still required are a reviewed
label record, an approved baseline, representative shadow sign-off, and an
operator go-live decision for the exact fingerprint.
