"""All model calls go through here, and through LiteLLM.

Rules from the spec: models come from .env; with no key and no local Ollama, run offline;
a hard spend cap switches to offline mode when reached; every response is cached on disk
by hash. The `litellm` package is an optional extra, imported lazily, so offline mode
never needs it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from replaybook import netguard

log = logging.getLogger(__name__)

PROVIDER_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "AZURE_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
)
DEFAULT_OLLAMA = "http://localhost:11434"


class ModelUnavailable(Exception):
    """Raised when a call cannot be made: offline mode, no provider, or spend cap reached."""


@dataclass(frozen=True)
class LLMConfig:
    model_fast: str = "ollama/llama3.1"
    model_strong: str = "ollama/llama3.1"
    max_spend_usd: float = 20.0
    offline: bool = False
    cache_dir: Path = Path(".replaybook/cache")
    ollama_base: str = DEFAULT_OLLAMA

    @classmethod
    def from_env(cls) -> LLMConfig:
        load_dotenv(override=False)
        return cls(
            model_fast=os.environ.get("REPLAYBOOK_MODEL_FAST", cls.model_fast),
            model_strong=os.environ.get("REPLAYBOOK_MODEL_STRONG", cls.model_strong),
            max_spend_usd=float(os.environ.get("REPLAYBOOK_MAX_SPEND_USD", cls.max_spend_usd)),
            offline=os.environ.get("REPLAYBOOK_OFFLINE", "false").lower() in ("1", "true", "yes"),
            cache_dir=Path(os.environ.get("REPLAYBOOK_CACHE_DIR", str(cls.cache_dir))),
            ollama_base=os.environ.get("OLLAMA_API_BASE", DEFAULT_OLLAMA),
        )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    cached: bool = False
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [tc.__dict__ for tc in self.tool_calls],
            "model": self.model,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], cached: bool = False) -> LLMResponse:
        return cls(
            text=data.get("text"),
            tool_calls=[ToolCall(**tc) for tc in data.get("tool_calls", [])],
            model=data.get("model", ""),
            cached=cached,
            cost_usd=float(data.get("cost_usd", 0.0)),
        )


def ollama_available(base: str = DEFAULT_OLLAMA, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def provider_key_present() -> bool:
    return any(os.environ.get(name) for name in PROVIDER_KEYS)


class LLMClient:
    def __init__(self, config: LLMConfig | None = None, check_providers: bool = True) -> None:
        netguard.install_from_env()
        self.config = config or LLMConfig.from_env()
        self.cache_dir = Path(self.config.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self.cache_dir / "spend.json"
        self.offline_reason: str | None = None
        if self.config.offline:
            self.offline_reason = "REPLAYBOOK_OFFLINE is set"
        elif check_providers:
            needs_ollama = self.config.model_fast.startswith(
                "ollama/"
            ) or self.config.model_strong.startswith("ollama/")
            if needs_ollama and not ollama_available(self.config.ollama_base):
                if not provider_key_present():
                    self.offline_reason = (
                        "no provider API key and no Ollama server on localhost:11434"
                    )
            elif not needs_ollama and not provider_key_present():
                self.offline_reason = "no provider API key found in the environment or .env"
        if self.spent_usd >= self.config.max_spend_usd:
            self.offline_reason = f"spend cap of {self.config.max_spend_usd} USD reached"
        if self.offline_reason:
            log.info("Replaybook model calls are offline: %s", self.offline_reason)

    @property
    def offline(self) -> bool:
        return self.offline_reason is not None

    @property
    def spent_usd(self) -> float:
        if not self._ledger_path.exists():
            return 0.0
        try:
            return float(
                json.loads(self._ledger_path.read_text(encoding="utf-8")).get("spent_usd", 0.0)
            )
        except (ValueError, OSError):
            return 0.0

    def _add_spend(self, cost: float) -> None:
        total = self.spent_usd + cost
        self._ledger_path.write_text(json.dumps({"spent_usd": total}), encoding="utf-8")
        if total >= self.config.max_spend_usd:
            self.offline_reason = f"spend cap of {self.config.max_spend_usd} USD reached"
            log.warning("Spend cap reached (%.4f USD); switching to offline mode", total)

    def model_for(self, tier: str) -> str:
        return self.config.model_strong if tier == "strong" else self.config.model_fast

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tier: str = "fast",
        temperature: float = 0.0,
    ) -> LLMResponse:
        """Chat completion with optional tools. Cached by hash; raises ModelUnavailable offline."""
        model = self.model_for(tier)
        key = self._cache_key(model, messages, tools, temperature)
        cached = self._read_cache(key)
        if cached is not None:
            return cached
        if self.offline:
            raise ModelUnavailable(self.offline_reason or "offline")
        response = self._call_model(model, messages, tools, temperature)
        self._write_cache(key, response)
        self._add_spend(response.cost_usd)
        return response

    def _call_model(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
    ) -> LLMResponse:
        try:
            import litellm
        except ImportError as exc:
            raise ModelUnavailable(
                "litellm is not installed; run `uv sync --extra models` for real-model runs"
            ) from exc
        # Replaybook sends nothing anywhere; LiteLLM must not either.
        os.environ.setdefault("LITELLM_TELEMETRY", "False")
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
        if model.startswith("ollama/"):
            kwargs["api_base"] = self.config.ollama_base
        raw = litellm.completion(**kwargs)
        choice = raw.choices[0].message
        tool_calls = []
        for tc in getattr(choice, "tool_calls", None) or []:
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=arguments))
        try:
            cost = float(litellm.completion_cost(completion_response=raw) or 0.0)
        except Exception:
            cost = 0.0
        return LLMResponse(text=choice.content, tool_calls=tool_calls, model=model, cost_usd=cost)

    # --- cache ------------------------------------------------------------------------

    @staticmethod
    def _cache_key(model: str, messages: Any, tools: Any, temperature: float) -> str:
        payload = json.dumps(
            {"model": model, "messages": messages, "tools": tools, "temperature": temperature},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_cache(self, key: str) -> LLMResponse | None:
        path = self.cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return LLMResponse.from_dict(json.loads(path.read_text(encoding="utf-8")), cached=True)
        except (ValueError, OSError):
            return None

    def _write_cache(self, key: str, response: LLMResponse) -> None:
        path = self.cache_dir / f"{key}.json"
        path.write_text(json.dumps(response.to_dict(), sort_keys=True), encoding="utf-8")
