from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from replaybook.adapters.synthetic import load_platform, load_tickets
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.registry import (
    ASK_TOOL,
    MockEnvironment,
    describe_all_tools,
    server_names,
    tool_schema_fingerprint,
)
from replaybook.mock_env.world import WorldState


def _kafka_ticket(data_dir: Path):
    for ticket in load_tickets(data_dir):
        if ticket.inventory.get("kafka") and any(
            t["name"].endswith((".v1", ".v2", ".v3")) for t in ticket.inventory["kafka"]["topics"]
        ):
            if not ticket.inventory["kafka"].get("consumer_groups"):
                return ticket
    raise AssertionError("no kafka topic ticket in dataset")


def test_world_from_ticket_overlays_platform_facts(small_dataset: Path) -> None:
    ticket = _kafka_ticket(small_dataset)
    world = WorldState.from_ticket(ticket, load_platform(small_dataset))
    assert world.state["kafka"]["mode"] == "kraft"
    assert world.state["openstack"]["quota_tool"] == "unified"
    assert world.state["kafka"]["topics"]
    assert world.ticket is not None and world.ticket.id == ticket.id
    assert all(c["role"] != "engineer" for c in world.ticket.comments)


def test_reads_answer_from_state_and_writes_are_recorded(small_dataset: Path) -> None:
    ticket = _kafka_ticket(small_dataset)
    world = WorldState.from_ticket(ticket, load_platform(small_dataset))
    env = MockEnvironment(world)
    cluster = world.state["kafka"]["cluster"]
    existing = world.state["kafka"]["topics"][0]

    info = env.call("kafka_cluster_info", {"cluster": cluster})
    assert info["mode"] == "kraft" and info["zookeeper"] is None
    described = env.call("kafka_describe_topic", {"cluster": cluster, "topic": existing["name"]})
    assert described["partitions"] == existing["partitions"]

    created = env.call(
        "kafka_create_topic",
        {"cluster": cluster, "topic": "new.topic.v1", "partitions": 6, "retention_ms": 1000},
    )
    assert created["topic"] == "new.topic.v1"
    names = [t["name"] for t in env.call("kafka_list_topics", {"cluster": cluster})["topics"]]
    assert "new.topic.v1" in names

    legacy = env.call(
        "kafka_zk_create_topic", {"cluster": cluster, "topic": "old.topic.v1", "partitions": 3}
    )
    assert "error" in legacy and "KRaft" in legacy["error"]

    deleted = env.call("kafka_delete_topic", {"cluster": cluster, "topic": existing["name"]})
    assert deleted["deleted"] == existing["name"]
    assert "error" in env.call(
        "kafka_describe_topic", {"cluster": cluster, "topic": existing["name"]}
    )

    writes = env.recorder.writes
    assert [w.tool for w in writes] == [
        "kafka_create_topic",
        "kafka_zk_create_topic",
        "kafka_delete_topic",
    ]
    assert writes[1].error is not None
    assert writes[0].args["partitions"] == 6
    assert len(env.recorder.reads) == 4
    assert "kafka_delete_topic" in env.destructive


def test_unknown_tool_and_bad_arguments_do_not_raise() -> None:
    env = MockEnvironment(WorldState())
    assert "error" in env.call("kafka_fly_to_moon", {})
    assert "error" in env.call("kafka_cluster_info", {"nope": 1})
    assert env.recorder.calls == []


def test_openstack_artifactory_platform_basics() -> None:
    world = WorldState.from_inventory(
        {
            "openstack": {
                "projects": [{"name": "lending-dev", "quotas": {"cores": 32}}],
                "networks": ["lending-net-dev"],
                "security_groups": [{"name": "api-sg", "project": "lending-dev", "rules": []}],
            },
            "artifactory": {"repositories": [], "groups": ["lending-developers"]},
            "dns": {"zones": [{"name": "corp.example", "records": []}]},
            "groups": [{"name": "platform-admins", "members": []}],
            "ci": {"projects": [{"path": "lending/ledger", "variables": []}], "runners": []},
        },
        {
            "current_procedures": {"openstack_quota": "unified"},
            "privileged_groups": ["platform-admins"],
        },
    )
    env = MockEnvironment(world)

    assert (
        env.call("openstack_quota_set", {"project": "lending-dev", "cores": 64})["quotas"]["cores"]
        == 64
    )
    assert "error" in env.call(
        "openstack_nova_quota_update", {"project": "lending-dev", "cores": 64}
    )
    assert "error" in env.call(
        "openstack_server_create",
        {
            "project": "lending-dev",
            "name": "x",
            "flavor": "huge",
            "image": "rhel-9",
            "network": "lending-net-dev",
        },
    )
    rule = env.call(
        "openstack_security_group_rule_create",
        {
            "project": "lending-dev",
            "security_group": "api-sg",
            "protocol": "tcp",
            "port_min": 22,
            "port_max": 22,
            "remote_ip_prefix": "0.0.0.0/0",
        },
    )
    assert "warning" in rule

    repo = env.call(
        "artifactory_create_repo", {"key": "lending-ledger-local", "package_type": "maven"}
    )
    assert repo["package_type"] == "maven"
    target = env.call(
        "artifactory_create_permission_target",
        {
            "name": "lending-ledger-local-read",
            "repositories": ["lending-ledger-local"],
            "groups": {"lending-developers": ["read"]},
        },
    )
    assert target["groups"] == {"lending-developers": ["read"]}
    assert "error" in env.call(
        "artifactory_create_permission_target",
        {"name": "other", "repositories": ["lending-ledger-local"], "groups": {"nobody": ["read"]}},
    )

    record = env.call(
        "platform_dns_upsert_record",
        {"zone": "corp.example", "name": "api", "type": "a", "value": "10.1.2.3"},
    )
    assert record["type"] == "A"
    added = env.call("platform_group_add_member", {"group": "platform-admins", "user": "ana.silva"})
    assert added["warning"] == "privileged group"
    var = env.call(
        "platform_ci_set_variable",
        {"project": "lending/ledger", "key": "TOKEN", "value": "x", "protected": True},
    )
    assert var["protected"] is True
    asked = env.call(ASK_TOOL, {"question": "Which flavor?"})
    assert asked["status"] == "Waiting on Requester"
    assert env.recorder.writes[-1].tool == ASK_TOOL


def test_every_tool_is_annotated_and_uniquely_named() -> None:
    described = describe_all_tools()
    names = [d["name"] for d in described]
    assert len(names) == len(set(names))
    assert len(names) >= 40
    env = MockEnvironment(WorldState())
    for tool in described:
        assert tool["name"].startswith(tool["server"] + "_"), tool["name"]
        assert tool["description"], tool["name"]
        annotations = tool["annotations"]
        assert "read_only_hint" in annotations and "destructive_hint" in annotations, tool["name"]
        assert annotations["read_only_hint"] == (env.kinds[tool["name"]] == "read"), tool["name"]
        assert annotations["destructive_hint"] == (tool["name"] in env.destructive), tool["name"]
    assert set(server_names()) == {d["server"] for d in described}
    assert "ticket" in server_names()


def test_fingerprint_is_stable_and_sensitive() -> None:
    a = describe_all_tools()
    b = describe_all_tools()
    assert tool_schema_fingerprint(a) == tool_schema_fingerprint(b)
    changed = json.loads(json.dumps(a))
    changed[0]["description"] += " (changed)"
    assert tool_schema_fingerprint(changed) != tool_schema_fingerprint(a)


def test_stdio_server_round_trip(tmp_path: Path) -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    world = WorldState.from_inventory(
        {"kafka": {"cluster": "kafka-dev", "topics": [{"name": "a.events.v1", "partitions": 3}]}},
        {"kafka": {"mode": "kraft"}},
    )
    world_path = tmp_path / "world.json"
    world_path.write_text(world.model_dump_json(), encoding="utf-8")
    record_path = tmp_path / "calls.jsonl"

    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "replaybook.cli",
            "mock-server",
            "--server",
            "kafka",
            "--world",
            str(world_path),
            "--record",
            str(record_path),
        ],
    )

    async def run() -> tuple[list[str], dict]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(
                    "kafka_create_topic",
                    {"cluster": "kafka-dev", "topic": "b.events.v1", "partitions": 6},
                )
                return [t.name for t in tools.tools], result.structured_content

    names, structured = asyncio.run(run())
    assert "kafka_create_topic" in names and "kafka_zk_create_topic" in names
    assert structured["topic"] == "b.events.v1"
    recorded = CallRecorder.load(record_path)
    assert [c.tool for c in recorded] == ["kafka_create_topic"]
    assert recorded[0].kind == "write"
