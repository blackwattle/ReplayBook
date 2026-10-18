"""Jira Cloud as a ticket source: REST API v2 with the changelog, paginated.

Credentials come from .env (JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN) and are only ever sent
as the Basic auth header to that site; nothing here logs them. The air-gap guard applies:
in air-gapped mode the site has to be listed in REPLAYBOOK_ALLOWED_HOSTS. Responses are
the same issue documents the Public Jira Dataset holds, so the conversion is shared.

Endpoints, as documented for the Jira Cloud platform REST API v2:
- GET /rest/api/2/search/jql?jql=...&fields=*all&expand=changelog&maxResults=100
  &nextPageToken=...   -> {"issues": [...], "nextPageToken": "...", "isLast": bool}
- GET /rest/api/2/issue/{key}/changelog?startAt=0&maxResults=100
  -> {"startAt", "maxResults", "total", "isLast", "values": [...]}
  (used when the changelog inlined in a search result is shorter than its total)

User objects in the raw copy are cut down to account ids before anything is written, so
display names and email addresses never land on disk.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dotenv import load_dotenv

from replaybook.adapters.public_jira import convert_project, data_card, summarise

SOURCE = "jira_cloud"
PAGE_SIZE = 100
USER_KEYS = ("accountId", "accountType", "active", "key", "name")
USER_FIELDS = {"assignee", "reporter", "creator"}
Fetch = Callable[[str, dict[str, str]], dict[str, Any]]


class JiraCloudError(RuntimeError):
    """Configuration or HTTP problem talking to Jira Cloud."""


@dataclass(frozen=True)
class JiraCloudConfig:
    site: str
    email: str
    token: str

    @classmethod
    def from_env(cls) -> JiraCloudConfig:
        load_dotenv(override=False)
        values = {
            name: os.environ.get(name, "").strip()
            for name in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN")
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise JiraCloudError(
                f"set {', '.join(missing)} in .env (see docs/ticket-sources.md for the setup steps)"
            )
        return cls(values["JIRA_SITE"].rstrip("/"), values["JIRA_EMAIL"], values["JIRA_API_TOKEN"])


def strip_users(value: Any) -> Any:
    """Keep only identifiers of Jira user objects, recursively.

    Changelog items for user fields (assignee, reporter) carry display names in their
    fromString and toString; those are replaced with the account ids in from and to.
    """
    if isinstance(value, dict):
        if "accountId" in value or ("displayName" in value and "self" in value):
            return {k: value[k] for k in USER_KEYS if k in value}
        if str(value.get("field", "")).lower() in USER_FIELDS and "toString" in value:
            value = {**value, "fromString": value.get("from"), "toString": value.get("to")}
        return {k: strip_users(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_users(item) for item in value]
    return value


class JiraCloud:
    def __init__(self, config: JiraCloudConfig, fetch: Fetch | None = None) -> None:
        self.config = config
        self.fetch = fetch or self._http_get

    # --- transport ----------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        raw = f"{self.config.email}:{self.config.token}".encode()
        return {
            "Authorization": "Basic " + base64.b64encode(raw).decode("ascii"),
            "Accept": "application/json",
        }

    def _http_get(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        shown = url.split("?")[0]
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise JiraCloudError(f"HTTP {exc.code} from {shown}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            hint = (
                " (air-gapped mode: add the site to REPLAYBOOK_ALLOWED_HOSTS)"
                if "air-gapped" in str(reason)
                else ""
            )
            raise JiraCloudError(f"cannot reach {shown}: {reason}{hint}") from exc

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.fetch(f"{self.config.site}{path}?{urlencode(params)}", self._headers())

    # --- reads ----------------------------------------------------------------------------

    def search(self, jql: str, page_size: int = PAGE_SIZE) -> Iterator[dict[str, Any]]:
        """Every issue matching the JQL, with its full changelog."""
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": "*all",
                "expand": "changelog",
            }
            if token:
                params["nextPageToken"] = token
            page = self._get("/rest/api/2/search/jql", params)
            for issue in page.get("issues", []) or []:
                yield self._complete_changelog(issue)
            token = page.get("nextPageToken")
            if not token or page.get("isLast"):
                break

    def changelog(self, key: str, page_size: int = PAGE_SIZE) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        start = 0
        while True:
            page = self._get(
                f"/rest/api/2/issue/{key}/changelog", {"startAt": start, "maxResults": page_size}
            )
            batch = page.get("values", []) or []
            values.extend(batch)
            if page.get("isLast", True) or not batch:
                return values
            start += len(batch)

    def _complete_changelog(self, issue: dict[str, Any]) -> dict[str, Any]:
        log = issue.get("changelog") or {}
        histories = log.get("histories") or []
        total = log.get("total")
        if isinstance(total, int) and total > len(histories) and issue.get("key"):
            histories = self.changelog(str(issue["key"]))
            issue["changelog"] = {"histories": histories, "total": len(histories)}
        return issue

    # --- export ---------------------------------------------------------------------------

    def export_project(
        self, project: str, out_dir: Path, jql: str | None = None, effort: str = "logged"
    ) -> dict[str, Any]:
        """Raw copy (users reduced to ids), canonical redacted tickets and a data card.
        `effort` is the effort source: logged, status_time or created_to_resolved."""
        from replaybook.adapters.effort import apply_effort_all

        out_dir = Path(out_dir)
        raw_path = out_dir / "raw" / f"{project}.jsonl.gz"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        query = jql or f'project = "{project}" ORDER BY created ASC'
        count = 0
        with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
            for issue in self.search(query):
                handle.write(json.dumps(strip_users(issue), default=str))
                handle.write("\n")
                count += 1
        tickets = apply_effort_all(convert_project(raw_path, source=SOURCE), effort)
        with (out_dir / "tickets.jsonl").open("w", encoding="utf-8") as handle:
            for ticket in tickets:
                handle.write(ticket.model_dump_json())
                handle.write("\n")
        (out_dir / "platform.yaml").write_text(
            "# Procedure changes for this project, if any (request_type, at, from_version,\n"
            "# to_version). Nothing is known automatically.\n"
            "procedure_changes: []\n",
            encoding="utf-8",
        )
        stats = summarise(tickets, project, self.config.site, effort)
        exported = datetime.now(UTC).strftime("%Y-%m-%d")
        (out_dir / "DATASET.md").write_text(
            data_card(
                stats,
                title=f"Jira Cloud, project {project}",
                provenance=(
                    f"Exported from {self.config.site} on {exported} by `replaybook jira-cloud "
                    f"export` with the query `{query}`. User objects were reduced to account "
                    "ids before anything was written; the tickets were redacted at ingest "
                    "(emails, public IP addresses and secret-looking strings)."
                ),
            ),
            encoding="utf-8",
        )
        stats["raw_documents"] = count
        return stats
