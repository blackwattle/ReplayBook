"""The only module allowed to read ground_truth.jsonl.

Ground truth is used to check Replaybook itself (zero false passes, group purity,
determinism), never to score an agent. Keeping the reader here, and testing that nothing
else imports it, is what makes that promise checkable.
"""

from __future__ import annotations

import json
from pathlib import Path

from replaybook.schema.ground_truth import GroundTruth

FILENAME = "ground_truth.jsonl"


def load_ground_truth(data_dir: Path) -> dict[str, GroundTruth]:
    """Return ground truth keyed by ticket id."""
    path = Path(data_dir) / FILENAME
    truth: dict[str, GroundTruth] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                record = GroundTruth.model_validate(json.loads(line))
                truth[record.ticket_id] = record
    return truth
