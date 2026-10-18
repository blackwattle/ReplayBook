"""The built-in LiteLLM agent: a plain tool-calling loop that follows a SKILL.md.

It needs a model. Offline it reports `stopped_reason="offline"` and does nothing, so it
never silently passes or fails a case.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from replaybook.agents.base import AgentRunResult
from replaybook.llm.client import ModelUnavailable
from replaybook.mock_env.registry import ASK_TOOL, COMMENT_TOOL, GET_TOOL, MockEnvironment
from replaybook.skills import load_skill

SYSTEM_RULES = f"""You are an operations agent working one support ticket in a mock environment.
Follow the skill below exactly. Rules that always apply:
- Start by calling {GET_TOOL} to read the ticket.
- Use read tools to check the current state before you change anything. You may search
  how similar past tickets were handled with knowledge_search_tickets.
- If the ticket is missing information the skill says you need, call
  {ASK_TOOL} once with a clear question and then stop. Never guess.
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


class SkillAgent:
    name = "skill"

    def __init__(
        self, request_type: str, llm: Any, tier: str = "strong", max_steps: int = 12
    ) -> None:
        self.request_type = request_type
        self.skill_text = load_skill(request_type)
        self.llm = llm
        self.tier = tier
        self.max_steps = max_steps

    @property
    def config(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "version": 1,
            "request_type": self.request_type,
            "model": self.llm.model_for(self.tier),
            "skill_sha256": hashlib.sha256(self.skill_text.encode("utf-8")).hexdigest(),
            "max_steps": self.max_steps,
        }

    def run(self, env: MockEnvironment) -> AgentRunResult:
        tools = tools_for_llm(env.describe_tools())
        ticket = env.call(GET_TOOL)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_RULES + "\n\n# Skill\n\n" + self.skill_text},
            {"role": "user", "content": "Ticket:\n" + json.dumps(ticket, indent=2)},
        ]
        transcript: list[dict[str, Any]] = [{"tool": GET_TOOL, "args": {}, "result": ticket}]
        model = self.llm.model_for(self.tier)
        for _ in range(self.max_steps):
            try:
                response = self.llm.complete(messages, tools=tools, tier=self.tier)
            except ModelUnavailable as exc:
                return AgentRunResult(
                    transcript=transcript, stopped_reason="offline", error=str(exc), model=model
                )
            if not response.tool_calls:
                return AgentRunResult(
                    transcript=transcript,
                    final_message=response.text or "",
                    stopped_reason="done",
                    model=model,
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
            asked = False
            for tc in response.tool_calls:
                result = env.call(tc.name, tc.arguments)
                transcript.append({"tool": tc.name, "args": tc.arguments, "result": result})
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)}
                )
                if tc.name == ASK_TOOL:
                    asked = True
            if asked:
                return AgentRunResult(
                    transcript=transcript,
                    final_message=response.text or "Asked the requester for more information.",
                    stopped_reason="asked",
                    model=model,
                )
        return AgentRunResult(transcript=transcript, stopped_reason="max_steps", model=model)
