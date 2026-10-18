"""The kafka slice of world state: clusters, topics, ACLs and consumer groups."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

PACK = "kafka"
DEFAULT_CLUSTERS = ["kafka-dev", "kafka-uat", "kafka-prod"]


class KafkaTopic(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    partitions: int = 3
    retention_ms: int | None = None


class KafkaAcl(BaseModel):
    topic: str
    principal: str
    operations: list[str]


class ConsumerGroup(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    topic: str
    lag: int = 0
    state: str = "Stable"
    offsets_reset_to: str | None = None


class KafkaState(BaseModel):
    mode: str = "kraft"
    cluster: str = "kafka-dev"
    clusters: list[str] = Field(default_factory=lambda: list(DEFAULT_CLUSTERS))
    topics: list[KafkaTopic] = Field(default_factory=list)
    acls: list[KafkaAcl] = Field(default_factory=list)
    consumer_groups: list[ConsumerGroup] = Field(default_factory=list)

    def topic(self, name: str) -> KafkaTopic | None:
        return next((t for t in self.topics if t.name == name), None)

    def group(self, name: str) -> ConsumerGroup | None:
        return next((g for g in self.consumer_groups if g.name == name), None)


def from_inventory(inventory: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """The ticket's kafka inventory overlaid with today's platform facts (mode, clusters)."""
    inv = inventory.get(PACK, {})
    state = KafkaState(
        cluster=inv.get("cluster", "kafka-dev"),
        topics=[KafkaTopic.model_validate(t) for t in inv.get("topics", [])],
        consumer_groups=[ConsumerGroup.model_validate(g) for g in inv.get("consumer_groups", [])],
    )
    kafka_facts = facts.get(PACK, {})
    state.mode = kafka_facts.get("mode", state.mode)
    state.clusters = list(kafka_facts.get("clusters", state.clusters))
    return state.model_dump(mode="json")
