---
name: artifactory-repo-permissions
description: Create an Artifactory repository and a permission target for a team group.
request_type: artifactory_repo
tools: artifactory_get_repo, artifactory_create_repo, artifactory_create_permission_target, ticket_ask_requester, ticket_comment
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
