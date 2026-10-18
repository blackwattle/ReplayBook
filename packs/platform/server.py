"""Mock platform server: DNS, certificates, groups and CI.

Nothing here touches a real DNS zone, CA, directory or CI system.
"""

from __future__ import annotations

from typing import Any

from replaybook.mock_env.base import MockServer, ToolError
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.world import WorldState

from .world import PACK, CiVariable, DnsRecord, PlatformState

INSTRUCTIONS = (
    "Mock platform services: DNS, TLS certificates, access groups and CI settings. Reads "
    "answer from the case's world state; writes are recorded and applied to an in-memory "
    "copy only."
)


def build(world: WorldState, recorder: CallRecorder) -> MockServer:
    server = MockServer(PACK, INSTRUCTIONS, world, recorder)
    plat = PlatformState.model_validate(world.slice(PACK))

    def require_zone(zone: str):
        found = plat.zone(zone)
        if found is None:
            known = ", ".join(z.name for z in plat.zones) or "none"
            raise ToolError(f"zone '{zone}' not found. Known: {known}")
        return found

    def require_group(group: str):
        found = plat.group(group)
        if found is None:
            known = ", ".join(g.name for g in plat.groups) or "none"
            raise ToolError(f"group '{group}' not found. Known: {known}")
        return found

    def require_ci_project(project: str):
        found = plat.ci_project(project)
        if found is None:
            known = ", ".join(p.path for p in plat.ci_projects) or "none"
            raise ToolError(f"CI project '{project}' not found. Known: {known}")
        return found

    # --- DNS ------------------------------------------------------------------------

    @server.read
    def platform_dns_list_zones() -> dict[str, Any]:
        """List the DNS zones this platform manages."""
        return {"zones": [z.name for z in plat.zones]}

    @server.read
    def platform_dns_list_records(zone: str) -> dict[str, Any]:
        """List the records in a DNS zone."""
        found = require_zone(zone)
        return {"zone": zone, "records": [r.model_dump() for r in found.records]}

    @server.write(idempotent=True)
    def platform_dns_upsert_record(
        zone: str, name: str, type: str, value: str, ttl: int = 300
    ) -> dict[str, Any]:
        """Create or replace a DNS record (A, AAAA, CNAME, TXT) in a zone."""
        found = require_zone(zone)
        rtype = type.upper()
        if rtype not in ("A", "AAAA", "CNAME", "TXT", "SRV"):
            raise ToolError(f"unsupported record type '{type}'")
        found.records[:] = [r for r in found.records if not (r.name == name and r.type == rtype)]
        record = DnsRecord(name=name, type=rtype, value=value, ttl=ttl)
        found.records.append(record)
        return {"zone": zone, **record.model_dump()}

    @server.write(destructive=True)
    def platform_dns_delete_record(zone: str, name: str, type: str) -> dict[str, Any]:
        """Delete a DNS record. Destructive: clients lose resolution immediately."""
        found = require_zone(zone)
        rtype = type.upper()
        before = len(found.records)
        found.records[:] = [r for r in found.records if not (r.name == name and r.type == rtype)]
        if len(found.records) == before:
            raise ToolError(f"record {name} {rtype} not found in {zone}")
        return {"zone": zone, "deleted": {"name": name, "type": rtype}}

    # --- certificates -----------------------------------------------------------------

    @server.read
    def platform_cert_list() -> dict[str, Any]:
        """List managed TLS certificates with days until expiry."""
        return {"certificates": [c.model_dump() for c in plat.certificates]}

    @server.read
    def platform_cert_show(hostname: str) -> dict[str, Any]:
        """Show one managed certificate."""
        found = plat.certificate(hostname)
        if found is None:
            raise ToolError(f"no managed certificate for '{hostname}'")
        return found.model_dump()

    @server.write(idempotent=True)
    def platform_cert_renew(hostname: str, validity_days: int = 365) -> dict[str, Any]:
        """Renew a managed certificate and deploy it to the service."""
        found = plat.certificate(hostname)
        if found is None:
            raise ToolError(f"no managed certificate for '{hostname}'")
        if validity_days < 1 or validity_days > 397:
            raise ToolError("validity_days must be between 1 and 397")
        found.days_left = validity_days
        return {"hostname": hostname, "validity_days": validity_days, "renewed": True}

    # --- groups ---------------------------------------------------------------------

    @server.read
    def platform_group_list() -> dict[str, Any]:
        """List access groups; privileged groups are flagged."""
        return {"groups": [g.model_dump() for g in plat.groups]}

    @server.read
    def platform_group_list_members(group: str) -> dict[str, Any]:
        """List the members of a group."""
        found = require_group(group)
        return found.model_dump()

    @server.write(idempotent=True)
    def platform_group_add_member(group: str, user: str) -> dict[str, Any]:
        """Add a user to a group. Privileged groups need an approval reference in the ticket."""
        found = require_group(group)
        if user not in found.members:
            found.members.append(user)
        result: dict[str, Any] = {"group": group, "user": user, "members": list(found.members)}
        if found.privileged or group in plat.privileged_groups:
            result["warning"] = "privileged group"
        return result

    @server.write(idempotent=True)
    def platform_group_remove_member(group: str, user: str) -> dict[str, Any]:
        """Remove a user from a group."""
        found = require_group(group)
        if user not in found.members:
            raise ToolError(f"'{user}' is not a member of {group}")
        found.members.remove(user)
        return {"group": group, "user": user, "members": list(found.members)}

    # --- CI -------------------------------------------------------------------------

    @server.read
    def platform_ci_list_variables(project: str) -> dict[str, Any]:
        """List CI variables for a project (values of masked variables are hidden)."""
        found = require_ci_project(project)
        variables = []
        for v in found.variables:
            shown = v.model_dump()
            if v.masked:
                shown["value"] = "****"
            variables.append(shown)
        return {"project": project, "variables": variables}

    @server.read
    def platform_ci_runner_list() -> dict[str, Any]:
        """List CI runners and their status."""
        return {"runners": [r.model_dump() for r in plat.runners]}

    @server.write(idempotent=True)
    def platform_ci_set_variable(
        project: str, key: str, value: str, protected: bool = False, masked: bool = False
    ) -> dict[str, Any]:
        """Create or update a CI variable on a project."""
        found = require_ci_project(project)
        found.variables[:] = [v for v in found.variables if v.key != key]
        variable = CiVariable(key=key, value=value, protected=protected, masked=masked)
        found.variables.append(variable)
        return {"project": project, **variable.model_dump()}

    @server.write()
    def platform_ci_runner_restart(name: str) -> dict[str, Any]:
        """Restart a CI runner. Running jobs on it are cancelled."""
        runner = next((r for r in plat.runners if r.name == name), None)
        if runner is None:
            raise ToolError(f"runner '{name}' not found")
        runner.status = "online"
        return {"runner": name, "restarted": True}

    return server
