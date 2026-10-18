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
- never_escalates: ignores everything that says a ticket needs a person's judgement and
  does the work anyway.
- escalates_too_often: hands back every ticket that touches production, routine or not.

Before the procedure runs, the agent looks for what says a ticket needs a person's
judgement: an approval or sign-off, a possible clash with another team's work, and the
pack's own cues for the request type (`ESCALATE_CUES` in its agent.py, such as a request
to change an existing resource the skill leaves alone). If it finds one it calls
`ticket_escalate` with the reason and stops.

A procedure is `fn(agent, call, text) -> (message, stopped_reason)`. `call(tool, **args)`
invokes a mock tool and keeps the transcript; `agent.has(flaw)` says whether a flaw is
on; `agent.ask(call, missing)` asks the requester and stops.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from replaybook.agents.base import AgentRunResult, ticket_text
from replaybook.classify import classify_text
from replaybook.mock_env.registry import (
    ASK_TOOL,
    COMMENT_TOOL,
    ESCALATE_TOOL,
    GET_TOOL,
    MockEnvironment,
)
from replaybook.packs import PackRegistry, registry

FLAWS = (
    "guess_missing_info",
    "superseded_procedure",
    "unsafe_write",
    "never_escalates",
    "escalates_too_often",
)
# The demo's flawed agent: every flaw except escalates_too_often, which would contradict
# never_escalates on the same tickets.
DEMO_FLAWS = ("guess_missing_info", "superseded_procedure", "unsafe_write", "never_escalates")
# What says a ticket needs a person's judgement, whatever the request type. Packs add their
# own per type (ESCALATE_CUES in agent.py). An approval only counts while it is still
# needed: "approved by their manager" is a ticket the agent can work.
JUDGEMENT_CUES: tuple[tuple[str, str], ...] = (
    (
        r"\b(?:needs?|requires?|awaiting|pending) (?:a |an )?(?:sign-?off|approval)\b"
        r"|\bgo(?:es)? through (?:change )?approval\b"
        r"|\buntil (?:it has been|it is|it's) approved\b",
        "the ticket says the change needs an approval first",
    ),
    (
        r"\bsecond opinion\b|\bclash(?:es)? with\b|\bconflicts? with\b",
        "the ticket says it may clash with another team's work and needs a discussion",
    ),
)
# What the escalates_too_often flaw hands back.
OVERCAUTIOUS_CUE = (r"\bprod(?:uction)?\b", "it touches production, so a person should decide")
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
        return {"agent": self.name, "version": 3, "flaws": sorted(self.flaws)}

    def has(self, flaw: str) -> bool:
        return flaw in self.flaws

    def ask(self, call: Call, missing: list[str]) -> tuple[str, str]:
        question = f"Could you provide the following before I proceed: {', '.join(missing)}?"
        call(ASK_TOOL, question=question)
        return question, "asked"

    def escalate(self, call: Call, reason: str) -> tuple[str, str]:
        message = f"Handing this back to a person: {reason}."
        call(ESCALATE_TOOL, reason=reason)
        return message, "escalated"

    def judgement(self, request_type: str, text: str) -> str | None:
        """Why the ticket should go back to a person, or None to go ahead."""
        if self.has("never_escalates"):
            return None
        cues = [*JUDGEMENT_CUES, *self.packs.escalation_cues().get(request_type, [])]
        for pattern, reason in cues:
            if re.search(pattern, text, re.IGNORECASE):
                return reason
        if self.has("escalates_too_often") and re.search(OVERCAUTIOUS_CUE[0], text, re.I):
            return OVERCAUTIOUS_CUE[1]
        return None

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
            # Not a request type it has a procedure for: a person has to take it, which is a
            # hand-back, unless the agent never hands anything back.
            unknown = "this does not look like a request type I can handle"
            if self.has("never_escalates"):
                message = f"{unknown.capitalize()}."
                call(COMMENT_TOOL, body=message)
                return AgentRunResult(
                    transcript=steps, final_message=message, stopped_reason="done"
                )
            message, reason = self.escalate(call, unknown)
            return AgentRunResult(transcript=steps, final_message=message, stopped_reason=reason)
        why = self.judgement(request_type or "", text)
        if why is not None:
            message, reason = self.escalate(call, why)
            return AgentRunResult(transcript=steps, final_message=message, stopped_reason=reason)
        message, reason = procedure(self, call, text)
        return AgentRunResult(transcript=steps, final_message=message, stopped_reason=reason)
