from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.external import ExternalAgent
from replaybook.agents.scripted import FLAWS, ScriptedAgent, supported_types
from replaybook.agents.skill_agent import SkillAgent, text_tool_call
from replaybook.classify import classify
from replaybook.eval.ground_truth import load_ground_truth
from replaybook.llm.client import LLMClient, LLMConfig, LLMResponse, ModelUnavailable
from replaybook.llm.fake import FakeLLM, text_response, tool_call_response
from replaybook.mock_env.registry import ASK_TOOL, GET_TOOL, NOTE_TOOLS, MockEnvironment
from replaybook.mock_env.world import WorldState
from replaybook.packs import registry
from replaybook.schema.ticket import Ticket

EXAMPLE_AGENT = Path(__file__).resolve().parents[1] / "examples" / "external_agent" / "agent.py"
DRIFT = {c.request_type: c.at for c in registry().procedure_changes()}


def _env_for(ticket: Ticket, data_dir: Path) -> MockEnvironment:
    return MockEnvironment(WorldState.from_ticket(ticket, load_platform(data_dir)))


def _tickets_of_type(data_dir: Path, request_type: str) -> list[Ticket]:
    return [t for t in load_tickets(data_dir) if classify(t)[0] == request_type]


def _waiting(ticket: Ticket) -> bool:
    return any(c.to_status == "Waiting on Requester" for c in ticket.status_changes)


def _normalised(tool: str, args: dict) -> tuple:
    plain = {k: sorted(v) if isinstance(v, list) else v for k, v in args.items() if v is not None}
    return tool, json.dumps(plain, sort_keys=True)


def _writes(env: MockEnvironment) -> list[tuple]:
    return [_normalised(w.tool, w.args) for w in env.recorder.writes if w.tool not in NOTE_TOOLS]


def test_classifier_matches_hidden_types(small_dataset: Path) -> None:
    truth = load_ground_truth(small_dataset)
    tickets = load_tickets(small_dataset)
    typed = [(t, truth[t.id]) for t in tickets if truth[t.id].request_type]
    correct = sum(classify(t)[0] == g.request_type for t, g in typed)
    assert correct / len(typed) > 0.9, correct / len(typed)
    noise = [t for t in tickets if truth[t.id].noise]
    false_positive = sum(classify(t)[0] is not None for t in noise)
    assert false_positive / len(noise) < 0.25, false_positive / len(noise)


@pytest.mark.parametrize("request_type", list(supported_types()))
def test_fixed_agent_matches_history_on_complete_current_tickets(
    small_dataset: Path, request_type: str
) -> None:
    agent = ScriptedAgent()
    truth = load_ground_truth(small_dataset)
    matched = total = 0
    for ticket in _tickets_of_type(small_dataset, request_type):
        if not ticket.actions or _waiting(ticket) or ticket.resolution == "Duplicate":
            continue
        if ticket.links:  # history later corrected: the agent is expected to differ
            continue
        if request_type in DRIFT and ticket.created < DRIFT[request_type]:
            continue
        env = _env_for(ticket, small_dataset)
        result = agent.run(env)
        if truth[ticket.id].expected_outcome == "escalate":
            # The ticket needed a person's judgement: the agent hands it back, writes nothing.
            assert result.stopped_reason == "escalated", (ticket.id, result)
            assert [w.tool for w in env.recorder.writes] == ["ticket_escalate"], ticket.id
            continue
        assert result.stopped_reason == "done", (ticket.id, result)
        expected = sorted(_normalised(a.tool, a.args) for a in ticket.actions)
        total += 1
        matched += sorted(_writes(env)) == expected
    assert total >= 3, total
    assert matched / total >= 0.9, (matched, total)


def test_fixed_agent_asks_when_info_is_missing_and_guesser_does_not(small_dataset: Path) -> None:
    fixed = ScriptedAgent()
    guesser = ScriptedAgent(flaws=["guess_missing_info"])
    asked = guessed = total = 0
    for request_type in ("kafka_topic", "artifactory_repo", "openstack_quota"):
        for ticket in _tickets_of_type(small_dataset, request_type):
            if not _waiting(ticket) or ticket.resolution == "Duplicate":
                continue
            total += 1
            env = _env_for(ticket, small_dataset)
            fixed.run(env)
            asked += [w.tool for w in env.recorder.writes] == [ASK_TOOL]
            env = _env_for(ticket, small_dataset)
            guesser.run(env)
            tools = [w.tool for w in env.recorder.writes]
            guessed += ASK_TOOL not in tools and bool(_writes(env))
    assert total >= 6, total
    # The careful agent always asks. The guesser fills in defaults whenever it can; a few
    # tickets leave nothing to guess from (no project and no team named), so it still asks.
    assert asked == total, (asked, total)
    assert guessed / total >= 0.5, (guessed, total)


def test_superseded_procedure_flaw_uses_retired_tools(small_dataset: Path) -> None:
    agent = ScriptedAgent(flaws=["superseded_procedure"])
    kafka_tools: set[str] = set()
    for ticket in _tickets_of_type(small_dataset, "kafka_topic")[:10]:
        if _waiting(ticket):
            continue
        env = _env_for(ticket, small_dataset)
        agent.run(env)
        kafka_tools |= {w.tool for w in env.recorder.writes}
    assert "kafka_zk_create_topic" in kafka_tools or "kafka_zk_add_acl" in kafka_tools
    assert "kafka_create_topic" not in kafka_tools and "kafka_add_acl" not in kafka_tools
    quota_tools: set[str] = set()
    for ticket in _tickets_of_type(small_dataset, "openstack_quota")[:10]:
        if _waiting(ticket):
            continue
        env = _env_for(ticket, small_dataset)
        agent.run(env)
        quota_tools |= {w.tool for w in env.recorder.writes}
    assert quota_tools & {"openstack_nova_quota_update", "openstack_cinder_quota_update"}
    assert "openstack_quota_set" not in quota_tools


def test_unsafe_write_flaw_deletes_existing_topics(small_dataset: Path) -> None:
    fixed = ScriptedAgent()
    unsafe = ScriptedAgent(flaws=["unsafe_write"])
    seen = 0
    for ticket in _tickets_of_type(small_dataset, "kafka_topic"):
        if _waiting(ticket):
            continue
        env = _env_for(ticket, small_dataset)
        world_topics = {t["name"] for t in env.world.state["kafka"]["topics"]}
        unsafe.run(env)
        creates = [w for w in env.recorder.writes if w.tool.endswith("create_topic")]
        requested = creates[0].args["topic"] if creates else None
        if requested in world_topics:
            seen += 1
            assert "kafka_delete_topic" in [w.tool for w in env.recorder.writes], ticket.id
            env2 = _env_for(ticket, small_dataset)
            fixed.run(env2)
            assert "kafka_delete_topic" not in [w.tool for w in env2.recorder.writes]
    assert seen >= 1


def test_unknown_flaw_is_rejected() -> None:
    with pytest.raises(ValueError):
        ScriptedAgent(flaws=["make_coffee"])
    assert set(FLAWS) == {
        "guess_missing_info",
        "superseded_procedure",
        "unsafe_write",
        "never_escalates",
        "escalates_too_often",
    }


def test_llm_client_offline_without_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    client = LLMClient(LLMConfig(model_fast="openai/gpt-4.1-mini", cache_dir=tmp_path))
    assert client.offline and "API key" in (client.offline_reason or "")
    with pytest.raises(ModelUnavailable):
        client.complete([{"role": "user", "content": "hi"}])
    forced = LLMClient(LLMConfig(offline=True, cache_dir=tmp_path))
    assert forced.offline


def test_llm_client_caches_and_enforces_spend_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = LLMClient(LLMConfig(max_spend_usd=1.0, cache_dir=tmp_path), check_providers=False)
    calls: list[str] = []

    def fake_call(model, messages, tools, temperature):
        calls.append(messages[-1]["content"])
        return LLMResponse(text="ok " + messages[-1]["content"], model=model, cost_usd=0.6)

    monkeypatch.setattr(client, "_call_model", fake_call)
    first = client.complete([{"role": "user", "content": "a"}])
    again = client.complete([{"role": "user", "content": "a"}])
    assert first.text == again.text == "ok a"
    assert not first.cached and again.cached
    assert calls == ["a"]
    assert client.spent_usd == pytest.approx(0.6)
    client.complete([{"role": "user", "content": "b"}])
    assert client.spent_usd == pytest.approx(1.2)
    assert client.offline and "spend cap" in (client.offline_reason or "")
    with pytest.raises(ModelUnavailable):
        client.complete([{"role": "user", "content": "c"}])
    # Cached answers still work offline.
    assert client.complete([{"role": "user", "content": "a"}]).cached


def test_skill_agent_runs_tool_calls_from_the_model(small_dataset: Path) -> None:
    ticket = next(t for t in _tickets_of_type(small_dataset, "kafka_topic") if not _waiting(t))
    env = _env_for(ticket, small_dataset)
    cluster = env.world.state["kafka"]["cluster"]
    llm = FakeLLM(
        responses=[
            tool_call_response(("kafka_cluster_info", {"cluster": cluster})),
            tool_call_response(
                (
                    "kafka_create_topic",
                    {"cluster": cluster, "topic": "x.events.v1", "partitions": 6},
                ),
                (
                    "kafka_add_acl",
                    {
                        "cluster": cluster,
                        "topic": "x.events.v1",
                        "principal": "svc-x",
                        "operations": ["READ"],
                    },
                ),
            ),
            text_response("Done."),
        ]
    )
    agent = SkillAgent("kafka_topic", llm)
    result = agent.run(env)
    assert result.stopped_reason == "done" and result.final_message == "Done."
    assert [w.tool for w in env.recorder.writes] == ["kafka_create_topic", "kafka_add_acl"]
    assert llm.calls[0]["tools"] and any(
        t["function"]["name"] == "kafka_create_topic" for t in llm.calls[0]["tools"]
    )
    assert "KRaft" in llm.calls[0]["messages"][0]["content"]
    assert agent.config["skill_sha256"] and agent.config["model"] == "fake/strong"
    # Offered: the ticket and knowledge tools plus the pack that owns the request type.
    offered = {t["function"]["name"] for t in llm.calls[0]["tools"]}
    assert agent.config["servers"] == ["ticket", "knowledge", "kafka"]
    assert {"ticket_get", "ticket_ask_requester", "knowledge_search_tickets"} <= offered
    assert all(name.split("_")[0] in ("ticket", "knowledge", "kafka") for name in offered)


def test_skill_agent_reports_a_tool_call_written_as_text(small_dataset: Path) -> None:
    """A model route without tool calling hands the call back as JSON text. That is an
    error with a hint, not a case where the agent chose to do nothing."""
    ticket = next(t for t in _tickets_of_type(small_dataset, "kafka_topic") if not _waiting(t))
    env = _env_for(ticket, small_dataset)
    written = '{"type": "function", "name": "kafka_create_topic", "parameters": {"topic": "x"}}'
    result = SkillAgent("kafka_topic", FakeLLM(responses=[text_response(written)])).run(env)
    assert result.stopped_reason == "error" and not env.recorder.writes
    assert "kafka_create_topic as text" in (result.error or "")
    assert "ollama_chat/" in (result.error or "")

    assert text_tool_call('```json\n[{"function": {"name": "a_b"}}]\n```', {"a_b"}) == "a_b"
    # What llama3.1 on Ollama wrote: the plan in prose, then each call as a JSON block.
    planned = (
        "Here's the step-by-step process:\n1. `a_b` for the cluster.\n\n"
        'Here are the function calls:\n```\n{"name": "a_b", "parameters": {"c": "x"}}\n```'
    )
    assert text_tool_call(planned, {"a_b"}) == "a_b"
    assert text_tool_call('{"name": "not_a_tool"} and {"oops"', {"a_b"}) is None
    assert text_tool_call("Done. I created the topic.", {"a_b"}) is None
    env = _env_for(ticket, small_dataset)
    result = SkillAgent("kafka_topic", FakeLLM(responses=[text_response("All done.")])).run(env)
    assert result.stopped_reason == "done" and result.error is None


def test_skill_agent_stops_after_asking_and_reports_offline(small_dataset: Path) -> None:
    ticket = _tickets_of_type(small_dataset, "openstack_quota")[0]
    env = _env_for(ticket, small_dataset)
    llm = FakeLLM(responses=[tool_call_response((ASK_TOOL, {"question": "Which project?"}))])
    result = SkillAgent("openstack_quota", llm).run(env)
    assert result.stopped_reason == "asked"
    assert [w.tool for w in env.recorder.writes] == [ASK_TOOL]

    env = _env_for(ticket, small_dataset)
    result = SkillAgent("openstack_quota", FakeLLM(offline=True)).run(env)
    assert result.stopped_reason == "offline" and not env.recorder.writes


def test_external_example_agent_creates_topic(small_dataset: Path) -> None:
    ticket = next(
        t
        for t in _tickets_of_type(small_dataset, "kafka_topic")
        if not _waiting(t) and t.actions and any(a.tool.endswith("create_topic") for a in t.actions)
    )
    env = _env_for(ticket, small_dataset)
    agent = ExternalAgent([sys.executable, str(EXAMPLE_AGENT)], timeout_s=120)
    result = agent.run(env)
    assert result.error is None, result.error
    tools = [w.tool for w in env.recorder.writes]
    assert "kafka_create_topic" in tools, (tools, result.final_message)
    assert env.recorder.reads and env.recorder.reads[0].tool == GET_TOOL
    assert agent.config["command"][-1].endswith("agent.py")
