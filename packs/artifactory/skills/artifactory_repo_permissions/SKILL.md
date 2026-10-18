---
name: artifactory-repo-permissions
description: Create an Artifactory repository and a permission target for a team group.
request_type: artifactory_repo
tools: artifactory_get_repo, artifactory_create_repo, artifactory_create_permission_target, ticket_ask_requester, ticket_escalate, ticket_comment
verify:
  artifactory_create_repo:
    tool: artifactory_get_repo
    args: {key: "{key}"}
    expect: {key: "{key}", package_type: "{package_type}", rclass: "{rclass}"}
    timeout_s: 30
  artifactory_create_permission_target:
    tool: artifactory_list_permission_targets
    args: {}
    expect:
      permission_targets:
        - {name: "{name}", repositories: "{repositories}", groups: "{groups}"}
    timeout_s: 30
---

# Artifactory repo + permissions

## When this applies
A team asks for a repository to publish or pull artifacts, with a package type and a
group that needs access.

## Information you need
- repository key (ends in `-local`, like `lending-ledger-local`)
- package type: maven, npm, docker, pypi, generic or helm
- group (like `lending-developers`)
- access level: read, read-write or deploy

If the package type or the access level is missing, ask the requester with
`ticket_ask_requester` and stop. Do not assume generic or read.

## When to hand back
Call `ticket_escalate` with a one-line reason and stop, without changing anything, when
the ticket needs a decision a person should make:
- it says the change still needs an approval or sign-off;
- it says it may clash with another team's work or needs a discussion first;
- it asks for more than a new repository and its permission target, such as changing an existing permission target.

Asking the requester is for missing details; handing back is for decisions.

## Procedure
1. `artifactory_get_repo` to check whether the key is taken.
2. If not, `artifactory_create_repo` with `rclass: local` and the package type.
3. `artifactory_create_permission_target` named `<key>-<access>` for the repository,
   granting the group: read = read; read-write = read, write, annotate; deploy = read,
   write, annotate, delete.
4. `ticket_comment` with a one-line summary.

## Never
- Never call `artifactory_delete_repo`.
- Never grant `manage`.
