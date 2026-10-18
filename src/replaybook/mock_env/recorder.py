"""Records every mock tool call. Writes are what the scorer compares with history."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CallKind = Literal["read", "write"]


class RecordedCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seq: int
    tool: str
    kind: CallKind
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: str | None = None
    at: datetime


class CallRecorder:
    """One recorder per replayed case. Optionally mirrors every call to a JSONL sink so a
    mock server running in another process can hand its record back."""

    def __init__(self, sink: Path | None = None) -> None:
        self.calls: list[RecordedCall] = []
        self._sink = Path(sink) if sink else None
        self._lock = threading.Lock()

    def record(
        self,
        tool: str,
        kind: CallKind,
        args: dict[str, Any],
        result: Any = None,
        error: str | None = None,
    ) -> RecordedCall:
        with self._lock:
            call = RecordedCall(
                seq=len(self.calls) + 1,
                tool=tool,
                kind=kind,
                args=_plain(args),
                result=_plain(result),
                error=error,
                at=datetime.now(UTC),
            )
            self.calls.append(call)
            if self._sink is not None:
                with self._sink.open("a", encoding="utf-8") as handle:
                    handle.write(call.model_dump_json())
                    handle.write("\n")
        return call

    @property
    def writes(self) -> list[RecordedCall]:
        return [c for c in self.calls if c.kind == "write"]

    @property
    def reads(self) -> list[RecordedCall]:
        return [c for c in self.calls if c.kind == "read"]

    def to_list(self) -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in self.calls]

    @staticmethod
    def load(path: Path) -> list[RecordedCall]:
        calls: list[RecordedCall] = []
        text = Path(path).read_text(encoding="utf-8") if Path(path).exists() else ""
        for line in text.splitlines():
            if line.strip():
                calls.append(RecordedCall.model_validate_json(line))
        return calls


def _plain(value: Any) -> Any:
    """JSON-safe copy, so recorded calls never hold live references to world objects."""
    if value is None:
        return None
    return json.loads(json.dumps(value, default=_default))


def _default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)
