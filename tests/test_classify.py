"""The text classifier on real-world phrasing that synthetic data never produces."""

from __future__ import annotations

from replaybook.classify import classify_text


def test_git_repositories_are_not_artifactory_repositories() -> None:
    kind, _ = classify_text(
        "Create git repository for apache/foo",
        "Please create a new GitHub repository named foo under the apache org.",
        [],
        [],
    )
    assert kind != "artifactory_repo"
    kind, _ = classify_text(
        "Artifactory repo for the git plugin",
        "We need a maven repository in Artifactory with read-write access.",
        [],
        [],
    )
    assert kind == "artifactory_repo"


def test_plain_requests_still_classify() -> None:
    assert classify_text("Renew TLS cert for a.example.org", "", [], [])[0] == "cert_renewal"
    assert classify_text("DNS CNAME for b.example.org", "", [], [])[0] == "dns_change"
