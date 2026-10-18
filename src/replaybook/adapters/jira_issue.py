"""Turn one Jira issue document into a canonical Ticket.

The Public Jira Dataset stores issues as the Jira REST API v2 returned them (`fields`,
`changelog`), and Jira Cloud returns the same shape live, so both adapters share this
module. Everything here is visible data; nothing is inferred beyond simple mappings:

- statuses map onto the canonical names the test-set builder understands (Open,
  In Progress, Waiting on Requester, Resolved, Closed); unknown names are kept as they are;
- comment authors get a role: the reporter is the requester, assignees and people who
  moved the issue through its workflow are engineers, well-known bots are bots;
- status changes come from the changelog, code changes from bot comments that mention
  commits or pull requests, links from issue links;
- there are no recorded engineer actions. Real history has prose, not an audit log, which
  is why expectations built from it stay unverified until a person confirms them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from replaybook.schema.ticket import (
    CodeChange,
    Comment,
    CommentRole,
    EngineerAction,
    StatusChange,
    Ticket,
    TicketLink,
)

STATUS_MAP = {
    "open": "Open",
    "reopened": "Open",
    "new": "Open",
    "to do": "Open",
    "backlog": "Open",
    "triage": "Open",
    "triage needed": "Open",
    "in progress": "In Progress",
    "in review": "In Progress",
    "under review": "In Progress",
    "waiting for infra": "In Progress",
    "waiting for assignee": "In Progress",
    "waiting for user": "Waiting on Requester",
    "waiting on user": "Waiting on Requester",
    "waiting for reporter": "Waiting on Requester",
    "waiting on reporter": "Waiting on Requester",
    "waiting for customer": "Waiting on Requester",
    "waiting on customer": "Waiting on Requester",
    "waiting for requester": "Waiting on Requester",
    "waiting on requester": "Waiting on Requester",
    "pending user": "Waiting on Requester",
    "need more info": "Waiting on Requester",
    "needs info": "Waiting on Requester",
    "incomplete": "Waiting on Requester",
    "resolved": "Resolved",
    "done": "Closed",
    "closed": "Closed",
}
RESOLUTION_MAP = {
    "fixed": "Done",
    "done": "Done",
    "resolved": "Done",
    "implemented": "Done",
    "completed": "Done",
    "delivered": "Done",
    "duplicate": "Duplicate",
}
BOT_NAMES = (
    "bot",
    "hudson",
    "jenkins",
    "jira-bot",
    "automation",
    "asf subversion and git services",
    "asf github bot",
    "githubbot",
    "gitbox",
    "sonar",
)
PR_URL = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
# The Public Jira Dataset anonymises user fields as <<|author_key|<uuid>|>> (or
# <<|author_self|>> when the value was dropped entirely).
PLACEHOLDER = re.compile(r"^<<\|\w+\|(?:(.*?)\|)?>>$")
COMMIT_REF = re.compile(r"\bcommit\s+([0-9a-f]{7,40})\b", re.IGNORECASE)
COMMIT_REPO = re.compile(r"\brepo(?:sitory)?\s*:?\s*([\w.-]+(?:/[\w.-]+)?)", re.IGNORECASE)


def parse_jira_datetime(value: Any) -> datetime | None:
    """Jira writes 2019-05-03T10:11:12.000+0000; Mongo dumps may hold real datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def unwrap(value: Any) -> str | None:
    """Plain text from a dataset placeholder: <<|author_key|abc|>> -> abc, <<|x|>> -> None."""
    if value is None or value == "":
        return None
    text = str(value)
    match = PLACEHOLDER.match(text)
    if match:
        return match.group(1) or None
    return text


def user_id(user: Any) -> str | None:
    """A stable, non-personal identifier for a Jira user object (or None)."""
    if not isinstance(user, dict):
        return unwrap(user)
    for key in ("accountId", "key", "name", "displayName"):
        found = unwrap(user.get(key))
        if found:
            return found
    return None


def user_display(user: Any) -> str:
    if isinstance(user, dict):
        return str(user.get("displayName") or user.get("name") or user.get("key") or "")
    return str(user or "")


def is_bot(user: Any) -> bool:
    text = f"{user_display(user)} {user_id(user) or ''}".lower()
    if isinstance(user, dict) and user.get("accountType") == "app":
        return True
    return any(name in text for name in BOT_NAMES)


def canonical_status(name: Any) -> str:
    text = str(name or "").strip()
    return STATUS_MAP.get(text.lower(), text or "Open")


def canonical_resolution(name: Any) -> str | None:
    if not name:
        return None
    text = str(name).strip()
    return RESOLUTION_MAP.get(text.lower(), text)


def text_of(value: Any) -> str:
    """Plain text from a string, or from an Atlassian Document Format tree (Cloud v3)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") == "text":
            return str(value.get("text", ""))
        if value.get("type") == "hardBreak":
            return "\n"
        parts = [text_of(child) for child in value.get("content", [])]
        joined = "".join(parts)
        return (
            joined + "\n" if value.get("type") in ("paragraph", "heading", "listItem") else joined
        )
    if isinstance(value, list):
        return "".join(text_of(item) for item in value)
    return str(value)


def workflow_movers(issue: dict[str, Any]) -> set[str]:
    """People who changed status, resolution or assignee: the team working the queue."""
    movers: set[str] = set()
    for history in (issue.get("changelog") or {}).get("histories", []) or []:
        fields = {str(item.get("field", "")).lower() for item in history.get("items", []) or []}
        if fields & {"status", "resolution", "assignee"}:
            who = user_id(history.get("author"))
            if who:
                movers.add(who)
    return movers


def _comment_role(author: Any, reporter: str | None, engineers: set[str]) -> CommentRole:
    who = user_id(author)
    if is_bot(author):
        return "bot"
    if who and who in engineers:
        return "engineer"
    if who and who == reporter:
        return "requester"
    return "other"


def _code_changes(comments: Iterable[dict[str, Any]], project: str) -> list[CodeChange]:
    found: list[CodeChange] = []
    seen: set[tuple[str, str]] = set()
    for comment in comments:
        if not is_bot(comment.get("author")):
            continue
        body = text_of(comment.get("body"))
        when = parse_jira_datetime(comment.get("created"))
        for match in PR_URL.finditer(body):
            key = (match.group(1), f"!{match.group(2)}")
            if key not in seen:
                seen.add(key)
                found.append(
                    CodeChange(repo=key[0], ref=key[1], title=body.strip()[:120], merged_at=when)
                )
        for match in COMMIT_REF.finditer(body):
            repo_match = COMMIT_REPO.search(body)
            repo = repo_match.group(1) if repo_match else project.lower()
            key = (repo, match.group(1)[:12])
            if key not in seen:
                seen.add(key)
                found.append(
                    CodeChange(repo=key[0], ref=key[1], title=body.strip()[:120], merged_at=when)
                )
    return found


def _links(issue_links: Iterable[dict[str, Any]]) -> list[TicketLink]:
    links: list[TicketLink] = []
    for link in issue_links or []:
        type_name = str((link.get("type") or {}).get("name", "")).lower()
        outward = (link.get("outwardIssue") or {}).get("key")
        inward = (link.get("inwardIssue") or {}).get("key")
        if outward and type_name == "duplicate":
            links.append(TicketLink(type="duplicates", target=str(outward)))
        elif outward:
            links.append(TicketLink(type="relates", target=str(outward)))
        elif inward:
            links.append(TicketLink(type="relates", target=str(inward)))
    return links


def issue_to_ticket(
    issue: dict[str, Any], source: str, engineers: set[str] | None = None
) -> Ticket:
    fields = issue.get("fields") or {}
    key = str(issue.get("key") or fields.get("key") or issue.get("id"))
    project = str((fields.get("project") or {}).get("key") or key.split("-")[0])
    reporter = user_id(fields.get("reporter"))
    assignee = user_id(fields.get("assignee"))
    team = set(engineers or ()) | workflow_movers(issue)
    if assignee:
        team.add(assignee)

    # Jira nests comments under fields.comment.comments; the Public Jira Dataset flattens
    # them to fields.comments.
    raw_comments = ((fields.get("comment") or {}).get("comments")) or fields.get("comments") or []
    comments: list[Comment] = []
    for raw in raw_comments:
        when = parse_jira_datetime(raw.get("created"))
        if when is None:
            continue
        comments.append(
            Comment(
                author=user_id(raw.get("author")) or "unknown",
                role=_comment_role(raw.get("author"), reporter, team),
                created=when,
                body=text_of(raw.get("body")),
            )
        )

    status_changes: list[StatusChange] = []
    for history in (issue.get("changelog") or {}).get("histories", []) or []:
        when = parse_jira_datetime(history.get("created"))
        if when is None:
            continue
        for item in history.get("items", []) or []:
            if str(item.get("field", "")).lower() == "status":
                status_changes.append(
                    StatusChange(
                        at=when,
                        from_status=canonical_status(item.get("fromString"))
                        if item.get("fromString")
                        else None,
                        to_status=canonical_status(item.get("toString")),
                    )
                )
    status_changes.sort(key=lambda c: c.at)

    created = parse_jira_datetime(fields.get("created")) or datetime(2000, 1, 1, tzinfo=UTC)
    time_spent = fields.get("timespent")
    return Ticket(
        id=key,
        source=source,
        project=project,
        summary=str(fields.get("summary") or ""),
        description=text_of(fields.get("description")),
        reporter=reporter or "unknown",
        assignee=assignee,
        created=created,
        resolved=parse_jira_datetime(fields.get("resolutiondate")),
        status=canonical_status((fields.get("status") or {}).get("name")),
        resolution=canonical_resolution((fields.get("resolution") or {}).get("name")),
        priority=str((fields.get("priority") or {}).get("name") or "Medium"),
        labels=[str(label) for label in fields.get("labels") or []],
        components=[str(c.get("name", "")) for c in fields.get("components") or [] if c],
        comments=comments,
        status_changes=status_changes,
        code_changes=_code_changes(raw_comments, project),
        actions=[],  # no audit log in a ticket system; see EngineerAction
        links=_links(fields.get("issuelinks") or []),
        time_spent_hours=round(float(time_spent) / 3600, 2) if time_spent else None,
        effort_source="logged" if time_spent else None,
    )


__all__ = [
    "EngineerAction",
    "canonical_resolution",
    "canonical_status",
    "issue_to_ticket",
    "parse_jira_datetime",
    "text_of",
    "user_id",
    "workflow_movers",
]
