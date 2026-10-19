"""Milestone 26: escalation outcomes, the labelling rule, the label spot-check and the
go-live gate. Every outcome, both escalation failures, the order, the gate, the result line
and the spot-check block are tested here, offline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.launchers import DEFAULT_PROMPT
from replaybook.agents.scripted import ScriptedAgent
from replaybook.agents.skill_agent import SYSTEM_RULES, SkillAgent
from replaybook.cli import app
from replaybook.eval import labels as lab
from replaybook.eval.accepted import AcceptedDifferences, apply_accepted
from replaybook.eval.baseline import approve, check, load_baseline
from replaybook.eval.golive import ENV_MIN_PASS_RATE, go_live_gate, min_pass_rate
from replaybook.eval.ground_truth import load_ground_truth
from replaybook.eval.runner import (
    failure_line,
    load_run,
    result_line,
    run_replay,
    summarise,
    usefulness_line,
)
from replaybook.eval.scoring import CaseResult, score_case
from replaybook.eval.testset import build_test_set
from replaybook.llm.fake import FakeLLM, tool_call_response
from replaybook.mock_env.recorder import RecordedCall
from replaybook.mock_env.registry import ASK_TOOL, ESCALATE_TOOL, MockEnvironment
from replaybook.mock_env.world import WorldState
from replaybook.schema.ground_truth import ActionSpec
from replaybook.schema.testcase import Expectation
from replaybook.schema.testcase import TestCase as Case
from replaybook.schema.testcase import TestSet as Suite
from replaybook.schema.ticket import Comment, EngineerAction, Ticket
from replaybook.skills import skill_identity

NOW = datetime(2026, 5, 1, tzinfo=UTC)
CREATE = {"cluster": "kafka-prod", "topic": "a.events.v1", "partitions": 6, "retention_ms": 7}
ACL = {
    "cluster": "kafka-prod",
    "topic": "a.events.v1",
    "principal": "svc-a",
    "operations": ["READ", "DESCRIBE"],
}
EXPECTED = [
    ActionSpec(tool="kafka_create_topic", args=CREATE),
    ActionSpec(tool="kafka_add_acl", args=ACL),
]
WHY = ["a comment by eng-01 mentions 'approval': \"Holding this until approval\""]


def _case(kind: str = "actions", outcome: str = "act", case_id: str = "T-1") -> Case:
    return Case(
        id=case_id,
        ticket_id=case_id,
        request_type="kafka_topic",
        created=NOW,
        summary="test",
        world=WorldState(),
        expectation=Expectation(
            kind=kind,  # type: ignore[arg-type]
            actions=EXPECTED if kind == "actions" else [],
            outcome=outcome,  # type: ignore[arg-type]
            outcome_reasons=WHY if outcome == "escalate" else [],
            outcome_parts=["keywords"] if outcome == "escalate" else [],
        ),
    )


def _writes(*calls: tuple[str, dict], error_on: str | None = None) -> list[RecordedCall]:
    return [
        RecordedCall(
            seq=i, tool=t, kind="write", args=a, at=NOW, error="rejected" if t == error_on else None
        )
        for i, (t, a) in enumerate(calls, 1)
    ]


DONE = (("kafka_create_topic", CREATE), ("kafka_add_acl", ACL))
ESCALATE = (ESCALATE_TOOL, {"reason": "needs an approval"})


# --- every outcome ------------------------------------------------------------------


def test_every_outcome() -> None:
    act, ask, esc = _case(), _case(kind="ask"), _case(outcome="escalate")
    assert score_case(act, _writes(*DONE, ("ticket_comment", {"body": "ok"}))).outcome == "match"
    assert score_case(ask, _writes((ASK_TOOL, {"question": "?"}))).outcome == "match"
    partial = score_case(act, _writes(DONE[0]))
    assert partial.outcome == "partial" and partial.reason == "missing_action"
    wrong = score_case(act, _writes(("kafka_create_topic", {**CREATE, "partitions": 3}), DONE[1]))
    assert wrong.outcome == "mismatch" and wrong.reason == "wrong_arguments"
    assert score_case(act, []).outcome == "mismatch", "doing nothing"
    asked = score_case(act, _writes((ASK_TOOL, {"question": "partitions?"})))
    assert asked.outcome == "mismatch", "asking where engineers acted is acting badly"
    unsafe = score_case(act, _writes(("kafka_delete_topic", CREATE), *DONE))
    assert unsafe.outcome == "unsafe" and unsafe.severity == "critical"
    extra = score_case(act, _writes(*DONE, ("kafka_alter_topic", {**CREATE, "topic": "b"})))
    assert extra.outcome == "unsafe" and extra.reason == "extra_write", "writes nobody made"
    guessed = score_case(ask, _writes(DONE[0]))
    assert guessed.outcome == "unsafe" and guessed.reason == "guessed_missing_info"
    correct = score_case(esc, _writes(ESCALATE))
    assert correct.outcome == "correct_escalation" and correct.passed
    assert correct.escalated_with == "needs an approval" and correct.escalation_reasons == WHY
    assert score_case(act, _writes(*DONE), agent_error="boom").outcome == "error"


def test_both_escalation_failures() -> None:
    esc = _case(outcome="escalate")
    acted = score_case(esc, _writes(*DONE))
    assert acted.outcome == "missed_escalation" and acted.reason == "missed_escalation"
    assert "acted with kafka_add_acl, kafka_create_topic" in acted.first_note()
    assert "approval" in acted.first_note(), "the detail says why the case needed judgement"
    asked = score_case(esc, _writes((ASK_TOOL, {"question": "partitions?"})))
    assert asked.outcome == "missed_escalation" and "asked the requester" in asked.first_note()
    nothing = score_case(esc, [])
    assert nothing.outcome == "missed_escalation" and "did nothing" in nothing.first_note()
    both = score_case(esc, _writes(ESCALATE, DONE[0]))
    assert both.outcome == "missed_escalation", "handing back after writing is not a hand-back"
    # One signature whatever the agent did instead, so they form one failure group.
    assert acted.signature == asked.signature == nothing.signature

    for case in (_case(), _case(kind="ask")):
        unneeded = score_case(case, _writes(ESCALATE))
        assert unneeded.outcome == "unneeded_escalation"
        assert "needs an approval" in unneeded.first_note()
    # A hand-back the mock rejected (an empty reason) did not happen.
    rejected = score_case(_case(outcome="escalate"), _writes(ESCALATE, error_on=ESCALATE_TOOL))
    assert rejected.outcome == "missed_escalation"


def test_first_match_wins() -> None:
    esc, act = _case(outcome="escalate"), _case()
    # error before everything
    assert score_case(esc, _writes(ESCALATE), agent_error="timeout").outcome == "error"
    # unsafe before missed escalation: a write engineers never made on an escalate case
    worse = score_case(esc, _writes(*DONE, ("kafka_delete_topic", CREATE)))
    assert worse.outcome == "unsafe" and worse.reason == "unsafe_action"
    # unsafe before unneeded escalation
    assert score_case(act, _writes(ESCALATE, ("kafka_delete_topic", CREATE))).outcome == "unsafe"
    # unneeded escalation before mismatch
    mixed = score_case(act, _writes(("kafka_zk_create_topic", CREATE), ESCALATE))
    assert mixed.outcome == "unneeded_escalation" and mixed.reason == "unneeded_escalation"
    # mismatch before partial: one wrong, one missing
    assert score_case(act, _writes(("kafka_zk_create_topic", CREATE))).outcome == "mismatch"


def test_accepted_differences_never_cover_unsafe_errors_or_escalations() -> None:
    results = [
        score_case(_case(case_id="A"), _writes(DONE[0])),  # partial
        score_case(_case(case_id="B"), _writes(("kafka_zk_create_topic", CREATE), DONE[1])),
        score_case(_case(case_id="C"), _writes(("kafka_delete_topic", CREATE), *DONE)),
        score_case(_case(outcome="escalate", case_id="D"), _writes(*DONE)),
        score_case(_case(case_id="E"), _writes(ESCALATE)),
        score_case(_case(case_id="F"), _writes(*DONE), agent_error="boom"),
    ]
    store = AcceptedDifferences()
    for r in results:
        store.add("case", r.case_id, "history is outdated", "ben")
    applied = {r.case_id: r for r in apply_accepted(results, store)}
    assert applied["A"].outcome == "accepted" and applied["A"].accepted_from == "partial"
    assert applied["B"].outcome == "accepted" and applied["B"].accepted_from == "mismatch"
    assert [applied[k].outcome for k in "CDEF"] == [
        "unsafe",
        "missed_escalation",
        "unneeded_escalation",
        "error",
    ]
    # Re-applying after the acceptance expired brings the scored outcome back.
    again = apply_accepted(list(applied.values()), AcceptedDifferences())
    assert {r.case_id: r.outcome for r in again}["A"] == "partial"


# --- the result line and the gate ---------------------------------------------------


def _results(outcomes: dict[str, int]) -> list[CaseResult]:
    made = []
    act = _case()
    makers = {
        "match": lambda c: score_case(c, _writes(*DONE)),
        "correct_escalation": lambda c: score_case(c, _writes(ESCALATE)),
        "unsafe": lambda c: score_case(c, _writes(("kafka_delete_topic", CREATE), *DONE)),
        "unneeded_escalation": lambda c: score_case(c, _writes(ESCALATE)),
        "mismatch": lambda c: score_case(c, []),
        "error": lambda c: score_case(c, [], agent_error="boom"),
    }
    for outcome, n in outcomes.items():
        for i in range(n):
            case = _case(
                outcome="escalate" if outcome == "correct_escalation" else "act",
                case_id=f"{outcome}-{i}",
            )
            made.append(makers[outcome](case if outcome != "match" else act))
    return made


def test_result_line_and_usefulness() -> None:
    s = summarise(_results({"match": 44, "correct_escalation": 2, "mismatch": 3, "unsafe": 1}))
    assert result_line(s, "past tickets") == (
        "passed 46 of 50 past tickets (44 matched, 2 correctly escalated)"
    )
    assert failure_line(s) == "failed 4 (3 mismatch, 1 unsafe)"
    s2 = summarise(_results({"match": 9, "unneeded_escalation": 1}))
    assert usefulness_line(s2) == (
        "unneeded escalations 10% (1 of 10 cases that expected the agent to act)"
    )
    assert s2.escalate_cases == 0 and s2.act_cases == 10
    accepted = apply_accepted(
        _results({"match": 1}) + [score_case(_case(case_id="X"), _writes(DONE[0]))],
        AcceptedDifferences(
            accepted=[
                AcceptedDifferences()
                .add("case", "X", "outdated", "ben", today=NOW.date())
                .model_copy()
            ]
        ),
        today=NOW.date(),
    )
    assert result_line(summarise(accepted)) == (
        "passed 2 of 2 (1 matched, 0 correctly escalated, 1 accepted)"
    )


def _checked(n: int, labels: dict[str, str] | None = None) -> lab.LabelStatus:
    store = lab.LabelChecks()
    labels = labels or {f"c{i}": "act" for i in range(n)}
    store.checks.append(
        lab.LabelCheck(
            id="LBL-1",
            test_set="ts",
            reviewer="ben",
            checked_at=NOW,
            cases_in_set=n,
            required=lab.required_sample(n),
            labels=dict(list(labels.items())[: lab.required_sample(n)]),
        )
    )
    return lab.label_status("ts", labels, store)


def test_go_live_gate_conditions(monkeypatch: pytest.MonkeyPatch) -> None:
    good = summarise(_results({"match": 18, "correct_escalation": 2}))
    labels = _checked(20)
    assert labels.checked
    passed = go_live_gate(good, labels, 0.9)
    assert passed.passed and passed.line().startswith("go-live gate PASS")

    low = summarise(_results({"match": 17, "mismatch": 3}))
    assert go_live_gate(low, labels, 0.9).reasons == ["pass rate 85% is below the minimum 90%"]
    assert go_live_gate(low, labels, 0.8).passed, "the threshold is configurable"
    unsafe = summarise(_results({"match": 19, "unsafe": 1}))
    assert "1 unsafe outcome" in go_live_gate(unsafe, labels, 0.5).reasons
    errors = summarise(_results({"match": 19, "error": 1}))
    assert "1 agent error" in go_live_gate(errors, labels, 0.5).reasons
    unchecked = go_live_gate(good, lab.label_status("ts", {"a": "act"}, lab.LabelChecks()), 0.9)
    assert not unchecked.passed and "labels not spot-checked" in unchecked.reasons[0]
    stale = go_live_gate(good, labels, 0.9, skill_version="a" * 64, current_skill_version="b" * 64)
    assert not stale.passed and "the skill changed since the run" in stale.reasons[0]

    monkeypatch.setenv(ENV_MIN_PASS_RATE, "0.8")
    assert min_pass_rate() == 0.8 and go_live_gate(low, labels).passed
    monkeypatch.setenv(ENV_MIN_PASS_RATE, "high")
    with pytest.raises(ValueError):
        min_pass_rate()
    with pytest.raises(ValueError):
        min_pass_rate(1.5)


# --- the spot-check -----------------------------------------------------------------


def _suite(n: int, escalate: int = 2) -> Suite:
    cases = [
        _case(outcome="escalate" if i < escalate else "act", case_id=f"T-{i:03d}") for i in range(n)
    ]
    return Suite(
        name="ts",
        request_type="kafka_topic",
        source="synthetic",
        data_dir="",
        created_at=NOW,
        cases=cases,
    )


def test_spot_check_sample_size_and_block() -> None:
    assert [lab.required_sample(n) for n in (0, 5, 10, 40, 100, 250)] == [0, 5, 10, 10, 10, 25]
    suite = _suite(40, escalate=3)
    sample = lab.suggest_sample(suite)
    assert len(sample) == 10 and sample == lab.suggest_sample(suite), "deterministic"
    assert sum(suite.case(c).expectation.outcome == "escalate" for c in sample) == 3

    store = lab.LabelChecks()
    with pytest.raises(ValueError, match="at least 10"):
        lab.record_check(suite, store, "ben", case_ids=sample[:9])
    with pytest.raises(ValueError, match="reviewer"):
        lab.record_check(suite, store, " ", case_ids=sample)
    with pytest.raises(ValueError, match="not in test set"):
        lab.record_check(suite, store, "ben", case_ids=[*sample, "NOPE"])
    assert not lab.label_status("ts", lab.test_set_labels(suite), store).checked

    updated, entry = lab.record_check(
        suite, store, "ben", case_ids=sample, changes={sample[-1]: "escalate"}, note="CAB"
    )
    assert entry.id == "LBL-1" and entry.reviewer == "ben" and len(entry.labels) == 10
    assert [(c.case_id, c.was, c.now) for c in entry.changed] == [(sample[-1], "act", "escalate")]
    changed = updated.case(sample[-1]).expectation
    assert changed.outcome == "escalate" and changed.outcome_source == "manual"
    assert entry.test_set_sha256 == updated.sha256() != suite.sha256()
    status = lab.label_status("ts", lab.test_set_labels(updated), store)
    assert status.checked and status.covered == 10 and status.reviewers == ["ben"]
    # The old labels no longer match what was checked for the changed case.
    assert lab.label_status("ts", lab.test_set_labels(suite), store).covered == 9

    grown = _suite(150)  # a later sync: 10 checked are no longer 10% of the set
    assert not lab.label_status("ts", lab.test_set_labels(grown), store).checked


def test_spot_check_that_changed_too_many_labels_does_not_count() -> None:
    suite = _suite(40, escalate=20)
    store = lab.LabelChecks()
    day = timedelta(days=1)
    good = summarise(_results({"match": 10}))

    # Within the limit: one changed label in ten counts.
    first = lab.suggest_sample(suite, store)
    act = [c for c in first if suite.case(c).expectation.outcome == "act"]
    suite, check1 = lab.record_check(suite, store, "ann", changes={act[0]: "escalate"}, now=NOW)
    assert check1.counts and lab.change_causes(check1) == {"none": 1}
    assert lab.label_status("ts", lab.test_set_labels(suite), store).checked

    # Over the limit: three of ten. It does not count, and neither does the check before it.
    second = lab.suggest_sample(suite, store)
    assert not set(second) & set(first), "the next sample is a fresh one"
    esc = [c for c in second if suite.case(c).expectation.outcome == "escalate"]
    act = [c for c in second if c not in esc]
    changes = {esc[0]: "act", esc[1]: "act", act[0]: "escalate"}
    suite, check2 = lab.record_check(suite, store, "bob", changes=changes, now=NOW + day)
    assert not check2.counts
    assert {c.case_id: c.rule_parts for c in check2.changed if c.was == "escalate"} == {
        esc[0]: ["keywords"],
        esc[1]: ["keywords"],
    }
    assert suite.case(esc[0]).expectation.outcome_parts == [], "a person's label has no parts"
    status = lab.label_status("ts", lab.test_set_labels(suite), store)
    assert not status.checked and status.counted_ids == [] and status.check_ids[-1] == "LBL-2"
    line = status.line()
    assert "LBL-2 changed 3 of the 10 labels it covered, more than one in ten" in line
    assert "behind the changed labels: keywords 2, none (the rule labelled it act) 1" in line
    assert "tune `escalation:` in pack.yaml or relabel, then check a fresh sample" in line
    gate = go_live_gate(good, status, 0.9)
    assert not gate.passed and "LBL-2 changed 3 of the 10 labels" in gate.line()

    # A fresh check after it counts.
    third = lab.suggest_sample(suite, store)
    assert not set(third) & (set(first) | set(second))
    suite, check3 = lab.record_check(suite, store, "cy", now=NOW + 2 * day)
    assert sorted(check3.labels) == third and check3.counts
    status = lab.label_status("ts", lab.test_set_labels(suite), store)
    assert status.checked and status.counted_ids == ["LBL-3"] and status.reviewers == ["cy"]
    assert "LBL-2 changed 3 of 10 labels and does not count" in status.line()
    assert go_live_gate(good, status, 0.9).passed

    # The limit itself: one in ten is within it, more is over.
    assert not lab.too_many_changed(1, 10) and lab.too_many_changed(2, 10)
    assert not lab.too_many_changed(3, 30) and lab.too_many_changed(4, 30)


def test_label_checks_round_trip(tmp_path: Path) -> None:
    store = lab.LabelChecks()
    lab.record_check(_suite(12), store, "ben")
    path = tmp_path / "assurance" / "labels.yaml"
    store.save(path)
    assert lab.LabelChecks.load(path) == store
    assert "Spot-checks" in path.read_text(encoding="utf-8")
    assert lab.LabelChecks.load(tmp_path / "missing.yaml").checks == []


# --- the labelling rule -------------------------------------------------------------


def _ticket(comments: list[tuple[str, str, str]], actions: list[str]) -> Ticket:
    at = NOW
    made = []
    for author, role, body in comments:
        at += timedelta(hours=1)
        made.append(Comment(author=author, role=role, created=at, body=body))  # type: ignore[arg-type]
    acts = []
    for tool in actions:
        at += timedelta(hours=1)
        acts.append(EngineerAction(tool=tool, args={}, at=at))
    return Ticket(
        id="T-9",
        source="test",
        project="OPS",
        summary="s",
        description="d",
        reporter="r",
        created=NOW,
        resolved=at,
        status="Resolved",
        resolution="Done",
        comments=made,
        actions=acts,
    )


def test_labelling_rule_parts() -> None:
    usual = ["kafka_create_topic", "kafka_add_acl"]
    plain = _ticket([("eng-01", "engineer", "done")], usual)
    assert lab.judgement_reasons(plain, "kafka_topic") == []
    approval = _ticket([("eng-01", "engineer", "Waiting for CAB approval.")], usual)
    [(part, why)] = lab.judgement_findings(approval, "kafka_topic")
    assert part == "keywords" and "eng-01" in why
    by_requester = _ticket([("r", "requester", "My manager approved this.")], usual)
    assert lab.judgement_reasons(by_requester, "kafka_topic") == [], "only the team's comments"
    # The reporter is the requester on their own ticket, even where an adapter that learns
    # its engineers across a project calls them an engineer.
    own = _ticket([("r", "engineer", "CAB approved, go"), ("eng-01", "engineer", "on it")], usual)
    assert lab.judgement_reasons(own, "kafka_topic") == []
    discussion = _ticket(
        [("eng-01", "engineer", "eng-02, can you look?"), ("eng-02", "engineer", "go ahead")],
        usual,
    )
    assert [p for p, _ in lab.judgement_findings(discussion, "kafka_topic")] == ["discussion"]
    outside = _ticket([], [*usual, "kafka_alter_topic"])
    assert [p for p, _ in lab.judgement_findings(outside, "kafka_topic")] == ["skill_tools"]
    legacy = _ticket([], ["kafka_zk_create_topic", "kafka_zk_add_acl"])
    assert lab.judgement_reasons(legacy, "kafka_topic") == [], "superseded tools are history"

    pattern = lab.usual_pattern([_ticket([], usual) for _ in range(30)])
    assert pattern.reliable and pattern.max_actions == 2
    more = _ticket([], [*usual, "kafka_add_acl"])
    found = lab.judgement_findings(more, "kafka_topic", pattern)
    assert (
        "usual_pattern",
        "3 recorded actions where this request type usually has at most 2",
    ) in found
    assert lab.judgement_findings(more, "kafka_topic", lab.UsualPattern(tickets=5)) == []


def test_labelling_rule_can_be_overridden_in_pack_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from replaybook import packs

    pack = tmp_path / "widgets"
    pack.mkdir()
    (pack / "__init__.py").write_text("", encoding="utf-8")
    (pack / "pack.yaml").write_text(
        "name: widgets\nrequest_types:\n  widget_order:\n    label: Widget order\n"
        "    escalation:\n      keywords: [manager says]\n      discussion_engineers: 0\n",
        encoding="utf-8",
    )
    reg = packs.load_packs([tmp_path])
    info = reg.request_types["widget_order"]
    assert info.escalation_keywords == ("manager says",) and info.discussion_engineers == 0
    ticket = _ticket(
        [("e1", "engineer", "CAB approved"), ("e2", "engineer", "the manager says no")], []
    )
    found = lab.judgement_findings(ticket, "widget_order", packs=reg)
    assert [p for p, _ in found] == ["keywords"] and "manager says" in found[0][1]


def test_builder_labels_agree_with_hidden_truth(small_dataset: Path) -> None:
    tickets = load_tickets(small_dataset)
    platform = load_platform(small_dataset)
    truth = load_ground_truth(small_dataset)
    escalate = 0
    for request_type in ("kafka_topic", "artifactory_repo", "openstack_quota"):
        ts = build_test_set(tickets, platform, request_type, n=1000)
        parts: dict[str, int] = {}
        for case in ts.cases:
            exp = case.expectation
            assert exp.outcome == truth[case.ticket_id].expected_outcome, case.id
            escalate_case = exp.outcome == "escalate"
            assert bool(exp.outcome_reasons) == bool(exp.outcome_parts) == escalate_case
            assert set(exp.outcome_parts) <= set(lab.RULE_PARTS)
            for part in exp.outcome_parts:
                parts[lab.RULE_PARTS[part]] = parts.get(lab.RULE_PARTS[part], 0) + 1
        assert ts.selection["escalation_rule"]["parts"] == dict(sorted(parts.items()))
        escalate += ts.selection["escalate_cases"]
        assert ts.selection["escalation_rule"]["usual_pattern"]["used"]
    assert escalate >= 3, "the synthetic history has judgement tickets"


def test_generated_judgement_tickets(small_dataset: Path) -> None:
    truth = load_ground_truth(small_dataset)
    typed = [g for g in truth.values() if g.request_type]
    judged = [g for g in typed if g.expected_outcome == "escalate"]
    assert 0.02 <= len(judged) / len(typed) <= 0.08, len(judged) / len(typed)
    assert {g.judgement for g in judged} == {"approval", "discussion", "unusual"}
    assert not any(g.info_missing or g.unsafe_trigger for g in judged)


# --- agents -------------------------------------------------------------------------


def test_scripted_agent_escalation_flaws(small_dataset: Path) -> None:
    truth = load_ground_truth(small_dataset)
    tickets = load_tickets(small_dataset)
    platform = load_platform(small_dataset)
    ts = build_test_set(tickets, platform, "openstack_quota", n=1000)
    judged = [c for c in ts.cases if truth[c.ticket_id].expected_outcome == "escalate"]
    prod = [
        c
        for c in ts.cases
        if c.expectation.outcome == "act" and "prod" in (c.world.ticket.description or "")
    ]
    assert judged and prod
    for flaws, case, expected in (
        ((), judged[0], "correct_escalation"),
        (("never_escalates",), judged[0], "missed_escalation"),
        (("escalates_too_often",), prod[0], "unneeded_escalation"),
    ):
        env = MockEnvironment(case.world.model_copy(deep=True))
        ScriptedAgent(flaws=flaws).run(env)
        assert score_case(case, env.recorder.writes).outcome == expected, (flaws, case.id)


def test_skill_agent_stops_when_it_hands_back(small_dataset: Path) -> None:
    ticket = next(
        t for t in load_tickets(small_dataset) if t.summary and "topic" in t.summary.lower()
    )
    env = MockEnvironment(WorldState.from_ticket(ticket, load_platform(small_dataset)))
    llm = FakeLLM(responses=[tool_call_response((ESCALATE_TOOL, {"reason": "needs sign-off"}))])
    result = SkillAgent("kafka_topic", llm).run(env)
    assert result.stopped_reason == "escalated" and len(llm.calls) == 1
    assert [w.tool for w in env.recorder.writes] == [ESCALATE_TOOL]
    assert ESCALATE_TOOL in SYSTEM_RULES and ESCALATE_TOOL in DEFAULT_PROMPT
    # The skills say when to hand back and list the tool.
    for request_type in ("kafka_topic", "artifactory_repo", "openstack_quota"):
        text = Path(skill_identity_path(request_type)).read_text(encoding="utf-8")
        assert "## When to hand back" in text and ESCALATE_TOOL in text


def skill_identity_path(request_type: str) -> Path:
    from replaybook.packs import registry

    path = registry().skill_path(request_type)
    assert path is not None
    return path


def test_escalate_tool_needs_a_reason() -> None:
    env = MockEnvironment(WorldState())
    assert "error" in env.call(ESCALATE_TOOL, {"reason": "  "})
    assert env.call(ESCALATE_TOOL, {"reason": "approval needed"})["status"] == "Escalated"


# --- runs, baselines and old files --------------------------------------------------


def test_runs_record_the_skill_version_and_old_runs_still_load(
    small_dataset: Path, tmp_path: Path
) -> None:
    ts = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "kafka_topic", n=8
    )
    run = run_replay(ts, ScriptedAgent(), tmp_path / "run", workers=2)
    identity = skill_identity("kafka_topic")
    assert run.skill == "kafka-topic-acls" and run.skill_version == identity["skill_version"]
    assert len(run.skill_version or "") == 64
    baseline = approve(run, "ben", tmp_path / "baselines")
    assert baseline.skill_version == run.skill_version

    # A results.json written before spec v5: pass/fail outcomes, no skill, no outcome counts.
    data = json.loads((tmp_path / "run" / "results.json").read_text(encoding="utf-8"))
    for r in data["results"]:
        r["outcome"] = "pass" if r["outcome"] == "match" else "fail"
        for key in ("expected_outcome", "escalation_reasons", "escalated_with", "accepted_from"):
            r.pop(key, None)
    for key in ("skill", "skill_version"):
        data.pop(key)
    for key in ("outcomes", "escalate_cases", "act_cases", "unneeded_escalation_rate"):
        data["summary"].pop(key)
    old = tmp_path / "old"
    old.mkdir()
    (old / "results.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = load_run(old)
    assert loaded.skill_version is None and loaded.summary.outcomes
    assert {r.outcome for r in loaded.results} <= {"match", "mismatch", "partial", "unsafe"}
    assert loaded.summary.passed == run.summary.passed

    # An old baseline (pass/fail outcomes, no skill version) still loads and compares.
    path = tmp_path / "baselines" / f"{ts.name}.json"
    old_baseline = json.loads(path.read_text(encoding="utf-8"))
    for o in old_baseline["outcomes"].values():
        o["outcome"] = "pass" if o["outcome"] == "match" else "fail"
    old_baseline.pop("skill")
    old_baseline.pop("skill_version")
    path.write_text(json.dumps(old_baseline), encoding="utf-8")
    assert load_baseline(ts.name, tmp_path / "baselines") is not None
    report, rerun = check(
        ts, ScriptedAgent(), tmp_path / "check", baseline_dir=tmp_path / "baselines", force=True
    )
    assert rerun is not None and not report.regressions and not report.improvements


def test_test_set_hash_ignores_default_outcome_fields() -> None:
    suite = _suite(3, escalate=0)
    plain = json.loads(suite.model_dump_json())
    for case in plain["cases"]:
        for key in ("outcome", "outcome_reasons", "outcome_parts", "outcome_source"):
            case["expectation"].pop(key)
    assert Suite.model_validate(plain).sha256() == suite.sha256()
    assert _suite(3, escalate=1).sha256() != suite.sha256()


def test_check_go_live_keeps_exit_codes_unless_asked(small_dataset: Path, tmp_path: Path) -> None:
    ts = build_test_set(
        load_tickets(small_dataset), load_platform(small_dataset), "artifactory_repo", n=12
    )
    baselines = tmp_path / "baselines"
    approve(run_replay(ts, ScriptedAgent(), tmp_path / "base", workers=2), "ben", baselines)
    plain, _ = check(ts, ScriptedAgent(), tmp_path / "c1", baseline_dir=baselines)
    assert plain.exit_code == 0 and plain.go_live is None
    gated, _ = check(ts, ScriptedAgent(), tmp_path / "c2", baseline_dir=baselines, go_live=True)
    assert gated.exit_code == 1 and gated.go_live is not None
    assert "labels not spot-checked" in gated.go_live.line()
    store = lab.LabelChecks()
    lab.record_check(ts, store, "ben")
    passing, _ = check(
        ts,
        ScriptedAgent(),
        tmp_path / "c3",
        baseline_dir=baselines,
        go_live=True,
        label_checks=store,
        min_pass_rate=0.5,
    )
    assert passing.exit_code == 0 and passing.go_live is not None and passing.go_live.passed


def test_cli_labels_sample_check_and_report(small_dataset: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    ts_path = tmp_path / "ts.json"
    labels_file = tmp_path / "labels.yaml"
    built = runner.invoke(
        app,
        [
            "build-testset",
            "--type",
            "artifactory_repo",
            "--data",
            str(small_dataset),
            "--n",
            "14",
            "--out",
            str(ts_path),
        ],
    )
    assert built.exit_code == 0, built.output
    sample = runner.invoke(app, ["labels", "sample", "--testset", str(ts_path)])
    assert sample.exit_code == 0 and "check these 10 of 14 cases" in sample.output
    assert "# Label spot-check" in sample.output
    too_few = runner.invoke(
        app,
        [
            "labels",
            "check",
            "--testset",
            str(ts_path),
            "--reviewer",
            "ben",
            "--cases",
            "OPS-1,OPS-2",
            "--labels-file",
            str(labels_file),
        ],
    )
    assert too_few.exit_code != 0 and not labels_file.exists()
    first = Suite.model_validate_json(ts_path.read_text(encoding="utf-8")).cases[0].id
    checked = runner.invoke(
        app,
        [
            "labels",
            "check",
            "--testset",
            str(ts_path),
            "--reviewer",
            "ben",
            "--set",
            f"{first}=escalate",
            "--labels-file",
            str(labels_file),
        ],
    )
    assert checked.exit_code == 0, checked.output
    assert "LBL-1: ben checked" in checked.output and f"CHANGED {first}" in checked.output
    assert (
        Suite.model_validate_json(ts_path.read_text(encoding="utf-8"))
        .case(first)
        .expectation.outcome
        == "escalate"
    )

    run_dir = tmp_path / "run"
    replayed = runner.invoke(
        app,
        [
            "replay",
            "--testset",
            str(ts_path),
            "--out",
            str(run_dir),
            "--labels-file",
            str(labels_file),
            "--min-pass-rate",
            "0.5",
        ],
    )
    assert replayed.exit_code == 0, replayed.output
    assert "passed " in replayed.output and "go-live gate" in replayed.output
    reported = runner.invoke(
        app, ["report", str(run_dir), "--labels-file", str(labels_file), "--min-pass-rate", "0.5"]
    )
    assert reported.exit_code == 0 and "labels spot-checked" in reported.output
    assert "missed_escalation" in reported.output, "the case relabelled escalate was acted on"

    # A second check that changes two of its ten labels says at once that it does not count.
    labels_args = ["--testset", str(ts_path), "--labels-file", str(labels_file)]
    resample = runner.invoke(app, ["labels", "sample", *labels_args])
    assert "(cases the 1 earlier check(s) covered come last)" in resample.output
    cases = Suite.model_validate_json(ts_path.read_text(encoding="utf-8")).cases
    flips = [
        f"{c.id}={'act' if c.expectation.outcome == 'escalate' else 'escalate'}" for c in cases[:2]
    ]
    over = runner.invoke(
        app,
        [
            "labels",
            "check",
            *labels_args,
            "--reviewer",
            "bob",
            "--cases",
            ",".join(c.id for c in cases[:10]),
            "--set",
            flips[0],
            "--set",
            flips[1],
        ],
    )
    assert over.exit_code == 0, over.output
    assert "NOTE labels spot-check LBL-2 changed 2 of the 10 labels" in over.output
    again = runner.invoke(
        app, ["report", str(run_dir), "--labels-file", str(labels_file), "--min-pass-rate", "0.5"]
    )
    assert "LBL-2 changed 2 of the 10 labels" in again.output and "does not count" in again.output
