"""The time-travel guard: knowledge tools never return the ticket under test or anything
created after it, and cut everything else at the ticket's creation time."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.agents.external import mcp_config
from replaybook.mock_env.knowledge import GET_TOOL, SEARCH_TOOL, Corpus, cut
from replaybook.mock_env.registry import MockEnvironment, describe_all_tools, server_names
from replaybook.mock_env.world import WorldState
from replaybook.schema.ticket import Comment, EngineerAction, StatusChange, Ticket

T0 = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)
ANSWER = "frobnicate-flag"


def _ticket(ticket_id: str, created: datetime, summary: str, description: str = "") -> Ticket:
    return Ticket(
        id=ticket_id,
        source="synthetic",
        project="OPS",
        summary=summary,
        description=description,
        reporter="ana",
        created=created,
        status="Open",
        status_changes=[StatusChange(at=created, to_status="Open")],
    )


def _resolve(ticket: Ticket, at: datetime, body: str, tool: str = "platform_cert_renew") -> Ticket:
    return ticket.model_copy(
        update={
            "status": "Resolved",
            "resolution": "Done",
            "resolved": at,
            "comments": [
                *ticket.comments,
                Comment(author="eng-1", role="engineer", created=at, body=body),
            ],
            "status_changes": [
                *ticket.status_changes,
                StatusChange(
                    at=at - timedelta(hours=1), from_status="Open", to_status="In Progress"
                ),
                StatusChange(at=at, from_status="In Progress", to_status="Resolved"),
            ],
            "actions": [EngineerAction(tool=tool, args={"hostname": ticket.id.lower()}, at=at)],
            "time_spent_hours": 1.0,
            "effort_source": "logged",
        }
    )


def _corpus() -> tuple[Corpus, Ticket]:
    under_test = _resolve(
        _ticket("T-NOW", T0, "Renew cert for now.example.org", "Expires soon"),
        T0 + timedelta(days=1),
        f"Done. The trick was the {ANSWER} setting.",
    )
    earlier = _resolve(
        _ticket("T-OLD", T0 - timedelta(days=30), "Renew cert for old.example.org", "Routine"),
        T0 - timedelta(days=29),
        f"Renewed; remember the {ANSWER} setting.",
    )
    later = _resolve(
        _ticket(
            "T-NEW",
            T0 + timedelta(hours=2),
            "Renew cert for new.example.org",
            f"Same {ANSWER} issue as T-NOW",
        ),
        T0 + timedelta(days=2),
        f"Used the {ANSWER} setting again.",
    )
    long_running = _resolve(
        _ticket("T-LONG", T0 - timedelta(days=3), "Renew cert for long.example.org", "Slow one"),
        T0 + timedelta(days=3),  # resolved after the horizon: the answer comment must be cut
        f"Finally done with the {ANSWER} setting.",
    )
    return Corpus([under_test, earlier, later, long_running]), under_test


def test_search_and_get_respect_the_horizon() -> None:
    corpus, under_test = _corpus()
    env = MockEnvironment(WorldState.from_ticket(under_test, {}), knowledge=corpus)
    hits = env.call(SEARCH_TOOL, {"query": ANSWER})
    ids = [h["id"] for h in hits["results"]]
    assert "T-OLD" in ids, "an earlier, resolved ticket is visible"
    assert "T-NOW" not in ids, "never the ticket under test"
    assert "T-NEW" not in ids, "nothing created after the ticket"
    assert "T-LONG" not in ids, (
        "a comment written after the horizon is cut, so it no longer matches"
    )
    assert hits["horizon"] == T0.isoformat()
    by_summary = env.call(SEARCH_TOOL, {"query": "renew cert", "limit": 10})
    assert [h["id"] for h in by_summary["results"]] == ["T-OLD", "T-LONG"]
    long_at_horizon = next(h for h in by_summary["results"] if h["id"] == "T-LONG")
    assert long_at_horizon["status"] == "Open" and long_at_horizon["resolution"] is None

    old = env.call(GET_TOOL, {"ticket_id": "T-OLD"})
    assert ANSWER in old["comments"][0]["body"] and old["status"] == "Resolved"
    assert old["actions"][0]["tool"] == "platform_cert_renew"
    long_full = env.call(GET_TOOL, {"ticket_id": "T-LONG"})
    assert long_full["comments"] == [] and long_full["actions"] == []
    assert long_full["resolved"] is None and long_full["status"] == "Open"
    assert "error" in env.call(GET_TOOL, {"ticket_id": "T-NOW"})
    assert "error" in env.call(GET_TOOL, {"ticket_id": "T-NEW"})
    assert "error" in env.call(GET_TOOL, {"ticket_id": "T-MISSING"})
    assert all(c.kind == "read" for c in env.recorder.calls), "knowledge tools are reads"
    # Leak check over everything the tools returned: the answer only ever arrives via T-OLD.
    for call in env.recorder.calls:
        text = json.dumps(call.result)
        if ANSWER in text:
            assert "T-OLD" in text and "T-NOW" not in text and "T-NEW" not in text


def test_cut_hides_everything_after_the_horizon() -> None:
    _unused, under_test = _corpus()
    at_horizon = cut(under_test, T0 + timedelta(hours=12))
    assert at_horizon.status == "Open" and at_horizon.resolved is None
    assert at_horizon.comments == [] and at_horizon.actions == []
    assert at_horizon.time_spent_hours is None and at_horizon.effort_source is None
    after = cut(under_test, T0 + timedelta(days=5))
    assert after.status == "Resolved" and after.resolution == "Done" and len(after.comments) == 1


def test_knowledge_server_is_always_present_and_empty_without_a_corpus(small_dataset: Path) -> None:
    names = server_names()
    assert names[:2] == ["ticket", "knowledge"]
    described = {t["name"]: t for t in describe_all_tools()}
    assert SEARCH_TOOL in described and GET_TOOL in described
    assert described[SEARCH_TOOL]["annotations"]["read_only_hint"] is True
    ticket = load_tickets(small_dataset)[50]
    bare = MockEnvironment(WorldState.from_ticket(ticket, load_platform(small_dataset)))
    assert bare.call(SEARCH_TOOL, {"query": "kafka"})["results"] == []
    assert "error" in bare.call(GET_TOOL, {"ticket_id": ticket.id})
    no_ticket = MockEnvironment(WorldState(), knowledge=Corpus.load(small_dataset))
    assert "error" in no_ticket.call(SEARCH_TOOL, {"query": "kafka"}), "no ticket, no horizon"


def test_dataset_corpus_never_leaks_the_future(small_dataset: Path) -> None:
    corpus = Corpus.load(small_dataset)
    assert Corpus.load(small_dataset) is corpus, "cached per directory"
    tickets = load_tickets(small_dataset)
    middle = tickets[len(tickets) // 2]
    env = MockEnvironment(
        WorldState.from_ticket(middle, load_platform(small_dataset)), knowledge=corpus
    )
    results = env.call(SEARCH_TOOL, {"query": middle.summary, "limit": 25})["results"]
    assert results, "a synthetic ticket always has look-alikes in its past"
    for hit in results:
        assert hit["id"] != middle.id
        assert datetime.fromisoformat(hit["created"]) < middle.created
        full = env.call(GET_TOOL, {"ticket_id": hit["id"]})
        for comment in full["comments"]:
            assert datetime.fromisoformat(comment["created"]) < middle.created
        for action in full["actions"]:
            assert datetime.fromisoformat(action["at"]) < middle.created
    assert Corpus.maybe(small_dataset / "nowhere") is None


def test_external_servers_get_the_corpus_path(small_dataset: Path, tmp_path: Path) -> None:
    config = mcp_config(tmp_path / "w.json", tmp_path, ["knowledge", "kafka"], small_dataset)
    args = config["mcpServers"]["knowledge"]["args"]
    assert args[-2:] == ["--knowledge", str(small_dataset)] and args[0] == "-m"
    assert config["mcpServers"]["kafka"]["command"] == sys.executable
    without = mcp_config(tmp_path / "w.json", tmp_path, ["knowledge"])
    assert "--knowledge" not in without["mcpServers"]["knowledge"]["args"]
