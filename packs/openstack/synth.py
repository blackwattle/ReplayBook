"""Synthetic request types for the openstack pack: quota increases, VM provisioning,
security group rules. Quota tickets before OPENSTACK_UNIFIED_QUOTA_SINCE were correctly
handled with the per-service tools."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

from replaybook.schema.ground_truth import ActionSpec
from replaybook.synth import names
from replaybook.synth.names import pick, weighted
from replaybook.synth.types import Mistake, Params, RequestType, current_version

OPENSTACK_UNIFIED_QUOTA_SINCE = datetime(2026, 2, 15, tzinfo=UTC)
FLAVORS = ("m1.small", "m1.medium", "m1.large", "c2.xlarge", "r3.large", "r3.2xlarge")
IMAGES = ("ubuntu-22.04", "ubuntu-24.04", "rhel-9", "rocky-9", "windows-2022")
QUOTA_RESOURCES = {
    "cores": ("nova", (32, 48, 64, 96, 128)),
    "ram": ("nova", (65536, 131072, 262144, 524288)),
    "instances": ("nova", (20, 30, 40, 60)),
    "volumes": ("cinder", (40, 60, 100)),
    "gigabytes": ("cinder", (2000, 4000, 8000)),
}
PORTS = {
    "https": ("tcp", 443),
    "ssh": ("tcp", 22),
    "postgres": ("tcp", 5432),
    "broker": ("tcp", 9092),
    "app": ("tcp", 8080),
    "dns": ("udp", 53),
    "rdp": ("tcp", 3389),
}

PROCEDURE_CHANGES: list[dict[str, Any]] = [
    {
        "request_type": "openstack_quota",
        "at": OPENSTACK_UNIFIED_QUOTA_SINCE,
        "from_version": "per-service",
        "to_version": "unified",
        "summary": (
            "OpenStack quotas are set with the unified openstack_quota_set tool instead of the "
            "per-service openstack_nova_quota_update and openstack_cinder_quota_update tools."
        ),
    }
]

CHANGE_TICKETS: tuple[dict[str, Any], ...] = (
    {
        "at": OPENSTACK_UNIFIED_QUOTA_SINCE,
        "summary": "OpenStack: move quota management to the unified quota command",
        "description": (
            "The per-service nova and cinder quota tooling is deprecated. Quota changes now go "
            "through the unified quota set command. Runbook updated."
        ),
        "labels": ("openstack", "platform-change"),
    },
)


def platform_facts() -> dict[str, Any]:
    return {"openstack": {"quota_tool": "openstack_quota_set"}}


def _quota_version(at: datetime) -> str:
    return "unified" if at >= OPENSTACK_UNIFIED_QUOTA_SINCE else "per-service"


# --- openstack_quota -------------------------------------------------------------------


def _quota_params(rng: random.Random) -> Params:
    team = pick(rng, names.TEAMS)
    env = weighted(rng, names.ENVS)
    resource = pick(rng, tuple(QUOTA_RESOURCES))
    service, values = QUOTA_RESOURCES[resource]
    new_value = pick(rng, values)
    return {
        "team": team,
        "env": env,
        "project": names.project_name(team, env),
        "resource": resource,
        "service": service,
        "current_value": max(1, new_value // 2),
        "new_value": new_value,
        "reason": pick(
            rng,
            (
                "a new batch workload",
                "load testing",
                "the quarter-end run",
                "migrating a legacy app",
                "more replicas for resilience",
            ),
        ),
    }


def _quota_actions(p: Params, version: str) -> list[ActionSpec]:
    args = {"project": p["project"], p["resource"]: p["new_value"]}
    if version == "unified":
        return [ActionSpec(tool="openstack_quota_set", args=args)]
    if p["service"] == "nova":
        return [ActionSpec(tool="openstack_nova_quota_update", args=args)]
    return [ActionSpec(tool="openstack_cinder_quota_update", args=args)]


def _quota_inventory(p: Params, _rng: random.Random) -> dict[str, Any]:
    quotas = {"cores": 32, "ram": 65536, "instances": 20, "volumes": 40, "gigabytes": 2000}
    quotas[p["resource"]] = p["current_value"]
    return {"openstack": {"projects": [{"name": p["project"], "quotas": quotas}]}}


def _quota_mistake(p: Params, _rng: random.Random) -> Mistake:
    wrong = dict(p)
    wrong["new_value"] = int(p["new_value"] * 0.75)
    what = (
        f"{p['resource']} quota for {p['project']} was set to {wrong['new_value']} "
        f"instead of {p['new_value']}"
    )
    return wrong, what


OPENSTACK_QUOTA = RequestType(
    key="openstack_quota",
    label="OpenStack quota increase",
    weight=0.13,
    missable_fields=("new_value", "project"),
    effort_hours=(0.25, 1.0),
    summaries=(
        "Quota increase for {project}: {resource} to {new_value}",
        "Please raise {resource} quota on {project}",
        "OpenStack quota bump ({project})",
    ),
    intros=(
        "We are hitting the {resource} quota in OpenStack and need it raised for {reason}.",
        "Please increase our OpenStack quota, we need more {resource} for {reason}.",
        "Quota request for the {team} team ({env}).",
    ),
    field_phrases={
        "project": ("Project: {project}.", "The project is {project}."),
        "resource": ("Resource: {resource}.",),
        "new_value": (
            "New value: {new_value}.",
            "Please set {resource} to {new_value}.",
            "We need {new_value} {resource} in total.",
        ),
    },
    field_labels={
        "project": ("Project",),
        "resource": ("Resource",),
        "new_value": ("New value", "Requested"),
    },
    sample_params=_quota_params,
    actions=_quota_actions,
    inventory=_quota_inventory,
    mistake=_quota_mistake,
    components=("openstack",),
    procedure_version=_quota_version,
)


# --- vm_provision ----------------------------------------------------------------------


def _vm_params(rng: random.Random) -> Params:
    team = pick(rng, names.TEAMS)
    env = weighted(rng, names.ENVS)
    service = pick(rng, names.SERVICES)
    return {
        "team": team,
        "env": env,
        "service": service,
        "project": names.project_name(team, env),
        "name": f"{service}-{env}-{rng.randint(1, 9):02d}",
        "flavor": pick(rng, FLAVORS),
        "image": pick(rng, IMAGES),
        "network": f"{team}-net-{env}",
    }


def _vm_actions(p: Params, _version: str) -> list[ActionSpec]:
    return [
        ActionSpec(
            tool="openstack_server_create",
            args={
                "project": p["project"],
                "name": p["name"],
                "flavor": p["flavor"],
                "image": p["image"],
                "network": p["network"],
            },
        )
    ]


def _vm_inventory(p: Params, _rng: random.Random) -> dict[str, Any]:
    return {
        "openstack": {
            "projects": [
                {"name": p["project"], "quotas": {"cores": 64, "ram": 131072, "instances": 40}}
            ],
            "flavors": list(FLAVORS),
            "images": list(IMAGES),
            "networks": [p["network"]],
            "servers": [
                {
                    "name": f"{p['service']}-{p['env']}-00",
                    "flavor": "m1.small",
                    "status": "ACTIVE",
                    "project": p["project"],
                }
            ],
        }
    }


def _vm_mistake(p: Params, rng: random.Random) -> Mistake:
    wrong = dict(p)
    wrong["flavor"] = pick(rng, tuple(f for f in FLAVORS if f != p["flavor"]))
    return wrong, f"{p['name']} was built as {wrong['flavor']} instead of {p['flavor']}"


VM_PROVISION = RequestType(
    key="vm_provision",
    label="VM provisioning",
    weight=0.12,
    missable_fields=("flavor", "image"),
    effort_hours=(1.0, 3.0),
    summaries=(
        "New VM {name} for {service} ({env})",
        "Provision a {flavor} instance in {project}",
        "Please build a server for {service}",
    ),
    intros=(
        "Please provision a VM for {service} in {env}.",
        "We need a new instance in project {project}.",
        "Can you build us a box for {service}? Environment {env}.",
    ),
    field_phrases={
        "name": ("Hostname: {name}.", "Call it {name}."),
        "flavor": ("Flavor: {flavor}.", "Size {flavor} should do.", "Use flavor {flavor}."),
        "image": ("Image: {image}.", "Base image {image}.", "OS: {image}."),
        "network": ("Network: {network}.", "Attach it to {network}."),
    },
    field_labels={
        "name": ("Hostname", "Name"),
        "flavor": ("Flavor", "Size"),
        "image": ("Image", "OS"),
        "network": ("Network",),
    },
    sample_params=_vm_params,
    actions=_vm_actions,
    inventory=_vm_inventory,
    mistake=_vm_mistake,
    components=("openstack",),
    procedure_version=current_version,
)


# --- secgroup_rule ---------------------------------------------------------------------


def _secgroup_params(rng: random.Random) -> Params:
    team = pick(rng, names.TEAMS)
    env = weighted(rng, names.ENVS)
    service = pick(rng, names.SERVICES)
    port_name = pick(rng, ("https", "postgres", "broker", "app", "ssh"))
    protocol, port = PORTS[port_name]
    return {
        "team": team,
        "env": env,
        "service": service,
        "project": names.project_name(team, env),
        "security_group": f"{service}-{env}-sg",
        "protocol": protocol,
        "port": port,
        "port_name": port_name,
        "cidr": f"10.{rng.randint(10, 250)}.{rng.randint(0, 255)}.0/24",
        "source": pick(
            rng,
            ("the batch subnet", "the monitoring hosts", "the bastion", "the API gateway subnet"),
        ),
    }


def _secgroup_actions(p: Params, _version: str) -> list[ActionSpec]:
    return [
        ActionSpec(
            tool="openstack_security_group_rule_create",
            args={
                "project": p["project"],
                "security_group": p["security_group"],
                "protocol": p["protocol"],
                "port_min": p["port"],
                "port_max": p["port"],
                "remote_ip_prefix": p["cidr"],
            },
        )
    ]


def _secgroup_inventory(p: Params, _rng: random.Random) -> dict[str, Any]:
    return {
        "openstack": {
            "projects": [{"name": p["project"]}],
            "security_groups": [
                {
                    "name": p["security_group"],
                    "project": p["project"],
                    "rules": [
                        {
                            "protocol": "tcp",
                            "port_min": 443,
                            "port_max": 443,
                            "remote_ip_prefix": "10.0.0.0/8",
                        }
                    ],
                },
            ],
        }
    }


def _secgroup_mistake(p: Params, _rng: random.Random) -> Mistake:
    wrong = dict(p)
    wrong["port"] = p["port"] + 1
    what = f"rule on {p['security_group']} opened port {wrong['port']} instead of {p['port']}"
    return wrong, what


SECGROUP_RULE = RequestType(
    key="secgroup_rule",
    label="Security group rule",
    weight=0.10,
    missable_fields=("cidr", "port"),
    effort_hours=(0.25, 1.0),
    summaries=(
        "Open {port_name} on {security_group} from {cidr}",
        "Security group rule for {service} ({env})",
        "Allow {protocol}/{port} into {security_group}",
    ),
    intros=(
        "Please add a security group rule for {service} in {env}.",
        "We need inbound {port_name} allowed to {service} from {source}.",
        "Firewall change request for project {project}.",
    ),
    field_phrases={
        "security_group": ("Security group: {security_group}.", "The group is {security_group}."),
        "port": ("Port {port}/{protocol}.", "Protocol {protocol}, port {port}."),
        "cidr": ("Source CIDR {cidr}.", "From {cidr}.", "Source range: {cidr}."),
    },
    field_labels={
        "security_group": ("Security group",),
        "port": ("Port",),
        "cidr": ("Source CIDR", "Source"),
    },
    sample_params=_secgroup_params,
    actions=_secgroup_actions,
    inventory=_secgroup_inventory,
    mistake=_secgroup_mistake,
    components=("openstack", "network"),
    procedure_version=current_version,
)

REQUEST_TYPES = (OPENSTACK_QUOTA, VM_PROVISION, SECGROUP_RULE)
