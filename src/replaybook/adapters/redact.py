"""Redaction at ingest: emails, public IP addresses and secret-looking strings.

Private (RFC 1918) addresses are kept because they are the content of DNS and firewall
requests. Replacements are consistent within a ticket so an agent can still tell two
addresses apart.
"""

from __future__ import annotations

import re

from replaybook.schema.ticket import Ticket

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
SECRET = re.compile(
    r"(glpat-[A-Za-z0-9_\-.]{10,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}"
    r"|(?i:password|passwd|secret|token)\s*(?:is|was|[:=])\s*['\"]?[^\s'\"]{4,}['\"]?)"
)


def _is_private(octets: tuple[int, int, int, int]) -> bool:
    a, b, _, _ = octets
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or a == 127


class Redactor:
    """Consistent placeholders within one ticket."""

    def __init__(self) -> None:
        self.emails: dict[str, str] = {}
        self.ips: dict[str, str] = {}

    def text(self, value: str) -> str:
        value = SECRET.sub(self._secret, value)
        value = EMAIL.sub(self._email, value)
        return IPV4.sub(self._ip, value)

    def _secret(self, match: re.Match[str]) -> str:
        head = match.group(0).split(":")[0].split("=")[0].split(" ")[0]
        if head.lower() in {"password", "passwd", "secret", "token"}:
            return f"{head}: <redacted>"
        return "<redacted-secret>"

    def _email(self, match: re.Match[str]) -> str:
        return self.emails.setdefault(match.group(0), f"<email-{len(self.emails) + 1}>")

    def _ip(self, match: re.Match[str]) -> str:
        octets = tuple(int(g) for g in match.groups())
        if any(o > 255 for o in octets) or _is_private(octets):  # type: ignore[arg-type]
            return match.group(0)
        return self.ips.setdefault(match.group(0), f"<ip-{len(self.ips) + 1}>")


def redact_text(value: str) -> str:
    return Redactor().text(value)


def redact_ticket(ticket: Ticket) -> Ticket:
    redactor = Redactor()
    comments = [c.model_copy(update={"body": redactor.text(c.body)}) for c in ticket.comments]
    return ticket.model_copy(
        update={
            "summary": redactor.text(ticket.summary),
            "description": redactor.text(ticket.description),
            "comments": comments,
        }
    )
