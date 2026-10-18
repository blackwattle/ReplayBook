# Contributing

Thanks for looking. Replaybook is small and deliberately boring, and contributions that
keep it that way are welcome.

## Licence and sign-off

Replaybook is licensed under Apache-2.0 (see `LICENSE` and `NOTICE`). By contributing you
agree that your contribution is licensed the same way.

Contributions use the Developer Certificate of Origin (https://developercertificate.org).
Add a sign-off line to every commit, which `git commit -s` does for you:

    Signed-off-by: Your Name <you@example.com>

The sign-off says that you wrote the change or otherwise have the right to submit it
under the project's licence. There is no separate contributor agreement.

## How a change reaches main

Fork the repository, or push a branch if you are a collaborator, and open a pull request
against `main`. Nobody pushes to `main` directly apart from the maintainer's sync commits
below, and nobody can force-push to it or delete it. A pull request can only be merged
once:

- CI passes: `test (ubuntu-latest)`, `test (windows-latest)` and `image`, and `sign-off`,
  which fails any commit without a `Signed-off-by` line;
- the maintainer, who owns every path (`.github/CODEOWNERS`), has approved it after its
  last push, and every review thread is resolved.

The rulesets that enforce this are in `.github/rulesets/`. Workflows on a pull request
from a fork wait for the maintainer's approval before they run.

In practice an accepted pull request is not merged here. `main` reaches this repository in
the maintainer's sync commits, so the change is applied with your authorship and sign-off,
arrives in the next sync commit, and the pull request is closed with a link to it.

## Before you open a pull request

Run the three checks the project runs before every commit:

    uv run pytest
    uv run ruff check .
    uv run ruff format --check .

Then check the rules in `CLAUDE.md`, in particular:

- No network in unit tests. Use the fake model, fake embeddings, the scripted agent,
  fixture rows and fake fetchers.
- No real data anywhere. Fixtures and examples are synthetic or from documented public
  API responses.
- Secrets only in `.env`, which is gitignored. Never in code, tests, logs or evidence
  packs.
- The core under `src/replaybook/` never imports or names a specific tool, vendor or
  agent harness. Tool-specific code goes in a pack under `packs/<name>/`; a test fails
  otherwise.
- Boring, readable code. No abstractions for sources that do not exist yet.

## What a good change looks like

One change per pull request, with a test that fails without it, and a line in the
README if the behaviour a user sees changes. If you made a judgement call, say so in the
description.

## Adding a pack

A pack is the right home for anything about one tool family: the mock server, its world
slice, scoring rules, request types, skills and scripted procedures. `replaybook tools
import` generates the skeleton from an MCP server; `examples/payroll/` is a complete
worked example that the core has never seen.
