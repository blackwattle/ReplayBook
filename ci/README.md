# Ready-to-copy CI jobs

These jobs run `replaybook check` against the approved baselines in `assurance/baselines`
and fail the pipeline on a regression or a critical unsafe action (exit code 1) or an
error (exit code 2). Copy the file that matches your CI into your agent's repository and
adjust the agent command. Everything runs offline against the mock servers; no real
infrastructure is touched.

- `github-actions-replaybook-check.yml`: a GitHub Actions workflow
- `gitlab-ci-replaybook-check.yml`: a GitLab CI job to `include`
