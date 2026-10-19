"""Expectations extracted from real ticket text are labelled with their source and stay
unverified; the builder uses them only for tickets without recorded actions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from replaybook.adapters import public_jira
from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.eval.extract import (
    ModelExtractor,
    RuleExtractor,
    make_extractor,
    parse_model_actions,
)
from replaybook.eval.testset import build_test_set
from replaybook.llm.fake import FakeLLM, text_response
from replaybook.packs import registry
from replaybook.schema.ticket import Ticket

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "jira" / "issues.jsonl"
RULES = registry().extract_rules()
rule_dns_change = RULES["dns_change"]
rule_group_access = RULES["group_access"]
rule_ci_fix = RULES["ci_fix"]


def _ticket(summary: str, description: str = "", comments: list[str] | None = None) -> Ticket:
    return Ticket(
        id="T-1",
        source="public_jira",
        project="INFRA",
        summary=summary,
        description=description,
        reporter="r",
        created=datetime(2024, 1, 1, tzinfo=UTC),
        status="Closed",
        comments=[
            {
                "author": "e",
                "role": "engineer",
                "created": datetime(2024, 1, 2, tzinfo=UTC),
                "body": c,
            }
            for c in comments or []
        ],
    )


def test_dns_rule_reads_cname_and_a_records() -> None:
    cname = rule_dns_change("Please point demo.apache.org at apache-demo.github.io (CNAME)")
    assert [a.model_dump() for a in cname] == [
        {
            "tool": "platform_dns_upsert_record",
            "args": {
                "zone": "apache.org",
                "name": "demo",
                "type": "CNAME",
                "value": "apache-demo.github.io",
            },
        }
    ]
    a_record = rule_dns_change("Create an A record for ci.example.org -> 203.0.113.5")
    assert a_record[0].args == {
        "zone": "example.org",
        "name": "ci",
        "type": "A",
        "value": "203.0.113.5",
    }
    assert rule_dns_change("Add a TXT record for example.org for domain verification") == []
    assert rule_dns_change("Nothing to see here, e.g. no hosts") == []


def test_group_and_ci_rules() -> None:
    group = rule_group_access("Please add user jdoe to the hadoop-committers LDAP group.")
    assert group[0].args == {"group": "hadoop-committers", "user": "jdoe"}
    assert rule_group_access("Please add me to something") == []
    ci = rule_ci_fix(
        "Pipeline for project apache/airflow needs DOCKERHUB_TOKEN = abc123 as a secret"
    )
    assert ci[0].args == {
        "project": "apache/airflow",
        "key": "DOCKERHUB_TOKEN",
        "value": "abc123",
        "protected": True,
        "masked": True,
    }
    assert rule_ci_fix("The runner is slow") == []


def test_rule_extractor_labels_its_output_and_skips_unknown_types() -> None:
    extractor = RuleExtractor()
    found = extractor.extract(
        _ticket("Renew the TLS certificate for lists.apache.org"), "cert_renewal"
    )
    assert found is not None and found.source == "rules"
    assert found.actions[0].args == {"hostname": "lists.apache.org"}
    assert extractor.extract(_ticket("Create kafka topic orders.events.v1"), "kafka_topic") is None
    assert extractor.extract(_ticket("Please help"), "cert_renewal") is None


def test_model_extractor_uses_the_model_then_falls_back_to_rules() -> None:
    tools = [{"name": "platform_cert_renew", "description": "Renew", "inputSchema": {}}]
    answer = text_response(
        'Sure: {"actions": [{"tool": "platform_cert_renew", "args": {"hostname": "x.apache.org", '
        '"validity_days": 90}}, {"tool": "rm_rf", "args": {}}]}'
    )
    client = FakeLLM(responses=[answer])
    extractor = ModelExtractor(client, tools)
    found = extractor.extract(_ticket("Renew cert for x.apache.org"), "cert_renewal")
    assert found is not None and found.source == "model"
    assert [a.tool for a in found.actions] == ["platform_cert_renew"]  # unknown tools dropped
    assert client.calls and client.calls[0]["tier"] == "strong"

    offline = ModelExtractor(FakeLLM(offline=True), tools)
    fallback = offline.extract(_ticket("Renew cert for y.apache.org"), "cert_renewal")
    assert fallback is not None and fallback.source == "rules"

    garbage = ModelExtractor(FakeLLM(responses=[text_response("no json here")]), tools)
    assert garbage.extract(_ticket("Renew cert for z.apache.org"), "cert_renewal").source == "rules"
    assert parse_model_actions("[]", {"x"}) == [] and parse_model_actions("{bad", {"x"}) == []


def test_make_extractor_names() -> None:
    assert make_extractor("none") is None and make_extractor("") is None
    assert isinstance(make_extractor("rules"), RuleExtractor)


def test_builder_extracts_unverified_expectations_only_without_recorded_actions(
    tmp_path: Path, small_dataset: Path
) -> None:
    out = tmp_path / "infra"
    public_jira.export(FIXTURE, "INFRA", out)
    tickets = load_tickets(out)
    platform = load_platform(out)
    # Without an extractor, real tickets have nothing to expect.
    bare = build_test_set(tickets, platform, "dns_change", n=10, source="public_jira")
    assert bare.cases == [] and bare.selection["excluded"] == {"no recorded actions": 1}
    # With rules: INFRA-1001 waited on the requester, so it is an "ask" case from history.
    dns = build_test_set(
        tickets, platform, "dns_change", n=10, source="public_jira", extractor=RuleExtractor()
    )
    assert [c.id for c in dns.cases] == ["INFRA-1001"]
    assert dns.cases[0].expectation.kind == "ask" and dns.cases[0].expectation.verified
    # A wait without an engineer question (a "please verify" loop) is not an ask case.
    quiet = [
        t.model_copy(update={"comments": [c for c in t.comments if c.role != "engineer"]})
        for t in tickets
    ]
    verify_loop = build_test_set(
        quiet, platform, "dns_change", n=10, source="public_jira", extractor=RuleExtractor()
    )
    assert verify_loop.cases[0].expectation.kind == "actions"
    # Nor is a question the reporter asked on their own ticket, though an adapter that learns
    # its engineers across the project calls them an engineer.
    own_question = [
        t.model_copy(
            update={
                "comments": [
                    c.model_copy(update={"author": t.reporter}) if c.role == "engineer" else c
                    for c in t.comments
                ]
            }
        )
        for t in tickets
    ]
    by_reporter = build_test_set(
        own_question, platform, "dns_change", n=10, source="public_jira", extractor=RuleExtractor()
    )
    assert by_reporter.cases[0].expectation.kind == "actions"
    cert = build_test_set(
        tickets, platform, "cert_renewal", n=10, source="public_jira", extractor=RuleExtractor()
    )
    assert [c.id for c in cert.cases] == ["INFRA-1002"]
    expectation = cert.cases[0].expectation
    assert expectation.kind == "actions" and expectation.source == "rules"
    assert expectation.verified is False
    assert expectation.actions[0].args == {"hostname": "lists.apache.org"}
    assert cert.selection["unverified"] == 1
    # The world is seeded with what the text names, so the agent's reads succeed.
    platform_world = cert.cases[0].world.state["platform"]
    assert [c["hostname"] for c in platform_world["certificates"]] == ["lists.apache.org"]
    zones = dns.cases[0].world.state["platform"]["zones"]
    assert [z["name"] for z in zones] == ["apache.org"]  # ask case too
    assert cert.selection["expectation_sources"] == {"rules": 1}
    # Synthetic history is untouched by the extractor: recorded actions win.
    synthetic = build_test_set(
        load_tickets(small_dataset),
        load_platform(small_dataset),
        "dns_change",
        n=5,
        extractor=RuleExtractor(),
    )
    assert synthetic.cases
    recorded = {t.id for t in load_tickets(small_dataset) if t.actions}
    for case in synthetic.cases:
        if case.id in recorded or case.expectation.kind == "ask":
            assert case.expectation.verified and case.expectation.source == "history"
        else:
            assert not case.expectation.verified and case.expectation.source == "rules"
    data = json.loads(cert.model_dump_json())
    assert data["cases"][0]["expectation"]["source"] == "rules"
