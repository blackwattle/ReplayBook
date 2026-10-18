from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.eval.ground_truth import load_ground_truth
from replaybook.packs import registry
from replaybook.schema.ticket import Ticket
from replaybook.synth.generator import GenerateOptions, generate, request_types

CHANGES = {c.request_type: c for c in registry().procedure_changes()}
KAFKA_KRAFT_SINCE = CHANGES["kafka_topic"].at
OPENSTACK_UNIFIED_QUOTA_SINCE = CHANGES["openstack_quota"].at


def _hashes(path: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.iterdir())}


def test_same_seed_same_bytes(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    generate(a, GenerateOptions(seed=3, n_tickets=150))
    generate(b, GenerateOptions(seed=3, n_tickets=150))
    assert _hashes(a) == _hashes(b)


def test_different_seed_different_data(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    generate(a, GenerateOptions(seed=3, n_tickets=150))
    generate(b, GenerateOptions(seed=4, n_tickets=150))
    assert _hashes(a)["tickets.jsonl"] != _hashes(b)["tickets.jsonl"]


def test_dataset_shape(small_dataset: Path) -> None:
    tickets = load_tickets(small_dataset, redact=False)
    truth = load_ground_truth(small_dataset)
    assert len(tickets) >= 400
    assert len(truth) == len(tickets)
    assert len({t.id for t in tickets}) == len(tickets)
    created = [t.created for t in tickets]
    assert created == sorted(created)

    typed = [g for g in truth.values() if g.request_type]
    noise = [g for g in truth.values() if g.noise]
    assert set(request_types()) == {g.request_type for g in typed}
    assert 0.15 < len(noise) / len(tickets) < 0.35
    missing = [g for g in typed if g.info_missing]
    assert 0.10 < len(missing) / len(typed) < 0.30
    assert any(g.is_duplicate_of for g in typed)
    assert any(g.history_mistake for g in typed)
    assert any(g.unsafe_trigger for g in typed)
    assert sum(bool(t.code_changes) for t in tickets) / len(tickets) > 0.2


def test_missing_info_is_really_missing(small_dataset: Path) -> None:
    tickets = {t.id: t for t in load_tickets(small_dataset, redact=False)}
    truth = load_ground_truth(small_dataset)
    checked = 0
    for g in truth.values():
        if not g.info_missing:
            continue
        ticket = tickets[g.ticket_id]
        shown = ticket.summary + "\n" + ticket.description
        for field_name in g.missing_fields:
            value = str(g.params[field_name])
            standalone = re.compile(rf"(?<![\w.]){re.escape(value)}(?![\w.])")
            assert not standalone.search(shown), (ticket.id, field_name, value)
            # The requester supplies it later, after the engineer asked.
            replies = [c for c in ticket.comments if c.role == "requester"]
            assert any(value in c.body for c in replies), ticket.id
            before = ticket.comments_before_first_engineer_reply()
            assert all(not standalone.search(c.body) for c in before)
        waiting = [c for c in ticket.status_changes if c.to_status == "Waiting on Requester"]
        assert waiting, ticket.id
        checked += 1
    assert checked > 10


def test_procedure_drift_shows_in_history(small_dataset: Path) -> None:
    tickets = {t.id: t for t in load_tickets(small_dataset, redact=False)}
    truth = load_ground_truth(small_dataset)
    for g in truth.values():
        ticket = tickets[g.ticket_id]
        tools = {a.tool for a in ticket.actions}
        if g.request_type == "kafka_topic" and tools:
            if ticket.created < KAFKA_KRAFT_SINCE:
                assert tools <= {"kafka_zk_create_topic", "kafka_zk_add_acl"}, ticket.id
                assert g.procedure_version == "zookeeper"
                assert g.current_actions != g.correct_actions
            else:
                assert tools <= {"kafka_create_topic", "kafka_add_acl"}, ticket.id
                assert g.current_actions == g.correct_actions
        if g.request_type == "openstack_quota" and tools:
            if ticket.created < OPENSTACK_UNIFIED_QUOTA_SINCE:
                assert tools <= {"openstack_nova_quota_update", "openstack_cinder_quota_update"}
            else:
                assert tools == {"openstack_quota_set"}
    platform = load_platform(small_dataset)
    assert platform["current_procedures"] == {"kafka_topic": "kraft", "openstack_quota": "unified"}
    assert len(platform["procedure_changes"]) == 2


def test_links_and_placeholders_resolved(small_dataset: Path) -> None:
    tickets = {t.id: t for t in load_tickets(small_dataset, redact=False)}
    truth = load_ground_truth(small_dataset)
    placeholder = re.compile(r"\{\d+\}")
    for ticket in tickets.values():
        assert not placeholder.search(ticket.summary), ticket.id
        assert not placeholder.search(ticket.description), ticket.id
        for comment in ticket.comments:
            assert not placeholder.search(comment.body), ticket.id
        for link in ticket.links:
            assert link.target in tickets, (ticket.id, link)
    for g in truth.values():
        if g.corrected_by:
            fixer = tickets[g.corrected_by]
            assert any(x.type == "fixes" and x.target == g.ticket_id for x in fixer.links)
            # History recorded the mistake, not the correct value.
            original = tickets[g.ticket_id]
            recorded = [a.args for a in original.actions]
            correct = [a.args for a in g.correct_actions]
            if recorded:
                assert recorded != correct, g.ticket_id
        if g.is_duplicate_of:
            dup = tickets[g.ticket_id]
            assert dup.resolution == "Duplicate"
            assert any(x.type == "duplicates" for x in dup.links)
            assert not dup.actions


def test_ticket_roundtrip(small_dataset: Path) -> None:
    line = (small_dataset / "tickets.jsonl").read_text(encoding="utf-8").splitlines()[0]
    ticket = Ticket.model_validate_json(line)
    again = Ticket.model_validate_json(ticket.model_dump_json())
    assert again == ticket
    assert json.loads(line) == json.loads(again.model_dump_json())
