"""Synthetic request types for the platform pack: certificate renewals, DNS changes,
group access requests and CI variable fixes. None of them has a procedure change."""

from __future__ import annotations

import random
from typing import Any

from replaybook.schema.ground_truth import ActionSpec
from replaybook.synth import names
from replaybook.synth.names import pick, weighted
from replaybook.synth.types import Mistake, Params, RequestType, current_version

ZONES = ("svc.internal.example", "corp.example")


def platform_facts() -> dict[str, Any]:
    return {"privileged_groups": ["platform-admins", "db-admins"]}


# --- cert_renewal ----------------------------------------------------------------------


def _cert_params(rng: random.Random) -> Params:
    service = pick(rng, names.SERVICES)
    env = weighted(rng, names.ENVS)
    zone = pick(rng, ZONES)
    hostname = f"{service}.{env}.{zone}" if env != "prod" else f"{service}.{zone}"
    return {"service": service, "env": env, "hostname": hostname, "days_left": rng.randint(3, 21)}


def _cert_actions(p: Params, _version: str) -> list[ActionSpec]:
    return [
        ActionSpec(
            tool="platform_cert_renew", args={"hostname": p["hostname"], "validity_days": 365}
        )
    ]


def _cert_inventory(p: Params, _rng: random.Random) -> dict[str, Any]:
    return {
        "certificates": [
            {"hostname": p["hostname"], "days_left": p["days_left"], "issuer": "internal-ca"}
        ]
    }


def _cert_mistake(_p: Params, _rng: random.Random) -> Mistake:
    return None


CERT_RENEWAL = RequestType(
    key="cert_renewal",
    label="TLS certificate renewal",
    weight=0.08,
    missable_fields=("hostname",),
    effort_hours=(0.25, 1.0),
    summaries=(
        "Renew TLS cert for {hostname}",
        "Certificate for {service} expires in {days_left} days",
        "TLS renewal: {service} ({env})",
    ),
    intros=(
        "The certificate for {service} in {env} expires in {days_left} days. Please renew it.",
        "Cert renewal needed for the {service} service ({env}).",
        "Our TLS cert is about to expire ({days_left} days). Can you renew?",
    ),
    field_phrases={
        "hostname": ("Hostname: {hostname}.", "It is served from {hostname}."),
    },
    field_labels={"hostname": ("Hostname", "Host")},
    sample_params=_cert_params,
    actions=_cert_actions,
    inventory=_cert_inventory,
    mistake=_cert_mistake,
    components=("certificates",),
    procedure_version=current_version,
)


# --- dns_change ------------------------------------------------------------------------


def _dns_params(rng: random.Random) -> Params:
    service = pick(rng, names.SERVICES)
    env = weighted(rng, names.ENVS)
    zone = pick(rng, ZONES)
    record_type = pick(rng, ("A", "A", "CNAME"))
    name = f"{service}-{env}" if env != "prod" else service
    if record_type == "A":
        value = f"10.{rng.randint(10, 250)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
    else:
        value = f"{service}-lb.{zone}"
    return {
        "service": service,
        "env": env,
        "zone": zone,
        "name": name,
        "record_type": record_type,
        "value": value,
        "ttl": pick(rng, (300, 300, 600, 3600)),
    }


def _dns_actions(p: Params, _version: str) -> list[ActionSpec]:
    return [
        ActionSpec(
            tool="platform_dns_upsert_record",
            args={
                "zone": p["zone"],
                "name": p["name"],
                "type": p["record_type"],
                "value": p["value"],
                "ttl": p["ttl"],
            },
        )
    ]


def _dns_inventory(p: Params, _rng: random.Random) -> dict[str, Any]:
    return {
        "dns": {
            "zones": [
                {
                    "name": p["zone"],
                    "records": [{"name": "www", "type": "A", "value": "10.1.1.10", "ttl": 300}],
                }
            ]
        }
    }


def _dns_mistake(p: Params, _rng: random.Random) -> Mistake:
    if p["record_type"] != "A":
        return None
    wrong = dict(p)
    parts = p["value"].split(".")
    parts[-1] = str((int(parts[-1]) % 254) + 1)
    wrong["value"] = ".".join(parts)
    what = f"{p['name']}.{p['zone']} was pointed at {wrong['value']} instead of {p['value']}"
    return wrong, what


DNS_CHANGE = RequestType(
    key="dns_change",
    label="DNS change",
    weight=0.10,
    missable_fields=("value",),
    effort_hours=(0.25, 0.75),
    summaries=(
        "DNS: {name}.{zone} -> {value}",
        "Add {record_type} record for {service} ({env})",
        "Please update DNS for {service}",
    ),
    intros=(
        "Please add a DNS record for {service} in {env}.",
        "DNS change request for the {service} service.",
        "We need {name}.{zone} to resolve for the new {env} deployment.",
    ),
    field_phrases={
        "name": ("Record name: {name}.", "Name: {name} in zone {zone}."),
        "record_type": ("Type: {record_type}.", "An {record_type} record."),
        "value": ("Value: {value}.", "Point it at {value}.", "Target {value}."),
        "ttl": ("TTL {ttl}.",),
    },
    field_labels={
        "name": ("Record", "Name"),
        "record_type": ("Type",),
        "value": ("Value", "Target"),
        "ttl": ("TTL",),
    },
    sample_params=_dns_params,
    actions=_dns_actions,
    inventory=_dns_inventory,
    mistake=_dns_mistake,
    components=("dns",),
    procedure_version=current_version,
)


# --- group_access ----------------------------------------------------------------------


def _group_params(rng: random.Random) -> Params:
    team = pick(rng, names.TEAMS)
    user = pick(rng, names.REQUESTERS)
    group = pick(
        rng,
        (f"{team}-developers", f"{team}-developers", f"{team}-readonly", f"{team}-deployers"),
    )
    return {
        "team": team,
        "user": user,
        "group": group,
        "reason": pick(
            rng,
            (
                "joined the team",
                "covering on-call",
                "needs to review MRs",
                "is a new starter",
                "moved from another squad",
            ),
        ),
    }


def _group_actions(p: Params, _version: str) -> list[ActionSpec]:
    return [
        ActionSpec(tool="platform_group_add_member", args={"group": p["group"], "user": p["user"]})
    ]


def _group_inventory(p: Params, _rng: random.Random) -> dict[str, Any]:
    return {
        "groups": [
            {"name": p["group"], "members": ["eng-01", "eng-02"]},
            {"name": "platform-admins", "members": ["eng-01"], "privileged": True},
        ]
    }


def _group_mistake(p: Params, _rng: random.Random) -> Mistake:
    wrong = dict(p)
    if p["group"].endswith("readonly"):
        wrong["group"] = f"{p['team']}-developers"
    else:
        wrong["group"] = f"{p['team']}-readonly"
    return wrong, f"{p['user']} was added to {wrong['group']} instead of {p['group']}"


GROUP_ACCESS = RequestType(
    key="group_access",
    label="Group access request",
    weight=0.10,
    missable_fields=("group",),
    effort_hours=(0.25, 0.5),
    summaries=(
        "Add {user} to {group}",
        "Access request: {group} for {user}",
        "Group membership for {user} ({team})",
    ),
    intros=(
        "Please add a user to a group, they {reason}.",
        "Access request for the {team} team.",
        "Can you grant group access? The person {reason}.",
    ),
    field_phrases={
        "user": ("User: {user}.", "The user is {user}."),
        "group": ("Group: {group}.", "Add them to {group}."),
    },
    field_labels={"user": ("User",), "group": ("Group",)},
    sample_params=_group_params,
    actions=_group_actions,
    inventory=_group_inventory,
    mistake=_group_mistake,
    components=("access",),
    procedure_version=current_version,
)


# --- ci_fix ----------------------------------------------------------------------------


def _ci_params(rng: random.Random) -> Params:
    team = pick(rng, names.TEAMS)
    service = pick(rng, names.SERVICES)
    variable = pick(
        rng,
        (
            "DEPLOY_TOKEN",
            "REGISTRY_URL",
            "SONAR_HOST",
            "NPM_REGISTRY",
            "ARTIFACT_STORE_URL",
            "KUBE_CONTEXT",
        ),
    )
    value = pick(
        rng,
        (
            "https://registry.internal.example",
            "prod-cluster-a",
            "https://sonar.internal.example",
            "https://artifacts.internal.example",
        ),
    )
    return {
        "team": team,
        "service": service,
        "ci_project": f"{team}/{service}",
        "variable": variable,
        "value": value,
        "protected": variable.endswith("TOKEN"),
    }


def _ci_actions(p: Params, _version: str) -> list[ActionSpec]:
    return [
        ActionSpec(
            tool="platform_ci_set_variable",
            args={
                "project": p["ci_project"],
                "key": p["variable"],
                "value": p["value"],
                "protected": p["protected"],
            },
        )
    ]


def _ci_inventory(p: Params, _rng: random.Random) -> dict[str, Any]:
    return {
        "ci": {
            "projects": [
                {
                    "path": p["ci_project"],
                    "variables": [{"key": "CI_DEBUG", "value": "false", "protected": False}],
                }
            ],
            "runners": [{"name": "shared-01", "status": "online"}],
        }
    }


def _ci_mistake(p: Params, _rng: random.Random) -> Mistake:
    if not p["protected"]:
        return None
    wrong = dict(p)
    wrong["protected"] = False
    return wrong, f"{p['variable']} on {p['ci_project']} was set unprotected"


CI_FIX = RequestType(
    key="ci_fix",
    label="CI variable or runner fix",
    weight=0.06,
    missable_fields=("value", "variable"),
    effort_hours=(0.5, 2.0),
    summaries=(
        "Set {variable} on {ci_project}",
        "Pipeline for {service} failing: missing {variable}",
        "CI variable fix for {team}/{service}",
    ),
    intros=(
        "The pipeline for {service} is failing because a CI variable is missing or wrong.",
        "Please fix our CI config for {ci_project}.",
        "Builds for {service} broke after the registry move.",
    ),
    field_phrases={
        "ci_project": ("Project: {ci_project}.",),
        "variable": ("Variable: {variable}.", "The variable is {variable}."),
        "value": ("Value: {value}.", "It should be {value}.", "Set it to {value}."),
    },
    field_labels={"ci_project": ("Project",), "variable": ("Variable",), "value": ("Value",)},
    sample_params=_ci_params,
    actions=_ci_actions,
    inventory=_ci_inventory,
    mistake=_ci_mistake,
    components=("ci",),
    procedure_version=current_version,
)

REQUEST_TYPES = (CERT_RENEWAL, DNS_CHANGE, GROUP_ACCESS, CI_FIX)
