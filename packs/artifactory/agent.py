"""Scripted reference procedure and text parser for Artifactory repository requests."""

from __future__ import annotations

from typing import Any

from replaybook.agents.parse import first
from replaybook.mock_env.registry import COMMENT_TOOL

from .synth import ARTIFACTORY_ACCESS


def parse_repo_request(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    key = first([r"\b([a-z0-9-]+-local)\b"], text)
    if key:
        found["repo_key"] = key.lower()
    package_type = first([r"\b(maven|npm|docker|pypi|generic|helm)\b"], text)
    if package_type:
        found["package_type"] = package_type.lower()
    group = first([r"\b([a-z0-9-]+-(?:developers|readonly|deployers))\b"], text)
    if group:
        found["group"] = group.lower()
    access = first([r"\b(read-write)\b", r"\b(deploy)\b", r"\b(read)\b"], text)
    if access:
        found["access"] = access.lower()
    return found


def artifactory_repo(agent: Any, call: Any, text: str) -> tuple[str, str]:
    p = parse_repo_request(text)
    missing = [f for f in ("repo_key", "package_type", "group", "access") if f not in p]
    if missing:
        if not agent.has("guess_missing_info"):
            return agent.ask(call, missing)
        p.setdefault("package_type", "generic")
        p.setdefault("access", "read")
        still = [f for f in ("repo_key", "group") if f not in p]
        if still:
            return agent.ask(call, still)
    existing = call("artifactory_get_repo", key=p["repo_key"])
    if "error" in existing:
        call(
            "artifactory_create_repo",
            key=p["repo_key"],
            package_type=p["package_type"],
            rclass="local",
        )
    call(
        "artifactory_create_permission_target",
        name=f"{p['repo_key']}-{p['access']}",
        repositories=[p["repo_key"]],
        groups={p["group"]: list(ARTIFACTORY_ACCESS[p["access"]])},
    )
    message = (
        f"Created {p['repo_key']} ({p['package_type']}) with {p['access']} access for {p['group']}."
    )
    call(COMMENT_TOOL, body=message)
    return message, "done"


PROCEDURES = {"artifactory_repo": artifactory_repo}
EXTRACTORS: dict[str, Any] = {}
# The skill creates new permission targets; changing an existing one is a person's call.
ESCALATE_CUES = {
    "artifactory_repo": [
        (
            r"existing permission target",
            "the ticket also asks to change an existing permission target, which the skill "
            "does not cover",
        )
    ]
}
