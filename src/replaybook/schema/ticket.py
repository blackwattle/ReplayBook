"""The canonical ticket: what every adapter (synthetic, public_jira, jira_cloud) produces.

A ticket is a past work item with its comments, timestamped status changes, linked code
changes and, where known, the actions the engineer took (from audit logs or change
records). Everything here is visible data a real team would have. Hidden ground truth
for synthetic tickets lives in a separate model and file.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CommentRole = Literal["requester", "engineer", "bot", "other"]
LinkType = Literal["duplicates", "relates", "fixes"]
# Where a ticket's effort figure came from. logged: time the engineer recorded.
# status_time: time the ticket spent in working statuses. created_to_resolved: wall-clock
# time from creation to resolution. estimate: derived from activity by discovery.
EffortSource = Literal["logged", "status_time", "created_to_resolved", "estimate"]


class Comment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: str
    role: CommentRole = "other"
    created: datetime
    body: str


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime
    from_status: str | None = None
    to_status: str


class CodeChange(BaseModel):
    """A merge request or commit linked to the ticket."""

    model_config = ConfigDict(extra="forbid")

    repo: str
    ref: str
    title: str
    merged_at: datetime | None = None


class EngineerAction(BaseModel):
    """One tool-level action the engineer took, as recorded by an audit log or change record.

    Tool names use the same flat `server_tool` naming as the mock MCP servers so that the
    scorer can compare them with recorded agent writes directly.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    at: datetime
    source: str = "audit_log"


class TicketLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: LinkType
    target: str


class Ticket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    project: str
    summary: str
    description: str
    reporter: str
    assignee: str | None = None
    created: datetime
    resolved: datetime | None = None
    status: str
    resolution: str | None = None
    priority: str = "Medium"
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)
    status_changes: list[StatusChange] = Field(default_factory=list)
    code_changes: list[CodeChange] = Field(default_factory=list)
    actions: list[EngineerAction] = Field(default_factory=list)
    links: list[TicketLink] = Field(default_factory=list)
    time_spent_hours: float | None = None
    # How time_spent_hours was obtained; None when no effort figure is known.
    effort_source: EffortSource | None = None
    # A snapshot of the inventory relevant to the ticket at the time it was raised (for
    # example existing topics or current quotas). Real sources fill this from a CMDB
    # export when they have one; it is empty when unknown.
    inventory: dict[str, Any] = Field(default_factory=dict)

    def comments_before_first_engineer_reply(self) -> list[Comment]:
        """What the agent gets to see: the ticket as filed, before anyone on the team acted."""
        shown: list[Comment] = []
        for comment in sorted(self.comments, key=lambda c: c.created):
            if comment.role == "engineer":
                break
            shown.append(comment)
        return shown
