"""The built-in LiteLLM agent: a plain tool-calling loop that follows a SKILL.md.

It needs a model. Offline it reports `stopped_reason="offline"` and does nothing, so it
never silently passes or fails a case. It is offered the tools a skill-scoped agent is
given: the core ticket and knowledge tools plus the pack that owns the request type.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from replaybook.agents.base import AgentRunResult
from replaybook.llm.client import LLMResponse, ModelUnavailable, usage_summary
from replaybook.mock_env import knowledge, ticket
from replaybook.mock_env.registry import (
    ASK_TOOL,
    COMMENT_TOOL,
    ESCALATE_TOOL,
    GET_TOOL,
    MockEnvironment,
)
from replaybook.packs import registry
from replaybook.skills import load_skill

SYSTEM_RULES = f"""You are an operations agent working one support ticket in a mock environment.
Follow the skill below exactly. Rules that always apply:
- Act only by calling tools, one step at a time. A call written out in your reply text
  does not happen.
- Start by calling {GET_TOOL} to read the ticket.
- Use read tools to check the current state before you change anything. You may search
  how similar past tickets were handled with knowledge_search_tickets.
- If the ticket is missing information the skill says you need, call
  {ASK_TOOL} once with a clear question and then stop. Never guess.
- If the ticket needs a decision a person must make (an approval that is still needed,
  a possible clash with another team's work, or more than the skill covers), call
  {ESCALATE_TOOL} once with the reason and stop without changing anything. Asking the
  requester is for missing details; handing back is for decisions.
- Never delete or recreate existing resources unless the skill explicitly says to.
- When finished, call {COMMENT_TOOL} with a one-line summary and stop.
"""


def tools_for_llm(described: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"] or "",
                "parameters": tool["input_schema"],
            },
        }
        for tool in described
    ]


def text_tool_call(text: str | None, tool_names: set[str]) -> str | None:
    """The first tool a model named in a call it wrote out as JSON text instead of making
    it: a bare object, a code block, or JSON inside prose. A model route without tool
    calling hands calls back this way, and so do models that describe a plan instead of
    acting. None when the text holds no such call."""
    decoder = json.JSONDecoder()
    for start, char in enumerate(text or ""):
        if char not in "{[":
            continue
        try:
            data, _ = decoder.raw_decode(text or "", start)
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(item.get("function"), dict):
                name = name or item["function"].get("name")
            if name in tool_names:
                return name
    return None


class SkillAgent:
    name = "skill"

    def __init__(
        self, request_type: str, llm: Any, tier: str = "strong", max_steps: int = 12
    ) -> None:
        self.request_type = request_type
        self.skill_text = load_skill(request_type)
        # Every pack's tools at once (53 with the example packs, some 21,000 characters of
        # schema on every call) push the skill out of a small model's context and cost more
        # per step than one pack plus the core tools (13 to 18 with the example skills).
        pack = registry().request_types[request_type].pack
        self.servers = [ticket.SERVER_NAME, knowledge.SERVER_NAME, pack]
        self.llm = llm
        self.tier = tier
        self.max_steps = max_steps

    @property
    def config(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "version": 3,
            "request_type": self.request_type,
            "model": self.llm.model_for(self.tier),
            "skill_sha256": hashlib.sha256(self.skill_text.encode("utf-8")).hexdigest(),
            "max_steps": self.max_steps,
            "servers": self.servers,
        }

    def run(self, env: MockEnvironment) -> AgentRunResult:
        offered = [t for t in env.describe_tools() if t["server"] in self.servers]
        tools = tools_for_llm(offered)
        tool_names = {t["name"] for t in offered}
        current = env.call(GET_TOOL)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_RULES + "\n\n# Skill\n\n" + self.skill_text},
            {"role": "user", "content": "Ticket:\n" + json.dumps(current, indent=2)},
        ]
        transcript: list[dict[str, Any]] = [{"tool": GET_TOOL, "args": {}, "result": current}]
        model = self.llm.model_for(self.tier)
        responses: list[LLMResponse] = []
        for _ in range(self.max_steps):
            try:
                response = self.llm.complete(messages, tools=tools, tier=self.tier)
            except ModelUnavailable as exc:
                return AgentRunResult(
                    transcript=transcript,
                    stopped_reason="offline",
                    error=str(exc),
                    model=model,
                    usage=usage_summary(responses),
                )
            responses.append(response)
            named = None if response.tool_calls else text_tool_call(response.text, tool_names)
            if named:
                return AgentRunResult(
                    transcript=transcript,
                    final_message=response.text or "",
                    stopped_reason="error",
                    error=(
                        f"the model wrote a call to {named} as text instead of making a tool "
                        "call, so it never ran: the model route may not support tool calling "
                        "(for Ollama, name the model ollama_chat/<model>) or the model does "
                        "not use it"
                    ),
                    model=model,
                    usage=usage_summary(responses),
                )
            if not response.tool_calls:
                return AgentRunResult(
                    transcript=transcript,
                    final_message=response.text or "",
                    stopped_reason="done",
                    model=model,
                    usage=usage_summary(responses),
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": response.text,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in response.tool_calls
                    ],
                }
            )
            stopped = None
            for tc in response.tool_calls:
                result = env.call(tc.name, tc.arguments)
                transcript.append({"tool": tc.name, "args": tc.arguments, "result": result})
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)}
                )
                if "error" not in result:
                    if tc.name == ASK_TOOL:
                        stopped = stopped or "asked"
                    elif tc.name == ESCALATE_TOOL:
                        stopped = "escalated"
            if stopped is not None:
                default = (
                    "Handed the ticket back to a person."
                    if stopped == "escalated"
                    else "Asked the requester for more information."
                )
                return AgentRunResult(
                    transcript=transcript,
                    final_message=response.text or default,
                    stopped_reason=stopped,
                    model=model,
                    usage=usage_summary(responses),
                )
        return AgentRunResult(
            transcript=transcript,
            stopped_reason="max_steps",
            model=model,
            usage=usage_summary(responses),
        )
