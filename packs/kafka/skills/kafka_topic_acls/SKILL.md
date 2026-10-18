---
name: kafka-topic-acls
description: Create a Kafka topic and grant a service principal access to it.
request_type: kafka_topic
tools: kafka_cluster_info, kafka_describe_topic, kafka_create_topic, kafka_add_acl, ticket_ask_requester, ticket_comment
---

# Kafka topic + ACLs

## When this applies
A team asks for a new Kafka topic, usually with a partition count, a retention period and
a service account (principal) that will produce to or consume from it.

## Information you need
- cluster: from the environment (dev and test use kafka-dev, uat uses kafka-uat, prod
  uses kafka-prod) or named explicitly
- topic name (like `payments.events.v1`)
- partitions
- principal (service account, like `svc-payments-prod`)
- access: produce, consume, or both
- retention (optional, default 7d)

If the topic name, partitions or principal is missing, ask the requester with
`ticket_ask_requester` and stop. Do not pick a default.

## Procedure
1. `kafka_cluster_info` for the cluster. All clusters run in KRaft mode; there is no
   ZooKeeper endpoint. Never use the `kafka_zk_*` tools.
2. `kafka_describe_topic` to see whether the topic already exists.
   - If it exists, do not delete or recreate it. Leave the partitions as they are, say so
     in a comment, and only add the ACLs.
3. If it does not exist, `kafka_create_topic` with the requested partitions and
   retention in milliseconds (24h = 86400000, 7d = 604800000, 30d = 2592000000).
4. `kafka_add_acl` for the principal: produce = WRITE and DESCRIBE, consume = READ and
   DESCRIBE, both = READ, WRITE and DESCRIBE.
5. `ticket_comment` with a one-line summary.

## Never
- Never call `kafka_delete_topic`. Deleting a topic destroys data.
- Never use the ZooKeeper tools; the migration to KRaft finished on 2025-12-01.
