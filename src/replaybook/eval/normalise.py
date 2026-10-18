"""Per-tool normalisation: which arguments matter, case, ordering, equivalent values.

The rules come from the packs' rules.yaml. Two writes are the same action when, after
normalisation, they have the same tool and the same arguments that matter. Tools in the
same family do the same job in different ways (a legacy and a current tool), which is
how a "wrong tool" is told apart from a completely different action. A tool without a
rule is compared on every argument it was given.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from replaybook.packs import PackRegistry, ToolRule, registry

__all__ = ["EQUIVALENTS", "NormalisedWrite", "ToolRule", "normalise", "same_action"]


def _duration_ms(value: Any) -> Any:
    """Accept '7d', '24h', '30m' or a number of milliseconds."""
    if isinstance(value, str):
        text = value.strip().lower()
        for suffix, factor in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000)):
            if text.endswith(suffix) and text[:-1].isdigit():
                return int(text[:-1]) * factor
    return value


# Named value normalisers a rules.yaml can refer to under `equivalents`.
EQUIVALENTS: dict[str, Callable[[Any], Any]] = {"duration_ms": _duration_ms}


@dataclass(frozen=True)
class NormalisedWrite:
    tool: str
    family: str
    target: tuple[Any, ...]
    args: dict[str, Any]
    attempted_only: bool = False

    @property
    def target_text(self) -> str:
        return " ".join(str(v) for v in self.target) or "-"

    def args_json(self) -> str:
        return json.dumps(self.args, sort_keys=True, default=str)


def canonical_value(value: Any, unordered: bool = False, keep_case: bool = False) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in ("true", "false"):
            return text.lower() == "true"
        if text.isdigit():
            return int(text)
        return text if keep_case else text.lower()
    if isinstance(value, list):
        items = [canonical_value(v, keep_case=keep_case) for v in value]
        if unordered or all(isinstance(i, str) for i in items):
            items = sorted(set(items), key=str) if unordered else items
        return [i.upper() if isinstance(i, str) and i.isalpha() and unordered else i for i in items]
    if isinstance(value, dict):
        return {
            str(k).lower(): canonical_value(v, unordered=True, keep_case=keep_case)
            for k, v in sorted(value.items(), key=lambda kv: str(kv[0]).lower())
        }
    return value


def normalise(
    tool: str,
    args: dict[str, Any],
    attempted_only: bool = False,
    packs: PackRegistry | None = None,
) -> NormalisedWrite:
    rules = (packs if packs is not None else registry()).rules
    rule = rules.get(tool)
    if rule is None:
        clean = {k: canonical_value(v) for k, v in sorted(args.items()) if v is not None}
        return NormalisedWrite(tool, tool, tuple(), clean, attempted_only)
    merged = {**rule.defaults, **{k: v for k, v in args.items() if v is not None}}
    clean: dict[str, Any] = {}
    for key in rule.matters:
        if key not in merged:
            continue
        value = merged[key]
        normaliser = EQUIVALENTS.get(rule.equivalents.get(key, ""))
        if normaliser is not None:
            value = normaliser(value)
        clean[key] = canonical_value(
            value, unordered=key in rule.unordered, keep_case=key in rule.keep_case
        )
    target = tuple(clean.get(key) for key in rule.target)
    return NormalisedWrite(tool, rule.family, target, clean, attempted_only)


def same_action(a: NormalisedWrite, b: NormalisedWrite) -> bool:
    return a.tool == b.tool and a.args == b.args


def differing_args(a: NormalisedWrite, b: NormalisedWrite) -> list[str]:
    keys = set(a.args) | set(b.args)
    return sorted(k for k in keys if a.args.get(k) != b.args.get(k))
