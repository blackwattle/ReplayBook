"""Packs: the core knows no tool; everything tool-specific loads from packs/<name>/."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from replaybook import packs
from replaybook.eval.normalise import normalise
from replaybook.eval.safety import unsafe_severity
from replaybook.mock_env.registry import MockEnvironment, describe_all_tools, server_names
from replaybook.mock_env.world import WorldState

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "replaybook"
# Vendor and product names that belong in packs, never in the core. Case-insensitive.
VENDOR_WORDS = (
    "kafka",
    "zookeeper",
    "kraft",
    "openstack",
    "nova",
    "cinder",
    "artifactory",
    "platform_dns",
    "platform_cert",
    "platform_group",
    "platform_ci",
    "platform_observer",
    "trustgraph",
)


def test_core_names_no_vendor_tool() -> None:
    pattern = re.compile("|".join(VENDOR_WORDS), re.IGNORECASE)
    offenders = []
    for path in SRC.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()[:80]}")
    assert not offenders, "\n".join(offenders)


def test_core_imports_no_pack_module() -> None:
    offenders = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "replaybook_packs" in text and path.name != "packs.py":
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, offenders


def test_registry_loads_the_example_packs() -> None:
    reg = packs.registry()
    assert {p.name for p in reg.packs} >= {"kafka", "openstack", "artifactory", "platform"}
    assert reg.request_types["kafka_topic"].pack == "kafka"
    assert reg.tool_types["kafka_zk_add_acl"] == "kafka_topic"
    assert reg.rules["kafka_create_topic"].family == "kafka.create_topic"
    assert reg.unsafe["kafka_delete_topic"][0] == "critical"
    assert "kafka_zk_create_topic" in reg.superseded
    assert set(reg.skills()) == {"kafka_topic", "artifactory_repo", "openstack_quota"}
    assert reg.current_versions() == {"current", "kraft", "unified"}
    assert server_names(reg) == [
        "ticket",
        "knowledge",
        "artifactory",
        "kafka",
        "openstack",
        "platform",
    ]
    # Every tool a rule or a request type names really exists on a mock server.
    served = {t["name"] for t in describe_all_tools(reg)}
    assert set(reg.rules) <= served
    assert set(reg.tool_types) <= served
    assert set(reg.unsafe) <= served


def test_world_state_is_one_slice_per_pack() -> None:
    world = WorldState.from_inventory(
        {"kafka": {"cluster": "kafka-dev", "topics": [{"name": "a.events.v1"}]}},
        {"kafka": {"mode": "zookeeper"}},
    )
    assert set(world.state) == {p.name for p in packs.registry().packs}
    assert world.state["kafka"]["mode"] == "zookeeper"
    again = WorldState.model_validate_json(world.model_dump_json())
    assert again == world
    env = MockEnvironment(world)
    assert env.call("kafka_cluster_info", {"cluster": "kafka-dev"})["mode"] == "zookeeper"


def test_pack_rules_drive_normalisation_and_safety() -> None:
    write = normalise("kafka_create_topic", {"cluster": "k", "topic": "t", "partitions": 3})
    assert write.args["retention_ms"] == 604800000, "defaults come from rules.yaml"
    unknown = normalise("some_other_tool", {"B": "x", "a": None})
    assert unknown.family == "some_other_tool" and unknown.args == {"B": "x"}
    world = WorldState.from_inventory({"groups": [{"name": "db-admins"}]}, {})
    verdict = unsafe_severity(
        normalise("platform_group_add_member", {"group": "db-admins", "user": "u"}), world
    )
    assert verdict is not None and verdict[0] == "critical"
    assert unsafe_severity(normalise("kafka_delete_topic", {"cluster": "k", "topic": "t"}), world)


def _write_pack(root: Path, name: str, tools: list[dict], rules: str = "") -> Path:
    pack = root / name
    pack.mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        f"name: {name}\ndescription: test pack\nrequest_types:\n  widget_order:\n"
        "    label: Widget order\n    keywords: [widget]\n    tools: [widget_order]\n",
        encoding="utf-8",
    )
    (pack / "tools.json").write_text(json.dumps({"tools": tools}), encoding="utf-8")
    if rules:
        (pack / "rules.yaml").write_text(rules, encoding="utf-8")
    return pack


def test_recording_mock_from_tools_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = [
        {
            "name": "widget_list",
            "description": "List widgets.",
            "inputSchema": {"type": "object", "properties": {"site": {"type": "string"}}},
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "widget_order",
            "description": "Order a widget.",
            "inputSchema": {
                "type": "object",
                "properties": {"sku": {"type": "string"}, "qty": {"type": "integer"}},
                "required": ["sku"],
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False},
        },
        {
            "name": "widget_scrap",
            "description": "Scrap a widget.",
            "inputSchema": {"type": "object", "properties": {"sku": {"type": "string"}}},
            "annotations": {"destructiveHint": True},
        },
    ]
    _write_pack(tmp_path, "widgets", tools, "unsafe:\n  widget_scrap: scraps a widget\n")
    monkeypatch.setenv(packs.ENV_VAR, str(tmp_path))
    packs.reset()
    try:
        reg = packs.registry()
        assert [p.name for p in reg.packs] == ["widgets"]
        assert reg.search_path == [tmp_path]
        world = WorldState.from_inventory(
            {"widgets": {"reads": {"widget_list": {"widgets": ["w-1"]}}}}, {}
        )
        env = MockEnvironment(world)
        assert env.call("widget_list", {"site": "x"}) == {"widgets": ["w-1"]}
        assert env.call("widget_order", {"sku": "w-2", "qty": 3}) == {
            "recorded": True,
            "sku": "w-2",
            "qty": 3,
        }
        assert "error" in env.call("widget_order", {"qty": 3}), "required argument missing"
        assert env.kinds == {
            "ticket_get": "read",
            "ticket_ask_requester": "write",
            "ticket_escalate": "write",
            "ticket_comment": "write",
            "knowledge_search_tickets": "read",
            "knowledge_get_ticket": "read",
            "widget_list": "read",
            "widget_order": "write",
            "widget_scrap": "write",
        }
        assert env.destructive == {"widget_scrap"}
        described = {t["name"]: t for t in env.describe_tools()}
        assert described["widget_order"]["input_schema"]["required"] == ["sku"]
        assert described["widget_list"]["annotations"]["read_only_hint"] is True
        assert described["widget_scrap"]["annotations"]["destructive_hint"] is True
        assert unsafe_severity(normalise("widget_scrap", {"sku": "w"}), world) == (
            "critical",
            "scraps a widget",
        )
        assert reg.tool_types == {"widget_order": "widget_order"}
    finally:
        monkeypatch.delenv(packs.ENV_VAR)
        packs.reset()
    assert "kafka" in {p.name for p in packs.registry().packs}


def test_malformed_pack_is_reported(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "pack.yaml").write_text("name: broken\n", encoding="utf-8")
    reg = packs.load_packs([tmp_path])
    with pytest.raises(packs.PackError):
        reg.pack("broken").build_server(WorldState(), None)
    with pytest.raises(packs.PackError):
        reg.pack("missing")
