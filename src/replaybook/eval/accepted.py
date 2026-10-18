"""Accepted differences: reviewed failures that should not count, for a limited time.

`assurance/accepted.yaml` records who accepted what, why and until when. An acceptance can
cover one case or every case with the same failure signature. Expired acceptances stop
applying and the cases fail again. The same file marks historical tickets whose procedure
is outdated (`kind: outdated_history`), which is the honest way to handle history that no
longer reflects how the platform works.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from replaybook.eval.scoring import CaseResult

DEFAULT_PATH = Path("assurance/accepted.yaml")
Scope = Literal["case", "signature"]
Kind = Literal["accepted_difference", "outdated_history"]


class Acceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope: Scope
    match: str
    reason: str
    approver: str
    approved_at: date
    expires: date
    kind: Kind = "accepted_difference"
    request_type: str | None = None
    run_id: str | None = None

    def covers(self, result: CaseResult) -> bool:
        if self.request_type and self.request_type != result.request_type:
            return False
        if self.scope == "case":
            return result.case_id == self.match
        return bool(result.signature) and result.signature == self.match

    def active_on(self, today: date) -> bool:
        return today <= self.expires


class AcceptedDifferences(BaseModel):
    accepted: list[Acceptance] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> AcceptedDifferences:
        path = Path(path)
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def save(self, path: Path = DEFAULT_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Accepted differences and outdated-history markers for Replaybook.\n"
            "# Each entry needs a reason, an approver and an expiry. Expired entries stop\n"
            "# applying and the cases fail again. Edit by hand or with `replaybook accept`.\n"
        )
        body = yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        path.write_text(header + body, encoding="utf-8")

    def next_id(self) -> str:
        numbers = [int(a.id.split("-")[-1]) for a in self.accepted if a.id.split("-")[-1].isdigit()]
        return f"ACC-{max(numbers, default=0) + 1}"

    def add(
        self,
        scope: Scope,
        match: str,
        reason: str,
        approver: str,
        expires: date | None = None,
        days: int = 90,
        kind: Kind = "accepted_difference",
        request_type: str | None = None,
        run_id: str | None = None,
        today: date | None = None,
    ) -> Acceptance:
        today = today or date.today()
        if not reason.strip():
            raise ValueError("an acceptance needs a reason")
        if not approver.strip():
            raise ValueError("an acceptance needs an approver")
        entry = Acceptance(
            id=self.next_id(),
            scope=scope,
            match=match,
            reason=reason.strip(),
            approver=approver.strip(),
            approved_at=today,
            expires=expires or (today + timedelta(days=days)),
            kind=kind,
            request_type=request_type,
            run_id=run_id,
        )
        self.accepted.append(entry)
        return entry

    def find(self, result: CaseResult, today: date) -> tuple[Acceptance | None, Acceptance | None]:
        """(active acceptance, expired acceptance) covering a result; case scope wins."""
        active = None
        expired = None
        for entry in sorted(self.accepted, key=lambda a: (a.scope != "case", a.id)):
            if not entry.covers(result):
                continue
            if entry.active_on(today):
                if active is None:
                    active = entry
            elif expired is None:
                expired = entry
        return active, expired


def apply_accepted(
    results: list[CaseResult], accepted: AcceptedDifferences, today: date | None = None
) -> list[CaseResult]:
    """Turn covered failures into 'accepted'. Expired coverage leaves them failed with a note."""
    today = today or date.today()
    updated: list[CaseResult] = []
    for result in results:
        if result.outcome in ("fail", "accepted"):
            base = result.model_copy(
                update={"outcome": "fail", "accepted_by": None, "accepted_reason": None}
            )
            active, expired = accepted.find(base, today)
            if active is not None:
                base = base.model_copy(
                    update={
                        "outcome": "accepted",
                        "accepted_by": (
                            f"{active.approver} ({active.id}, until {active.expires.isoformat()})"
                        ),
                        "accepted_reason": active.reason,
                    }
                )
            elif expired is not None:
                base = base.model_copy(
                    update={
                        "accepted_reason": (
                            f"acceptance {expired.id} by {expired.approver} expired on "
                            f"{expired.expires.isoformat()}; failing again"
                        )
                    }
                )
            updated.append(base)
        else:
            updated.append(result)
    return updated


def suggest(results: list[CaseResult]) -> list[dict[str, Any]]:
    """Failing cases whose visible tags say history itself may be outdated or wrong."""
    suggestions = []
    for r in results:
        if r.outcome != "fail" or not r.tags:
            continue
        if "pre-procedure-change" in r.tags:
            why = "ticket predates a procedure change for this request type"
            kind: Kind = "outdated_history"
        else:
            why = "a later ticket corrected this one; history holds the mistake"
            kind = "accepted_difference"
        suggestions.append(
            {"case_id": r.case_id, "reason": r.reason, "tags": r.tags, "why": why, "kind": kind}
        )
    return suggestions
