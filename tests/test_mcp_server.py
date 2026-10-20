"""`replaybook mcp`: the workflow as MCP tools, driven by an MCP client in-process. The tools
call the same functions as the CLI and leave the same files on disk."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.client.client import Client
from typer.testing import CliRunner

from replaybook.cli import app
from replaybook.eval.runner import load_run
from replaybook.mcp_server import build_server
from replaybook.schema.testcase import TestSet as Suite

EXPECTED_TOOLS = {
    "replaybook_list_packs",
    "replaybook_list_launchers",
    "replaybook_generate",
    "replaybook_build_testset",
    "replaybook_replay",
    "replaybook_report",
    "replaybook_groups",
    "replaybook_check",
    "replaybook_evidence",
    "replaybook_shadow_record",
    "replaybook_shadow_score",
    "replaybook_shadow_report",
    "replaybook_labels_sample",
    "replaybook_skills_validate",
    "replaybook_live_report",
    "replaybook_approval_request",
    "replaybook_knowledge_eval",
}


class Driver:
    """Calls the server through the SDK's in-memory client, one call per connection."""

    def __init__(self) -> None:
        self.server = build_server()

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        async def run() -> tuple[bool, Any]:
            async with Client(self.server) as client:
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    return True, result.content[0].text if result.content else "error"
                return False, result.structured_content

        # Raise outside the client context, or the task group wraps it in an ExceptionGroup.
        failed, payload = asyncio.run(run())
        if failed:
            raise RuntimeError(payload)
        assert payload is not None
        return payload

    def list_tools(self) -> list[Any]:
        async def run() -> list[Any]:
            async with Client(self.server) as client:
                return list((await client.list_tools()).tools)

        return asyncio.run(run())


def test_tool_surface_matches_the_workflow() -> None:
    tools = {t.name: t for t in Driver().list_tools()}
    assert set(tools) == EXPECTED_TOOLS
    replay = tools["replaybook_replay"]
    assert set(replay.input_schema["required"]) == {"testset", "out"}
    assert replay.input_schema["properties"]["agent"]["default"] == "scripted"
    assert "launcher" in (replay.description or "")
    assert replay.annotations is not None and replay.annotations.read_only_hint is False
    assert tools["replaybook_report"].annotations.read_only_hint is True
    assert tools["replaybook_list_packs"].annotations.read_only_hint is True
    # Sign-off stays with a person at the CLI.
    assert not {n for n in tools if "accept" in n or "approve" in n}


def test_whole_workflow_through_the_mcp_tools(tmp_path: Path) -> None:
    driver = Driver()
    packs = driver.call("replaybook_list_packs")
    types = {rt["key"] for p in packs["packs"] for rt in p["request_types"]}
    assert "kafka_topic" in types and packs["message"].startswith("packs:")

    data = tmp_path / "data"
    generated = driver.call("replaybook_generate", out=str(data), seed=7, n=600)
    assert generated["tickets"] >= 600 and (data / "tickets.jsonl").exists()
    assert "Wrote" in generated["message"]

    ts_path = tmp_path / "ts.json"
    built = driver.call(
        "replaybook_build_testset",
        request_type="kafka_topic",
        data=str(data),
        n=12,
        out=str(ts_path),
    )
    assert built["cases"] == 12 and built["selection"]["selected"] == 12
    test_set = Suite.model_validate_json(ts_path.read_text(encoding="utf-8"))
    assert test_set.request_type == "kafka_topic"

    acc = tmp_path / "accepted.yaml"
    baselines = tmp_path / "baselines"
    v1 = driver.call(
        "replaybook_replay",
        testset=str(ts_path),
        out=str(tmp_path / "v1"),
        flaws="guess_missing_info,unsafe_write",
        workers=2,
        accepted_file=str(acc),
    )
    assert v1["summary"]["total"] == 12 and v1["gate"] in ("pass", "fail")
    assert v1["exit_code"] == (0 if v1["gate"] == "pass" else 1)
    assert "pass rate" in v1["message"]
    run = load_run(tmp_path / "v1")
    assert run.agent_config["flaws"] == ["guess_missing_info", "unsafe_write"]

    reported = driver.call("replaybook_report", run_dir=str(tmp_path / "v1"))
    assert reported["summary"] == v1["summary"]
    assert len(reported["non_passing"]) == v1["summary"]["failed"] + v1["summary"]["errors"]

    grouped = driver.call("replaybook_groups", run_dir=str(tmp_path / "v1"))
    assert (tmp_path / "v1" / "groups.json").exists()
    assert len(grouped["groups"]) == len(
        json.loads((tmp_path / "v1" / "groups.json").read_text(encoding="utf-8"))
    )
    if grouped["groups"]:
        assert grouped["groups"][0]["examples"] and grouped["groups"][0]["cause"]

    # No baseline yet: check reports exit code 2 and says why, without crashing.
    unchecked = driver.call(
        "replaybook_check",
        testset=str(ts_path),
        out=str(tmp_path / "check"),
        baseline_dir=str(baselines),
        accepted_file=str(acc),
    )
    assert unchecked["exit_code"] == 2 and "no baseline" in unchecked["message"]

    # A person approves at the CLI; the MCP server has no such tool.
    approved = CliRunner().invoke(
        app,
        [
            "approve",
            "--run",
            str(tmp_path / "v1"),
            "--approver",
            "ben",
            "--baseline-dir",
            str(baselines),
        ],
    )
    assert approved.exit_code == 0, approved.output
    checked = driver.call(
        "replaybook_check",
        testset=str(ts_path),
        out=str(tmp_path / "check"),
        baseline_dir=str(baselines),
        accepted_file=str(acc),
        workers=2,
    )
    assert checked["reran"] is True and checked["fingerprint_changed"] is True
    assert checked["exit_code"] in (0, 1) and checked["regressions"] == []
    # The fixed agent passes what the flaws broke; cases whose history is outdated or was
    # later corrected still fail for both, so they are not improvements.
    assert 1 <= len(checked["improvements"]) <= v1["summary"]["failed"]
    assert (tmp_path / "check" / "check.json").exists()

    pack = driver.call(
        "replaybook_evidence",
        run=[str(tmp_path / "check")],
        testset=[str(ts_path)],
        out=str(tmp_path / "evidence"),
        baseline_dir=str(baselines),
        accepted_file=str(acc),
        title="MCP-driven pack",
    )
    assert Path(pack["html"]).exists() and Path(pack["manifest"]).exists()
    assert pack["gate"] in ("PASS", "FAIL") and pack["runs"] == ["check"]
    assert "MCP-driven pack" in Path(pack["html"]).read_text(encoding="utf-8")

    recorded = driver.call(
        "replaybook_shadow_record",
        request_type="kafka_topic",
        out=str(tmp_path / "shadow"),
        data=str(data),
    )
    assert recorded["open_tickets"] == recorded["recorded"] + recorded["already_recorded"]
    assert "Nothing was executed" in recorded["message"]
    scored = driver.call("replaybook_shadow_score", out=str(tmp_path / "shadow"), data=str(data))
    assert scored["pending"] == recorded["recorded"], "open tickets are pending until resolved"
    shadow = driver.call("replaybook_shadow_report", out=str(tmp_path / "shadow"))
    assert shadow["proposals"] == recorded["recorded"]
    assert shadow["request_types"] == (["kafka_topic"] if recorded["recorded"] else [])


def test_errors_come_back_as_tool_errors_with_the_reason(tmp_path: Path) -> None:
    driver = Driver()
    with pytest.raises(RuntimeError, match="no results.json"):
        driver.call("replaybook_report", run_dir=str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="no tickets.jsonl"):
        driver.call("replaybook_build_testset", request_type="kafka_topic", data=str(tmp_path))
    with pytest.raises(RuntimeError, match="no test set"):
        driver.call(
            "replaybook_replay", testset=str(tmp_path / "none.json"), out=str(tmp_path / "r")
        )
    with pytest.raises(RuntimeError, match="no shadow.json"):
        driver.call("replaybook_shadow_report", out=str(tmp_path))
    with pytest.raises(RuntimeError, match="give at least one run"):
        driver.call("replaybook_evidence", run=[], out=str(tmp_path / "e"))


def test_cli_exposes_the_mcp_command() -> None:
    result = CliRunner().invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0, result.output
    assert "stdio" in result.output
