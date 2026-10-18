"""Discovery groups past tickets into request types and ranks them by engineer-hours."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from replaybook.adapters import public_jira
from replaybook.adapters.synthetic import load_tickets
from replaybook.cli import app
from replaybook.discover import discover, estimate_hours, render_markdown, write_report
from replaybook.eval import quality

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "jira" / "issues.jsonl"


def test_discovery_ranks_types_by_hours_and_agrees_with_ground_truth(small_dataset: Path) -> None:
    tickets = load_tickets(small_dataset)
    report = discover(tickets)
    assert report.tickets == len(tickets)
    assert report.typed + report.untyped == report.tickets
    assert report.types and report.types == sorted(report.types, key=lambda s: -s.hours)
    typed_share = sum(s.share_of_hours for s in report.types)
    assert abs(typed_share + report.untyped_hours / report.total_hours - 1) < 0.01
    assert 0.5 < typed_share < 1.0  # one-off noise tickets take the rest
    assert 0 < report.logged_share < 1
    keys = {s.key for s in report.types}
    assert {"kafka_topic", "openstack_quota", "artifactory_repo"} <= keys
    by_key = {s.key: s for s in report.types}
    assert by_key["kafka_topic"].skill_available and not by_key["dns_change"].skill_available
    assert all(len(s.examples) <= 3 for s in report.types)
    assert report.recommendation == [s.key for s in report.types[:3]]
    # Against the hidden true effort: the ranking is close, not a coincidence.
    agreement = quality.discovery_agreement(report.recommendation, small_dataset)
    assert agreement["overlap"] >= 2, agreement


def test_discovery_is_deterministic(small_dataset: Path) -> None:
    tickets = load_tickets(small_dataset)
    assert discover(tickets).model_dump() == discover(list(reversed(tickets))).model_dump()


def test_estimate_hours_prefers_logged_time(small_dataset: Path) -> None:
    tickets = load_tickets(small_dataset)
    logged = next(t for t in tickets if t.time_spent_hours is not None)
    assert estimate_hours(logged) == (logged.time_spent_hours, True)
    unlogged = next(t for t in tickets if t.time_spent_hours is None)
    hours, is_logged = estimate_hours(unlogged)
    assert not is_logged and 0.25 <= hours <= 8.0


def test_report_files_and_cli(small_dataset: Path, tmp_path: Path) -> None:
    report = discover(load_tickets(small_dataset))
    paths = write_report(report, tmp_path / "disc")
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["types"][0]["key"] == report.types[0].key
    text = render_markdown(report)
    assert "| Rank | Request type |" in text and "## Test first" in text and "## Method" in text
    assert report.types[0].label in text
    result = CliRunner().invoke(
        app, ["discover", "--data", str(small_dataset), "--out", str(tmp_path / "cli")]
    )
    assert result.exit_code == 0, result.output
    assert "Test first:" in result.output and (tmp_path / "cli" / "discovery.md").exists()


def test_discovery_on_real_tickets_without_logged_time(tmp_path: Path) -> None:
    out = tmp_path / "infra"
    public_jira.export(FIXTURE, "INFRA", out)
    report = discover(load_tickets(out))
    assert report.tickets == 3
    keys = [s.key for s in report.types]
    assert "dns_change" in keys and "cert_renewal" in keys
    assert report.logged_share > 0  # two of the three fixture tickets logged time
    markdown = render_markdown(report)
    assert "Untyped" not in markdown or report.untyped
