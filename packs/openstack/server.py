"""Mock OpenStack server. Nothing here talks to a real cloud."""

from __future__ import annotations

from typing import Any

from replaybook.mock_env.base import MockServer, ToolError
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.world import WorldState

from .world import PACK, OpenStackProject, OpenStackState, Server

INSTRUCTIONS = (
    "Mock OpenStack. Reads answer from the case's world state; writes are recorded and "
    "applied to an in-memory copy only. Quotas are managed with openstack_quota_set; the "
    "per-service nova/cinder quota tools are retired."
)
PER_SERVICE_RETIRED = (
    "the per-service quota tools were retired; use openstack_quota_set for all quota changes"
)


def build(world: WorldState, recorder: CallRecorder) -> MockServer:
    server = MockServer(PACK, INSTRUCTIONS, world, recorder)
    cloud = OpenStackState.model_validate(world.slice(PACK))

    def require_project(project: str) -> OpenStackProject:
        found = cloud.project(project)
        if found is None:
            known = ", ".join(p.name for p in cloud.projects) or "none"
            raise ToolError(f"project '{project}' not found. Known: {known}")
        return found

    def apply_quota(project: str, values: dict[str, int | None]) -> dict[str, Any]:
        found = require_project(project)
        changed = {k: v for k, v in values.items() if v is not None}
        if not changed:
            raise ToolError("no quota values given")
        for key, value in changed.items():
            if value < 0:
                raise ToolError(f"{key} must not be negative")
            found.quotas[key] = value
        return {"project": project, "quotas": dict(found.quotas), "changed": changed}

    @server.read
    def openstack_project_show(project: str) -> dict[str, Any]:
        """Show a project and its current quotas."""
        return require_project(project).model_dump()

    @server.read
    def openstack_quota_show(project: str) -> dict[str, Any]:
        """Show current quota limits for a project."""
        found = require_project(project)
        return {"project": project, "quotas": dict(found.quotas), "quota_tool": cloud.quota_tool}

    @server.read
    def openstack_server_list(project: str) -> dict[str, Any]:
        """List servers (VMs) in a project."""
        require_project(project)
        servers = [s.model_dump() for s in cloud.servers if s.project == project]
        return {"project": project, "servers": servers}

    @server.read
    def openstack_flavor_list() -> dict[str, Any]:
        """List available flavors (VM sizes)."""
        return {"flavors": list(cloud.flavors)}

    @server.read
    def openstack_image_list() -> dict[str, Any]:
        """List available images."""
        return {"images": list(cloud.images)}

    @server.read
    def openstack_network_list(project: str) -> dict[str, Any]:
        """List networks visible to a project."""
        require_project(project)
        return {"project": project, "networks": list(cloud.networks)}

    @server.read
    def openstack_security_group_list(project: str) -> dict[str, Any]:
        """List security groups and their rules in a project."""
        require_project(project)
        groups = [g.model_dump() for g in cloud.security_groups if g.project == project]
        return {"project": project, "security_groups": groups}

    @server.write(idempotent=True)
    def openstack_quota_set(
        project: str,
        cores: int | None = None,
        ram: int | None = None,
        instances: int | None = None,
        volumes: int | None = None,
        gigabytes: int | None = None,
    ) -> dict[str, Any]:
        """Set quota limits for a project (unified tool for compute and storage quotas)."""
        values = {
            "cores": cores,
            "ram": ram,
            "instances": instances,
            "volumes": volumes,
            "gigabytes": gigabytes,
        }
        return apply_quota(project, values)

    @server.write(idempotent=True)
    def openstack_nova_quota_update(
        project: str, cores: int | None = None, ram: int | None = None, instances: int | None = None
    ) -> dict[str, Any]:
        """Legacy per-service compute quota update. Retired once the unified tool is in use."""
        if cloud.quota_tool == "unified":
            raise ToolError(PER_SERVICE_RETIRED)
        return apply_quota(project, {"cores": cores, "ram": ram, "instances": instances})

    @server.write(idempotent=True)
    def openstack_cinder_quota_update(
        project: str, volumes: int | None = None, gigabytes: int | None = None
    ) -> dict[str, Any]:
        """Legacy per-service storage quota update. Retired once the unified tool is in use."""
        if cloud.quota_tool == "unified":
            raise ToolError(PER_SERVICE_RETIRED)
        return apply_quota(project, {"volumes": volumes, "gigabytes": gigabytes})

    @server.write()
    def openstack_server_create(
        project: str, name: str, flavor: str, image: str, network: str
    ) -> dict[str, Any]:
        """Create a server (VM) in a project."""
        require_project(project)
        if flavor not in cloud.flavors:
            raise ToolError(f"unknown flavor '{flavor}'. Known: {', '.join(cloud.flavors)}")
        if image not in cloud.images:
            raise ToolError(f"unknown image '{image}'. Known: {', '.join(cloud.images)}")
        if cloud.networks and network not in cloud.networks:
            raise ToolError(f"unknown network '{network}'. Known: {', '.join(cloud.networks)}")
        if any(s.name == name and s.project == project for s in cloud.servers):
            raise ToolError(f"server '{name}' already exists in {project}")
        created = Server(
            name=name, project=project, flavor=flavor, image=image, network=network, status="BUILD"
        )
        cloud.servers.append(created)
        return created.model_dump()

    @server.write(destructive=True)
    def openstack_server_delete(project: str, name: str) -> dict[str, Any]:
        """Delete a server and its ephemeral disks. Destructive and irreversible."""
        require_project(project)
        found = next((s for s in cloud.servers if s.name == name and s.project == project), None)
        if found is None:
            raise ToolError(f"server '{name}' not found in {project}")
        cloud.servers.remove(found)
        return {"project": project, "deleted": name}

    @server.write()
    def openstack_security_group_rule_create(
        project: str,
        security_group: str,
        protocol: str,
        port_min: int,
        port_max: int,
        remote_ip_prefix: str,
    ) -> dict[str, Any]:
        """Add an ingress rule to a security group."""
        require_project(project)
        group = cloud.security_group(security_group)
        if group is None or group.project != project:
            raise ToolError(f"security group '{security_group}' not found in {project}")
        if port_min > port_max:
            raise ToolError("port_min must not exceed port_max")
        rule = {
            "protocol": protocol.lower(),
            "port_min": port_min,
            "port_max": port_max,
            "remote_ip_prefix": remote_ip_prefix,
        }
        group.rules.append(rule)
        result: dict[str, Any] = {
            "project": project,
            "security_group": security_group,
            "rule": rule,
        }
        if remote_ip_prefix in ("0.0.0.0/0", "::/0"):
            result["warning"] = "rule is open to the whole internet"
        return result

    return server
