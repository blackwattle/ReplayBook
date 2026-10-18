"""Synthetic request type for the artifactory pack: a repository plus a permission target."""

from __future__ import annotations

import random
from typing import Any

from replaybook.schema.ground_truth import ActionSpec
from replaybook.synth import names
from replaybook.synth.names import pick
from replaybook.synth.types import Mistake, Params, RequestType, Unusual, current_version

PACKAGE_TYPES = ("maven", "npm", "docker", "pypi", "generic", "helm")
ARTIFACTORY_ACCESS = {
    "read": ["read"],
    "read-write": ["read", "write", "annotate"],
    "deploy": ["read", "write", "annotate", "delete"],
}


def _artifactory_params(rng: random.Random) -> Params:
    team = pick(rng, names.TEAMS)
    service = pick(rng, names.SERVICES)
    package_type = pick(rng, PACKAGE_TYPES)
    access = pick(rng, tuple(ARTIFACTORY_ACCESS))
    return {
        "team": team,
        "service": service,
        "repo_key": f"{team}-{service}-local",
        "package_type": package_type,
        "group": f"{team}-developers",
        "access": access,
    }


def _artifactory_actions(p: Params, _version: str) -> list[ActionSpec]:
    return [
        ActionSpec(
            tool="artifactory_create_repo",
            args={"key": p["repo_key"], "package_type": p["package_type"], "rclass": "local"},
        ),
        ActionSpec(
            tool="artifactory_create_permission_target",
            args={
                "name": f"{p['repo_key']}-{p['access']}",
                "repositories": [p["repo_key"]],
                "groups": {p["group"]: list(ARTIFACTORY_ACCESS[p["access"]])},
            },
        ),
    ]


def _artifactory_inventory(p: Params, rng: random.Random) -> dict[str, Any]:
    repos = [
        {
            "key": f"{pick(rng, names.TEAMS)}-{pick(rng, names.SERVICES)}-maven-local",
            "package_type": "maven",
            "rclass": "local",
        }
        for _ in range(rng.randint(2, 4))
    ]
    return {
        "artifactory": {
            "repositories": repos,
            "groups": [p["group"], f"{p['team']}-readonly", "platform-admins"],
        }
    }


def _artifactory_mistake(p: Params, rng: random.Random) -> Mistake:
    wrong = dict(p)
    others = tuple(t for t in PACKAGE_TYPES if t != p["package_type"])
    wrong["package_type"] = pick(rng, others)
    what = (
        f"repo {p['repo_key']} was created as {wrong['package_type']} "
        f"instead of {p['package_type']}"
    )
    return wrong, what


def _artifactory_unusual(p: Params, _version: str, _rng: random.Random) -> Unusual:
    """Also change an existing permission target, which the skill does not cover: engineers
    checked with its owners first (artifactory_update_permission_target)."""
    target = f"{p['team']}-shared-builds"
    return Unusual(
        cue=(
            f"Please also add {p['group']} to our existing permission target {target} so the "
            "team sees the shared builds."
        ),
        actions=[
            ActionSpec(
                tool="artifactory_update_permission_target",
                args={
                    "name": target,
                    "repositories": [f"{p['team']}-shared-builds-local"],
                    "groups": {p["group"]: ["read"]},
                },
            )
        ],
        params={"other_permission_target": target},
    )


ARTIFACTORY_REPO = RequestType(
    key="artifactory_repo",
    label="Artifactory repo + permissions",
    weight=0.12,
    missable_fields=("package_type", "access"),
    effort_hours=(0.5, 1.5),
    summaries=(
        "Artifactory repo {repo_key} for {service}",
        "New {package_type} repository in Artifactory for {team}",
        "Please create {repo_key} with {access} access for {group}",
    ),
    intros=(
        "We need a new Artifactory repository for {service}.",
        "Could you create an Artifactory repo for the {team} team? It is for {service}.",
        "Requesting an artifact repository so {service} can publish builds.",
    ),
    field_phrases={
        "repo_key": ("Repo key: {repo_key}.", "Please call it {repo_key}."),
        "package_type": (
            "Package type: {package_type}.",
            "It will hold {package_type} packages.",
            "Type {package_type}.",
        ),
        "group": ("Grant it to the {group} group.", "Group: {group}."),
        "access": ("Access level: {access}.", "They need {access} access."),
    },
    field_labels={
        "repo_key": ("Repository key", "Repo"),
        "package_type": ("Package type", "Type"),
        "group": ("Group",),
        "access": ("Access",),
    },
    sample_params=_artifactory_params,
    actions=_artifactory_actions,
    inventory=_artifactory_inventory,
    mistake=_artifactory_mistake,
    components=("artifactory",),
    procedure_version=current_version,
    unusual=_artifactory_unusual,
)

REQUEST_TYPES = (ARTIFACTORY_REPO,)
