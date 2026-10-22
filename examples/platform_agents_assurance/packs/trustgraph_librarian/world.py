"""Per-case fixture state for trustgraph-librarian recording mocks."""

from __future__ import annotations

from typing import Any

PACK = "trustgraph_librarian"


def from_inventory(inventory: dict[str, Any], _facts: dict[str, Any]) -> dict[str, Any]:
    return dict((inventory or {}).get(PACK) or {})
