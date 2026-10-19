# Real-data check: Apache INFRA from the Public Jira Dataset

What happened when the pipeline built for synthetic history was pointed at real tickets:
the Apache Software Foundation's infrastructure queue (project INFRA) from the Public Jira
Dataset (Montgomery, Lüders, Maalej, MSR 2022; Zenodo 10.5281/zenodo.5882881, CC BY 4.0).
Run on 2026-10-18 on the dev VM, offline and air-gapped, with the deterministic scripted
reference agent. Nothing here is evidence about any agent; it is evidence about what the
harness can and cannot do with real history.

## What was run

```
replaybook public-jira export --input mongodump-JiraReposAnon.archive --collection Apache --project INFRA --out infra
replaybook discover --data infra --out discovery --top 5
replaybook build-testset --type <type> --data infra --n 40 --extract rules      # seven types
replaybook replay --testset testsets/<type>.json --out runs/<type> --agent scripted   # dns_change, cert_renewal, group_access
replaybook groups runs/<type>
replaybook evidence --run ... --out evidence
```

The 5.8 GB dump was downloaded once (Zenodo throttles each connection to about 180 KB/s;
24 parallel range requests brought it down in 30 minutes) and read straight from the
mongodump archive. No MongoDB was needed: the archive reader streams the file once, skips
the fifteen other Jiras without decoding them and checks each issue key on the raw bytes.
Apache INFRA came out in 2 minutes 40 seconds.

## The export

| Measure | Value |
|---|---|
| Tickets | 15,777 |
| Date range | 2003-09-04 to 2022-01-05 |
| Comments | 66,511 |
| Status changes (from the changelog) | 30,710 |
| Tickets that waited on the requester at some point | 6,587 (42%) |
| Tickets with time logged | 201 (1%) |
| Resolutions | Done 12,665; Won't Fix 856; Information Provided 480; Not A Problem 402; Invalid 343; Duplicate 260; twelve more |

Two things in the data were only found by running it, not by reading about it: the
dataset flattens comments to `fields.comments` (Jira itself nests them under
`fields.comment.comments`), and it wraps every anonymised user field in a placeholder
such as `<<|author_key|<uuid>|>>`. Both are handled now; the first export showed zero
comments.

## Discovery

| Rank | Request type | Tickets | Hours (estimated) | Skill |
|---|---|---|---|---|
| 1 | CI variable or runner fix | 757 | 1,023 | no |
| 2 | VM provisioning | 430 | 887 | no |
| 3 | Artifactory repo + permissions | 549 | 815 | yes |
| 4 | DNS change | 226 | 317 | no |
| 5 | TLS certificate renewal | 128 | 188 | no |
| 6 | Kafka topic + ACLs | 84 | 108 | yes |
| 7 | Group access request | 79 | 93 | no |

2,375 of 15,777 tickets (15%) matched one of the ten synthetic request types; 85% did
not. That is the honest shape of a real queue: INFRA is about git and GitHub
repositories, mailing lists, Jenkins, websites, LDAP and accounts, and the ten types were
designed for a bank's platform team. Only 1% of tickets logged time, so 99% of the hours
are estimates from engineer activity, and the report says so.

**What broke:** the first discovery run put "Artifactory repo + permissions" first with
1,630 tickets. They were requests for git, GitHub and Subversion repositories; the
keyword classifier counted "repo" and "repository" for Artifactory. Fixed by giving the
type exclusion words (git, github, gitbox, gitlab, svn, subversion) that apply unless
"artifactory" itself is mentioned. The count fell to 549, which still includes some
false positives (any "repository" ticket without a VCS word).

## Test sets with unverified expectations

Real tickets have no audit log, so `--extract rules` derives expected actions from the
text and labels them unverified. Forty newest eligible tickets per type:

| Type | Cases | Ask cases (history) | Action cases (rules, unverified) | Excluded: no extractable expectation | Excluded: other resolutions and duplicates |
|---|---|---|---|---|---|
| dns_change | 40 | 14 | 26 | 46 | 57 |
| cert_renewal | 40 | 12 | 28 | 8 | 34 |
| group_access | 16 | 13 | 3 | 50 | 13 |
| ci_fix | 40 | 36 | 4 | 466 | 113 |
| artifactory_repo | 40 | 40 | 0 | 328 | 116 |
| kafka_topic | 15 | 15 | 0 | 50 | 20 |
| openstack_quota | 3 | 3 | 0 | 16 | 8 |

**What broke, and what was changed:**

- **A waiting status is not an ask.** 42% of INFRA tickets went to "Waiting for user" at
  some point, often after the work ("please verify", "can this be closed?"). The first
  builder treated every such wait as "engineers asked before acting", which made 26 of 40
  DNS cases and 28 of 40 group cases ask cases, and the agent "guessed" whenever it did
  the work. The rule now requires, for tickets without an audit log, an engineer comment
  containing a question by the time the wait began. Ask cases fell to 14 and 12 for DNS
  and certificates.
- **Empty worlds make agents ask for the wrong reason.** Real tickets carry no inventory
  snapshot, so the mock had no certificate to renew and no zone to write to; the agent's
  sanity reads failed and it asked, which passed every ask case for free and failed every
  action case. The builder now seeds the world with the things the text names (the
  certificate, the zone, the group), for ask cases too. A seeded world is recorded on the
  case for review.
- **Rule extraction is noisy.** It reads the first host name as the record, the second
  as the target, and "add X to Y" as user and group. On real text it produced
  `value: env` for "Set up DNS for SkyWalking demo env" and `user: granted` for "Add me
  to moderation for announce@". It also turned "please remove obsolete DNS entries" into
  an upsert, because the rules know no delete. These are exactly why extracted
  expectations stay unverified and why the evidence pack counts them.
- **The classifier mistakes bug reports for requests.** "Windows: powershell can't fetch
  SSL" and "Cert error connecting to ai.google" became certificate-renewal cases because
  they mention certificates. A model-based typer, or a person confirming the test set,
  is needed before a real pass rate means anything.
- **Most of INFRA's recurring work has no mock behind it.** 466 CI tickets and 328
  repository tickets had nothing the rules could extract because they are about Jenkins
  jobs, GitHub settings and git repositories, which the four mock servers do not model.
  The on-ramp shows what to build mocks for next.

## Replaying the reference agent

The fixed scripted agent, in air-gapped mode, against the seeded worlds:

| Type | Pass | Fail | Pass rate (95% Wilson) | Failure reasons |
|---|---|---|---|---|
| dns_change | 26 | 14 | 65% (50% to 78%) | 11 wrote where history asked, 3 asked where history wrote |
| cert_renewal | 29 | 11 | 72% (57% to 84%) | 10 wrote where history asked, 1 asked where history wrote |
| group_access | 14 | 2 | 88% (64% to 97%) | 2 asked where history wrote |

Gate: pass (no critical action, no agent error). Every number above is bounded by the
unverified expectations: 26 of the 40 DNS expectations and 28 of the 40 certificate
expectations came from rules, so a "fail" may be the expectation's fault as often as the
agent's. The evidence pack carries that as a limitation, and the same mechanism that
accepts outdated history (`replaybook accept`) is how a reviewer confirms or corrects
each one.

Compared with synthetic history, where the fixed agent matches 90% or more of complete
current tickets for every type it handles, the drop comes from three places in roughly
equal measure: the ask signal (what engineers chose to ask about is a judgement the
status does not capture), the extraction (rules misread real text), and the typing
(bug reports and requests look alike to keywords).

## What a team with its own Jira should expect

- Export works on the first try through `jira-cloud export` or the archive reader; the
  shape of the data is the same. Plan for a status mapping (`STATUS_MAP` in
  `jira_issue.py`) that matches the team's workflow names.
- Use discovery to pick the types, then expect to write mocks for the top ones; a public
  infrastructure queue shared only four of the ten types with a private platform team.
- Confirm expectations before trusting a pass rate. Rules give a starting point and the
  model extractor (`--extract model`) a better one; neither replaces a person for the
  first test set of each type.
- An inventory export (CMDB, DNS zone list, certificate inventory) turns seeded worlds
  into real ones. Without it, agent reads succeed only for what the text names.

## Where the artefacts are

On the dev VM: the archive under `/srv/replaybook-data`, the export, test sets, runs,
discovery report and evidence pack under `/srv/replaybook-out`. None of it is committed:
the dataset is 5.9 GB and CC BY 4.0, and the export is 59 MB of real tickets that belong
in a data directory, not a repository.

## After the pack refactor (spec v3, 2026-10-18)

The findings above stand. What changed underneath them:

- The request types INFRA was typed into now come from the packs (`packs/platform` for
  DNS, certificates, groups and CI; the others from `packs/kafka`, `packs/openstack`,
  `packs/artifactory`), and the classifier words that caused the false positives noted
  above live in each pack's `pack.yaml`. A team can tune or replace them without touching
  the core, and `replaybook tools import` plus a request type in the manifest is how the
  Jenkins, GitHub and mailing-list request types that INFRA actually has most of would be
  added.
- The csv/jsonl adapter would have been the right on-ramp for a one-off dataset like this
  one; the mongodump reader remains because the Public Jira Dataset only comes that way.
- Effort in the discovery ranking is labelled by source. INFRA logs time on a minority of
  tickets, so most of its hours are estimates, and the report now says so per ticket
  instead of one aggregate share.
- An agent replayed against INFRA can now search earlier INFRA tickets through the
  knowledge tools, behind the time-travel guard; the "empty world" problem is unchanged
  and still wants a CMDB export.

## The escalation labels on real tickets (milestone 30, 2026-10-19)

Every case is labelled act or escalate by a rule (`docs/replay-outcomes.md`) that was built
on synthetic history, where it agrees with the hidden truth on every case. To see how it
behaves on real comment threads, INFRA was exported again on the dev VM and the same seven
test sets were rebuilt (40 newest eligible tickets per type, `--extract rules`).
`selection.escalation_rule` in each set counts the cases each part of the rule labelled:

| Type | Cases | Ask cases | Escalate | Discussion part | Keyword part | Escalate in the first build |
|---|---|---|---|---|---|---|
| dns_change | 40 | 11 | 11 | 10 | 2 | 23 of 40 |
| cert_renewal | 40 | 11 | 10 | 10 | 1 | 24 of 40 |
| group_access | 16 | 13 | 7 | 7 | 0 | 14 of 16 |
| ci_fix | 40 | 33 | 17 | 16 | 1 | 38 of 40 |
| artifactory_repo | 40 | 40 | 22 | 22 | 2 | 34 of 40 |
| kafka_topic | 14 | 14 | 10 | 10 | 1 | 13 of 15 |
| openstack_quota | 2 | 2 | 2 | 2 | 0 | 3 of 3 |
| **Total** | **192** | **124** | **79 (41%)** | **77** | **7** | **149 of 194 (77%)** |

A case can meet both parts. The other two parts never fired: real history has no recorded
actions, so there is no tool outside the skill to see and no usual pattern to compare with
(`usual_pattern.used` is false for every set). On real tickets only the comments decide.

**What broke, and what was changed:**

- **The reporter counted as an engineer.** The first build labelled 149 of 194 cases
  escalate, 148 of them through the discussion part, and in 132 of those one of the two
  "engineers" was the ticket's own reporter. The Jira adapters learn their engineers across
  the whole project (anyone who was ever an assignee or moved an issue), and most INFRA
  reporters have closed or reopened a ticket somewhere, so on their own request they
  counted as engineers. The documented rule already said the requester never counts; the
  code went by the adapter's role. The same slip made ask cases: 31 of the first build's
  133 rested only on a question the reporter asked on their own ticket ("Should I close
  this issue now?"). The labelling rule and the ask rule now both skip the ticket's
  reporter. That is a change to the core, which names no system, so the adapters stay as
  they were. Synthetic labels and ask cases did not move (synthetic reporters are
  requesters, and synthetic ask cases are read from the audit log). Because fewer tickets
  now count as asks, some sets drew different tickets; the ask-case counts in the table at
  the top of this page predate the fix.
- **Two engineers is too low for a busy public queue.** In a sample of 12 discussion
  cases (three each of DNS, certificates, repositories and groups), three were clear
  judgement calls: a podling name that needed a brand approval, an engineer asking
  colleagues "any ideas here?", a migration "evaluated on a case by case basis". Three
  were arguable. Six were routine threads: a project member chiming in, a colleague
  finishing the work, an automated "Pending Closed" reminder posted from an account the
  export takes for a person. On INFRA a second commenter is normal, not a sign of
  judgement. Three engineers would have labelled 27 of the 192 cases (14%), four 11, and
  three is closer to what the sample suggests. A team with threads like these sets
  `escalation: {discussion_engineers: 3}` for its request types in `pack.yaml`. The default
  stays at two, which matches the synthetic history; only a team's own queue can say what
  fits, and the spot-check is where that shows.
- **The keywords mostly fit.** Seven cases: "approved" three times, "approval",
  "approve", "escalation" and "sign-off" once each. Six show an approval chain or a policy
  question: "Does this need board escalation?", "we'd need sign-off by [~gstein] to create
  role accounts", "can you link an approval thread from a geode list?", "This doesn't seem
  to be an approved release process". In the seventh, "it got approved" was a vendor's
  routine approval of renewed signing credentials. Before the ask fix the sets also held
  "Approved, please test", where approving a GitHub Action was the answer to the request,
  so the list misfires where approving is the work itself.
- **The spot-check limit would have caught the first build.** These labels stay unverified
  until a person checks a sample. On the first build a reviewer would have changed far more
  than one label in ten, so under the limit added in this milestone the check would not
  have counted until the rule was fixed or tuned, and the gate line would have named the
  discussion part as the cause.

The export, the three generations of test sets (`testsets-reporter-counted/`,
`testsets-labels-fixed/` and `testsets/`) and the per-case measurements (`labels*.json`)
are under `/srv/replaybook-out/m30` on the dev VM.
