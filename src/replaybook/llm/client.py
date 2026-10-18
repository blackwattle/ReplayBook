"""All model calls go through here, and through LiteLLM.

Rules from the spec: models come from .env; with no key and no local Ollama, run offline;
a hard spend cap switches to offline mode when reached; every response is cached on disk
by hash. The `litellm` package is an optional extra, imported lazily, so offline mode
never needs it.

Gateways: REPLAYBOOK_MODEL_API_BASE sends every call to one OpenAI-compatible endpoint (a
LiteLLM proxy, a cloud API gateway, an internal model server), whatever provider prefix the
model names carry, the way OLLAMA_API_BASE does for Ollama; REPLAYBOOK_MODEL_API_KEY is the
gateway's own key when it has one. Behind a gateway LiteLLM often cannot price a call. The
cost is then recorded as unknown (never as zero), or as reported by the gateway when it
sends one back, and the ledger, `replaybook status` and the evidence pack say which.

Ollama: name models `ollama_chat/<model>`. That route uses Ollama's chat endpoint, which
returns tool calls as tool calls; the older `ollama/` route goes through the generate
endpoint, where a model's tool call comes back as JSON text that no agent can act on.
Both get OLLAMA_API_BASE.
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
OLLAMA_PREFIXES = ("ollama_chat/", "ollama/")
# How a call's cost was established. measured: LiteLLM priced it from its model table.
# gateway: the gateway reported it in a response header. unknown: nobody could say.
COST_SOURCES = ("measured", "gateway", "unknown")
# After this many consecutive failed calls the client switches to offline mode.
MAX_CONSECUTIVE_FAILURES = 3


class ModelUnavailable(Exception):
    """Raised when a call cannot be made: offline mode, no provider, or spend cap reached."""


@dataclass(frozen=True)
class LLMConfig:
    model_fast: str = "ollama_chat/llama3.1"
    model_strong: str = "ollama_chat/llama3.1"
    max_spend_usd: float = 20.0
    offline: bool = False
    cache_dir: Path = Path(".replaybook/cache")
    ollama_base: str = DEFAULT_OLLAMA
    # One OpenAI-compatible gateway for every call, whatever the provider prefix.
    api_base: str | None = None
    api_key: str | None = None

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
            api_base=os.environ.get("REPLAYBOOK_MODEL_API_BASE", "").strip() or None,
            api_key=os.environ.get("REPLAYBOOK_MODEL_API_KEY", "").strip() or None,
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
    # None means unknown: nobody could price the call. Never zero for "don't know".
    cost_usd: float | None = 0.0
    cost_source: str = "measured"

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [tc.__dict__ for tc in self.tool_calls],
            "model": self.model,
            "cost_usd": self.cost_usd,
            "cost_source": self.cost_source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], cached: bool = False) -> LLMResponse:
        cost = data.get("cost_usd", 0.0)
        return cls(
            text=data.get("text"),
            tool_calls=[ToolCall(**tc) for tc in data.get("tool_calls", [])],
            model=data.get("model", ""),
            cached=cached,
            cost_usd=None if cost is None else float(cost),
            cost_source=str(data.get("cost_source", "unknown" if cost is None else "measured")),
        )


def ollama_available(base: str = DEFAULT_OLLAMA, timeout: float = 0.5) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


def provider_key_present() -> bool:
    return any(os.environ.get(name) for name in PROVIDER_KEYS)


def is_ollama(model: str) -> bool:
    return model.startswith(OLLAMA_PREFIXES)


def usage_summary(responses: list[LLMResponse]) -> dict[str, Any]:
    """What a run records about its model calls: how many, what they cost where that is
    known, how many nobody could price, and where the figure came from."""
    known = [r.cost_usd for r in responses if r.cost_usd is not None]
    sources = sorted({r.cost_source for r in responses if r.cost_usd is not None})
    return {
        "calls": len(responses),
        "cached_calls": sum(r.cached for r in responses),
        "cost_usd": round(sum(known), 6) if responses else 0.0,
        "unknown_cost_calls": sum(r.cost_usd is None for r in responses),
        "cost_sources": sources,
        "models": sorted({r.model for r in responses if r.model}),
    }


class LLMClient:
    def __init__(self, config: LLMConfig | None = None, check_providers: bool = True) -> None:
        netguard.install_from_env()
        self.config = config or LLMConfig.from_env()
        self.cache_dir = Path(self.config.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._ledger_path = self.cache_dir / "spend.json"
        self.offline_reason: str | None = None
        self.consecutive_failures = 0
        if self.config.offline:
            self.offline_reason = "REPLAYBOOK_OFFLINE is set"
        elif self.config.api_base:
            pass  # a gateway is configured; its own errors show up at call time
        elif check_providers:
            needs_ollama = is_ollama(self.config.model_fast) or is_ollama(self.config.model_strong)
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

    # --- ledger ---------------------------------------------------------------------------

    def ledger(self) -> dict[str, Any]:
        """Spend so far across every run that used this cache directory."""
        empty = {"spent_usd": 0.0, "calls": 0, "unknown_cost_calls": 0, "gateway_reported_usd": 0.0}
        if not self._ledger_path.exists():
            return empty
        try:
            data = json.loads(self._ledger_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return empty
        return {k: type(v)(data.get(k, v)) for k, v in empty.items()}

    @property
    def spent_usd(self) -> float:
        return float(self.ledger()["spent_usd"])

    def _record_spend(self, response: LLMResponse) -> None:
        ledger = self.ledger()
        ledger["calls"] += 1
        if response.cost_usd is None:
            ledger["unknown_cost_calls"] += 1
        else:
            ledger["spent_usd"] = round(ledger["spent_usd"] + response.cost_usd, 8)
            if response.cost_source == "gateway":
                ledger["gateway_reported_usd"] = round(
                    ledger["gateway_reported_usd"] + response.cost_usd, 8
                )
        self._ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
        if ledger["spent_usd"] >= self.config.max_spend_usd:
            self.offline_reason = f"spend cap of {self.config.max_spend_usd} USD reached"
            log.warning(
                "Spend cap reached (%.4f USD); switching to offline mode", ledger["spent_usd"]
            )

    def spend_line(self) -> str:
        """One line for `replaybook status`: what was spent and what could not be priced."""
        ledger = self.ledger()
        line = (
            f"spend so far: {ledger['spent_usd']:.4f} USD over {ledger['calls']} call(s), "
            f"cap {self.config.max_spend_usd} USD"
        )
        if ledger["gateway_reported_usd"]:
            line += f" ({ledger['gateway_reported_usd']:.4f} USD of that reported by the gateway)"
        if ledger["unknown_cost_calls"]:
            line += (
                f"; {ledger['unknown_cost_calls']} call(s) could not be priced (cost unknown, "
                "not zero: the cap cannot see them, so rely on the gateway's own budget)"
            )
        return line

    def model_for(self, tier: str) -> str:
        return self.config.model_strong if tier == "strong" else self.config.model_fast

    # --- calls ----------------------------------------------------------------------------

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
        try:
            response = self._call_model(model, messages, tools, temperature)
        except ModelUnavailable:
            raise
        except Exception as exc:  # LiteLLM's own exception types; never let one kill a run
            self.consecutive_failures += 1
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self.offline_reason = (
                    f"{self.consecutive_failures} model calls failed in a row; last error: "
                    f"{type(exc).__name__}: {str(exc)[:200]}"
                )
                log.warning("Switching to offline mode: %s", self.offline_reason)
            raise ModelUnavailable(f"model call failed: {type(exc).__name__}: {exc}") from exc
        self.consecutive_failures = 0
        self._write_cache(key, response)
        self._record_spend(response)
        return response

    def _call_model(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float,
    ) -> LLMResponse:
        # Replaybook sends nothing anywhere; LiteLLM must not either. Without the local cost
        # map it fetches its price table from the internet on import, and prices would follow
        # whatever is online that day instead of the pinned package. Both are set before
        # the import because LiteLLM reads them there.
        os.environ.setdefault("LITELLM_TELEMETRY", "False")
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        try:
            import litellm
        except ImportError as exc:
            raise ModelUnavailable(
                "litellm is not installed; run `uv sync --extra models` for real-model runs"
            ) from exc
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        kwargs: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
        if self.config.api_base:
            kwargs["api_base"] = self.config.api_base
            if self.config.api_key:
                kwargs["api_key"] = self.config.api_key
        elif is_ollama(model):
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
        cost, source = price(litellm, raw, model)
        return LLMResponse(
            text=choice.content,
            tool_calls=tool_calls,
            model=model,
            cost_usd=cost,
            cost_source=source,
        )

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


def price(litellm: Any, raw: Any, model: str = "") -> tuple[float | None, str]:
    """What the call cost and who says so. LiteLLM's own pricing first; then a cost the
    gateway reported in a response header; otherwise unknown, never zero."""
    try:
        cost = litellm.completion_cost(completion_response=raw)
        if cost is not None:
            return float(cost), "measured"
    except Exception:
        pass
    if model.startswith("ollama_chat/"):
        # LiteLLM's table lists Ollama models under ollama/ only. ollama_chat/ is the same
        # model on the same server, so it takes that price; an unlisted model stays unknown.
        try:
            prompt, completion = litellm.cost_per_token(
                model="ollama/" + model.removeprefix("ollama_chat/"),
                prompt_tokens=raw.usage.prompt_tokens,
                completion_tokens=raw.usage.completion_tokens,
            )
            return float(prompt + completion), "measured"
        except Exception:
            pass
    reported = gateway_reported_cost(raw)
    if reported is not None:
        return reported, "gateway"
    return None, "unknown"


def gateway_reported_cost(raw: Any) -> float | None:
    """A cost the gateway put in a response header (a LiteLLM proxy sends
    x-litellm-response-cost; LiteLLM surfaces headers under _hidden_params)."""
    hidden = getattr(raw, "_hidden_params", None) or {}
    headers = hidden.get("additional_headers") or {}
    for name, value in headers.items():
        if "response-cost" in str(name).lower() or "response_cost" in str(name).lower():
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None
