"""The openstack slice of world state: projects with quotas, servers, security groups."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PACK = "openstack"
DEFAULT_FLAVORS = ["m1.small", "m1.medium", "m1.large", "c2.xlarge", "r3.large"]
DEFAULT_IMAGES = ["ubuntu-22.04", "ubuntu-24.04", "rhel-9"]


class OpenStackProject(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    quotas: dict[str, int] = Field(
        default_factory=lambda: {
            "cores": 32,
            "ram": 65536,
            "instances": 20,
            "volumes": 40,
            "gigabytes": 2000,
        }
    )


class Server(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    project: str
    flavor: str = "m1.small"
    image: str = "ubuntu-24.04"
    network: str | None = None
    status: str = "ACTIVE"


class SecurityGroup(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    project: str
    rules: list[dict[str, Any]] = Field(default_factory=list)


class OpenStackState(BaseModel):
    quota_tool: str = "unified"
    projects: list[OpenStackProject] = Field(default_factory=list)
    servers: list[Server] = Field(default_factory=list)
    flavors: list[str] = Field(default_factory=lambda: list(DEFAULT_FLAVORS))
    images: list[str] = Field(default_factory=lambda: list(DEFAULT_IMAGES))
    networks: list[str] = Field(default_factory=list)
    security_groups: list[SecurityGroup] = Field(default_factory=list)

    def project(self, name: str) -> OpenStackProject | None:
        return next((p for p in self.projects if p.name == name), None)

    def security_group(self, name: str) -> SecurityGroup | None:
        return next((g for g in self.security_groups if g.name == name), None)


def from_inventory(inventory: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """The ticket's openstack inventory overlaid with today's quota procedure."""
    inv = inventory.get(PACK, {})
    state = OpenStackState(
        projects=[OpenStackProject.model_validate(p) for p in inv.get("projects", [])],
        servers=[Server.model_validate(s) for s in inv.get("servers", [])],
        networks=list(inv.get("networks", [])),
        security_groups=[SecurityGroup.model_validate(g) for g in inv.get("security_groups", [])],
    )
    if inv.get("flavors"):
        state.flavors = list(inv["flavors"])
    if inv.get("images"):
        state.images = list(inv["images"])
    current = facts.get("current_procedures", {})
    state.quota_tool = current.get("openstack_quota", state.quota_tool)
    return state.model_dump(mode="json")
