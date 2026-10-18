"""A fake payroll MCP server: the conformance test for a pack the core has never seen.

It is written with the MCP SDK only, imports nothing from Replaybook, and answers from a
small in-memory table. Replaybook imports its tool list with

    replaybook tools import --name payroll --command "python examples/payroll/server.py"

and gets a pack whose recording mock stands in for it during replay. This server itself is
never used during replay; it only exists so there is a real tool list to import.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer(name="payroll", instructions="Fake payroll system for the conformance test.")

EMPLOYEES: dict[str, dict[str, Any]] = {
    "E1001": {
        "employee_id": "E1001",
        "name": "Jordan Lee",
        "hourly_rate": 40.0,
        "cost_centre": "CC-10",
    },
    "E1002": {
        "employee_id": "E1002",
        "name": "Sam Patel",
        "hourly_rate": 52.0,
        "cost_centre": "CC-12",
    },
}


def read(title: str) -> ToolAnnotations:
    return ToolAnnotations(title=title, read_only_hint=True, destructive_hint=False)


def write(title: str, destructive: bool = False, idempotent: bool = False) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, read_only_hint=False, destructive_hint=destructive, idempotent_hint=idempotent
    )


@server.tool(annotations=read("get employee"))
def payroll_get_employee(employee_id: str) -> dict[str, Any]:
    """Look up an employee by id: name, current hourly rate, cost centre."""
    found = EMPLOYEES.get(employee_id)
    if found is None:
        return {"error": f"no employee {employee_id}"}
    return dict(found)


@server.tool(annotations=read("list pay rate history"))
def payroll_list_rate_history(employee_id: str) -> dict[str, Any]:
    """List the pay rate changes recorded for an employee."""
    return {"employee_id": employee_id, "history": []}


@server.tool(annotations=write("set pay rate", idempotent=True))
def payroll_set_pay_rate(
    employee_id: str, hourly_rate: float, effective_from: str | None = None
) -> dict[str, Any]:
    """Set an employee's hourly pay rate, optionally from a date (YYYY-MM-DD)."""
    found = EMPLOYEES.get(employee_id)
    if found is None:
        return {"error": f"no employee {employee_id}"}
    found["hourly_rate"] = hourly_rate
    return {
        "employee_id": employee_id,
        "hourly_rate": hourly_rate,
        "effective_from": effective_from,
    }


@server.tool(annotations=write("add allowance"))
def payroll_add_allowance(employee_id: str, code: str, amount: float) -> dict[str, Any]:
    """Add a recurring allowance (for example a travel allowance) to an employee."""
    return {"employee_id": employee_id, "code": code, "amount": amount}


@server.tool(annotations=write("delete employee", destructive=True))
def payroll_delete_employee(employee_id: str) -> dict[str, Any]:
    """Delete an employee record and its pay history. Destructive and irreversible."""
    EMPLOYEES.pop(employee_id, None)
    return {"deleted": employee_id}


if __name__ == "__main__":
    server.run("stdio")
