"""Conformance: a pack the core has never seen works end to end with zero core changes.

The fake payroll MCP server in examples/payroll/ is imported with `tools import`, the
overlay a person would write (request types, a scripted procedure, a skill) is copied in,
and the whole pipeline runs through the CLI against that pack alone: test set, replay,
groups, evidence pack. The core files are hashed before and after to prove nothing in
src/ was touched; the vendor-word test proves nothing in src/ knows the pack's tools.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from replaybook import packs
from replaybook.adapters.synthetic import load_tickets
from replaybook.agents.scripted import ScriptedAgent
from replaybook.agents.skill_agent import SkillAgent
from replaybook.cli import app
from replaybook.eval.runner import run_replay
from replaybook.eval.scoring import score_case
from replaybook.eval.testset import build_test_set
from replaybook.llm.fake import FakeLLM, text_response, tool_call_response
from replaybook.mock_env.recorder import RecordedCall
from replaybook.mock_env.registry import ASK_TOOL, MockEnvironment
from replaybook.mock_env.world import WorldState
from replaybook.schema.testcase import TestSet as Suite

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "replaybook"
EXAMPLE = ROOT / "examples" / "payroll"
runner = CliRunner()


def _src_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(SRC.rglob("*.py")):
        digest.update(str(path.relative_to(SRC)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def payroll_pack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Import the payroll server as a pack, apply the overlay, make it the only pack."""
    packs_dir = tmp_path_factory.mktemp("packs")
    command = f'"{sys.executable}" "{EXAMPLE / "server.py"}"'
    result = runner.invoke(
        app, ["tools", "import", "--name", "payroll", "--command", command, "--out", str(packs_dir)]
    )
    assert result.exit_code == 0, result.output
    assert "5 tools (2 reads, 3 writes, 1 destructive)" in result.output
    pack = packs_dir / "payroll"
    shutil.copytree(EXAMPLE / "pack-overlay", pack, dirs_exist_ok=True)
    data_dir = tmp_path_factory.mktemp("data")
    shutil.copy(EXAMPLE / "tickets.jsonl", data_dir / "tickets.jsonl")
    before = _src_digest()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(packs.ENV_VAR, str(packs_dir))
        packs.reset()
        yield data_dir
        packs.reset()
    assert _src_digest() == before, "the conformance run must not change the core"
    assert [p.name for p in packs.registry().packs] != ["payroll"]


def test_only_the_payroll_pack_is_loaded(payroll_pack: Path) -> None:
    reg = packs.registry()
    assert [p.name for p in reg.packs] == ["payroll"]
    assert reg.tool_types == {"payroll_set_pay_rate": "pay_rate_change"}
    assert reg.unsafe["payroll_delete_employee"][0] == "critical"
    assert set(reg.skills()) == {"pay_rate_change"}
    assert set(reg.procedures()) == {"pay_rate_change"}


def test_pipeline_through_the_cli(payroll_pack: Path, tmp_path: Path) -> None:
    ts_path = tmp_path / "pay_rate_change.json"
    built = runner.invoke(
        app,
        [
            "build-testset",
            "--type",
            "pay_rate_change",
            "--data",
            str(payroll_pack),
            "--n",
            "20",
            "--out",
            str(ts_path),
        ],
    )
    assert built.exit_code == 0, built.output
    test_set = Suite.model_validate_json(ts_path.read_text(encoding="utf-8"))
    assert len(test_set.cases) == 10, built.output
    assert test_set.selection["ask_cases"] == 2
    assert (
        test_set.selection["excluded"] == {"no recorded actions": 0}
        or "no recorded actions" not in test_set.selection["excluded"]
    )
    assert all(set(c.world.state) == {"payroll"} for c in test_set.cases)

    fixed = runner.invoke(
        app,
        [
            "replay",
            "--testset",
            str(ts_path),
            "--out",
            str(tmp_path / "fixed"),
            "--workers",
            "2",
            "--accepted-file",
            str(tmp_path / "acc.yaml"),
        ],
    )
    assert fixed.exit_code == 0, fixed.output
    assert "10 pass, 0 fail" in fixed.output and "gate PASS" in fixed.output

    flawed = runner.invoke(
        app,
        [
            "replay",
            "--testset",
            str(ts_path),
            "--out",
            str(tmp_path / "flawed"),
            "--workers",
            "2",
            "--flaws",
            "guess_missing_info",
            "--accepted-file",
            str(tmp_path / "acc.yaml"),
        ],
    )
    assert flawed.exit_code == 0, flawed.output  # no critical action, so the gate holds
    assert "8 pass, 2 fail" in flawed.output and "guessed_missing_info 2" in flawed.output

    grouped = runner.invoke(app, ["groups", str(tmp_path / "flawed")])
    assert grouped.exit_code == 0 and "payroll_set_pay_rate" in grouped.output
    evidence = runner.invoke(
        app,
        [
            "evidence",
            "--run",
            str(tmp_path / "flawed"),
            "--testset",
            str(ts_path),
            "--out",
            str(tmp_path / "evidence"),
            "--accepted-file",
            str(tmp_path / "acc.yaml"),
            "--baseline-dir",
            str(tmp_path / "baselines"),
        ],
    )
    assert evidence.exit_code == 0, evidence.output
    data = json.loads((tmp_path / "evidence" / "evidence.json").read_text(encoding="utf-8"))
    assert data["pass_rates"][0]["request_type"] == "pay_rate_change"
    html = (tmp_path / "evidence" / "evidence.html").read_text(encoding="utf-8")
    assert "payroll_set_pay_rate" in html and "Pay rate change" in html


def test_destructive_payroll_tool_is_critical(payroll_pack: Path) -> None:
    tickets = load_tickets(payroll_pack)
    test_set = build_test_set(tickets, {}, "pay_rate_change", n=5)
    case = next(c for c in test_set.cases if c.expectation.kind == "actions")
    expected = case.expectation.actions[0]
    writes = [
        RecordedCall(
            seq=1,
            tool="payroll_delete_employee",
            kind="write",
            args={"employee_id": expected.args["employee_id"]},
            at=case.created,
        ),
        RecordedCall(
            seq=2, tool=expected.tool, kind="write", args=dict(expected.args), at=case.created
        ),
    ]
    result = score_case(case, writes)
    assert result.reason == "unsafe_action" and result.severity == "critical" and result.is_critical
    assert "annotated destructive" in result.diffs[0].detail


def test_skill_agent_and_external_servers_see_the_pack(payroll_pack: Path) -> None:
    tickets = load_tickets(payroll_pack)
    ticket = next(
        t
        for t in tickets
        if t.actions and not any(c.to_status == "Waiting on Requester" for c in t.status_changes)
    )
    env = MockEnvironment(WorldState.from_ticket(ticket, {}))
    employee = ticket.actions[0].args["employee_id"]
    llm = FakeLLM(
        responses=[
            tool_call_response(("payroll_get_employee", {"employee_id": employee})),
            tool_call_response(
                ("payroll_set_pay_rate", {"employee_id": employee, "hourly_rate": 45.5})
            ),
            text_response("Done."),
        ]
    )
    result = SkillAgent("pay_rate_change", llm).run(env)
    assert result.stopped_reason == "done"
    assert [w.tool for w in env.recorder.writes] == ["payroll_set_pay_rate"]
    assert "payroll_set_pay_rate" in llm.calls[0]["messages"][0]["content"]
    assert env.call("payroll_get_employee", {"employee_id": employee})["employee_id"] == employee

    # The scripted agent asks when the rate is missing and acts when it is there.
    asked_ticket = next(
        t for t in tickets if any(c.to_status == "Waiting on Requester" for c in t.status_changes)
    )
    env = MockEnvironment(WorldState.from_ticket(asked_ticket, {}))
    ScriptedAgent().run(env)
    assert [w.tool for w in env.recorder.writes] == [ASK_TOOL]
    run = run_replay(
        build_test_set(tickets, {}, "pay_rate_change", n=10),
        ScriptedAgent(),
        payroll_pack / "run",
        workers=2,
    )
    assert run.summary.passed == 10 and run.summary.gate == "pass"
    assert run.tool_fingerprint
