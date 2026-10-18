"""One-off noise tickets: summary, description, labels. No standard procedure applies.

These are the tickets every team has that are not a recurring request: questions,
incidents, reviews, onboarding. They name no tool; the packs' request types do.
"""

from __future__ import annotations

# One-off noise tickets: summary, description, labels. No standard procedure applies.
NOISE_TEMPLATES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "Grafana dashboard for {service} shows no data",
        "Since yesterday the {service} dashboard in Grafana is empty. Prometheus seems fine. "
        "Can someone take a look?",
        ("monitoring",),
    ),
    (
        "Question about log retention policy",
        "How long do we keep application logs in {env}? Audit asked and I could not find it "
        "documented anywhere.",
        ("question",),
    ),
    (
        "Please review MR !{n} for the deploy pipeline",
        "Small change to the deploy stage for {service}. Needs a platform reviewer.",
        ("review",),
    ),
    (
        "Laptop cannot reach the {env} VPN",
        "Since the update this morning my laptop cannot connect to the {env} VPN. Error is "
        "'authentication failed'. Mail me at {email} if you need logs.",
        ("vpn", "endpoint"),
    ),
    (
        "Intermittent 502s from the API gateway",
        "We are seeing intermittent 502s on {service} behind the gateway in {env}. About 1 in "
        "200 requests. Started around 09:40. Source IP seen in logs: {public_ip}.",
        ("incident",),
    ),
    (
        "License renewal for the scanning tool",
        "The vulnerability scanner license expires at the end of the month. Who owns the renewal?",
        ("licensing",),
    ),
    (
        "Onboarding checklist for new starter",
        "New engineer starting Monday in {team}. Please run through the standard onboarding.",
        ("onboarding",),
    ),
    (
        "Disk filling up on the build host",
        "/var on the build host for {team} is at 93%. Probably old docker images.",
        ("incident",),
    ),
    (
        "Can we get a sandbox for {service}?",
        "The team wants a throwaway environment to test an upgrade of {service}. Not sure what "
        "we need yet, happy to discuss.",
        ("question",),
    ),
    (
        "Terraform state lock stuck",
        "The state for {team}/{service} is locked by a cancelled pipeline. Please force-unlock.",
        ("terraform",),
    ),
    (
        "Slow queries on the reporting database",
        "Nightly reports for {service} have been running 3x longer this week. Could be the "
        "index change. Happy to pair.",
        ("database",),
    ),
    (
        "Clarify ownership of the {service} runbook",
        "The runbook page lists a team that no longer exists. Who owns {service} now?",
        ("documentation",),
    ),
    (
        "Request for a Confluence space",
        "The {team} team would like a Confluence space for design docs.",
        ("tooling",),
    ),
    (
        "Alert fatigue: too many pages from {service}",
        "We got 14 pages from {service} last night, all auto-resolved. Thresholds need tuning.",
        ("monitoring",),
    ),
    (
        "Rotate the shared mailbox password",
        "The {team} shared mailbox password is older than policy allows. Please rotate and share "
        "securely. Old one was 'Summer2024!' which should never have been used.",
        ("security",),
    ),
    (
        "Datacentre maintenance window confirmation",
        "Please confirm the maintenance window on the 14th does not affect {service} in {env}.",
        ("question",),
    ),
    (
        "Backup restore test for {service}",
        "Annual DR exercise: restore last night's backup of {service} into {env} and verify.",
        ("dr",),
    ),
    (
        "Unexpected cost increase on object storage",
        "Object storage bill for {team} doubled last month. Need help finding what grew.",
        ("cost",),
    ),
    (
        "SSO login loop on the internal portal",
        "After the IdP change some users in {team} get an SSO redirect loop. Reported by {email}.",
        ("incident", "sso"),
    ),
    (
        "Deprecate the old {service} API",
        "Tracking ticket for turning off v1 of the {service} API after consumers migrate.",
        ("project",),
    ),
)
