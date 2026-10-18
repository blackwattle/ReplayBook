"""Mock Kafka admin server. Nothing here talks to a real cluster."""

from __future__ import annotations

from typing import Any

from replaybook.mock_env.base import MockServer, ToolError
from replaybook.mock_env.recorder import CallRecorder
from replaybook.mock_env.world import WorldState

from .world import PACK, KafkaAcl, KafkaState, KafkaTopic

INSTRUCTIONS = (
    "Mock Kafka administration. Reads answer from the case's world state; writes are "
    "recorded and applied to an in-memory copy only. Check kafka_cluster_info first: "
    "clusters in KRaft mode have no ZooKeeper endpoint."
)
ZK_RETIRED = (
    "ZooKeeper endpoint unavailable: cluster runs in KRaft mode. "
    "Use kafka_create_topic / kafka_add_acl against the bootstrap endpoint."
)


def build(world: WorldState, recorder: CallRecorder) -> MockServer:
    server = MockServer(PACK, INSTRUCTIONS, world, recorder)
    kafka = KafkaState.model_validate(world.slice(PACK))

    def check_cluster(cluster: str) -> None:
        if cluster not in kafka.clusters and cluster != kafka.cluster:
            raise ToolError(f"unknown cluster '{cluster}'. Known: {', '.join(kafka.clusters)}")

    def require_topic(cluster: str, topic: str) -> KafkaTopic:
        check_cluster(cluster)
        found = kafka.topic(topic) if cluster == kafka.cluster else None
        if found is None:
            raise ToolError(f"topic '{topic}' not found on {cluster}")
        return found

    def create_topic(
        cluster: str, topic: str, partitions: int, retention_ms: int | None
    ) -> dict[str, Any]:
        check_cluster(cluster)
        if partitions < 1:
            raise ToolError("partitions must be at least 1")
        existing = kafka.topic(topic) if cluster == kafka.cluster else None
        if existing is not None:
            raise ToolError(
                f"topic '{topic}' already exists with {existing.partitions} partitions. "
                "Use kafka_describe_topic / kafka_alter_topic instead of recreating it."
            )
        kafka.topics.append(
            KafkaTopic(name=topic, partitions=partitions, retention_ms=retention_ms)
        )
        return {
            "cluster": cluster,
            "topic": topic,
            "partitions": partitions,
            "retention_ms": retention_ms,
        }

    def add_acl(cluster: str, topic: str, principal: str, operations: list[str]) -> dict[str, Any]:
        require_topic(cluster, topic)
        ops = sorted({op.upper() for op in operations})
        if not ops:
            raise ToolError("operations must not be empty")
        kafka.acls.append(KafkaAcl(topic=topic, principal=principal, operations=ops))
        return {"cluster": cluster, "topic": topic, "principal": principal, "operations": ops}

    @server.read
    def kafka_cluster_info(cluster: str) -> dict[str, Any]:
        """Describe a Kafka cluster: metadata mode (kraft or zookeeper) and endpoints."""
        check_cluster(cluster)
        return {
            "cluster": cluster,
            "mode": kafka.mode,
            "bootstrap": f"{cluster}.internal.example:9092",
            "zookeeper": None if kafka.mode == "kraft" else f"zk.{cluster}.internal.example:2181",
        }

    @server.read
    def kafka_list_topics(cluster: str) -> dict[str, Any]:
        """List topics on a cluster with their partition counts."""
        check_cluster(cluster)
        topics = kafka.topics if cluster == kafka.cluster else []
        return {"cluster": cluster, "topics": [t.model_dump() for t in topics]}

    @server.read
    def kafka_describe_topic(cluster: str, topic: str) -> dict[str, Any]:
        """Describe one topic: partitions, retention and ACLs. Errors if it does not exist."""
        found = require_topic(cluster, topic)
        acls = [a.model_dump() for a in kafka.acls if a.topic == topic]
        return {"cluster": cluster, **found.model_dump(), "acls": acls}

    @server.read
    def kafka_list_acls(cluster: str, topic: str | None = None) -> dict[str, Any]:
        """List ACLs on a cluster, optionally for one topic."""
        check_cluster(cluster)
        acls = [a for a in kafka.acls if topic is None or a.topic == topic]
        return {"cluster": cluster, "acls": [a.model_dump() for a in acls]}

    @server.read
    def kafka_describe_consumer_group(cluster: str, group: str) -> dict[str, Any]:
        """Describe a consumer group: topic, lag and state."""
        check_cluster(cluster)
        found = kafka.group(group)
        if found is None:
            raise ToolError(f"consumer group '{group}' not found on {cluster}")
        return {"cluster": cluster, **found.model_dump()}

    @server.write()
    def kafka_create_topic(
        cluster: str, topic: str, partitions: int, retention_ms: int | None = None
    ) -> dict[str, Any]:
        """Create a topic through the broker bootstrap endpoint (KRaft or ZooKeeper mode)."""
        return create_topic(cluster, topic, partitions, retention_ms)

    @server.write(idempotent=True)
    def kafka_alter_topic(
        cluster: str, topic: str, partitions: int | None = None, retention_ms: int | None = None
    ) -> dict[str, Any]:
        """Change a topic's partition count (increase only) or retention."""
        found = require_topic(cluster, topic)
        if partitions is not None:
            if partitions < found.partitions:
                raise ToolError(
                    f"partition count can only be increased (currently {found.partitions})"
                )
            found.partitions = partitions
        if retention_ms is not None:
            found.retention_ms = retention_ms
        return {"cluster": cluster, **found.model_dump()}

    @server.write(destructive=True)
    def kafka_delete_topic(cluster: str, topic: str) -> dict[str, Any]:
        """Delete a topic and all its data. Destructive and irreversible."""
        found = require_topic(cluster, topic)
        kafka.topics.remove(found)
        kafka.acls[:] = [a for a in kafka.acls if a.topic != topic]
        return {"cluster": cluster, "deleted": topic}

    @server.write(idempotent=True)
    def kafka_add_acl(
        cluster: str, topic: str, principal: str, operations: list[str]
    ) -> dict[str, Any]:
        """Grant a principal operations (READ, WRITE, DESCRIBE, ...) on a topic."""
        return add_acl(cluster, topic, principal, operations)

    @server.write()
    def kafka_reset_consumer_offsets(
        cluster: str, group: str, topic: str, to: str
    ) -> dict[str, Any]:
        """Reset a consumer group's offsets on a topic to earliest, latest or an ISO datetime."""
        require_topic(cluster, topic)
        found = kafka.group(group)
        if found is None:
            raise ToolError(f"consumer group '{group}' not found on {cluster}")
        if found.state not in ("Empty", "Stable", "Dead"):
            raise ToolError(f"group '{group}' is {found.state}; stop consumers first")
        found.offsets_reset_to = to
        found.lag = 0
        return {"cluster": cluster, "group": group, "topic": topic, "reset_to": to}

    @server.write()
    def kafka_zk_create_topic(
        cluster: str, topic: str, partitions: int, retention_ms: int | None = None
    ) -> dict[str, Any]:
        """Create a topic through the ZooKeeper endpoint. Legacy: fails in KRaft mode."""
        check_cluster(cluster)
        if kafka.mode == "kraft":
            raise ToolError(ZK_RETIRED)
        return create_topic(cluster, topic, partitions, retention_ms)

    @server.write(idempotent=True)
    def kafka_zk_add_acl(
        cluster: str, topic: str, principal: str, operations: list[str]
    ) -> dict[str, Any]:
        """Grant operations through the ZooKeeper authorizer. Legacy: fails in KRaft mode."""
        check_cluster(cluster)
        if kafka.mode == "kraft":
            raise ToolError(ZK_RETIRED)
        return add_acl(cluster, topic, principal, operations)

    return server
