"""Rule-based request-type classifier over visible ticket text.

Used by the test-set builder to pick tickets of a type and by the scripted agent to decide
which procedure applies. It only ever sees visible data (text, labels, components). The
request types, their keywords and which words count double come from the packs.
"""

from __future__ import annotations

import re

from replaybook.packs import PackRegistry, registry
from replaybook.schema.ticket import Ticket

MIN_SCORE = 3.0


def classify_text(
    summary: str,
    description: str,
    labels: list[str],
    components: list[str],
    packs: PackRegistry | None = None,
) -> tuple[str | None, float]:
    """Return (request type, score) or (None, score) when nothing fits well enough."""
    reg = packs if packs is not None else registry()
    summary_l = summary.lower()
    body_l = description.lower()
    tags = {t.lower() for t in [*labels, *components]}
    best: tuple[str | None, float] = (None, 0.0)
    for key, rt in reg.request_types.items():
        if rt.exclude_keywords and not _any_word(rt.anchor_keywords, summary_l, body_l):
            if _any_word(rt.exclude_keywords, summary_l, body_l):
                continue
        strong = set(rt.strong_keywords)
        score = 0.0
        for word in rt.keywords:
            pattern = re.compile(r"\b" + re.escape(word) + r"\b", re.IGNORECASE)
            weight = 2.0 if word in strong else 1.0
            if pattern.search(summary_l):
                score += 2.0 * weight
            if pattern.search(body_l):
                score += 1.0 * weight
        if tags & set(rt.components):
            score += 3.0
        if key.replace("_", "-") in tags or key in tags:
            score += 3.0
        if score > best[1]:
            best = (key, score)
    if best[1] < MIN_SCORE:
        return None, best[1]
    return best


def _any_word(words: tuple[str, ...], *texts: str) -> bool:
    return any(
        re.search(r"\b" + re.escape(word) + r"\b", text, re.IGNORECASE)
        for word in words
        for text in texts
    )


def classify(ticket: Ticket, packs: PackRegistry | None = None) -> tuple[str | None, float]:
    return classify_text(
        ticket.summary, ticket.description, ticket.labels, ticket.components, packs=packs
    )
