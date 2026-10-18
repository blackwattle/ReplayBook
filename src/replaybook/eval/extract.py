"""Expected actions from ticket text, for histories that have no audit log.

Real ticket systems record prose, not tool calls. Rules work offline and a model is used
when one is reachable; either way the expectation is labelled with its source and
`verified=False`. A person has to confirm it before it counts as more than a hint, and the
evidence pack says how many expectations are unverified.

The rules live in the packs and share their parsers with the scripted procedures, so an
agent built on the same regular expressions agrees with rule-extracted expectations by
construction. That is why
the rules only ever produce unverified expectations: they are a starting point for review,
not a judge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from replaybook.packs import registry
from replaybook.schema.ground_truth import ActionSpec
from replaybook.schema.ticket import Ticket

Source = Literal["rules", "model"]


@dataclass
class Extracted:
    actions: list[ActionSpec]
    source: Source
    notes: str = ""


class Extractor(Protocol):
    name: str

    def extract(self, ticket: Ticket, request_type: str) -> Extracted | None: ...


def ticket_text(ticket: Ticket) -> str:
    """Summary, description and every comment: the request and what engineers said they did."""
    parts = [ticket.summary, ticket.description, *(c.body for c in ticket.comments)]
    return "\n".join(p for p in parts if p)


class RuleExtractor:
    """Regular expressions per request type, supplied by the packs (EXTRACTORS in their
    agent.py). Deterministic, offline, deliberately simple."""

    name = "rules"

    def extract(self, ticket: Ticket, request_type: str) -> Extracted | None:
        rule = registry().extract_rules().get(request_type)
        if rule is None:
            return None
        actions = rule(ticket_text(ticket))
        if not actions:
            return None
        return Extracted(actions=actions, source="rules", notes="extracted by rules from the text")


class ModelExtractor:
    """Ask the strong model for the tool calls the ticket describes; rules when it is offline."""

    name = "model"
    PROMPT = (
        "You read a resolved operations ticket and list the platform write actions the engineers "
        "performed, as tool calls. Use only the tools listed, with their argument names. Answer "
        'with JSON only: {"actions": [{"tool": "...", "args": {...}}]}. If the ticket does '
        'not describe a concrete write, answer {"actions": []}.'
    )

    def __init__(self, client: Any, tools: list[dict[str, Any]]) -> None:
        self.client = client
        self.tools = tools
        self.fallback = RuleExtractor()
        self.known = {str(t.get("name")) for t in tools}

    def extract(self, ticket: Ticket, request_type: str) -> Extracted | None:
        if getattr(self.client, "offline", False):
            return self.fallback.extract(ticket, request_type)
        from replaybook.llm.client import ModelUnavailable

        tool_text = "\n".join(
            f"- {t.get('name')}: {t.get('description', '')} "
            f"args {json.dumps(t.get('input_schema') or t.get('inputSchema') or {})}"
            for t in self.tools
        )
        messages = [
            {"role": "system", "content": self.PROMPT},
            {
                "role": "user",
                "content": (
                    f"Request type: {request_type}\nTools:\n{tool_text}\n\n"
                    f"Ticket:\n{ticket_text(ticket)[:6000]}"
                ),
            },
        ]
        try:
            response = self.client.complete(messages, tier="strong")
        except ModelUnavailable:
            return self.fallback.extract(ticket, request_type)
        actions = parse_model_actions(response.text or "", self.known)
        if not actions:
            return self.fallback.extract(ticket, request_type)
        return Extracted(actions=actions, source="model", notes=f"extracted by {response.model}")


def parse_model_actions(text: str, known_tools: set[str]) -> list[ActionSpec]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    actions: list[ActionSpec] = []
    for item in data.get("actions", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or item.get("tool") not in known_tools:
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        actions.append(ActionSpec(tool=str(item["tool"]), args=args))
    return actions


def make_extractor(kind: str) -> Extractor | None:
    """CLI helper: none, rules or model."""
    if kind in ("", "none"):
        return None
    if kind == "rules":
        return RuleExtractor()
    if kind == "model":
        from replaybook.llm.client import LLMClient
        from replaybook.mock_env.registry import describe_all_tools

        tools = [
            t for t in describe_all_tools() if not (t.get("annotations") or {}).get("readOnlyHint")
        ]
        return ModelExtractor(LLMClient(), tools)
    raise ValueError(f"unknown extractor '{kind}' (none, rules or model)")
