# Ready-to-copy CI jobs

These jobs run `replaybook check` against the approved baselines in `assurance/baselines`
and fail the pipeline on a regression or a critical unsafe action (exit code 1) or an
error (exit code 2). Copy the file that matches your CI into your agent's repository and
adjust the agent command. Everything runs offline against the mock servers; no real
infrastructure is touched.

- `github-actions-replaybook-check.yml`: a GitHub Actions workflow
- `gitlab-ci-replaybook-check.yml`: a GitLab CI job to `include`

Two optional steps, both offline:

- `replaybook check ... --go-live` also fails the job (exit code 1) when the go-live gate
  fails: a pass rate below `REPLAYBOOK_MIN_PASS_RATE` (0.9), an unsafe outcome, an agent
  error, escalation labels nobody spot-checked, or a run of an older skill version
  (`docs/replay-outcomes.md`). Without the flag the exit codes are as above.
- `replaybook live check --live-dir live` on a schedule exits 1 when a live skill is
  degraded (approval or verification rate below its threshold, `docs/go-live.md`), so the
  scheduler's own alerting tells the team; Replaybook itself sends nothing.
