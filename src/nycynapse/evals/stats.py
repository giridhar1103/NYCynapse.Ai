"""Statistics for evaluation results.

Small evaluation sets give wide margins, so every headline number comes with a confidence
interval, and differences between two systems are tested on the same questions:

* Confidence intervals come from a cluster bootstrap. Paraphrases of one question are not
  independent, so a resample draws whole paraphrase groups, not single cases.
* Two systems are compared case by case: the interval is on the per-case difference, and an
  exact McNemar test uses the cases where exactly one of them is right.
* Repeated runs give the spread from run to run, and the share of cases a system gets right on
  every run, which says more about reliability than the average.
* Declines are scored on two axes: answering a question that should be declined, and giving a
  wrong answer to one that should be answered.
"""

import math
import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

B = 10_000
SEED = 20261005


@dataclass
class Estimate:
    value: float | None
    low: float | None
    high: float | None
    n: int

    def as_dict(self) -> dict:
        return {"value": self.value, "low": self.low, "high": self.high, "n": self.n}


def is_correct(r: dict) -> bool:
    if r["expected"] == "answerable":
        return bool(r["classification_ok"] and r["result_match"])
    return bool(r["classification_ok"])


def cluster_of(r: dict) -> str:
    return r.get("group") or r["id"]


def _clusters(rows: Sequence[dict]) -> list[list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[cluster_of(r)].append(r)
    return [by[k] for k in sorted(by)]


def bootstrap(
    rows: Sequence[dict],
    stat: Callable[[Sequence[dict]], float | None],
    b: int = B,
    seed: int = SEED,
    level: float = 0.95,
) -> Estimate:
    """Point estimate and percentile interval, resampling whole clusters."""
    value = stat(rows) if rows else None
    clusters = _clusters(rows)
    if value is None or len(clusters) < 2:
        return Estimate(value, None, None, len(rows))
    rng = random.Random(seed)
    k = len(clusters)
    draws = []
    for _ in range(b):
        sample = [r for _ in range(k) for r in clusters[rng.randrange(k)]]
        v = stat(sample)
        if v is not None:
            draws.append(v)
    draws.sort()
    lo = draws[int((1 - level) / 2 * len(draws))]
    hi = draws[min(len(draws) - 1, int((1 + level) / 2 * len(draws)))]
    return Estimate(value, lo, hi, len(rows))


def rate(rows: Sequence[dict], pick: Callable[[dict], bool], where=lambda r: True):
    xs = [pick(r) for r in rows if where(r)]
    return sum(xs) / len(xs) if xs else None


def accuracy(rows: Sequence[dict]) -> float | None:
    return rate(rows, is_correct)


# Two axes for declining.
def answered_unanswerable(rows: Sequence[dict]) -> float | None:
    """Of questions that should be declined or clarified, the share it answered anyway."""
    return rate(
        rows, lambda r: r["predicted"] == "answerable", lambda r: r["expected"] != "answerable"
    )


def wrong_when_answered(rows: Sequence[dict]) -> float | None:
    """Of answerable questions it chose to answer, the share answered wrongly."""
    return rate(
        rows,
        lambda r: not r["result_match"],
        lambda r: r["expected"] == "answerable" and r["predicted"] == "answerable",
    )


def declined_answerable(rows: Sequence[dict]) -> float | None:
    """Of answerable questions, the share it declined or asked about instead."""
    return rate(
        rows, lambda r: r["predicted"] != "answerable", lambda r: r["expected"] == "answerable"
    )


def mcnemar_exact(a_only: int, b_only: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts."""
    n = a_only + b_only
    if n == 0:
        return 1.0
    k = min(a_only, b_only)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired(a: Sequence[dict], b: Sequence[dict], seed: int = SEED) -> dict:
    """Compare two runs on the cases they share: B minus A."""
    a_by = {r["id"]: r for r in a}
    rows = []
    for r in b:
        if r["id"] in a_by:
            rows.append({**r, "_a": is_correct(a_by[r["id"]]), "_b": is_correct(r)})
    if not rows:
        return {"n": 0}

    def diff(sample):
        return sum(x["_b"] - x["_a"] for x in sample) / len(sample)

    est = bootstrap(rows, diff, seed=seed)
    a_only = sum(x["_a"] and not x["_b"] for x in rows)
    b_only = sum(x["_b"] and not x["_a"] for x in rows)
    return {
        "n": len(rows),
        "difference": est.as_dict(),
        "only_a_right": a_only,
        "only_b_right": b_only,
        "p_value": mcnemar_exact(a_only, b_only),
    }


def repeats(runs: Sequence[Sequence[dict]]) -> dict:
    """Spread across repeated runs of the same system on the same cases."""
    accs = [accuracy(r) for r in runs if r]
    by_case: dict[str, list[bool]] = defaultdict(list)
    for run in runs:
        for r in run:
            by_case[r["id"]].append(is_correct(r))
    full = [v for v in by_case.values() if len(v) == len(runs)]
    mean = sum(accs) / len(accs) if accs else None
    sd = (
        math.sqrt(sum((x - mean) ** 2 for x in accs) / (len(accs) - 1)) if len(accs) > 1 else None
    )
    return {
        "runs": len(runs),
        "accuracy_mean": mean,
        "accuracy_sd": sd,
        "right_every_time": sum(all(v) for v in full) / len(full) if full else None,
        "right_at_least_once": sum(any(v) for v in full) / len(full) if full else None,
        "flipped": sum(any(v) and not all(v) for v in full),
    }
