"""`replaybook knowledge eval`: how well does a past-ticket search find what engineers found?

An agent that searches old tickets is only as good as what the search hands back. This
scores a search on its own, before any agent is involved, against the team's own history:

- The ground truth for a ticket is the earlier tickets engineers tied it to: its links
  (duplicates, relates, fixes, in either direction) and the ticket ids mentioned in its
  comments. Only tickets created before it count, because nothing later existed then.
  Duplicates weigh twice as much as other links in nDCG.
- The query is the ticket as filed: its summary and description (cut at --query-chars),
  which is what an agent working the ticket would search with.
- Every search runs at the ticket's creation time, behind the time-travel guard. For a
  knowledge pack the guard also counts what the server returned that the agent must not see
  (the ticket itself, later tickets, undated results): its leaks.

Sources are `builtin` (Replaybook's own keyword search over the dataset, the baseline) and
any loaded knowledge pack by name. Several sources in one run are scored on the same cases
side by side. Links are never complete, so the absolute numbers are lower bounds; the
comparison between sources, or between two settings of one source, is what to read. So
every hit rate carries a 95% Wilson interval, and every source after the first is compared
with the first ticket by ticket: on how many tickets it ranked the first related ticket
higher, lower or the same, with an exact sign test on the difference.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from replaybook.eval.stats import wilson_interval
from replaybook.mock_env.guarded import GuardStats, same_id
from replaybook.schema.ticket import Ticket

BUILTIN = "builtin"
KS = (1, 3, 5, 10)
GRADE = {"duplicates": 2, "relates": 1, "fixes": 1, "mentioned": 1}
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*[A-Za-z0-9]")
RESULTS_FILE = "retrieval.json"
REPORT_FILE = "retrieval.md"


# --- ground truth ------------------------------------------------------------------------


def ground_truth(tickets: list[Ticket]) -> dict[str, dict[str, int]]:
    """ticket id -> {earlier related ticket id: grade}. Links count in both directions;
    ids mentioned in a ticket's comments count as related to it."""
    by_id = {t.id.casefold(): t for t in tickets}
    pairs: dict[tuple[str, str], int] = {}

    def relate(a: Ticket, b: Ticket, grade: int) -> None:
        if a.id == b.id:
            return
        later, earlier = (a, b) if (a.created, a.id) > (b.created, b.id) else (b, a)
        if earlier.created < later.created:
            key = (later.id, earlier.id)
            pairs[key] = max(pairs.get(key, 0), grade)

    for ticket in tickets:
        for link in ticket.links:
            other = by_id.get(link.target.casefold())
            if other is not None:
                relate(ticket, other, GRADE.get(link.type, 1))
        for comment in ticket.comments:
            for token in TOKEN.findall(comment.body):
                other = by_id.get(token.casefold())
                if other is not None:
                    relate(ticket, other, GRADE["mentioned"])
    truth: dict[str, dict[str, int]] = {}
    for (later, earlier), grade in pairs.items():
        truth.setdefault(later, {})[earlier] = grade
    return truth


def select_cases(
    tickets: list[Ticket],
    truth: dict[str, dict[str, int]],
    n: int,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[Ticket]:
    """The newest `n` tickets that have ground truth, within the dates given."""
    chosen = [
        t
        for t in tickets
        if truth.get(t.id)
        and (since is None or t.created >= since)
        and (until is None or t.created < until)
    ]
    chosen.sort(key=lambda t: (t.created, t.id), reverse=True)
    return chosen[: max(0, n)]


def query_text(ticket: Ticket, chars: int) -> str:
    text = f"{ticket.summary}\n\n{ticket.description}".strip()
    return text if len(text) <= chars else text[:chars]


# --- metrics -----------------------------------------------------------------------------


def _dedupe(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in ids:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def cutoffs(k_max: int) -> list[int]:
    """The k values scored: the usual ones up to k_max, and k_max itself."""
    return sorted({k for k in KS if k <= k_max} | {k_max})


def score_ranking(ranked: list[str], relevant: dict[str, int], k_max: int) -> dict[str, float]:
    ranked = _dedupe(ranked)[:k_max]
    grades = {key.casefold(): grade for key, grade in relevant.items()}
    hits = [grades.get(item.casefold(), 0) for item in ranked]
    scores: dict[str, float] = {}
    for k in cutoffs(k_max):
        found = sum(1 for h in hits[:k] if h)
        scores[f"hit@{k}"] = 1.0 if found else 0.0
        scores[f"recall@{k}"] = found / len(grades)
    first = next((i for i, h in enumerate(hits) if h), None)
    scores["mrr"] = 1.0 / (first + 1) if first is not None else 0.0
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(hits))
    ideal = sorted(grades.values(), reverse=True)[:k_max]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    scores[f"ndcg@{k_max}"] = dcg / idcg if idcg else 0.0
    duplicates = {key for key, grade in grades.items() if grade >= 2}
    if duplicates:
        scores[f"duplicate_found@{k_max}"] = (
            1.0 if any(item.casefold() in duplicates for item in ranked) else 0.0
        )
    return scores


# --- sources -----------------------------------------------------------------------------

Search = Callable[[Ticket, str, int], tuple[list[str], GuardStats | None]]


def builtin_search(corpus: Any) -> Search:
    """Replaybook's own keyword search over the dataset, behind its own guard."""
    from replaybook.mock_env import knowledge
    from replaybook.mock_env.recorder import CallRecorder
    from replaybook.mock_env.world import TicketContext, WorldState

    def search(ticket: Ticket, query: str, k: int) -> tuple[list[str], GuardStats | None]:
        world = WorldState(ticket=TicketContext.from_ticket(ticket))
        server = knowledge.build(world, CallRecorder(), corpus)
        found = server.tools[knowledge.SEARCH_TOOL](query=query, limit=k)
        return [str(r["id"]) for r in found.get("results", [])], None

    return search


def pack_search(pack: Any) -> Search:
    """A knowledge pack's search tool, through the time-travel guard."""
    from replaybook.mock_env.guarded import source_for

    source = source_for(pack)
    spec = source.spec
    if spec.search is None:
        raise ValueError(f"knowledge pack '{pack.name}' names no search tool (knowledge.search)")
    id_field = spec.tools[spec.search].id

    def search(ticket: Ticket, query: str, k: int) -> tuple[list[str], GuardStats | None]:
        items, stats = source.search(query, k, ticket.created, ticket.id)
        return [str(item.get(id_field)) for item in items], stats

    return search


@dataclass
class CaseScore:
    ticket_id: str
    created: str
    relevant: dict[str, int]
    returned: list[str]
    scores: dict[str, float] = field(default_factory=dict)
    guard: dict[str, int] | None = None
    seconds: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "created": self.created,
            "relevant": self.relevant,
            "returned": self.returned,
            "found": [r for r in self.returned if r.casefold() in _folded(self.relevant)],
            "scores": self.scores,
            "guard": self.guard,
            "seconds": self.seconds,
            "error": self.error,
        }


def _folded(relevant: dict[str, int]) -> set[str]:
    return {key.casefold() for key in relevant}


def run_source(
    search: Search,
    cases: list[Ticket],
    truth: dict[str, dict[str, int]],
    k: int,
    query_chars: int,
    workers: int = 4,
) -> list[CaseScore]:
    def one(ticket: Ticket) -> CaseScore:
        relevant = truth[ticket.id]
        started = time.perf_counter()
        try:
            returned, stats = search(ticket, query_text(ticket, query_chars), k)
        except Exception as exc:  # the source is someone else's; score it as an error
            return CaseScore(
                ticket.id,
                ticket.created.isoformat(),
                relevant,
                [],
                error=f"{type(exc).__name__}: {exc}"[:500],
                seconds=round(time.perf_counter() - started, 4),
            )
        returned = [r for r in _dedupe(returned) if not same_id(r, ticket.id)][:k]
        return CaseScore(
            ticket.id,
            ticket.created.isoformat(),
            relevant,
            returned,
            scores=score_ranking(returned, relevant, k),
            guard=stats.as_dict() if stats is not None else None,
            seconds=round(time.perf_counter() - started, 4),
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, cases))


def summarise(scores: list[CaseScore], k: int) -> dict[str, Any]:
    scored = [s for s in scores if s.error is None]
    metrics: dict[str, float] = {}
    intervals: dict[str, list[float]] = {}
    names = sorted({name for s in scored for name in s.scores})
    for name in names:
        values = [s.scores[name] for s in scored if name in s.scores]
        metrics[name] = round(sum(values) / len(values), 4) if values else 0.0
        if name.startswith(("hit@", "duplicate_found@")):
            low, high = wilson_interval(int(sum(values)), len(values))
            intervals[name] = [round(low, 4), round(high, 4)]
    guard = GuardStats()
    guarded = [s for s in scored if s.guard is not None]
    for s in guarded:
        stats = GuardStats(**{k_: v for k_, v in (s.guard or {}).items() if k_ != "leaks"})
        guard.add(stats)
    seconds = sorted(s.seconds for s in scored)
    return {
        "cases": len(scores),
        "errors": len(scores) - len(scored),
        "empty": sum(1 for s in scored if not s.returned),
        "metrics": metrics,
        "intervals": intervals,
        "duplicate_cases": sum(1 for s in scored if f"duplicate_found@{k}" in s.scores),
        "guard": guard.as_dict() if guarded else None,
        "cases_with_leaks": sum(1 for s in guarded if (s.guard or {}).get("leaks")),
        "seconds_mean": round(sum(seconds) / len(seconds), 4) if seconds else 0.0,
        "seconds_p95": seconds[min(len(seconds) - 1, int(0.95 * len(seconds)))] if seconds else 0.0,
        "first_errors": [s.error for s in scores if s.error][:3],
    }


def sign_test(better: int, worse: int) -> float:
    """Two-sided exact sign test: how likely a split at least this uneven is by chance."""
    n = better + worse
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(better, worse) + 1)) / 2**n
    return min(1.0, 2 * tail)


def compare(baseline: list[CaseScore], other: list[CaseScore]) -> dict[str, Any]:
    """Ticket by ticket: did `other` rank the first related ticket higher than the baseline
    (reciprocal rank within k), lower, or the same? Tickets where either errored are left out."""
    by_id = {s.ticket_id: s for s in baseline if s.error is None}
    better = worse = same = 0
    for score in other:
        base = by_id.get(score.ticket_id)
        if base is None or score.error is not None:
            continue
        mine, theirs = score.scores.get("mrr", 0.0), base.scores.get("mrr", 0.0)
        if mine > theirs:
            better += 1
        elif mine < theirs:
            worse += 1
        else:
            same += 1
    return {
        "cases": better + worse + same,
        "better": better,
        "worse": worse,
        "same": same,
        "sign_test_p": round(sign_test(better, worse), 4),
    }


# --- the report --------------------------------------------------------------------------


def _pct(value: float) -> str:
    return f"{value:.0%}"


def _with_ci(summary: dict[str, Any], name: str) -> str:
    value = summary["metrics"].get(name, 0.0)
    low, high = summary.get("intervals", {}).get(name, [value, value])
    return f"{value:.0%} ({low:.0%}-{high:.0%})"


def _comparison(result: dict[str, Any]) -> str:
    comparisons = result.get("comparison") or {}
    if not comparisons:
        return ""
    baseline = result["baseline"]
    lines = [
        f"## Ticket by ticket against {baseline}",
        "",
        f"For each ticket: did the source rank the first related ticket higher than {baseline} "
        "(within the top k), lower, or the same? The sign test says how likely a split at least "
        "this uneven is by chance; below 0.05 the difference is unlikely to be luck.",
        "",
        "| Source | Higher | Same | Lower | Sign test p |",
        "|---|---|---|---|---|",
    ]
    for name, c in comparisons.items():
        lines.append(
            f"| {name} | {c['better']} | {c['same']} | {c['worse']} | {c['sign_test_p']:.3g} |"
        )
    return "\n".join(lines) + "\n"


def render(result: dict[str, Any]) -> str:
    k = result["k"]
    sources = result["sources"]
    names = list(sources)
    rows: list[tuple[str, Callable[[dict[str, Any]], str]]] = [
        ("Cases scored", lambda s: f"{s['cases'] - s['errors']} of {s['cases']}"),
    ]
    for cut in cutoffs(k):
        rows.append((f"Hit@{cut} (95% CI)", lambda s, c=cut: _with_ci(s, f"hit@{c}")))
    rows += [
        (f"Recall@{k}", lambda s: _pct(s["metrics"].get(f"recall@{k}", 0))),
        ("MRR", lambda s: f"{s['metrics'].get('mrr', 0):.3f}"),
        (f"nDCG@{k}", lambda s: f"{s['metrics'].get(f'ndcg@{k}', 0):.3f}"),
        (
            f"Duplicate found@{k}",
            lambda s: (
                f"{_with_ci(s, f'duplicate_found@{k}')} of {s['duplicate_cases']}"
                if s["duplicate_cases"]
                else "-"
            ),
        ),
        ("No results", lambda s: str(s["empty"])),
        ("Seconds per search (mean / p95)", lambda s: f"{s['seconds_mean']} / {s['seconds_p95']}"),
    ]
    table = ["| | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for label, cell in rows:
        table.append(f"| {label} | " + " | ".join(cell(sources[n]) for n in names) + " |")
    leak_lines = []
    for name in names:
        guard = sources[name]["guard"]
        if guard is None:
            leak_lines.append(
                f"- **{name}**: Replaybook's own search, guarded at the source; nothing to count."
            )
            continue
        leak_lines.append(
            f"- **{name}**: the server returned {guard['returned_by_server']} results; the guard "
            f"removed {guard['leaks']} the agent must not see ({guard['the_ticket_itself']} the "
            f"ticket itself, {guard['after_horizon']} created later, {guard['undated']} "
            f"undated) in {sources[name]['cases_with_leaks']} of {sources[name]['cases']} "
            f"searches, and {guard['fields_removed']} fields that only exist after resolution "
            "or reveal the present."
        )
    errors = [
        f"- **{n}**: {sources[n]['errors']} searches failed, e.g. `{sources[n]['first_errors'][0]}`"
        for n in names
        if sources[n]["errors"]
    ]
    return f"""# Past-ticket search: {", ".join(names)}

Dataset `{result["data"]}`: {result["cases"]} tickets with known related earlier tickets
(the newest{" since " + result["since"] if result["since"] else ""}{
        " until " + result["until"] if result["until"] else ""
    }), {result["relevant_pairs"]} related pairs. Each ticket is searched with its summary and
description as filed (first {result["query_chars"]} characters), at its own creation time;
the top {k} results are scored. Written {result["created_at"]}.

{chr(10).join(table)}

**Hit@k**: share of tickets where at least one related ticket is in the top k, with its 95%
Wilson interval. **Recall@k**: share of the related tickets found in the top k. **MRR**:
1 / rank of the first related ticket. **nDCG**: ranking quality, duplicates weighted twice.

{_comparison(result)}
## Leaks the guard removed

{chr(10).join(leak_lines)}

A leak here never reaches an agent during replay; it says how far the source itself can be
trusted without the guard. A source with an "as of" filter should show none after the
horizon, but the ticket itself and the same-day results still come back if it filters by date.
{chr(10) + "## Errors" + chr(10) + chr(10) + chr(10).join(errors) + chr(10) if errors else ""}
## Reading this

- Links and mentions are never complete: an unlinked ticket that would have helped counts
  as a miss. The absolute numbers are lower bounds; compare sources, or one source before
  and after a change, on the same cases.
- The ground truth is what engineers recorded. A search that finds a better match than the
  linked one is not rewarded for it; read `cases_<source>.jsonl` for examples.
- Ranking may still be influenced by text written after the horizon (comments added to an
  old ticket later, embeddings computed today). The guard removes such tickets and fields
  from the results; it cannot change how the source ranked them.
"""


def evaluate(
    data_dir: Path,
    sources: list[str],
    out_dir: Path,
    n: int = 200,
    k: int = 10,
    since: datetime | None = None,
    until: datetime | None = None,
    query_chars: int = 1000,
    workers: int = 4,
) -> dict[str, Any]:
    from replaybook.adapters.synthetic import load_tickets
    from replaybook.mock_env.knowledge import Corpus
    from replaybook.packs import registry

    if not 1 <= k <= 25:
        raise ValueError("k must be between 1 and 25")
    tickets = load_tickets(Path(data_dir))
    truth = ground_truth(tickets)
    cases = select_cases(tickets, truth, n, since, until)
    if not cases:
        raise ValueError(
            f"no ticket in {data_dir} has a link to, or mentions, an earlier ticket in the "
            "dataset; import links (docs/ticket-sources.md) to give the search a ground truth"
        )
    searches: dict[str, Search] = {}
    for name in sources or [BUILTIN]:
        if name == BUILTIN:
            searches[name] = builtin_search(Corpus.load(Path(data_dir)))
            continue
        pack = registry().by_name.get(name)
        if pack is None or pack.knowledge is None:
            known = [p.name for p in registry().packs if p.knowledge is not None]
            raise ValueError(
                f"'{name}' is not a knowledge pack; choose builtin or one of: "
                f"{', '.join(known) or 'none loaded'}"
            )
        searches[name] = pack_search(pack)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "data": str(data_dir),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cases": len(cases),
        "relevant_pairs": sum(len(truth[c.id]) for c in cases),
        "k": k,
        "query_chars": query_chars,
        "since": since.date().isoformat() if since else None,
        "until": until.date().isoformat() if until else None,
        "sources": {},
    }
    scored: dict[str, list[CaseScore]] = {}
    for name, search in searches.items():
        scores = run_source(search, cases, truth, k, query_chars, workers)
        with (out_dir / f"cases_{name}.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for score in scores:
                fh.write(json.dumps(score.as_dict()) + "\n")
        result["sources"][name] = summarise(scores, k)
        scored[name] = scores
    names = list(scored)
    result["baseline"] = names[0]
    result["comparison"] = {name: compare(scored[names[0]], scored[name]) for name in names[1:]}
    (out_dir / RESULTS_FILE).write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out_dir / REPORT_FILE).write_text(render(result), encoding="utf-8")
    return result
