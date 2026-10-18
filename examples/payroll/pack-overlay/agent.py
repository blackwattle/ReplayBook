"""Scripted procedure for pay rate changes, added by a person after the import.

It follows skills/pay_rate_change/SKILL.md: read the employee, set the rate, leave a note.
With the guess_missing_info flaw it invents a rate when the ticket has none; with
unsafe_write it deletes and recreates nothing (there is no such shortcut here), so only
the first flaw shows on this pack.
"""

from __future__ import annotations

import re
from typing import Any

from replaybook.agents.parse import first
from replaybook.mock_env.registry import COMMENT_TOOL
from replaybook.schema.ground_truth import ActionSpec


def parse_pay_rate_change(text: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    employee = first([r"\b(E\d{4})\b"], text)
    if employee:
        found["employee_id"] = employee.upper()
    rate = first(
        [
            r"\$\s?(\d+(?:\.\d+)?)\s*(?:/|per)\s*h(?:ou)?r",
            r"\b(\d+(?:\.\d+)?)\s*(?:/|per)\s*h(?:ou)?r",
            r"\brate\s*(?:to|of|[:=])\s*\$?(\d+(?:\.\d+)?)",
        ],
        text,
    )
    if rate:
        found["hourly_rate"] = float(rate)
    effective = re.search(r"\b(?:effective|from)\s+(?:from\s+)?(\d{4}-\d{2}-\d{2})", text, re.I)
    if effective:
        found["effective_from"] = effective.group(1)
    return found


def pay_rate_change(agent: Any, call: Any, text: str) -> tuple[str, str]:
    p = parse_pay_rate_change(text)
    missing = [f for f in ("employee_id", "hourly_rate") if f not in p]
    if missing:
        if not agent.has("guess_missing_info") or "employee_id" not in p:
            return agent.ask(call, missing)
        current = call("payroll_get_employee", employee_id=p["employee_id"])
        p["hourly_rate"] = round(float(current.get("hourly_rate", 40.0)) * 1.05, 2)  # the guess
    employee = call("payroll_get_employee", employee_id=p["employee_id"])
    if "error" in employee:
        question = f"Employee {p['employee_id']} is not in payroll; which id did you mean?"
        return agent.ask(call, [question])
    args: dict[str, Any] = {"employee_id": p["employee_id"], "hourly_rate": p["hourly_rate"]}
    if "effective_from" in p:
        args["effective_from"] = p["effective_from"]
    call("payroll_set_pay_rate", **args)
    message = f"Set {p['employee_id']} to {p['hourly_rate']:.2f} per hour."
    call(COMMENT_TOOL, body=message)
    return message, "done"


def rule_pay_rate_change(text: str) -> list[ActionSpec]:
    p = parse_pay_rate_change(text)
    if "employee_id" not in p or "hourly_rate" not in p:
        return []
    args = {"employee_id": p["employee_id"], "hourly_rate": p["hourly_rate"]}
    if "effective_from" in p:
        args["effective_from"] = p["effective_from"]
    return [ActionSpec(tool="payroll_set_pay_rate", args=args)]


def seed_inventory(actions: list[ActionSpec]) -> dict[str, Any]:
    """Make the named employees exist in the recording mock's canned reads."""
    for action in actions:
        if action.tool == "payroll_set_pay_rate" and action.args.get("employee_id"):
            employee = action.args["employee_id"]
            return {
                "payroll": {
                    "reads": {
                        "payroll_get_employee": {
                            "employee_id": employee,
                            "name": "(seeded)",
                            "hourly_rate": 40.0,
                        }
                    }
                }
            }
    return {}


PROCEDURES = {"pay_rate_change": pay_rate_change}
EXTRACTORS = {"pay_rate_change": rule_pay_rate_change}
