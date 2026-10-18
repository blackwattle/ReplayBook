"""Generic text helpers for the packs' parameter extractors.

A pack's agent.py reads the visible ticket text with regular expressions to find the
parameters its procedure needs. The helpers here know about host names, environments
and IP addresses, nothing about any tool. Missing keys mean the information was not in
the ticket; whether to ask or guess is the agent's decision, not the extractor's.
"""

from __future__ import annotations

import re

ENVS = ("dev", "test", "uat", "prod")
HOSTNAME = re.compile(r"\b((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,})\b", re.IGNORECASE)
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ABBREVIATIONS = ("e.g", "i.e", "etc", "vs")
FILE_SUFFIXES = (".txt", ".md", ".py", ".sh", ".yml", ".yaml", ".json", ".log", ".html")
STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "has",
    "her",
    "him",
    "in",
    "is",
    "me",
    "needs",
    "of",
    "on",
    "the",
    "them",
    "this",
    "to",
    "us",
    "user",
    "someone",
    "somebody",
}


def first(patterns: list[str], text: str, flags: int = re.IGNORECASE) -> str | None:
    """The first capture group of the first pattern that matches."""
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return match.group(1)
    return None


def find_env(text: str) -> str | None:
    match = re.search(r"\b(dev|test|uat|prod)\b", text, re.IGNORECASE)
    return match.group(1).lower() if match else None


def hostnames(text: str) -> list[str]:
    """Distinct host names in order of appearance, skipping abbreviations and file names."""
    found: list[str] = []
    for match in HOSTNAME.finditer(text):
        host = match.group(1).lower().strip(".")
        if host in ABBREVIATIONS or host.endswith(FILE_SUFFIXES) or host in found:
            continue
        found.append(host)
    return found


def clean(value: str | None) -> str | None:
    return value.strip().rstrip(".,;:)").lower() if value else None


def team_name(text: str) -> str | None:
    found = first([r"\b([a-z0-9-]+) team\b"], text)
    return found.lower() if found else None
