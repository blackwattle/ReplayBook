"""Model gateways and honest cost accounting: one base-URL setting routes every call, a cost
nobody can price is recorded as unknown (never zero), and the status output, the run and the
evidence pack say so. Everything here uses a fake litellm module; no network."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.skill_agent import SkillAgent
from replaybook.cli import app
from replaybook.eval.accepted import AcceptedDifferences
from replaybook.eval.evidence import EvidenceInputs, RunBundle, build_evidence
from replaybook.eval.runner import load_run, model_usage, run_replay
from replaybook.eval.testset import build_test_set
from replaybook.llm.client import (
    MAX_CONSECUTIVE_FAILURES,
    LLMClient,
    LLMConfig,
    LLMResponse,
    ModelUnavailable,
    usage_summary,
)
from replaybook.llm.fake import FakeLLM, text_response, tool_call_response


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = None


class _Response:
    def __init__(self, content: str, headers: dict[str, str] | None = None) -> None:
        self.choices = [types.SimpleNamespace(message=_Message(content))]
        self._hidden_params = {"additional_headers": headers or {}}
        self.usage = types.SimpleNamespace(prompt_tokens=120, completion_tokens=30)


def fake_litellm(
    monkeypatch: pytest.MonkeyPatch,
    cost: float | None | Exception = 0.001,
    headers: dict[str, str] | None = None,
    fail: Exception | None = None,
) -> Any:
    """A stand-in for the litellm package: records the kwargs it was called with."""
    fake = types.ModuleType("litellm")
    fake.telemetry = True  # type: ignore[attr-defined]
    fake.suppress_debug_info = False  # type: ignore[attr-defined]
    fake.calls = []  # type: ignore[attr-defined]

    def completion(**kwargs: Any) -> _Response:
        fake.calls.append(kwargs)  # type: ignore[attr-defined]
        if fail is not None:
            raise fail
        return _Response("hello", headers)

    def completion_cost(completion_response: Any) -> float | None:
        if isinstance(cost, Exception):
            raise cost
        return cost

    fake.completion = completion  # type: ignore[attr-defined]
    fake.completion_cost = completion_cost  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", fake)
    return fake


def test_config_reads_the_gateway_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPLAYBOOK_MODEL_API_BASE", " https://models.internal/v1 ")
    monkeypatch.setenv("REPLAYBOOK_MODEL_API_KEY", "gw-key")
    config = LLMConfig.from_env()
    assert config.api_base == "https://models.internal/v1" and config.api_key == "gw-key"
    monkeypatch.setenv("REPLAYBOOK_MODEL_API_BASE", "")
    monkeypatch.delenv("REPLAYBOOK_MODEL_API_KEY")
    assert LLMConfig.from_env().api_base is None and LLMConfig.from_env().api_key is None


def test_gateway_base_url_routes_every_call_and_needs_no_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    fake = fake_litellm(monkeypatch, cost=0.002)
    client = LLMClient(
        LLMConfig(
            model_fast="openai/gpt-4.1-mini",
            model_strong="anthropic/some-model",
            cache_dir=tmp_path,
            api_base="https://models.internal/v1",
            api_key="gw-key",
        )
    )
    assert not client.offline, "a gateway is configured, so no provider key is required"
    client.complete([{"role": "user", "content": "a"}], tier="fast")
    client.complete([{"role": "user", "content": "b"}], tier="strong")
    assert [c["api_base"] for c in fake.calls] == ["https://models.internal/v1"] * 2
    assert [c["api_key"] for c in fake.calls] == ["gw-key"] * 2
    assert [c["model"] for c in fake.calls] == ["openai/gpt-4.1-mini", "anthropic/some-model"]
    assert client.ledger()["spent_usd"] == pytest.approx(0.004)
    assert "spend so far: 0.0040 USD over 2 call(s)" in client.spend_line()

    # Without a gateway, Ollama models still get the Ollama base and nothing else does.
    fake.calls.clear()
    plain = LLMClient(
        LLMConfig(model_fast="ollama/llama3.1", model_strong="openai/x", cache_dir=tmp_path / "b"),
        check_providers=False,
    )
    plain.complete([{"role": "user", "content": "a"}], tier="fast")
    plain.complete([{"role": "user", "content": "a"}], tier="strong")
    assert fake.calls[0]["api_base"] == plain.config.ollama_base
    assert "api_base" not in fake.calls[1] and "api_key" not in fake.calls[1]


def test_both_ollama_routes_get_the_ollama_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ollama_chat/ (the chat endpoint, tool calls come back as tool calls) is the default
    and is routed like the older ollama/ (generate endpoint, tool calls come back as text)."""
    assert LLMConfig().model_fast == LLMConfig().model_strong == "ollama_chat/llama3.1"
    fake = fake_litellm(monkeypatch)
    client = LLMClient(
        LLMConfig(
            model_fast="ollama_chat/llama3.1",
            model_strong="ollama/llama3.1",
            cache_dir=tmp_path,
            ollama_base="http://ollama:11434",
        ),
        check_providers=False,
    )
    client.complete([{"role": "user", "content": "a"}], tier="fast")
    client.complete([{"role": "user", "content": "a"}], tier="strong")
    assert [c["api_base"] for c in fake.calls] == ["http://ollama:11434"] * 2


def test_ollama_chat_takes_the_price_listed_for_the_same_ollama_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LiteLLM's table lists Ollama models under ollama/ only. The chat route is the same model
    on the same server, so it takes that price; a model the table lacks stays unknown."""
    fake = fake_litellm(monkeypatch, cost=Exception("This model isn't mapped yet"))
    table = {"ollama/llama3.1": (0.0, 0.0)}

    def cost_per_token(
        model: str, prompt_tokens: int, completion_tokens: int
    ) -> tuple[float, float]:
        if model not in table:
            raise Exception("This model isn't mapped yet")
        return table[model]

    fake.cost_per_token = cost_per_token  # type: ignore[attr-defined]
    client = LLMClient(
        LLMConfig(
            model_fast="ollama_chat/llama3.1",
            model_strong="ollama_chat/unlisted",
            cache_dir=tmp_path,
        ),
        check_providers=False,
    )
    listed = client.complete([{"role": "user", "content": "a"}], tier="fast")
    assert listed.cost_usd == 0.0 and listed.cost_source == "measured"
    unlisted = client.complete([{"role": "user", "content": "a"}], tier="strong")
    assert unlisted.cost_usd is None and unlisted.cost_source == "unknown"


def test_unpriceable_calls_are_unknown_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_litellm(monkeypatch, cost=Exception("This model isn't mapped yet"))
    client = LLMClient(
        LLMConfig(model_fast="openai/internal-model", cache_dir=tmp_path, api_base="http://gw/v1")
    )
    response = client.complete([{"role": "user", "content": "a"}])
    assert response.cost_usd is None and response.cost_source == "unknown"
    ledger = client.ledger()
    assert ledger == {
        "spent_usd": 0.0,
        "calls": 1,
        "unknown_cost_calls": 1,
        "gateway_reported_usd": 0.0,
    }
    assert "1 call(s) could not be priced (cost unknown, not zero" in client.spend_line()
    again = client.complete([{"role": "user", "content": "a"}])
    assert again.cached and again.cost_usd is None and again.cost_source == "unknown"
    summary = usage_summary([response, again])
    assert summary["unknown_cost_calls"] == 2 and summary["cost_sources"] == []
    assert summary["cached_calls"] == 1 and summary["cost_usd"] == 0.0


def test_gateway_reported_cost_is_kept_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_litellm(
        monkeypatch,
        cost=Exception("not mapped"),
        headers={"llm_provider-x-litellm-response-cost": "0.0125"},
    )
    client = LLMClient(
        LLMConfig(model_fast="openai/proxied", cache_dir=tmp_path, api_base="http://gw/v1")
    )
    response = client.complete([{"role": "user", "content": "a"}])
    assert response.cost_usd == pytest.approx(0.0125) and response.cost_source == "gateway"
    ledger = client.ledger()
    assert ledger["spent_usd"] == pytest.approx(0.0125)
    assert ledger["gateway_reported_usd"] == pytest.approx(0.0125)
    assert "reported by the gateway" in client.spend_line()


def test_repeated_failures_switch_to_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_litellm(monkeypatch, fail=RuntimeError("connection refused"))
    client = LLMClient(
        LLMConfig(model_fast="openai/x", cache_dir=tmp_path, api_base="http://dead-gateway/v1")
    )
    for n in range(MAX_CONSECUTIVE_FAILURES):
        with pytest.raises(ModelUnavailable, match="model call failed"):
            client.complete([{"role": "user", "content": str(n)}])
    assert client.offline and "failed in a row" in (client.offline_reason or "")
    with pytest.raises(ModelUnavailable, match="failed in a row"):
        client.complete([{"role": "user", "content": "another"}])


def test_status_reports_the_gateway_and_unknown_costs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPLAYBOOK_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("REPLAYBOOK_MODEL_API_BASE", "http://gw.internal/v1")
    monkeypatch.setenv("REPLAYBOOK_MODEL_FAST", "openai/internal-model")
    monkeypatch.setenv("REPLAYBOOK_AIRGAPPED", "false")
    (tmp_path / "spend.json").write_text(
        json.dumps(
            {"spent_usd": 0.5, "calls": 7, "unknown_cost_calls": 3, "gateway_reported_usd": 0.0}
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "gateway: every model call goes to http://gw.internal/v1" in result.output
    assert "online" in result.output
    assert "spend so far: 0.5000 USD over 7 call(s)" in result.output
    assert "3 call(s) could not be priced (cost unknown, not zero" in result.output


def test_run_and_evidence_carry_model_usage(small_dataset: Path, tmp_path: Path) -> None:
    tickets = load_tickets(small_dataset)
    platform = load_platform(small_dataset)
    test_set = build_test_set(tickets, platform, "kafka_topic", n=3, data_dir=str(small_dataset))
    ts_path = tmp_path / "ts.json"
    ts_path.write_text(test_set.model_dump_json(), encoding="utf-8")

    def responder(messages: list[dict[str, Any]], tools: Any) -> LLMResponse:
        if messages[-1]["role"] == "user":
            return tool_call_response(("ticket_comment", {"body": "looked"}))
        done = text_response("Done.")
        done.cost_usd = None  # the gateway could not price this one
        done.cost_source = "unknown"
        return done

    priced = FakeLLM(responder=responder)
    run_dir = tmp_path / "run"
    run = run_replay(test_set, SkillAgent("kafka_topic", priced), run_dir, workers=1)
    assert run.model_usage is not None
    assert run.model_usage["calls"] == 6 and run.model_usage["unknown_cost_calls"] == 3
    assert run.model_usage["cost_known"] is False
    assert run.model_usage["cost_sources"] == ["measured"]
    assert all(r.model_usage and r.model_usage["calls"] == 2 for r in run.results)
    assert load_run(run_dir).model_usage == run.model_usage
    assert model_usage([]) is None

    out = tmp_path / "evidence"
    build_evidence(
        EvidenceInputs(
            title="Gateway pack",
            bundles=[RunBundle(run=run, test_set=test_set, run_dir=run_dir, test_set_path=ts_path)],
            accepted=AcceptedDifferences(),
        ),
        out,
    )
    data = json.loads((out / "evidence.json").read_text(encoding="utf-8"))
    scope = data["scope"][0]
    assert scope["model_usage"]["unknown_cost_calls"] == 3
    assert "could not be priced" in scope["model_cost"] and "lower bound" in scope["model_cost"]
    assert any("unknown, not zero" in item for item in data["limitations"])
    html = (out / "evidence.html").read_text(encoding="utf-8")
    assert "Model usage, kafka_topic" in html and "fake/strong" in html

    # A run without a model says so and adds no cost limitation.
    from replaybook.agents.scripted import ScriptedAgent

    plain = run_replay(test_set, ScriptedAgent(), tmp_path / "plain", workers=1)
    assert plain.model_usage is None
    build_evidence(
        EvidenceInputs(
            title="Plain pack",
            bundles=[RunBundle(run=plain, test_set=test_set, run_dir=tmp_path / "plain")],
            accepted=AcceptedDifferences(),
        ),
        tmp_path / "plain-evidence",
    )
    plain_data = json.loads((tmp_path / "plain-evidence" / "evidence.json").read_text("utf-8"))
    assert plain_data["scope"][0]["model_cost"].startswith("No model calls")
    assert not any("could not be priced" in item for item in plain_data["limitations"])


def test_calls_written_as_text_are_agent_errors_in_report_and_evidence(
    small_dataset: Path, tmp_path: Path
) -> None:
    """A model that writes its calls out instead of making them leaves cases nobody can judge.
    The report and the evidence pack say what went wrong instead of 'did nothing'."""
    from replaybook import workflow

    tickets = load_tickets(small_dataset)
    test_set = build_test_set(tickets, load_platform(small_dataset), "kafka_topic", n=2)
    planned = 'First I will check the cluster:\n{"name": "kafka_cluster_info", "parameters": {}}'
    llm = FakeLLM(responder=lambda messages, tools: text_response(planned))
    run_dir = tmp_path / "run"
    run = run_replay(test_set, SkillAgent("kafka_topic", llm), run_dir, workers=1)
    assert run.summary.errors == 2 and run.summary.gate == "fail"

    notes = [case["note"] for case in workflow.report(run_dir)["non_passing"]]
    assert notes and all("kafka_cluster_info as text" in note for note in notes)

    out = tmp_path / "evidence"
    build_evidence(
        EvidenceInputs(
            title="Text calls",
            bundles=[RunBundle(run=run, test_set=test_set, run_dir=run_dir)],
            accepted=AcceptedDifferences(),
        ),
        out,
    )
    data = json.loads((out / "evidence.json").read_text(encoding="utf-8"))
    assert {e["case_id"] for e in data["agent_errors"]} == {r.case_id for r in run.results}
    assert all("as text" in e["error"] for e in data["agent_errors"])
    assert "Agent errors" in (out / "evidence.html").read_text(encoding="utf-8")
