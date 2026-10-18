"""The artifactory slice of world state: repositories, permission targets, groups."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PACK = "artifactory"


class Repository(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    package_type: str = "generic"
    rclass: str = "local"


class PermissionTarget(BaseModel):
    name: str
    repositories: list[str] = Field(default_factory=list)
    groups: dict[str, list[str]] = Field(default_factory=dict)


class ArtifactoryState(BaseModel):
    repositories: list[Repository] = Field(default_factory=list)
    permission_targets: list[PermissionTarget] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)

    def repository(self, key: str) -> Repository | None:
        return next((r for r in self.repositories if r.key == key), None)

    def permission_target(self, name: str) -> PermissionTarget | None:
        return next((p for p in self.permission_targets if p.name == name), None)


def from_inventory(inventory: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    inv = inventory.get(PACK, {})
    state = ArtifactoryState(
        repositories=[Repository.model_validate(r) for r in inv.get("repositories", [])],
        groups=list(inv.get("groups", [])),
    )
    return state.model_dump(mode="json")
