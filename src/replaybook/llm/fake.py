"""A fake model for tests: scripted responses, no network, same interface as LLMClient."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from replaybook.llm.client import LLMResponse, ModelUnavailable, ToolCall

Responder = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None], LLMResponse]


class FakeLLM:
    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        responder: Responder | None = None,
        offline: bool = False,
    ) -> None:
        self._responses = list(responses or [])
        self._responder = responder
        self.offline = offline
        self.offline_reason = "fake offline" if offline else None
        self.calls: list[dict[str, Any]] = []

    def model_for(self, tier: str) -> str:
        return f"fake/{tier}"

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tier: str = "fast",
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "tier": tier})
        if self.offline:
            raise ModelUnavailable("fake offline")
        if self._responder is not None:
            return self._responder(messages, tools)
        if not self._responses:
            return LLMResponse(text="(no more scripted responses)", model=self.model_for(tier))
        return self._responses.pop(0)


def tool_call_response(*calls: tuple[str, dict[str, Any]]) -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(calls)
        ],
        model="fake/strong",
    )


def text_response(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="fake/strong")
