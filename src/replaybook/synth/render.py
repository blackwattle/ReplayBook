"""Turns sampled parameters into messy human text, offline and deterministically.

Templates reference parameters as `{name}`. A ticket with missing information simply
leaves out the phrases and lines for the missing fields, and never picks a summary that
would leak them.
"""

from __future__ import annotations

import random
import string
from collections.abc import Iterable
from typing import Any

from replaybook.synth.names import pick
from replaybook.synth.types import RequestType

CLOSINGS = (
    "Thanks!",
    "Cheers.",
    "Thanks in advance.",
    "",
    "",
    "",
    "Let me know if you need more info.",
    "This is blocking our release, so asap please.",
    "No rush, sometime this sprint is fine.",
)
URGENT_PREFIXES = ("URGENT: ", "[ops] ", "pls help - ", "Fwd: ")
DONE_COMMENTS = (
    "done",
    "Done.",
    "completed",
    "Sorted.",
    "Done, please verify.",
    "All done - closing.",
    "Fixed",
)
ASK_TEMPLATES = (
    "Hi {reporter}, what {label} do you need? I can't proceed without it.",
    "Which {label}? It isn't in the ticket.",
    "Could you add the {label} please, then I'll get this done.",
    "Need the {label} before I can action this - can you update the ticket?",
)
REPLY_TEMPLATES = (
    "{label}: {value}",
    "Sorry, it's {value}.",
    "{value} please.",
    "Ah sorry - {value}. Thanks!",
)
REOPEN_COMMENTS = (
    "Still not working on our side, can you check?",
    "Reopening - the change does not seem to have taken effect.",
    "Hmm, we still get the same error. Reopening.",
)
REOPEN_FIX_COMMENTS = (
    "Apologies, a cache needed clearing. Should be fine now.",
    "Had to re-apply, please try again.",
    "Fixed properly this time.",
)

# Judgement tickets: what the requester adds that should make an agent hand the ticket back
# to a person, and how the engineers then handled it (vendor-neutral, any request type).
APPROVAL_CUES = (
    "Heads-up: this one needs sign-off from the change manager before it goes ahead.",
    "This has to go through change approval before anyone actions it.",
    "Please don't action this until it has been approved by the platform lead.",
)
DISCUSSION_CUES = (
    "Not sure this is the right setup: it may clash with what the {team} team is doing, so "
    "it probably needs a second opinion.",
    "We are not sure whether this conflicts with the {team} team's change on the same "
    "platform; worth a discussion before doing it.",
)
APPROVAL_HOLD_COMMENTS = (
    "Holding this until the change is approved; asked the change manager for sign-off.",
    "This needs CAB approval before I touch it, raised a change request.",
)
APPROVAL_GIVEN_COMMENTS = ("Approved, go ahead.", "CAB approved the change, fine to proceed.")
DISCUSSION_ASK_COMMENTS = (
    "This may clash with the {team} team's work on the same setup. {other}, can you take a "
    "look before I go ahead?",
    "{other}, could you check this against the {team} team's change before I do anything?",
)
DISCUSSION_REPLY_COMMENTS = (
    "Checked with the {team} team: no clash, fine to go ahead as requested.",
    "Looked at it with the {team} team, go ahead as asked.",
)
UNUSUAL_DONE_COMMENTS = (
    "Did the extra change from the description as well.",
    "Also done the additional bit you asked for.",
)


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fmt(template: str, params: dict[str, Any], rt: RequestType | None = None) -> str:
    extra = rt.derived(params) if rt is not None and rt.derived is not None else {}
    return template.format_map(_SafeDict({**params, **extra}))


def placeholders(template: str) -> set[str]:
    return {
        field_name.split(".")[0].split("[")[0]
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name
    }


def _without_missing(templates: Iterable[str], missing: Iterable[str]) -> list[str]:
    missing_set = set(missing)
    return [t for t in templates if not (placeholders(t) & missing_set)]


def render_summary(
    rt: RequestType, params: dict[str, Any], missing: list[str], rng: random.Random
) -> str:
    candidates = _without_missing(rt.summaries, missing) or [f"{rt.label} request"]
    text = fmt(pick(rng, candidates), params, rt)
    if rng.random() < 0.08:
        text = pick(rng, URGENT_PREFIXES) + text
    if rng.random() < 0.05:
        text = text.lower()
    return text


def render_description(
    rt: RequestType,
    params: dict[str, Any],
    missing: list[str],
    rng: random.Random,
) -> str:
    """Prose or a details block, with the missing fields left out."""
    intros = _without_missing(rt.intros, missing) or [f"{rt.label} request."]
    intro = fmt(pick(rng, intros), params, rt) if rng.random() > 0.1 else ""
    present = [f for f in rt.field_phrases if f not in missing]
    style = "details" if rng.random() < 0.55 else "prose"

    if style == "details":
        lines = []
        for field_name in present:
            label = pick(rng, rt.field_labels[field_name])
            lines.append(f"{label}: {params[field_name]}")
        body = "\n".join(lines)
        text = f"{intro}\n\n{body}" if intro else body
    else:
        sentences = [fmt(pick(rng, rt.field_phrases[f]), params, rt) for f in present]
        if len(sentences) > 2 and rng.random() < 0.3:
            cut = rng.randint(1, len(sentences) - 1)
            body = " ".join(sentences[:cut]) + "\n\n" + " ".join(sentences[cut:])
        else:
            body = " ".join(sentences)
        text = f"{intro} {body}".strip()

    closing = pick(rng, CLOSINGS)
    if closing:
        text = f"{text}\n\n{closing}"
    return mess_up(text, rng, protected=[str(v) for v in params.values()])


def mess_up(text: str, rng: random.Random, protected: Iterable[str] = ()) -> str:
    """Typos in ordinary words (never in identifiers or parameter values) and the
    occasional all-lowercase rant."""
    keep = {word for v in protected for word in str(v).lower().split()}
    if rng.random() < 0.15:
        words = text.split(" ")
        candidates = [
            i
            for i, w in enumerate(words)
            if len(w) > 4 and w.isalpha() and w.islower() and w not in keep
        ]
        for i in rng.sample(candidates, k=min(len(candidates), rng.randint(1, 3))):
            w = words[i]
            j = rng.randint(1, len(w) - 2)
            words[i] = w[:j] + w[j + 1] + w[j] + w[j + 2 :]
        text = " ".join(words)
    if rng.random() < 0.06:
        text = text.lower()
    return text


def render_ask(reporter: str, label: str, rng: random.Random) -> str:
    return fmt(pick(rng, ASK_TEMPLATES), {"reporter": reporter, "label": label.lower()})


def render_reply(label: str, value: Any, rng: random.Random) -> str:
    return fmt(pick(rng, REPLY_TEMPLATES), {"label": label, "value": value})


def render_done(rt: RequestType, params: dict[str, Any], rng: random.Random) -> str:
    if rng.random() < 0.6:
        return pick(rng, DONE_COMMENTS)
    shown = [f for f in rt.field_phrases if f in params][:2]
    details = ", ".join(f"{pick(rng, rt.field_labels[f]).lower()} {params[f]}" for f in shown)
    return f"Done. Applied with {details}." if details else "Done."
