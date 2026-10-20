"""Knowledge packs: a team's own past-ticket search server behind the time-travel guard.

The fake server in examples/ticket_rag/ is a typical retrieval server: it returns every
match whenever it was created, with its resolution, all comments and its age. These tests
seed the same leaks as test_time_travel.py (a later ticket quoting the answer, the ticket
under test with its own resolution, a long-running ticket whose answer came later) and
prove none of it reaches the agent, with and without the server's own "as of" filter.
The server runs in-process through the MCP client; one test goes through stdio.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from replaybook import packs
from replaybook.cli import app
from replaybook.eval import retrieval
from replaybook.mock_env import guarded
from replaybook.mock_env.base import ToolError
from replaybook.mock_env.guarded import (
    GUARD_NOTE,
    GuardedSource,
    KnowledgeSpec,
    KnowledgeSpecError,
    ServerSpec,
    ToolSpec,
    before,
    expand_env,
    guard_result,
)
from replaybook.mock_env.registry import MockEnvironment
from replaybook.mock_env.remote import RemoteServer, result_to_dict
from replaybook.mock_env.world import WorldState
from replaybook.schema.ticket import Comment, StatusChange, Ticket, TicketLink

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "ticket_rag"
EXAMPLE_PACK = EXAMPLE / "packs" / "ticket_rag"
T0 = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)
ANSWER = "frobnicate-flag"


def _fake_server() -> Any:
    spec = importlib.util.spec_from_file_location("ticket_rag_server", EXAMPLE / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKE = _fake_server()


def _ticket(
    ticket_id: str,
    created: datetime,
    summary: str,
    description: str = "",
    resolved: datetime | None = None,
    answer: str = "",
    links: list[TicketLink] | None = None,
) -> Ticket:
    comments = (
        [Comment(author="eng-1", role="engineer", created=resolved, body=answer)]
        if resolved is not None and answer
        else []
    )
    return Ticket(
        id=ticket_id,
        source="synthetic",
        project="OPS",
        summary=summary,
        description=description,
        reporter="ana",
        created=created,
        resolved=resolved,
        status="Resolved" if resolved else "Open",
        resolution="Done" if resolved else None,
        status_changes=[StatusChange(at=created, to_status="Open")],
        comments=comments,
        links=links or [],
    )


def _seeded() -> tuple[list[Ticket], Ticket]:
    under_test = _ticket(
        "T-NOW",
        T0,
        "Renew cert for now.example.org",
        "Expires soon",
        T0 + timedelta(days=1),
        f"Done. The trick was the {ANSWER} setting.",
    )
    earlier = _ticket(
        "T-OLD",
        T0 - timedelta(days=30),
        "Renew cert for old.example.org",
        "Routine",
        T0 - timedelta(days=29),
        f"Renewed; remember the {ANSWER} setting.",
    )
    later = _ticket(
        "T-NEW",
        T0 + timedelta(hours=2),
        "Renew cert for new.example.org",
        f"Same {ANSWER} issue as T-NOW",
        T0 + timedelta(days=2),
        f"Used the {ANSWER} again.",
    )
    long_running = _ticket(
        "T-LONG",
        T0 - timedelta(days=3),
        "Renew cert for long.example.org",
        "Slow one",
        T0 + timedelta(days=3),
        f"Finally done with the {ANSWER} setting.",
    )
    same_day = _ticket("T-SAME", T0 - timedelta(hours=1), "Renew cert for same.example.org")
    return [under_test, earlier, later, long_running, same_day], under_test


def _rows(tickets: list[Ticket]) -> list[dict[str, Any]]:
    return [t.model_dump(mode="json") for t in tickets]


class Spy:
    """Wraps a remote and records every call it forwards."""

    def __init__(self, remote: RemoteServer) -> None:
        self.remote = remote
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, dict(args)))
        return self.remote.call(name, args)

    def close(self) -> None:
        self.remote.close()


def _install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tickets: list[Ticket], as_of: bool = True
) -> Spy:
    """The example pack, loaded from a temp packs directory next to the repo's packs, with
    the fake server in-process over the tickets given."""
    pack_dir = tmp_path / "packs" / "ticket_rag"
    shutil.copytree(EXAMPLE_PACK, pack_dir, dirs_exist_ok=True)  # keeps a recorded .cache
    if not as_of:
        manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text(encoding="utf-8"))
        for key in ("as_of_arg", "as_of_format"):
            manifest["knowledge"]["tools"]["search_tickets"].pop(key)
        (pack_dir / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    monkeypatch.setenv(
        packs.ENV_VAR, os.pathsep.join([str(ROOT / "packs"), str(tmp_path / "packs")])
    )
    packs.reset()
    pack = packs.registry().pack("ticket_rag")
    spec = pack.knowledge
    tools = json.loads((pack_dir / "tools.json").read_text(encoding="utf-8"))["tools"]
    spy = Spy(RemoteServer(spec.server, target=FAKE.build(_rows(tickets))))
    guarded._sources[pack.path.resolve()] = GuardedSource(spec, tools, spy)
    return spy


@pytest.fixture(autouse=True)
def _forget_packs() -> Any:
    yield
    packs.reset()


# --- the guard on its own ----------------------------------------------------------------


def test_before_is_strict_and_a_bare_date_must_be_a_whole_day_earlier() -> None:
    assert before("2026-06-10T08:59:59Z", T0)
    assert not before("2026-06-10T09:00:00+00:00", T0), "the horizon itself is not before"
    assert before("2026-06-10T10:00:00+02:00", T0), "time zones are honoured"
    assert before("2026-06-09T09:00:00", T0), "a naive time is UTC"
    assert before("2026-06-09", T0) and not before("2026-06-10", T0)
    assert not before(None, T0) and not before("yesterday", T0)
    assert before(date(2026, 6, 9), T0)


def test_guard_result_fails_closed() -> None:
    spec = ToolSpec.parse(
        "search",
        {
            "results": "hits",
            "id": "key",
            "created": "at",
            "resolved": "done",
            "until_resolved": ["answer"],
            "remove": ["age"],
            "count_field": "count",
        },
    )
    raw = {
        "count": 6,
        "hits": [
            {
                "key": "A",
                "at": "2026-06-01T00:00:00Z",
                "done": "2026-06-02T00:00:00Z",
                "answer": "x",
                "age": 9,
            },
            {
                "key": "B",
                "at": "2026-06-01T00:00:00Z",
                "done": "2026-06-11T00:00:00Z",
                "answer": "later",
            },
            {"key": "now", "at": "2026-06-01T00:00:00Z"},
            {"key": "C", "at": "2026-06-11T00:00:00Z"},
            {"key": "D"},
            "not a ticket",
        ],
    }
    shown, stats = guard_result(spec, raw, T0, exclude="NOW", limit=10)
    assert [h["key"] for h in shown["hits"]] == ["A", "B"]
    assert shown["hits"][0] == {
        "key": "A",
        "at": "2026-06-01T00:00:00Z",
        "done": "2026-06-02T00:00:00Z",
        "answer": "x",
    }
    assert shown["hits"][1] == {"key": "B", "at": "2026-06-01T00:00:00Z"}, (
        "resolved after the horizon: the resolution and its time are removed"
    )
    assert shown["count"] == 2 and shown["horizon"] == T0.isoformat()
    assert (stats.the_ticket_itself, stats.after_horizon, stats.undated) == (1, 1, 2)
    assert stats.leaks == 4 and stats.returned_by_server == 6 and raw["count"] == 6
    trimmed, trim_stats = guard_result(spec, raw, T0, exclude="NOW", limit=1)
    assert [h["key"] for h in trimmed["hits"]] == ["A"] and trim_stats.trimmed == 1
    assert guard_result(spec, {"ok": False, "error": "index down"}, T0, None)[0] == {
        "error": "index down"
    }
    with pytest.raises(ToolError):
        guard_result(spec, {"something": []}, T0, None)
    single = ToolSpec.parse("get", {"result": "ticket", "id": "key", "created": "at"})
    with pytest.raises(ToolError, match="not available"):
        guard_result(single, {"ticket": {"key": "C", "at": "2026-06-11T00:00:00Z"}}, T0, None)


def test_spec_errors_are_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    server = {"url": "http://rag.internal/mcp"}
    with pytest.raises(KnowledgeSpecError, match="exactly one of results"):
        KnowledgeSpec.parse({"server": server, "tools": {"s": {"id": "k"}}}, Path("."))
    with pytest.raises(KnowledgeSpecError, match="lookup"):
        KnowledgeSpec.parse(
            {"server": server, "tools": {"s": {"results": "r", "ticket_arg": "key"}}}, Path(".")
        )
    with pytest.raises(KnowledgeSpecError, match="query_arg"):
        KnowledgeSpec.parse(
            {"server": server, "search": "s", "tools": {"s": {"results": "r"}}}, Path(".")
        )
    with pytest.raises(KnowledgeSpecError, match="exactly one of url or command"):
        ServerSpec.parse({"url": "http://x/mcp", "command": ["python"]}, Path("."))
    monkeypatch.delenv("RAG_TOKEN", raising=False)
    with pytest.raises(KnowledgeSpecError, match="RAG_TOKEN is not set"):
        expand_env("Bearer ${RAG_TOKEN}")
    monkeypatch.setenv("RAG_TOKEN", "abc")
    assert expand_env("Bearer ${RAG_TOKEN}") == "Bearer abc"
    spec = ServerSpec.parse(
        {"url": "https://rag.internal/mcp?key=secret", "headers": {"A": "${RAG_TOKEN}"}}, Path(".")
    )
    assert spec.describe() == "https://rag.internal/mcp", "no query string, no headers"


def test_check_tools_names_every_mismatch() -> None:
    spec = KnowledgeSpec.parse(
        {
            "server": {"command": ["python", "server.py"]},
            "tools": {
                "search": {"results": "r", "query_arg": "q", "as_of_arg": "before"},
                "write_note": {"results": "r"},
                "missing": {"results": "r"},
            },
        },
        Path("."),
    )
    listed = [
        {
            "name": "search",
            "annotations": {"readOnlyHint": True},
            "inputSchema": {"properties": {"query": {}, "before": {}}, "required": ["before"]},
        },
        {"name": "write_note", "annotations": {}, "inputSchema": {"properties": {}}},
    ]
    problems = spec.check_tools(listed)
    assert any("query_arg 'q'" in p for p in problems)
    assert any("as_of_arg 'before' must be optional" in p for p in problems)
    assert any("write_note: not annotated read-only" in p for p in problems)
    assert any("missing: the server has no such tool" in p for p in problems)


def test_result_to_dict_shapes() -> None:
    class Text:
        def __init__(self, text: str) -> None:
            self.text = text

    class Result:
        def __init__(self, content: list[Any], structured: Any = None, error: bool = False):
            self.content, self.structured_content, self.is_error = content, structured, error

    assert result_to_dict(Result([], {"a": 1})) == {"a": 1}
    assert result_to_dict(Result([], [1, 2])) == {"result": [1, 2]}
    assert result_to_dict(Result([Text('{"b": 2}')])) == {"b": 2}
    assert result_to_dict(Result([Text("plain")])) == {"text": "plain"}
    assert result_to_dict(Result([Text("boom")], error=True)) == {"error": "boom"}


# --- the guarded tools during replay ------------------------------------------------------


@pytest.mark.parametrize("as_of", [True, False], ids=["server-as-of", "no-as-of"])
def test_guarded_tools_never_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_of: bool
) -> None:
    tickets, under_test = _seeded()
    spy = _install(tmp_path, monkeypatch, tickets, as_of)
    env = MockEnvironment(WorldState.from_ticket(under_test, {}))
    hits = env.call("search_tickets", {"query": f"renew cert {ANSWER}", "top_k": 10})
    ids = [h["key"] for h in hits["results"]]
    assert "T-OLD" in ids, "an earlier, resolved ticket is visible"
    assert "T-NOW" not in ids and "T-NEW" not in ids
    long_at_horizon = next(h for h in hits["results"] if h["key"] == "T-LONG")
    assert "resolution_note" not in long_at_horizon and "resolved" not in long_at_horizon
    assert "status" not in long_at_horizon
    assert all("age_days" not in h and "score" not in h for h in hits["results"])
    assert hits["count"] == len(hits["results"]) and hits["horizon"] == T0.isoformat()

    old = env.call("get_ticket", {"ticket_key": "T-OLD"})["ticket"]
    assert ANSWER in old["comments"][0]["body"] and old["resolution_note"] == "Done"
    assert "links" not in old
    long_full = env.call("get_ticket", {"ticket_key": "T-LONG"})["ticket"]
    assert long_full["comments"] == [] and "resolved" not in long_full
    for hidden in ("T-NOW", "T-NEW", "t-now", "T-MISSING"):
        assert "error" in env.call("get_ticket", {"ticket_key": hidden}), hidden
        assert "error" in env.call("find_similar_tickets", {"ticket_key": hidden}), hidden
    similar = env.call("find_similar_tickets", {"ticket_key": "T-OLD", "top_k": 10})
    assert {h["key"] for h in similar["results"]} <= {"T-LONG", "T-SAME"}

    assert all(c.kind == "read" for c in env.recorder.calls)
    for call in env.recorder.calls:
        text = json.dumps(call.result)
        if ANSWER in text:
            assert "T-OLD" in text and "T-NOW" not in text and "T-NEW" not in text
    searches = [args for name, args in spy.calls if name == "search_tickets"]
    assert searches[0]["top_k"] == 20, "over-fetched to max_limit, trimmed for the agent"
    if as_of:
        assert searches[0]["created_before"] == T0.isoformat()
    else:
        assert "created_before" not in searches[0]


def test_agent_sees_the_tools_without_the_horizon_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tickets, under_test = _seeded()
    spy = _install(tmp_path, monkeypatch, tickets)
    env = MockEnvironment(WorldState.from_ticket(under_test, {}))
    described = {t["name"]: t for t in env.describe_tools() if t["server"] == "ticket_rag"}
    assert set(described) == {"search_tickets", "find_similar_tickets", "get_ticket"}
    search = described["search_tickets"]
    assert set(search["input_schema"]["properties"]) == {"query", "top_k"}
    assert GUARD_NOTE in search["description"] and search["annotations"]["read_only_hint"]
    sneaky = env.call(
        "search_tickets", {"query": "renew", "top_k": 1, "created_before": "2099-01-01"}
    )
    assert "error" in sneaky, "the horizon argument is not the agent's to set"
    one = env.call("search_tickets", {"query": "renew cert", "top_k": 1})
    assert len(one["results"]) == 1 and one["count"] == 1
    assert spy.calls[-1][1]["created_before"] == T0.isoformat()


def test_no_ticket_means_no_horizon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tickets, _unused = _seeded()
    spy = _install(tmp_path, monkeypatch, tickets)
    env = MockEnvironment(WorldState())
    assert "horizon" in env.call("search_tickets", {"query": "renew"})["error"]
    assert spy.calls == [], "nothing is asked of the server without a horizon"


def test_skill_agent_is_offered_knowledge_packs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from replaybook.agents.skill_agent import SkillAgent

    tickets, _unused = _seeded()
    _install(tmp_path, monkeypatch, tickets)
    agent = SkillAgent("kafka_topic", llm=None)
    assert agent.servers == ["ticket", "knowledge", "kafka", "ticket_rag"]


def test_mock_server_serves_the_pack_over_stdio(tmp_path: Path) -> None:
    """The way an external agent reaches it: `replaybook mock-server --server ticket_rag`
    starts the team's server (here the example, over stdio) and guards every answer."""
    from mcp.client.client import Client
    from mcp.client.stdio import StdioServerParameters

    tickets, under_test = _seeded()
    data = tmp_path / "data"
    data.mkdir()
    (data / "tickets.jsonl").write_text(
        "\n".join(t.model_dump_json() for t in tickets) + "\n", encoding="utf-8"
    )
    world = tmp_path / "world.json"
    world.write_text(WorldState.from_ticket(under_test, {}).model_dump_json(), encoding="utf-8")
    record = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        packs.ENV_VAR: str(EXAMPLE / "packs"),
        "TICKET_RAG_DATA": str(data),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "replaybook.cli",
            "mock-server",
            "--server",
            "ticket_rag",
            "--world",
            str(world),
            "--record",
            str(record),
        ],
        env=env,
        cwd=str(tmp_path),
    )

    async def run() -> tuple[list[str], dict[str, Any], dict[str, Any]]:
        async with Client(params, read_timeout_seconds=60) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            hits = await client.call_tool("search_tickets", {"query": ANSWER, "top_k": 10})
            mine = await client.call_tool("get_ticket", {"ticket_key": "T-NOW"})
            return names, result_to_dict(hits), result_to_dict(mine)

    names, hits, mine = asyncio.run(asyncio.wait_for(run(), timeout=120))
    assert sorted(names) == ["find_similar_tickets", "get_ticket", "search_tickets"]
    assert "T-OLD" in [h["key"] for h in hits["results"]]
    assert "T-NOW" not in json.dumps(hits) and "T-NEW" not in json.dumps(hits)
    assert "not available" in mine.get("error", "")
    recorded = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [r["tool"] for r in recorded] == ["search_tickets", "get_ticket"]
    assert all(r["kind"] == "read" for r in recorded)


# --- knowledge eval -----------------------------------------------------------------------


def test_ground_truth_is_symmetric_earlier_only_and_reads_mentions() -> None:
    a = _ticket("A-1", T0 - timedelta(days=10), "first")
    b = _ticket(
        "A-2", T0 - timedelta(days=5), "second", links=[TicketLink(type="duplicates", target="a-1")]
    )
    c = _ticket("A-3", T0, "third").model_copy(
        update={"comments": [Comment(author="e", role="engineer", created=T0, body="see A-1, A-9")]}
    )
    d = _ticket("A-4", T0 + timedelta(days=1), "fourth")
    a = a.model_copy(update={"links": [TicketLink(type="relates", target="A-4")]})
    truth = retrieval.ground_truth([a, b, c, d])
    assert truth == {"A-2": {"A-1": 2}, "A-3": {"A-1": 1}, "A-4": {"A-1": 1}}
    cases = retrieval.select_cases([a, b, c, d], truth, n=2)
    assert [t.id for t in cases] == ["A-4", "A-3"], "newest first"


def test_score_ranking() -> None:
    scores = retrieval.score_ranking(["X", "A", "x", "B"], {"A": 2, "B": 1, "C": 1}, 3)
    assert scores["hit@1"] == 0.0 and scores["hit@3"] == 1.0
    assert scores["recall@3"] == pytest.approx(2 / 3)
    assert scores["mrr"] == 0.5
    assert 0 < scores["ndcg@3"] < 1 and scores["duplicate_found@3"] == 1.0
    assert set(retrieval.cutoffs(7)) == {1, 3, 5, 7}


@pytest.mark.parametrize("fail_fast", [False, True])
def test_run_source_reports_each_completion_without_waiting_for_case_order(
    fail_fast: bool,
) -> None:
    release_slow = Event()
    cases = [_ticket("T-SLOW", T0, "slow case"), _ticket("T-FAST", T0, "fast case")]
    truth = {ticket.id: {"T-RELATED": 1} for ticket in cases}
    updates: list[tuple[int, int, int]] = []

    def search(ticket: Ticket, _query: str, _limit: int) -> retrieval.Found:
        if ticket.id == "T-SLOW":
            assert release_slow.wait(5), "progress waited for the first input"
        elif fail_fast:
            raise RuntimeError("synthetic search failure")
        return retrieval.Found(["T-RELATED"])

    def progress(completed: int, total: int, errors: int) -> None:
        updates.append((completed, total, errors))
        if completed == 1:
            release_slow.set()

    scores = retrieval.run_source(search, cases, truth, 5, 1000, workers=2, progress=progress)
    assert updates == [(0, 2, 0), (1, 2, int(fail_fast)), (2, 2, int(fail_fast))]
    assert [score.ticket_id for score in scores] == [ticket.id for ticket in cases]
    assert scores[0].error is None
    assert (scores[1].error is not None) == fail_fast


def test_run_source_progress_handles_no_cases() -> None:
    updates: list[tuple[int, int, int]] = []
    scores = retrieval.run_source(
        lambda *_args: retrieval.Found([]),
        [],
        {},
        5,
        1000,
        progress=lambda completed, total, errors: updates.append((completed, total, errors)),
    )
    assert scores == [] and updates == [(0, 0, 0)]


def test_knowledge_eval_progress_is_written_to_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    from replaybook import workflow

    def evaluate(*args: Any, progress: Any) -> dict[str, str]:
        progress("sample", 0, 2, 0)
        progress("sample", 1, 2, 0)
        progress("sample", 2, 2, 1)
        return {"message": "report ready"}

    monkeypatch.setattr(workflow, "knowledge_eval", evaluate)
    result = CliRunner().invoke(app, ["knowledge", "eval", "--n", "2"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "report ready\n"
    assert result.stderr.splitlines() == [
        "sample: completed 0/2 (errors: 0)",
        "sample: completed 1/2 (errors: 0)",
        "sample: completed 2/2 (errors: 1)",
    ]


def test_eval_compares_builtin_and_the_pack(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from replaybook.adapters.synthetic import load_tickets

    tickets = load_tickets(small_dataset)
    _install(tmp_path, monkeypatch, tickets, as_of=False)
    out = tmp_path / "retrieval"
    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "eval",
            "--data",
            str(small_dataset),
            "--source",
            "builtin",
            "--source",
            "ticket_rag",
            "--n",
            "30",
            "--k",
            "5",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "builtin: hit@5" in result.output and "ticket_rag: hit@5" in result.output
    report = json.loads((out / "retrieval.json").read_text(encoding="utf-8"))
    assert set(report["sources"]) == {"builtin", "ticket_rag"}
    for source in report["sources"]:
        assert f"{source}: completed 0/{report['cases']} (errors: 0)" in result.stderr
        assert (
            f"{source}: completed {report['cases']}/{report['cases']} (errors: 0)" in result.stderr
        )
    pack = report["sources"]["ticket_rag"]
    assert pack["errors"] == 0 and pack["guard"]["leaks"] > 0, (
        "without an as-of filter the server returns the ticket itself and later ones"
    )
    created = {t.id: t.created for t in tickets}
    for line in (out / "cases_ticket_rag.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        assert case["ticket_id"] not in case["returned"]
        assert all(created[r] < created[case["ticket_id"]] for r in case["returned"])
    markdown = (out / "retrieval.md").read_text(encoding="utf-8")
    assert "| Hit@5 (95% CI) |" in markdown and "Leaks the guard removed" in markdown
    bad = CliRunner().invoke(
        app,
        ["knowledge", "eval", "--data", str(small_dataset), "--source", "kafka", "--out", str(out)],
    )
    assert bad.exit_code != 0 and "not a knowledge pack" in bad.output


@pytest.mark.parametrize("agent", ["", "skill"])
def test_knowledge_eval_invalid_pack_is_a_usage_error(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    from replaybook import workflow

    pack_dir = tmp_path / "packs" / "broken"
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(
        yaml.safe_dump({"name": "broken", "knowledge": {"server": {"command": ["unused"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(packs.ENV_VAR, str(pack_dir.parent))
    packs.reset()
    out = tmp_path / "retrieval"
    with pytest.raises(
        workflow.WorkflowError, match=r"knowledge\.tools must list at least one tool"
    ):
        workflow.knowledge_eval(
            data=small_dataset,
            sources=["broken"],
            out=out,
            agent=agent,
            request_type="broken_research",
        )
    args = [
        "knowledge",
        "eval",
        "--data",
        str(small_dataset),
        "--source",
        "broken",
        "--out",
        str(out),
    ]
    if agent:
        args += ["--agent", agent, "--type", "broken_research"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "knowledge.tools" in result.output
    assert "Traceback" not in result.output
    assert not out.exists()


# --- import and check ---------------------------------------------------------------------


def test_import_writes_a_skeleton_that_check_rejects_until_filled_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = json.loads((EXAMPLE_PACK / "tools.json").read_text(encoding="utf-8"))["tools"]
    listed.append({"name": "add_comment", "inputSchema": {"properties": {}}, "annotations": {}})
    saved = tmp_path / "tools.json"
    saved.write_text(json.dumps({"tools": listed}), encoding="utf-8")
    out = tmp_path / "packs"
    imported = CliRunner().invoke(
        app, ["knowledge", "import", "--name", "rag", "--json", str(saved), "--out", str(out)]
    )
    assert imported.exit_code == 0, imported.output
    assert "left out (not annotated read-only): add_comment" in imported.output
    manifest = yaml.safe_load((out / "rag" / "pack.yaml").read_text(encoding="utf-8"))
    knowledge = manifest["knowledge"]
    assert knowledge["search"] == "search_tickets" and knowledge["lookup"] == "get_ticket"
    search = knowledge["tools"]["search_tickets"]
    assert (search["query_arg"], search["limit_arg"], search["as_of_arg"]) == (
        "query",
        "top_k",
        "created_before",
    )
    assert knowledge["tools"]["get_ticket"]["ticket_arg"] == "ticket_key"
    assert "add_comment" not in knowledge["tools"]

    monkeypatch.setenv(packs.ENV_VAR, str(out))
    packs.reset()
    checked = CliRunner().invoke(app, ["knowledge", "check", "rag"])
    assert checked.exit_code == 1 and "is still CHANGE_ME" in checked.output
    listed_packs = CliRunner().invoke(app, ["packs"])
    assert "knowledge pack: http ${RAG_URL}" in listed_packs.output


def test_check_probes_through_the_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tickets, _unused = _seeded()
    _install(tmp_path, monkeypatch, tickets)
    result = CliRunner().invoke(
        app, ["knowledge", "check", "ticket_rag", "--query", "renew cert", "--at", T0.isoformat()]
    )
    assert result.exit_code == 0, result.output
    assert "the agent would see" in result.output and "ok" in result.output
    assert "fields of the first raw result: age_days, created, key" in result.output


# --- the cache, run records and evidence ----------------------------------------------------


def _set_cache(tmp_path: Path, mode: Any) -> None:
    path = tmp_path / "packs" / "ticket_rag" / "pack.yaml"
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    manifest["knowledge"]["cache"] = mode
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")


def _install_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tickets: list[Ticket], mode: Any
) -> tuple[Spy, Any]:
    """The example pack with a cache mode, its cache built the way source_for builds it."""
    spy = _install(tmp_path, monkeypatch, tickets)
    _set_cache(tmp_path, mode)
    packs.reset()
    pack = packs.registry().pack("ticket_rag")
    tools = json.loads((pack.path / "tools.json").read_text(encoding="utf-8"))["tools"]
    cache = guarded.ResponseCache(
        pack.knowledge.cache.effective(pack.name), pack.knowledge.server.describe()
    )
    spy = Spy(spy.remote)
    guarded._sources[pack.path.resolve()] = GuardedSource(pack.knowledge, tools, spy, cache)
    return spy, pack


def test_cache_modes_parse_and_default_to_off(tmp_path: Path) -> None:
    for value, mode in [
        (None, "off"),
        (False, "off"),
        ("off", "off"),
        (True, "record"),
        ("record", "record"),
        ({"mode": "replay", "dir": "pinned"}, "replay"),
    ]:
        spec = guarded.CacheSpec.parse(value, tmp_path)
        assert spec.mode == mode, value
    assert guarded.CacheSpec.parse({"mode": "replay", "dir": "pinned"}, tmp_path).dir == (
        tmp_path / "pinned"
    )
    assert yaml.safe_load("cache: off")["cache"] is False, "YAML reads a bare off as false"
    with pytest.raises(KnowledgeSpecError, match="off, record, replay"):
        guarded.CacheSpec.parse("sometimes", tmp_path)


def test_record_then_replay_pins_the_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tickets, under_test = _seeded()
    spy, pack = _install_cached(tmp_path, monkeypatch, tickets, "record")
    env = MockEnvironment(WorldState.from_ticket(under_test, {}))
    first = env.call("search_tickets", {"query": "renew cert", "top_k": 3})
    again = env.call("search_tickets", {"query": "renew cert", "top_k": 3})
    assert first == again and len(spy.calls) == 1, "the second call came from the cache"
    assert "error" in env.call("get_ticket", {"ticket_key": "T-MISSING"})
    assert "error" in env.call("get_ticket", {"ticket_key": "T-MISSING"})
    assert len(spy.calls) == 3, "a server error is not stored, so it is asked again"
    stored = list((pack.path / ".cache").rglob("*.json"))
    raw = stored[0].read_text(encoding="utf-8") if len(stored) == 1 else ""
    assert '"age_days"' in raw and '"age_days"' not in json.dumps(first), (
        "the cache keeps the raw answer; the guard runs again on every read"
    )
    digest = guarded.cache_digest(pack.path / ".cache")
    assert digest is not None and len(digest) == 64

    monkeypatch.setenv(guarded.CACHE_MODE_ENV, "replay")
    spy, pack = _install_cached(tmp_path, monkeypatch, tickets, "record")
    env = MockEnvironment(WorldState.from_ticket(under_test, {}))
    assert env.call("search_tickets", {"query": "renew cert", "top_k": 3}) == first
    missing = env.call("search_tickets", {"query": "something never asked", "top_k": 3})
    assert "replay mode" in missing["error"] and spy.calls == [], "replay never calls the server"


def test_cache_dir_can_live_outside_the_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tickets, under_test = _seeded()
    monkeypatch.setenv(guarded.CACHE_DIR_ENV, str(tmp_path / "shared"))
    _spy, _pack = _install_cached(tmp_path, monkeypatch, tickets, "record")
    MockEnvironment(WorldState.from_ticket(under_test, {})).call(
        "search_tickets", {"query": "renew cert"}
    )
    assert list((tmp_path / "shared" / "ticket_rag").rglob("*.json"))


def test_runs_record_their_knowledge_packs_and_evidence_says_so(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from replaybook.eval.evidence import knowledge_limitations

    _install(tmp_path, monkeypatch, _seeded()[0])
    runner = CliRunner()
    testset = tmp_path / "kafka.json"
    built = runner.invoke(
        app,
        [
            "build-testset",
            "--type",
            "kafka_topic",
            "--data",
            str(small_dataset),
            "--n",
            "3",
            "--out",
            str(testset),
        ],
    )
    assert built.exit_code == 0, built.output
    replayed = runner.invoke(
        app,
        [
            "replay",
            "--testset",
            str(testset),
            "--out",
            str(tmp_path / "run"),
            "--agent",
            "scripted",
        ],
    )
    assert replayed.exit_code in (0, 1), replayed.output
    run = json.loads((tmp_path / "run" / "results.json").read_text(encoding="utf-8"))
    recorded = run["environment"]["knowledge_packs"]
    assert recorded == {
        "ticket_rag": {
            "server": "python ../../server.py --data ${TICKET_RAG_DATA}",
            "transport": "stdio",
            "cache": "off",
            "cache_sha256": None,
        }
    }, "the reference to the variable is recorded, never its value"
    (live,) = knowledge_limitations([run["environment"]])
    assert "live index" in live and "cache: replay" in live
    pinned = {
        "ticket_rag": {**recorded["ticket_rag"], "cache": "replay", "cache_sha256": "ab" * 32}
    }
    (fixed,) = knowledge_limitations([{"knowledge_packs": pinned}])
    assert "recorded responses (cache sha256 abababababab)" in fixed
    (mixed,) = knowledge_limitations([{"knowledge_packs": pinned}, run["environment"]])
    assert "live index" in mixed, "one live run makes the pack live in the evidence"
    assert knowledge_limitations([{"air_gapped": True}]) == []


def test_eval_reports_intervals_and_a_ticket_by_ticket_comparison(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from replaybook.adapters.synthetic import load_tickets

    _install(tmp_path, monkeypatch, load_tickets(small_dataset))
    result = retrieval.evaluate(
        small_dataset, ["builtin", "ticket_rag"], tmp_path / "out", n=30, k=5
    )
    assert result["baseline"] == "builtin" and set(result["comparison"]) == {"ticket_rag"}
    compared = result["comparison"]["ticket_rag"]
    assert compared["better"] + compared["worse"] + compared["same"] == compared["cases"]
    assert 0 <= compared["sign_test_p"] <= 1
    low, high = result["sources"]["builtin"]["intervals"]["hit@5"]
    assert low <= result["sources"]["builtin"]["metrics"]["hit@5"] <= high
    markdown = (tmp_path / "out" / "retrieval.md").read_text(encoding="utf-8")
    assert "Hit@5 (95% CI)" in markdown and "Ticket by ticket against builtin" in markdown
    assert retrieval.sign_test(10, 0) == pytest.approx(2 / 1024)
    assert retrieval.sign_test(3, 3) == 1.0 and retrieval.sign_test(0, 0) == 1.0


# --- other organisations' setups -----------------------------------------------------------


# A throwaway self-signed CA certificate (no key kept anywhere): something for
# ssl.create_default_context to load.
TEST_CA = """-----BEGIN CERTIFICATE-----
MIIBkTCCATegAwIBAgIUKHiV5lbhUvEyYeo1adrJDXaptvwwCgYIKoZIzj0EAwIw
HTEbMBkGA1UEAwwSUmVwbGF5Ym9vayB0ZXN0IENBMCAXDTI2MTAyMDAzMTE0NFoY
DzIxMjYwOTI2MDMxMTQ0WjAdMRswGQYDVQQDDBJSZXBsYXlib29rIHRlc3QgQ0Ew
WTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAASA3HOsm7gnMw4LXWxOyJ++YZsOvQz8
+H9He5Wbl8hp0NN1lFZNoF6ET9RLQaoCAeySf6oVj3zUzGf8QnUxinH4o1MwUTAd
BgNVHQ4EFgQU6HdHhYNPxePNgg5DOrGMLW3jVRAwHwYDVR0jBBgwFoAU6HdHhYNP
xePNgg5DOrGMLW3jVRAwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNIADBF
AiBz0kmq3Qh4kYLm3lR/IcvE3MNVBQWH2pBlq+AaqwlPLgIhAMeMJiFMgixHHbny
bXx5rENctqcRNW7IkaSzjcFMrgsP
-----END CERTIFICATE-----
"""


def test_ca_bundle_for_a_server_behind_a_private_certificate_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ssl

    with pytest.raises(KnowledgeSpecError, match="url only"):
        ServerSpec.parse({"command": ["python"], "ca_bundle": "ca.pem"}, tmp_path)
    spec = ServerSpec.parse({"url": "https://rag.internal/mcp", "ca_bundle": "ca.pem"}, tmp_path)
    with pytest.raises(KnowledgeSpecError, match="no file at"):
        spec.ca_path()
    (tmp_path / "ca.pem").write_text(TEST_CA, encoding="utf-8")
    assert spec.ca_path() == tmp_path / "ca.pem"
    monkeypatch.setenv("RAG_CA", str(tmp_path / "ca.pem"))
    from_env = ServerSpec.parse(
        {"url": "https://rag.internal/mcp", "ca_bundle": "${RAG_CA}"}, Path("/")
    )
    assert from_env.ca_path() == tmp_path / "ca.pem"
    loaded: list[str] = []
    real = ssl.create_default_context

    def spy(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        loaded.append(str(kwargs.get("cafile")))
        return real(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", spy)
    client = RemoteServer(spec)._http_client()
    asyncio.run(client.aclose())
    assert str(tmp_path / "ca.pem") in loaded
    plain = RemoteServer(ServerSpec.parse({"url": "https://rag.example/mcp"}, tmp_path))
    loaded.clear()
    asyncio.run(plain._http_client().aclose())
    assert str(tmp_path / "ca.pem") not in loaded, "no bundle named, the default trust store"


def test_servers_started_by_someone_else_get_their_settings_from_a_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agents and harnesses start mock servers with their own, minimal environment; the
    settings travel in a per-case file whose path, not contents, is in the arguments."""
    from replaybook.agents.external import case_files, server_environment
    from replaybook.cli import apply_env_file

    tickets, under_test = _seeded()
    _install(tmp_path, monkeypatch, tickets)
    monkeypatch.setenv("TICKET_RAG_DATA", "/data/tickets")
    monkeypatch.setenv("REPLAYBOOK_MODEL_API_KEY", "not-for-mock-servers")
    monkeypatch.chdir(tmp_path)
    settings = server_environment()
    assert settings["TICKET_RAG_DATA"] == "/data/tickets", "a variable the pack refers to"
    assert "REPLAYBOOK_MODEL_API_KEY" not in settings, "nothing a mock server does not need"
    assert all(Path(p).is_absolute() for p in settings["REPLAYBOOK_PACKS"].split(os.pathsep))
    env = MockEnvironment(WorldState.from_ticket(under_test, {}))
    with case_files(env) as files:
        assert files.env_path.is_file()
        if os.name == "posix":
            assert files.env_path.stat().st_mode & 0o777 == 0o600
        args = files.mcp_servers()["ticket_rag"]["args"]
        assert args[args.index("--env-file") + 1] == str(files.env_path)
        assert "/data/tickets" not in json.dumps(files.mcp_servers())
        monkeypatch.delenv("TICKET_RAG_DATA")
        apply_env_file(files.env_path)
        assert os.environ["TICKET_RAG_DATA"] == "/data/tickets"


def test_import_writes_a_research_type_and_a_valid_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "packs"
    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "import",
            "--name",
            "rag",
            "--json",
            str(EXAMPLE_PACK / "tools.json"),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    skill = (out / "rag" / "skills" / "research" / "SKILL.md").read_text(encoding="utf-8")
    assert "request_type: rag_research" in skill and "`search_tickets`" in skill
    monkeypatch.setenv(packs.ENV_VAR, str(out))
    packs.reset()
    validated = CliRunner().invoke(app, ["skills", "validate"])
    assert validated.exit_code == 0 and "rag_research" in validated.output, validated.output


def test_agent_researches_each_ticket_behind_the_guard(
    small_dataset: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The external research agent runs as its own process and starts its own mock servers
    (which start the example search server); it is scored on the ids it cites."""
    monkeypatch.setenv(
        packs.ENV_VAR, os.pathsep.join([str(ROOT / "packs"), str(EXAMPLE / "packs")])
    )
    monkeypatch.setenv("TICKET_RAG_DATA", str(small_dataset))
    packs.reset()
    out = tmp_path / "out"
    command = f'"{sys.executable}" "{EXAMPLE / "research_agent.py"}"'
    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "eval",
            "--data",
            str(small_dataset),
            "--source",
            "ticket_rag",
            "--agent",
            "external",
            "--type",
            "ticket_research",
            "--command",
            command,
            "--n",
            "3",
            "--k",
            "5",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads((out / "retrieval.json").read_text(encoding="utf-8"))
    agent = report["sources"]["agent-external"]
    assert agent["errors"] == 0, agent["first_errors"]
    assert agent["agent"]["tool_calls_mean"] == 3.0 and agent["agent"]["unseen_citations"] == 0
    assert report["agent"]["agent"] == "external"
    created = {
        t["id"]: t["created"]
        for t in map(
            json.loads, (small_dataset / "tickets.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    for line in (out / "cases_agent-external.jsonl").read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        assert case["returned"], "the agent cited what the search returned"
        assert all(created[r] < created[case["ticket_id"]] for r in case["returned"])
    for bad, why in ((["--agent", "scripted"], "scripted"), (["--agent", "skill"], "--type")):
        refused = CliRunner().invoke(
            app, ["knowledge", "eval", "--data", str(small_dataset), *bad, "--out", str(out)]
        )
        assert refused.exit_code != 0 and why in refused.output


def test_citations_are_dataset_ids_in_order_without_the_ticket_itself() -> None:
    known = {"ops-1": "OPS-1", "ops-2": "OPS-2", "ops-3": "OPS-3"}
    text = "Related: OPS-2, ops-1 (and OPS-9, OPS-3 again OPS-2); this is OPS-3"
    assert retrieval.citations(text, known, exclude="OPS-3") == ["OPS-2", "OPS-1"]
