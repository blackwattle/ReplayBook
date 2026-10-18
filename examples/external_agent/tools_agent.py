"""The same tiny agent, written for the function-calling door instead of MCP.

Replaybook's `--transport tools` starts a local HTTP endpoint per case and sets
REPLAYBOOK_TOOLS_URL. GET /tools returns the tool list as JSON Schema in the shape
provider-native function calling expects (hand it to your model); POST /tools/<name> calls
one. Nothing here imports MCP or Replaybook: plain HTTP from the standard library.

    replaybook replay --agent external --transport tools --command "python .../tools_agent.py" ...
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from typing import Any

BASE = os.environ["REPLAYBOOK_TOOLS_URL"].rstrip("/")


def call(tool: str, arguments: dict[str, Any]) -> Any:
    request = urllib.request.Request(
        f"{BASE}/tools/{tool}",
        data=json.dumps({"arguments": arguments}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())["result"]


def main() -> None:
    with urllib.request.urlopen(f"{BASE}/tools", timeout=30) as response:
        schemas = json.loads(response.read())["tools"]
    names = {t["function"]["name"] for t in schemas}
    assert {"ticket_get", "kafka_create_topic"} <= names, sorted(names)

    ticket = call("ticket_get", {})
    text = f"{ticket['summary']}\n{ticket['description']}"
    topic = re.search(r"\b([a-z0-9-]+\.[a-z]+\.v\d)\b", text)
    partitions = re.search(r"(\d+)\s+partitions?|partitions?[^\d]{0,12}(\d+)", text, re.IGNORECASE)
    env = re.search(r"\b(dev|test|uat|prod)\b", text, re.IGNORECASE)
    if "kafka" not in text.lower() or not topic:
        call("ticket_comment", {"body": "Not a Kafka topic request; skipping."})
        print("skipped")
        return
    if not partitions:
        call("ticket_ask_requester", {"question": "How many partitions does the topic need?"})
        print("asked")
        return
    count = int(partitions.group(1) or partitions.group(2))
    cluster = {"dev": "kafka-dev", "test": "kafka-dev", "uat": "kafka-uat", "prod": "kafka-prod"}[
        env.group(1).lower() if env else "dev"
    ]
    created = call(
        "kafka_create_topic", {"cluster": cluster, "topic": topic.group(1), "partitions": count}
    )
    print(json.dumps(created))
    call("ticket_comment", {"body": f"Created {topic.group(1)}."})


if __name__ == "__main__":
    main()
