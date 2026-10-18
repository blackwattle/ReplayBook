"""The scripted reference agent: offline, deterministic, with switchable flaws.

The agent itself knows no tool. It reads the ticket, classifies it, and hands it to the
procedure the pack provides for that request type (`PROCEDURES` in the pack's agent.py).
Each procedure follows the current runbook when no flaw is switched on and shows a
realistic failure mode when one is:

- guess_missing_info: fills in missing parameters with "sensible" defaults instead of
  asking the requester.
- superseded_procedure: uses the tools from before the last procedure change regardless
  of what the platform says.
- unsafe_write: takes a destructive shortcut, such as deleting and recreating a resource
  that already exists with different settings.

A procedure is `fn(agent, call, text) -> (message, stopped_reason)`. `call(tool, **args)`
invokes a mock tool and keeps the transcript; `agent.has(flaw)` says whether a flaw is
on; `agent.ask(call, missing)` asks the requester and stops.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from replaybook.agents.base import AgentRunResult, ticket_text
from replaybook.classify import classify_text
from replaybook.mock_env.registry import ASK_TOOL, COMMENT_TOOL, GET_TOOL, MockEnvironment
from replaybook.packs import PackRegistry, registry

FLAWS = ("guess_missing_info", "superseded_procedure", "unsafe_write")
Call = Callable[..., dict[str, Any]]
Procedure = Callable[["ScriptedAgent", Call, str], tuple[str, str]]


def supported_types(packs: PackRegistry | None = None) -> tuple[str, ...]:
    """Request types that have a scripted procedure in some pack."""
    reg = packs if packs is not None else registry()
    return tuple(reg.procedures())


class ScriptedAgent:
    name = "scripted"

    def __init__(self, flaws: Iterable[str] = (), packs: PackRegistry | None = None) -> None:
        self.flaws = frozenset(flaws)
        unknown = self.flaws - set(FLAWS)
        if unknown:
            raise ValueError(f"unknown flaws: {sorted(unknown)}. Known: {FLAWS}")
        self._packs = packs

    @property
    def packs(self) -> PackRegistry:
        return self._packs if self._packs is not None else registry()

    @property
    def config(self) -> dict[str, Any]:
        return {"agent": self.name, "version": 2, "flaws": sorted(self.flaws)}

    def has(self, flaw: str) -> bool:
        return flaw in self.flaws

    def ask(self, call: Call, missing: list[str]) -> tuple[str, str]:
        question = f"Could you provide the following before I proceed: {', '.join(missing)}?"
        call(ASK_TOOL, question=question)
        return question, "asked"

    def run(self, env: MockEnvironment) -> AgentRunResult:
        steps: list[dict[str, Any]] = []

        def call(tool: str, **args: Any) -> dict[str, Any]:
            result = env.call(tool, args)
            steps.append({"tool": tool, "args": args, "result": result})
            return result

        ticket = call(GET_TOOL)
        if "error" in ticket:
            return AgentRunResult(transcript=steps, stopped_reason="error", error=ticket["error"])
        text = ticket_text(env)
        request_type, _ = classify_text(
            ticket["summary"],
            ticket["description"],
            ticket["labels"],
            ticket["components"],
            packs=self.packs,
        )
        procedure: Procedure | None = self.packs.procedures().get(request_type or "")
        if procedure is None:
            message = "This does not look like a request type I can handle; leaving it for a human."
            call(COMMENT_TOOL, body=message)
            return AgentRunResult(transcript=steps, final_message=message, stopped_reason="done")
        message, reason = procedure(self, call, text)
        return AgentRunResult(transcript=steps, final_message=message, stopped_reason=reason)
