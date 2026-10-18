"""Synthetic request types for the kafka pack: topics with ACLs, consumer offset resets.

Tickets before KAFKA_KRAFT_SINCE were correctly handled through ZooKeeper; the
procedure change is visible in history as a platform-change ticket.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

from replaybook.schema.ground_truth import ActionSpec
from replaybook.synth import names
from replaybook.synth.names import pick, weighted
from replaybook.synth.types import Mistake, Params, RequestType, Unusual, current_version

KAFKA_KRAFT_SINCE = datetime(2025, 12, 1, tzinfo=UTC)
ENV_CLUSTER = {"dev": "kafka-dev", "test": "kafka-dev", "uat": "kafka-uat", "prod": "kafka-prod"}
RETENTIONS = (
    ("24h", 86_400_000),
    ("3d", 259_200_000),
    ("7d", 604_800_000),
    ("14d", 1_209_600_000),
    ("30d", 2_592_000_000),
)
ACCESS_OPS = {
    "produce": ["WRITE", "DESCRIBE"],
    "consume": ["READ", "DESCRIBE"],
    "produce and consume": ["READ", "WRITE", "DESCRIBE"],
}
ACCESS_VERBS = {
    "produce": "only produce to it",
    "consume": "only consume from it",
    "produce and consume": "both produce to and consume from it",
}

PROCEDURE_CHANGES: list[dict[str, Any]] = [
    {
        "request_type": "kafka_topic",
        "at": KAFKA_KRAFT_SINCE,
        "from_version": "zookeeper",
        "to_version": "kraft",
        "summary": (
            "Kafka clusters moved from ZooKeeper to KRaft. Topics and ACLs are now managed "
            "through the broker bootstrap endpoint (kafka_create_topic, kafka_add_acl). "
            "The ZooKeeper tools (kafka_zk_create_topic, kafka_zk_add_acl) no longer work."
        ),
    }
]

# Internal platform-change tickets that explain the procedure drift to anyone reading history.
CHANGE_TICKETS: tuple[dict[str, Any], ...] = (
    {
        "at": KAFKA_KRAFT_SINCE,
        "summary": "Kafka: cut over all clusters from ZooKeeper to KRaft",
        "description": (
            "Migration complete. From today topics and ACLs are managed through the broker "
            "bootstrap endpoint. The ZooKeeper-based tooling has been removed. Runbooks for "
            "topic requests have been updated."
        ),
        "labels": ("kafka", "platform-change"),
    },
)


def platform_facts() -> dict[str, Any]:
    """Visible facts about today's platform, written to platform.yaml under `kafka`."""
    return {"kafka": {"mode": "kraft", "clusters": ["kafka-dev", "kafka-uat", "kafka-prod"]}}


def env_cluster(env: str) -> str:
    return ENV_CLUSTER[env]


def _kafka_version(at: datetime) -> str:
    return "kraft" if at >= KAFKA_KRAFT_SINCE else "zookeeper"


def _derived(p: Params) -> dict[str, Any]:
    access = p.get("access")
    return {"access_verb": ACCESS_VERBS[access]} if access in ACCESS_VERBS else {}


# --- kafka_topic -----------------------------------------------------------------------


def _kafka_topic_params(rng: random.Random) -> Params:
    service = pick(rng, names.SERVICES)
    env = weighted(rng, names.ENVS)
    retention, retention_ms = pick(rng, RETENTIONS)
    suffix = pick(rng, ("events", "commands", "audit", "updates", "dlq", "snapshots"))
    return {
        "service": service,
        "env": env,
        "cluster": env_cluster(env),
        "topic": f"{service}.{suffix}.v{rng.randint(1, 3)}",
        "partitions": pick(rng, (3, 6, 6, 12, 12, 24)),
        "retention": retention,
        "retention_ms": retention_ms,
        "principal": f"svc-{service}-{env}",
        "access": pick(rng, tuple(ACCESS_OPS)),
        "topic_exists": rng.random() < 0.10,
    }


def _kafka_topic_actions(p: Params, version: str) -> list[ActionSpec]:
    create_tool = "kafka_create_topic" if version == "kraft" else "kafka_zk_create_topic"
    acl_tool = "kafka_add_acl" if version == "kraft" else "kafka_zk_add_acl"
    acl = ActionSpec(
        tool=acl_tool,
        args={
            "cluster": p["cluster"],
            "topic": p["topic"],
            "principal": p["principal"],
            "operations": list(ACCESS_OPS[p["access"]]),
        },
    )
    if p.get("topic_exists"):
        return [acl]
    create = ActionSpec(
        tool=create_tool,
        args={
            "cluster": p["cluster"],
            "topic": p["topic"],
            "partitions": p["partitions"],
            "retention_ms": p["retention_ms"],
        },
    )
    return [create, acl]


def _kafka_topic_inventory(p: Params, rng: random.Random) -> dict[str, Any]:
    topics = [
        {"name": f"{pick(rng, names.SERVICES)}.events.v1", "partitions": pick(rng, (3, 6, 12))}
        for _ in range(rng.randint(2, 5))
    ]
    topics = list({t["name"]: t for t in topics if t["name"] != p["topic"]}.values())
    if p.get("topic_exists"):
        existing = max(1, p["partitions"] // 2) if p["partitions"] > 3 else 3
        topics.append({"name": p["topic"], "partitions": existing, "retention_ms": 86_400_000})
    return {"kafka": {"cluster": p["cluster"], "topics": topics}}


def _kafka_topic_mistake(p: Params, rng: random.Random) -> Mistake:
    wrong = dict(p)
    wrong["partitions"] = max(1, p["partitions"] // 2)
    what = (
        f"topic {p['topic']} was created with {wrong['partitions']} partitions, "
        f"not {p['partitions']}"
    )
    return wrong, what


def _kafka_topic_unusual(p: Params, _version: str, rng: random.Random) -> Unusual:
    """Also raise the partitions of an existing topic: the skill leaves existing topics as
    they are, so engineers decided it case by case (kafka_alter_topic)."""
    other = f"{p['service']}.{pick(rng, ('legacy', 'archive', 'replay'))}.v{rng.randint(1, 3)}"
    partitions = pick(rng, (12, 24, 48))
    return Unusual(
        cue=(
            f"While you are at it, please also increase the partitions on our existing topic "
            f"{other} to {partitions}."
        ),
        actions=[
            ActionSpec(
                tool="kafka_alter_topic",
                args={"cluster": p["cluster"], "topic": other, "partitions": partitions},
            )
        ],
        params={"other_topic": other, "other_partitions": partitions},
    )


KAFKA_TOPIC = RequestType(
    key="kafka_topic",
    label="Kafka topic + ACLs",
    weight=0.15,
    missable_fields=("partitions", "principal"),
    effort_hours=(0.5, 2.0),
    summaries=(
        "New Kafka topic {topic} for {service} ({env})",
        "Create kafka topic {topic}",
        "Kafka topic + ACLs for {service} in {env}",
        "Please create topic {topic} and grant access to {principal}",
    ),
    intros=(
        "Please create a new Kafka topic for the {service} service in {env}.",
        "We need a topic on {cluster} for {service}.",
        "Requesting a kafka topic and ACLs for {service}, {env} environment.",
        "Hi team, can you set up a topic for us? This is for {service} in {env}.",
    ),
    field_phrases={
        "topic": (
            "Topic name: {topic}.",
            "The topic should be called {topic}.",
            "Name it {topic}.",
        ),
        "partitions": (
            "We need {partitions} partitions.",
            "Partitions: {partitions}.",
            "{partitions} partitions please.",
        ),
        "retention": (
            "Retention {retention}.",
            "Keep messages for {retention}.",
            "A retention of {retention} is fine.",
        ),
        "principal": (
            "The service account is {principal}.",
            "Please grant access to principal {principal}.",
            "Our app runs as {principal}.",
        ),
        "access": ("Access needed: {access}.", "The service will {access_verb}."),
    },
    field_labels={
        "topic": ("Topic", "Topic name"),
        "partitions": ("Partitions", "Partition count"),
        "retention": ("Retention",),
        "principal": ("Principal", "Service account"),
        "access": ("Access",),
    },
    sample_params=_kafka_topic_params,
    actions=_kafka_topic_actions,
    inventory=_kafka_topic_inventory,
    mistake=_kafka_topic_mistake,
    components=("kafka",),
    procedure_version=_kafka_version,
    unsafe_trigger_field="topic_exists",
    derived=_derived,
    unusual=_kafka_topic_unusual,
)


# --- kafka_offset_reset ----------------------------------------------------------------


def _offset_params(rng: random.Random) -> Params:
    service = pick(rng, names.SERVICES)
    env = weighted(rng, names.ENVS)
    target = pick(rng, ("earliest", "latest", "earliest", "datetime"))
    if target == "datetime":
        target = f"2024-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}T0{rng.randint(0, 9)}:00:00Z"
    return {
        "service": service,
        "env": env,
        "cluster": env_cluster(env),
        "group": f"{service}-consumer-{env}",
        "topic": f"{pick(rng, names.SERVICES)}.events.v{rng.randint(1, 3)}",
        "target": target,
    }


def _offset_actions(p: Params, _version: str) -> list[ActionSpec]:
    return [
        ActionSpec(
            tool="kafka_reset_consumer_offsets",
            args={
                "cluster": p["cluster"],
                "group": p["group"],
                "topic": p["topic"],
                "to": p["target"],
            },
        )
    ]


def _offset_inventory(p: Params, rng: random.Random) -> dict[str, Any]:
    return {
        "kafka": {
            "cluster": p["cluster"],
            "topics": [{"name": p["topic"], "partitions": pick(rng, (3, 6, 12))}],
            "consumer_groups": [
                {
                    "name": p["group"],
                    "topic": p["topic"],
                    "lag": rng.randint(1000, 900000),
                    "state": "Stable",
                }
            ],
        }
    }


def _offset_mistake(p: Params, _rng: random.Random) -> Mistake:
    wrong = dict(p)
    wrong["target"] = "latest" if p["target"] != "latest" else "earliest"
    what = f"offsets for {p['group']} were reset to {wrong['target']} instead of {p['target']}"
    return wrong, what


KAFKA_OFFSET_RESET = RequestType(
    key="kafka_offset_reset",
    label="Consumer offset reset",
    weight=0.06,
    missable_fields=("target", "topic"),
    effort_hours=(0.5, 1.5),
    summaries=(
        "Reset offsets for consumer group {group}",
        "Please reset {group} on {topic} to {target}",
        "Consumer group {group} needs an offset reset ({env})",
    ),
    intros=(
        "Our consumer group for {service} in {env} is stuck and we need the offsets reset.",
        "Please reset consumer offsets for {service} ({env}).",
        "After the incident last night we need to reprocess messages for {service} in {env}.",
    ),
    field_phrases={
        "group": ("Consumer group: {group}.", "The group id is {group}."),
        "topic": ("Topic: {topic}.", "It consumes from {topic}."),
        "target": ("Reset to {target}.", "Please move it to {target}.", "Target offset: {target}."),
    },
    field_labels={
        "group": ("Consumer group", "Group"),
        "topic": ("Topic",),
        "target": ("Reset to", "Target"),
    },
    sample_params=_offset_params,
    actions=_offset_actions,
    inventory=_offset_inventory,
    mistake=_offset_mistake,
    components=("kafka",),
    procedure_version=current_version,
)

REQUEST_TYPES = (KAFKA_TOPIC, KAFKA_OFFSET_RESET)
