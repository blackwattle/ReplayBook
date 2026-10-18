"""Verification checks: the read-only check that confirms each write a skill makes.

A skill's front matter maps every write tool it lists to a check:

    verify:
      widget_order:                              # a write the skill lists in `tools:`
        tool: widget_get                         # read-only by its MCP annotations
        args: {sku: "{sku}"}
        expect: {sku: "{sku}", quantity: "{quantity}"}
        timeout_s: 30                            # optional, default 30

`args` and `expect` are templates over the write's arguments: "{name}" is replaced by the
write's argument of that name (a string that is only a placeholder keeps the value's type,
so a list stays a list), and "{{" and "}}" are literal braces. An argument the write was
not given renders as null, and null parts of `expect` are not compared, so a check can
name optional arguments (a rendered check leaves such nulls out altogether). The core
ticket tools take no check: the ticket shows their effect.

Replay never runs a check. It renders each planned write's check and lists it on the case
result and in the evidence pack. A team's runtime runs it after the live write and
compares the read's result with `expect` through `compare` (or `replaybook checks
compare`), so no runtime has to reimplement the comparison; docs/verified-writes.md is the
contract.

The comparison: an object in `expect` must be a subset of the result (each key present and
matching), a list in `expect` must have each item match some item of the result's list, a
single value matches an equal value or a list that contains one, and values compare
ignoring case and surrounding space, with "6" equal to 6. A result with a top-level
`error` the expectation does not ask for, or no result at all, is a verify_error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from replaybook.mock_env.registry import TICKET_WRITE_TOOLS, describe_all_tools
from replaybook.packs import PackRegistry
from replaybook.skills import Skill, SkillError, find_skill

DEFAULT_TIMEOUT_S = 30.0
PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
WHOLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
VerifyStatus = Literal["verified", "verify_failed", "verify_error"]
STATUSES: tuple[str, ...] = ("verified", "verify_failed", "verify_error")


class SkillInvalid(ValueError):
    """A skill that fails validation; replay, shadow record and approval requests refuse it."""

    def __init__(self, request_type: str, problems: list[str]) -> None:
        self.request_type = request_type
        self.problems = problems
        listed = "; ".join(problems)
        super().__init__(
            f"the skill for {request_type} fails validation ({listed}); fix it and run "
            "`replaybook skills validate`"
        )


@dataclass(frozen=True)
class Check:
    write: str
    tool: str
    args: dict[str, Any]
    expect: Any
    timeout_s: float = DEFAULT_TIMEOUT_S

    def render(self, write_args: dict[str, Any]) -> dict[str, Any]:
        """The check for one write: the read tool, its arguments, the state it must show and
        the timeout, with the write's arguments filled in."""
        return {
            "tool": self.tool,
            "args": prune(render(self.args, write_args)),
            "expect": prune(render(self.expect, write_args)),
            "timeout_s": self.timeout_s,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "write": self.write,
            "tool": self.tool,
            "args": self.args,
            "expect": self.expect,
            "timeout_s": self.timeout_s,
        }


# --- templates ------------------------------------------------------------------------


def render(template: Any, values: dict[str, Any]) -> Any:
    if isinstance(template, str):
        whole = WHOLE.fullmatch(template)
        if whole:
            return values.get(whole.group(1))

        def sub(match: re.Match[str]) -> str:
            if match.group(0) == "{{":
                return "{"
            if match.group(0) == "}}":
                return "}"
            value = values.get(match.group(1))
            return "" if value is None else str(value)

        return PLACEHOLDER.sub(sub, template)
    if isinstance(template, dict):
        return {key: render(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [render(value, values) for value in template]
    return template


def prune(value: Any) -> Any:
    """Drop the nulls a rendering left for arguments the write was not given: an absent
    optional argument is neither passed to the read nor compared."""
    if isinstance(value, dict):
        return {k: prune(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [prune(v) for v in value if v is not None]
    return value


def placeholders(template: Any) -> set[str]:
    """The argument names a template refers to."""
    if isinstance(template, str):
        return {m.group(1) for m in PLACEHOLDER.finditer(template) if m.group(1)}
    if isinstance(template, dict):
        return {name for value in template.values() for name in placeholders(value)}
    if isinstance(template, list):
        return {name for value in template for name in placeholders(value)}
    return set()


# --- reading and validating a skill's checks --------------------------------------------


def parse_checks(skill: Skill) -> tuple[dict[str, Check], list[str]]:
    """The skill's checks by write tool, and the problems that stop an entry being read."""
    checks: dict[str, Check] = {}
    problems: list[str] = []
    if "_invalid" in skill.verify:
        return {}, ["`verify` must map each write tool to its check"]
    for write, spec in skill.verify.items():
        if not isinstance(spec, dict):
            problems.append(f"the check for {write} must be a mapping with tool, args and expect")
            continue
        tool = spec.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            problems.append(f"the check for {write} names no read tool (`tool:`)")
            continue
        args = spec.get("args") or {}
        if not isinstance(args, dict):
            problems.append(f"the check for {write}: `args` must be a mapping")
            continue
        if "expect" not in spec or spec.get("expect") in (None, {}, []):
            # Still read the entry, so validation can name what else is wrong with it.
            problems.append(
                f"the check for {write} has no `expect`: it would confirm nothing about the write"
            )
        timeout = spec.get("timeout_s", DEFAULT_TIMEOUT_S)
        try:
            timeout_s = float(timeout)
        except (TypeError, ValueError):
            timeout_s = -1.0
        if timeout_s <= 0:
            problems.append(
                f"the check for {write}: timeout_s must be a positive number of seconds"
            )
            continue
        checks[str(write)] = Check(
            write=str(write),
            tool=tool.strip(),
            args=dict(args),
            expect=spec.get("expect"),
            timeout_s=timeout_s,
        )
    return checks, problems


def validate_skill(skill: Skill, tools: list[dict[str, Any]] | None = None) -> list[str]:
    """Everything wrong with a skill's checks, empty when it is valid. A skill is invalid
    when a write tool it lists has no check, a check's tool is unknown or not read-only, a
    template names an argument the write does not have, or a check passes the read tool an
    argument it does not take (or leaves out one it needs)."""
    described = {t["name"]: t for t in (tools if tools is not None else describe_all_tools())}
    problems: list[str] = []
    for name in skill.tools:
        if name not in described:
            problems.append(f"`tools:` names {name}, which no loaded pack serves")
    writes = [
        name
        for name in skill.tools
        if name in described and not _read_only(described[name]) and name not in TICKET_WRITE_TOOLS
    ]
    checks, parse_problems = parse_checks(skill)
    problems.extend(parse_problems)
    for write in writes:
        if write not in checks and not any(
            p.startswith(f"the check for {write}") for p in parse_problems
        ):
            problems.append(f"the write {write} has no check under `verify:`")
    for write, check in checks.items():
        if write in TICKET_WRITE_TOOLS:
            problems.append(
                f"{write} is a ticket tool and takes no check: the ticket shows its effect"
            )
            continue
        if write not in writes:
            problems.append(f"`verify:` has a check for {write}, which is not a write in `tools:`")
            continue
        read = described.get(check.tool)
        if read is None:
            problems.append(f"the check for {write} uses {check.tool}, which no loaded pack serves")
            continue
        if not _read_only(read):
            problems.append(
                f"the check for {write} uses {check.tool}, which is not read-only (a check must "
                "not change anything)"
            )
            continue
        write_args = set(_properties(described[write]))
        unknown = sorted((placeholders(check.args) | placeholders(check.expect)) - write_args)
        if unknown:
            problems.append(
                f"the check for {write} names {', '.join('{' + u + '}' for u in unknown)}, "
                f"which {write} does not take"
            )
        read_props = _properties(read)
        extra = sorted(set(check.args) - set(read_props))
        if extra:
            problems.append(
                f"the check for {write} passes {', '.join(extra)} to {check.tool}, which does "
                "not take it"
            )
        needed = sorted(set(_required(read)) - set(check.args))
        if needed:
            problems.append(
                f"the check for {write} leaves out {', '.join(needed)}, which {check.tool} needs"
            )
    return problems


def skill_problems(
    request_type: str, packs: PackRegistry | None = None, tools: list[dict[str, Any]] | None = None
) -> list[str]:
    """The problems of a request type's skill; empty when it is valid or there is none."""
    try:
        skill = find_skill(request_type, packs)
    except SkillError as exc:
        return [str(exc)]
    if skill is None:
        return []
    return validate_skill(skill, tools if tools is not None else describe_all_tools(packs))


def require_valid_skill(request_type: str, tools: list[dict[str, Any]] | None = None) -> None:
    """Raise SkillInvalid when the request type's skill fails validation. Replay, shadow
    record and approval requests call this before they start."""
    problems = skill_problems(request_type, tools=tools)
    if problems:
        raise SkillInvalid(request_type, problems)


def checks_for(request_type: str) -> dict[str, Check]:
    """The request type's checks by write tool (empty without a skill)."""
    skill = find_skill(request_type)
    if skill is None:
        return {}
    return parse_checks(skill)[0]


def planned_checks(checks: dict[str, Check], writes: list[Any]) -> list[dict[str, Any]]:
    """Each infrastructure write an agent made (or proposed), with the check that would
    confirm it live. A write the skill has no check for is listed with check None: it is
    outside the skill, which the score already says. Writes the mock rejected are left out."""
    planned = []
    for write in writes:
        if write.tool in TICKET_WRITE_TOOLS or write.error is not None:
            continue
        check = checks.get(write.tool)
        planned.append(
            {
                "write": {"tool": write.tool, "args": dict(write.args)},
                "check": check.render(write.args) if check is not None else None,
                "note": ""
                if check is not None
                else "no check declared: the skill does not list this write",
            }
        )
    return planned


def _read_only(tool: dict[str, Any]) -> bool:
    return bool((tool.get("annotations") or {}).get("read_only_hint"))


def _properties(tool: dict[str, Any]) -> dict[str, Any]:
    return dict(((tool.get("input_schema") or {}).get("properties")) or {})


def _required(tool: dict[str, Any]) -> list[str]:
    return list(((tool.get("input_schema") or {}).get("required")) or [])


# --- the comparison -------------------------------------------------------------------


@dataclass
class CheckOutcome:
    status: VerifyStatus
    differences: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "differences": list(self.differences)}


def compare(expect: Any, result: Any) -> CheckOutcome:
    """Compare a read's result with a rendered `expect`: verified, verify_failed (the state
    differs) or verify_error (the check returned an error or nothing)."""
    if result is None:
        return CheckOutcome("verify_error", ["the check returned no result"])
    if isinstance(result, dict) and "error" in result and not _asks_for_error(expect):
        return CheckOutcome("verify_error", [f"the check returned an error: {result['error']}"])
    differences: list[str] = []
    _match(expect, result, "", differences)
    return CheckOutcome("verify_failed" if differences else "verified", differences)


def _asks_for_error(expect: Any) -> bool:
    return isinstance(expect, dict) and "error" in expect


def _match(expect: Any, actual: Any, path: str, differences: list[str]) -> bool:
    where = path or "the result"
    if expect is None:
        return True
    if isinstance(expect, dict):
        if not isinstance(actual, dict):
            differences.append(f"{where}: expected an object, got {_show(actual)}")
            return False
        ok = True
        for key, value in expect.items():
            if value is None:
                continue
            if key not in actual:
                differences.append(f"{_join(path, key)}: missing")
                ok = False
                continue
            ok = _match(value, actual[key], _join(path, key), differences) and ok
        return ok
    if isinstance(expect, list):
        if not isinstance(actual, list):
            differences.append(f"{where}: expected a list, got {_show(actual)}")
            return False
        ok = True
        for item in expect:
            if item is None:
                continue
            if not any(_match(item, candidate, path, []) for candidate in actual):
                differences.append(f"{where}: no item matches {_show(item)}")
                ok = False
        return ok
    if isinstance(actual, list):
        if any(_same(expect, candidate) for candidate in actual):
            return True
        differences.append(f"{where}: {_show(expect)} is not in {_show(actual)}")
        return False
    if _same(expect, actual):
        return True
    differences.append(f"{where}: expected {_show(expect)}, got {_show(actual)}")
    return False


def _same(a: Any, b: Any) -> bool:
    return _canonical(a) == _canonical(b)


def _canonical(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "false"):
            return text == "true"
        try:
            return float(text)
        except ValueError:
            return text
    return value


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else str(key)


def _show(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= 120 else text[:117] + "..."
