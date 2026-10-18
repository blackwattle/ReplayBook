"""`replaybook tools import`: any MCP server's tool list becomes a pack with a recording mock."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from replaybook import packs
from replaybook.cli import app
from replaybook.eval.normalise import normalise, same_action
from replaybook.eval.safety import unsafe_severity
from replaybook.mock_env.registry import MockEnvironment, describe_all_tools
from replaybook.mock_env.world import WorldState
from replaybook.tools_import import list_tools_from_json, list_tools_from_mcp, rules_skeleton

SAVED_TOOLS = {
    "tools": [
        {
            "name": "hr_get_employee",
            "description": "Look up an employee.",
            "inputSchema": {
                "type": "object",
                "properties": {"employee_id": {"type": "string"}},
                "required": ["employee_id"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "hr_set_manager",
            "description": "Change an employee's manager.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "employee_id": {"type": "string"},
                    "manager_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["employee_id", "manager_id"],
            },
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
        },
        {
            "name": "hr_terminate",
            "description": "Terminate employment.",
            "inputSchema": {
                "type": "object",
                "properties": {"employee_id": {"type": "string"}},
                "required": ["employee_id"],
            },
            "annotations": {"destructiveHint": True},
        },
    ]
}


def test_import_from_saved_json_writes_a_usable_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = tmp_path / "hr-tools.json"
    saved.write_text(json.dumps(SAVED_TOOLS), encoding="utf-8")
    out = tmp_path / "packs"
    result = CliRunner().invoke(
        app, ["tools", "import", "--name", "hr", "--json", str(saved), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "3 tools (1 reads, 2 writes, 1 destructive)" in result.output
    pack = out / "hr"
    assert {p.name for p in pack.iterdir()} == {
        "__init__.py",
        "pack.yaml",
        "rules.yaml",
        "tools.json",
        "skills",
    }
    rules = yaml.safe_load((pack / "rules.yaml").read_text(encoding="utf-8"))
    assert set(rules["tools"]) == {"hr_set_manager", "hr_terminate"}, "reads get no rule"
    assert rules["tools"]["hr_set_manager"] == {
        "family": "hr_set_manager",
        "target": ["employee_id", "manager_id"],
        "matters": ["employee_id", "manager_id", "note"],
    }
    assert rules["unsafe"]["hr_terminate"]["severity"] == "critical"
    manifest = yaml.safe_load((pack / "pack.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "hr" and manifest["request_types"] == {}
    assert "request_types:" in (pack / "pack.yaml").read_text(encoding="utf-8")

    # The generated pack loads and serves a recording mock with zero core changes.
    monkeypatch.setenv(packs.ENV_VAR, str(out))
    packs.reset()
    try:
        reg = packs.registry()
        assert [p.name for p in reg.packs] == ["hr"]
        names = {t["name"] for t in describe_all_tools(reg)}
        assert names == {
            "ticket_get",
            "ticket_ask_requester",
            "ticket_comment",
            "knowledge_search_tickets",
            "knowledge_get_ticket",
            "hr_get_employee",
            "hr_set_manager",
            "hr_terminate",
        }
        world = WorldState.from_inventory(
            {"hr": {"reads": {"hr_get_employee": {"employee_id": "E1", "manager_id": "M1"}}}}, {}
        )
        env = MockEnvironment(world)
        assert env.call("hr_get_employee", {"employee_id": "E1"})["manager_id"] == "M1"
        done = env.call("hr_set_manager", {"employee_id": "E1", "manager_id": "M2"})
        assert done == {"recorded": True, "employee_id": "E1", "manager_id": "M2"}
        assert [w.tool for w in env.recorder.writes] == ["hr_set_manager"]
        a = normalise("hr_set_manager", {"employee_id": "E1", "manager_id": "m2"})
        b = normalise("hr_set_manager", {"manager_id": "M2", "employee_id": "e1", "note": None})
        assert same_action(a, b) and a.target == ("e1", "m2")
        verdict = unsafe_severity(normalise("hr_terminate", {"employee_id": "E1"}), world)
        assert verdict is not None and verdict[0] == "critical"
    finally:
        monkeypatch.delenv(packs.ENV_VAR)
        packs.reset()

    again = CliRunner().invoke(
        app, ["tools", "import", "--name", "hr", "--json", str(saved), "--out", str(out)]
    )
    assert again.exit_code == 2 and "already exists" in again.output
    neither = CliRunner().invoke(app, ["tools", "import", "--name", "x", "--out", str(out)])
    assert neither.exit_code == 2


def test_import_from_a_live_mcp_server_over_stdio(tmp_path: Path) -> None:
    """The repo's own kafka mock is an MCP server like any other."""
    command = [sys.executable, "-m", "replaybook.cli", "mock-server", "--server", "kafka"]
    tools = list_tools_from_mcp(command)
    names = {t["name"] for t in tools}
    assert "kafka_create_topic" in names and "kafka_delete_topic" in names
    by_name = {t["name"]: t for t in tools}
    assert by_name["kafka_cluster_info"]["annotations"]["read_only_hint"] is True
    assert by_name["kafka_delete_topic"]["annotations"]["destructive_hint"] is True
    assert "cluster" in by_name["kafka_create_topic"]["inputSchema"]["properties"]
    skeleton = rules_skeleton(tools)
    assert skeleton["tools"]["kafka_create_topic"]["target"] == ["cluster", "topic", "partitions"]
    assert "kafka_delete_topic" in skeleton["unsafe"]
    assert "kafka_cluster_info" not in skeleton["tools"]
    # Round trip: what the live server said can be saved and imported later.
    saved = tmp_path / "kafka.json"
    saved.write_text(json.dumps({"tools": tools}), encoding="utf-8")
    assert [t["name"] for t in list_tools_from_json(saved)] == [t["name"] for t in tools]


def test_cli_import_from_mcp_command(tmp_path: Path) -> None:
    out = tmp_path / "packs"
    command = f'"{sys.executable}" -m replaybook.cli mock-server --server artifactory'
    result = CliRunner().invoke(
        app, ["tools", "import", "--name", "art2", "--command", command, "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "8 tools (4 reads, 4 writes, 1 destructive)" in result.output
    assert (out / "art2" / "tools.json").exists()
