---
name: openstack-quota-increase
description: Raise an OpenStack project quota to a requested value.
request_type: openstack_quota
tools: openstack_quota_show, openstack_quota_set, ticket_ask_requester, ticket_escalate, ticket_comment
verify:
  openstack_quota_set:
    tool: openstack_quota_show
    args: {project: "{project}"}
    expect:
      quotas:
        cores: "{cores}"
        ram: "{ram}"
        instances: "{instances}"
        volumes: "{volumes}"
        gigabytes: "{gigabytes}"
    timeout_s: 30
---

# OpenStack quota increase

## When this applies
A team is hitting a quota (cores, ram, instances, volumes or gigabytes) and asks for a
higher limit on a project.

## Information you need
- project (like `risk-tech-test`)
- resource: cores, ram, instances, volumes or gigabytes
- the new value

If the project or the new value is missing, ask the requester with
`ticket_ask_requester` and stop. Do not double the current value or guess.

## When to hand back
Call `ticket_escalate` with a one-line reason and stop, without changing anything, when
the ticket needs a decision a person should make:
- it says the change still needs an approval or sign-off;
- it says it may clash with another team's work or needs a discussion first;
- it asks for a change to a project the requester's team does not own.

Asking the requester is for missing details; handing back is for decisions.

## Procedure
1. `openstack_quota_show` for the project to confirm it exists and see current limits.
2. `openstack_quota_set` with the project and the one resource being changed. This is the
   unified quota tool and handles compute and storage quotas alike.
3. `ticket_comment` with a one-line summary.

## Never
- Never use `openstack_nova_quota_update` or `openstack_cinder_quota_update`; the
  per-service tools were retired on 2026-02-15.
- Never lower a quota in a quota-increase ticket.
