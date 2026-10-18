"""The Jira Cloud adapter, against fixtures shaped like the documented REST v2 responses:
paginated search with nextPageToken, changelog completion, Basic auth, user stripping."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from typer.testing import CliRunner

from replaybook.adapters.jira_cloud import JiraCloud, JiraCloudConfig, JiraCloudError, strip_users
from replaybook.adapters.synthetic import load_tickets
from replaybook.cli import app

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"
SITE = "https://example.atlassian.net"
CONFIG = JiraCloudConfig(site=SITE, email="ben@example.com", token="secret-token")


def _infra_issues() -> list[dict[str, Any]]:
    docs = [
        json.loads(line)
        for line in (FIXTURES / "issues.jsonl").read_text("utf-8").splitlines()
        if line
    ]
    issues = [d for d in docs if d["key"].startswith("INFRA-")]
    for issue in issues:
        issue["fields"]["reporter"] = {
            "self": f"{SITE}/rest/api/2/user?accountId=acc-r",
            "accountId": issue["fields"]["reporter"].get("key")
            or issue["fields"]["reporter"].get("accountId"),
            "displayName": "A Person",
            "emailAddress": "a.person@example.com",
        }
    # The first search page inlines a truncated changelog for INFRA-1001 (total 3, one shown).
    first = issues[0]
    first["changelog"] = {
        "startAt": 0,
        "maxResults": 1,
        "total": 3,
        "histories": first["changelog"]["histories"][:1],
    }
    return issues


class FakeJira:
    """Serves documented response shapes and records every request."""

    def __init__(self) -> None:
        self.issues = _infra_issues()
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        self.calls.append((url, headers))
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        if parts.path == "/rest/api/2/search/jql":
            assert query["expand"] == ["changelog"] and query["fields"] == ["*all"]
            if "nextPageToken" not in query:
                return {"issues": self.issues[:2], "nextPageToken": "page-2", "isLast": False}
            assert query["nextPageToken"] == ["page-2"]
            return {"issues": self.issues[2:], "isLast": True}
        if parts.path == "/rest/api/2/issue/INFRA-1001/changelog":
            return json.loads((FIXTURES / "cloud_changelog.json").read_text(encoding="utf-8"))
        raise AssertionError(f"unexpected request {url}")


def test_search_paginates_and_completes_truncated_changelogs() -> None:
    fake = FakeJira()
    client = JiraCloud(CONFIG, fetch=fake)
    issues = list(client.search('project = "INFRA" ORDER BY created ASC'))
    assert [i["key"] for i in issues] == ["INFRA-1001", "INFRA-1002", "INFRA-1003"]
    assert len(issues[0]["changelog"]["histories"]) == 3 and issues[0]["changelog"]["total"] == 3
    paths = [urlsplit(url).path for url, _ in fake.calls]
    assert paths == [
        "/rest/api/2/search/jql",
        "/rest/api/2/issue/INFRA-1001/changelog",
        "/rest/api/2/search/jql",
    ]
    expected_auth = "Basic " + base64.b64encode(b"ben@example.com:secret-token").decode()
    assert all(h["Authorization"] == expected_auth for _, h in fake.calls)
    assert all(h["Accept"] == "application/json" for _, h in fake.calls)


def test_export_writes_tickets_without_personal_data(tmp_path: Path) -> None:
    fake = FakeJira()
    stats = JiraCloud(CONFIG, fetch=fake).export_project("INFRA", tmp_path / "infra")
    assert stats["raw_documents"] == 3 and stats["tickets"] == 3
    with gzip.open(tmp_path / "infra" / "raw" / "INFRA.jsonl.gz", "rt", encoding="utf-8") as handle:
        raw = handle.read()
    assert "a.person@example.com" not in raw and "A Person" not in raw
    assert "infra.one@example.com" not in raw and "Infra One" not in raw
    tickets = load_tickets(tmp_path / "infra")
    assert [t.source for t in tickets] == ["jira_cloud"] * 3
    first = tickets[0]
    assert [c.to_status for c in first.status_changes] == [
        "Waiting on Requester",
        "In Progress",
        "Closed",
    ]
    assert first.reporter == "u-reporter-1" and "<email-1>" in first.description
    card = (tmp_path / "infra" / "DATASET.md").read_text(encoding="utf-8")
    assert "Jira Cloud, project INFRA" in card and SITE in card and "secret-token" not in card
    assert "unverified" in card


def test_strip_users_keeps_only_identifiers() -> None:
    doc = {
        "fields": {
            "assignee": {
                "self": "x",
                "accountId": "a1",
                "displayName": "Name",
                "emailAddress": "e@x",
                "active": True,
            },
            "comment": {
                "comments": [{"author": {"accountId": "a2", "displayName": "N2"}, "body": "hi"}]
            },
            "labels": ["keep"],
        }
    }
    stripped = strip_users(doc)
    assert stripped["fields"]["assignee"] == {"accountId": "a1", "active": True}
    item = {"field": "assignee", "from": "a1", "fromString": "Name", "to": "a2", "toString": "N2"}
    scrubbed = strip_users(item)
    assert scrubbed["fromString"] == "a1" and scrubbed["toString"] == "a2"
    assert stripped["fields"]["comment"]["comments"][0]["author"] == {"accountId": "a2"}
    assert stripped["fields"]["labels"] == ["keep"]


def test_config_from_env_reports_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(JiraCloudError, match="JIRA_SITE"):
        JiraCloudConfig.from_env()
    monkeypatch.setenv("JIRA_SITE", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "ben@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    assert JiraCloudConfig.from_env().site == "https://example.atlassian.net"


def test_cli_fails_clearly_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("JIRA_SITE", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    result = CliRunner().invoke(
        app, ["jira-cloud", "export", "--project", "OPS", "--out", str(tmp_path / "o")]
    )
    assert result.exit_code == 2
    assert "JIRA_API_TOKEN" in result.output


def test_http_errors_become_clear_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error
    import urllib.request

    def failing(request: Any, timeout: float = 0) -> Any:
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", failing)
    with pytest.raises(JiraCloudError, match="HTTP 401"):
        list(JiraCloud(CONFIG).search("project = X"))

    def unreachable(request: Any, timeout: float = 0) -> Any:
        raise urllib.error.URLError("air-gapped mode refused lookup of example.atlassian.net")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    with pytest.raises(JiraCloudError, match="REPLAYBOOK_ALLOWED_HOSTS"):
        list(JiraCloud(CONFIG).search("project = X"))
