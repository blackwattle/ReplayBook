"""The platform slice of world state: DNS zones, certificates, groups, CI projects."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PACK = "platform"
DEFAULT_PRIVILEGED = ["platform-admins", "db-admins"]


class DnsRecord(BaseModel):
    name: str
    type: str
    value: str
    ttl: int = 300


class DnsZone(BaseModel):
    name: str
    records: list[DnsRecord] = Field(default_factory=list)


class Certificate(BaseModel):
    model_config = ConfigDict(extra="allow")

    hostname: str
    days_left: int = 30
    issuer: str = "internal-ca"


class Group(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    members: list[str] = Field(default_factory=list)
    privileged: bool = False


class CiVariable(BaseModel):
    key: str
    value: str
    protected: bool = False
    masked: bool = False


class CiProject(BaseModel):
    path: str
    variables: list[CiVariable] = Field(default_factory=list)


class Runner(BaseModel):
    name: str
    status: str = "online"


class PlatformState(BaseModel):
    zones: list[DnsZone] = Field(default_factory=list)
    certificates: list[Certificate] = Field(default_factory=list)
    groups: list[Group] = Field(default_factory=list)
    privileged_groups: list[str] = Field(default_factory=lambda: list(DEFAULT_PRIVILEGED))
    ci_projects: list[CiProject] = Field(default_factory=list)
    runners: list[Runner] = Field(default_factory=list)

    def zone(self, name: str) -> DnsZone | None:
        return next((z for z in self.zones if z.name == name), None)

    def certificate(self, hostname: str) -> Certificate | None:
        return next((c for c in self.certificates if c.hostname == hostname), None)

    def group(self, name: str) -> Group | None:
        return next((g for g in self.groups if g.name == name), None)

    def ci_project(self, path: str) -> CiProject | None:
        return next((p for p in self.ci_projects if p.path == path), None)


def from_inventory(inventory: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """The platform inventory keys (dns, certificates, groups, ci) plus today's list of
    privileged groups."""
    state = PlatformState(
        zones=[DnsZone.model_validate(z) for z in inventory.get("dns", {}).get("zones", [])],
        certificates=[Certificate.model_validate(c) for c in inventory.get("certificates", [])],
        groups=[Group.model_validate(g) for g in inventory.get("groups", [])],
        ci_projects=[
            CiProject.model_validate(p) for p in inventory.get("ci", {}).get("projects", [])
        ],
        runners=[Runner.model_validate(r) for r in inventory.get("ci", {}).get("runners", [])],
    )
    if facts.get("privileged_groups"):
        state.privileged_groups = list(facts["privileged_groups"])
    for group in state.groups:
        if group.name in state.privileged_groups:
            group.privileged = True
    return state.model_dump(mode="json")
