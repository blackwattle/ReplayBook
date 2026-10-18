from __future__ import annotations

from pathlib import Path

from replaybook.adapters.redact import redact_text
from replaybook.adapters.synthetic import load_tickets


def test_emails_and_public_ips_are_redacted_consistently() -> None:
    text = (
        "Mail ana@example.com or ana@example.com, logs from 203.0.113.9 and 198.51.100.2, "
        "again 203.0.113.9. Internal 10.20.30.40 stays."
    )
    out = redact_text(text)
    assert "ana@example.com" not in out
    assert out.count("<email-1>") == 2
    assert out.count("<ip-1>") == 2
    assert "<ip-2>" in out
    assert "10.20.30.40" in out


def test_secrets_are_redacted() -> None:
    out = redact_text("token glpat-abcdefghijklmnopqrstu and password: Summer2024! here")
    assert "glpat-" not in out
    assert "Summer2024" not in out
    assert "password: <redacted>" in out


def test_loader_redacts_noise_tickets(small_dataset: Path) -> None:
    raw = load_tickets(small_dataset, redact=False)
    clean = load_tickets(small_dataset, redact=True)
    assert any("@" in t.description for t in raw)
    assert not any("@example" in t.description for t in clean)
    assert not any("203.0.113." in t.description for t in clean)
