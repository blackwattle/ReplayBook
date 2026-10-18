"""Scripted reference procedure and text parser for OpenStack quota requests."""

from __future__ import annotations

from typing import Any

from replaybook.agents.parse import find_env, first, team_name
from replaybook.mock_env.registry import ASK_TOOL, COMMENT_TOOL

NOVA = ("cores", "ram", "instances")


def parse_quota_request(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    project = first([r"\b([a-z0-9-]+-(?:dev|test|uat|prod))\b"], text)
    if project:
        found["project"] = project.lower()
    resource = first([r"\b(cores|ram|instances|volumes|gigabytes)\b"], text)
    if resource:
        found["resource"] = resource.lower()
    value = first(
        [
            r"new value\s*[:=]\s*(\d+)",
            r"requested\s*[:=]\s*(\d+)",
            r"set (?:cores|ram|instances|volumes|gigabytes) to (\d+)",
            r"need (\d+) (?:cores|ram|instances|volumes|gigabytes)",
            r"(?:cores|ram|instances|volumes|gigabytes) to (\d+)\b",
        ],
        text,
    )
    if value:
        found["new_value"] = int(value)
    team = team_name(text)
    if team:
        found["team"] = team
    env = find_env(text)
    if env:
        found["env"] = env
    return found


def openstack_quota(agent: Any, call: Any, text: str) -> tuple[str, str]:
    p = parse_quota_request(text)
    missing = [f for f in ("project", "resource", "new_value") if f not in p]
    if missing:
        if not agent.has("guess_missing_info"):
            return agent.ask(call, missing)
        if "project" not in p and p.get("team") and p.get("env"):
            p["project"] = f"{p['team']}-{p['env']}"
        if "project" not in p or "resource" not in p:
            return agent.ask(call, [f for f in ("project", "resource") if f not in p])
    current = call("openstack_quota_show", project=p["project"])
    if "error" in current:
        message = f"Project {p['project']} does not exist; please check the project name."
        call(ASK_TOOL, question=message)
        return message, "asked"
    if "new_value" not in p:
        p["new_value"] = int(current["quotas"].get(p["resource"], 0)) * 2
    if agent.has("superseded_procedure"):
        tool = (
            "openstack_nova_quota_update"
            if p["resource"] in NOVA
            else "openstack_cinder_quota_update"
        )
    else:
        tool = "openstack_quota_set"
    call(tool, project=p["project"], **{p["resource"]: p["new_value"]})
    message = f"Set {p['resource']} quota on {p['project']} to {p['new_value']}."
    call(COMMENT_TOOL, body=message)
    return message, "done"


PROCEDURES = {"openstack_quota": openstack_quota}
EXTRACTORS: dict[str, Any] = {}
