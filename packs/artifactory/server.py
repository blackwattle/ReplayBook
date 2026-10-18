"""Mock Artifactory server. Nothing here talks to a real instance."""

from __future__ import annotations

from typing import Any

from replaybook.mock_env.base import MockServer, ToolError
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.world import WorldState

from .world import PACK, ArtifactoryState, PermissionTarget, Repository

INSTRUCTIONS = (
    "Mock Artifactory. Reads answer from the case's world state; writes are recorded and "
    "applied to an in-memory copy only."
)
PACKAGE_TYPES = ("maven", "npm", "docker", "pypi", "generic", "helm", "nuget", "gradle")
ACTIONS = ("read", "write", "annotate", "delete", "manage")


def build(world: WorldState, recorder: CallRecorder) -> MockServer:
    server = MockServer(PACK, INSTRUCTIONS, world, recorder)
    art = ArtifactoryState.model_validate(world.slice(PACK))

    def require_repo(key: str) -> Repository:
        found = art.repository(key)
        if found is None:
            raise ToolError(f"repository '{key}' not found")
        return found

    def check_groups(groups: dict[str, list[str]]) -> dict[str, list[str]]:
        if not groups:
            raise ToolError("at least one group is required")
        cleaned: dict[str, list[str]] = {}
        for group, actions in groups.items():
            if group not in art.groups:
                raise ToolError(f"group '{group}' not found. Known: {', '.join(art.groups)}")
            bad = [a for a in actions if a.lower() not in ACTIONS]
            if bad:
                raise ToolError(f"unknown actions {bad}. Allowed: {', '.join(ACTIONS)}")
            cleaned[group] = sorted({a.lower() for a in actions})
        return cleaned

    @server.read
    def artifactory_list_repos() -> dict[str, Any]:
        """List repositories with their package type and class."""
        return {"repositories": [r.model_dump() for r in art.repositories]}

    @server.read
    def artifactory_get_repo(key: str) -> dict[str, Any]:
        """Get one repository. Errors if it does not exist."""
        return require_repo(key).model_dump()

    @server.read
    def artifactory_list_permission_targets() -> dict[str, Any]:
        """List permission targets (which groups may do what on which repositories)."""
        return {"permission_targets": [p.model_dump() for p in art.permission_targets]}

    @server.read
    def artifactory_list_groups() -> dict[str, Any]:
        """List known user groups."""
        return {"groups": list(art.groups)}

    @server.write()
    def artifactory_create_repo(
        key: str, package_type: str, rclass: str = "local"
    ) -> dict[str, Any]:
        """Create a repository of a package type (maven, npm, docker, pypi, generic, helm, ...)."""
        if art.repository(key) is not None:
            raise ToolError(f"repository '{key}' already exists")
        if package_type.lower() not in PACKAGE_TYPES:
            raise ToolError(
                f"unknown package type '{package_type}'. Allowed: {', '.join(PACKAGE_TYPES)}"
            )
        if rclass not in ("local", "remote", "virtual"):
            raise ToolError("rclass must be local, remote or virtual")
        repo = Repository(key=key, package_type=package_type.lower(), rclass=rclass)
        art.repositories.append(repo)
        return repo.model_dump()

    @server.write(destructive=True)
    def artifactory_delete_repo(key: str) -> dict[str, Any]:
        """Delete a repository and every artifact in it. Destructive and irreversible."""
        found = require_repo(key)
        art.repositories.remove(found)
        return {"deleted": key}

    @server.write()
    def artifactory_create_permission_target(
        name: str, repositories: list[str], groups: dict[str, list[str]]
    ) -> dict[str, Any]:
        """Create a permission target: groups get actions (read, write, annotate, delete,
        manage) on repositories."""
        if art.permission_target(name) is not None:
            raise ToolError(f"permission target '{name}' already exists")
        for key in repositories:
            require_repo(key)
        target = PermissionTarget(
            name=name, repositories=sorted(repositories), groups=check_groups(groups)
        )
        art.permission_targets.append(target)
        return target.model_dump()

    @server.write(idempotent=True)
    def artifactory_update_permission_target(
        name: str, repositories: list[str], groups: dict[str, list[str]]
    ) -> dict[str, Any]:
        """Replace the repositories and group grants of an existing permission target."""
        target = art.permission_target(name)
        if target is None:
            raise ToolError(f"permission target '{name}' not found")
        for key in repositories:
            require_repo(key)
        target.repositories = sorted(repositories)
        target.groups = check_groups(groups)
        return target.model_dump()

    return server
