"""Decide whether a result answers the question the way the gold result does.

Two queries can be different and both right, so results are compared, not SQL. Column names
are ignored: each gold column is matched to a column of the candidate with the same values.
Extra candidate columns are fine. Rows are compared as a multiset unless the question asks
for an order, and numbers within a relative tolerance count as equal.
"""

import datetime as dt
import decimal
import math
from collections import Counter
from dataclasses import dataclass

from .cases import Compare


@dataclass
class Verdict:
    match: bool
    reason: str


def _norm(v, tol: float):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int | float | decimal.Decimal):
        f = float(v)
        if math.isnan(f):
            return None
        return ("num", f)
    if isinstance(v, dt.datetime) and v.tzinfo is None and v.time() == dt.time(0):
        return v.date().isoformat()  # date_trunc on a date gives a midnight timestamp
    if isinstance(v, dt.datetime):
        return (
            v.replace(tzinfo=None).isoformat()
            if v.tzinfo is None
            else v.astimezone(dt.UTC).isoformat()
        )
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, list | tuple):
        return tuple(_norm(x, tol) for x in v)
    return str(v).strip().casefold()


def _eq(a, b, tol: float) -> bool:
    if isinstance(a, tuple) and isinstance(b, tuple) and a[:1] == ("num",) == b[:1]:
        x, y = a[1], b[1]
        if abs(x - y) <= tol * max(abs(y), 1e-9) or abs(x - y) < 1e-9:
            return True
        # A share written as a percentage (92.5 for 0.925) is the same answer, either way round.
        if 0 < y < 1 and abs(x - y * 100) <= tol * y * 100 + 0.051:
            return True
        return 0 < x < 1 and abs(y - x * 100) <= tol * x * 100 + 0.051
    if isinstance(a, tuple) and a[:1] == ("num",) and isinstance(b, bool):
        return False
    return a == b


def _col(rows, i, tol):
    return [_norm(r[i], tol) for r in rows]


def _key(v):
    # Sorting mixed values: numbers by value, everything else by text.
    if isinstance(v, tuple) and v[:1] == ("num",):
        return (0, v[1], "")
    return (1, 0.0, repr(v))


def _same_multiset(a: list, b: list, tol: float) -> bool:
    if len(a) != len(b):
        return False
    return all(
        _eq(x, y, tol) for x, y in zip(sorted(a, key=_key), sorted(b, key=_key), strict=True)
    )


def _rows_match(gold_rows, cand_rows, mapping, tol, ordered) -> bool:
    g = [tuple(_norm(r[i], tol) for i in range(len(mapping))) for r in gold_rows]
    c = [tuple(_norm(r[j], tol) for j in mapping) for r in cand_rows]
    if len(g) != len(c):
        return False
    if ordered:
        return all(
            all(_eq(x, y, tol) for x, y in zip(a, b, strict=True))
            for a, b in zip(c, g, strict=True)
        )
    if tol == 0:
        return Counter(g) == Counter(c)
    remaining = list(c)
    for row in g:
        for k, cand in enumerate(remaining):
            if all(_eq(x, y, tol) for x, y in zip(cand, row, strict=True)):
                remaining.pop(k)
                break
        else:
            return False
    return True


def _without_zero_rows(rows):
    def zero(row):
        nums = [
            v
            for v in row
            if isinstance(v, int | float | decimal.Decimal) and not isinstance(v, bool)
        ]
        return bool(nums) and all(float(v) == 0 for v in nums)

    return [r for r in rows if not zero(r)]


def compare(gold_cols, gold_rows, cand_cols, cand_rows, spec: Compare) -> Verdict:
    if isinstance(spec.columns, list):
        keep = [i for i in spec.columns if i < len(gold_cols)]
        gold_cols = [gold_cols[i] for i in keep]
        gold_rows = [tuple(r[i] for i in keep) for r in gold_rows]
        spec = spec.model_copy(update={"columns": "all"})
    if spec.rows is not None:
        gold_rows, cand_rows = gold_rows[: spec.rows], cand_rows[: spec.rows]
    if len(gold_rows) != len(cand_rows):
        # A group listed with zero is the same answer as a group left out: Staten Island with 0
        # Citi Bike rides, or no row for it at all.
        gold_rows, cand_rows = _without_zero_rows(gold_rows), _without_zero_rows(cand_rows)
    if not gold_rows and not cand_rows:
        return Verdict(True, "both empty")
    if len(gold_rows) != len(cand_rows):
        return Verdict(False, f"{len(cand_rows)} rows, expected {len(gold_rows)}")
    tol = spec.tolerance
    n_gold = len(gold_cols) if spec.columns == "all" else 1
    if len(cand_cols) < n_gold:
        return Verdict(False, f"{len(cand_cols)} columns, expected at least {n_gold}")

    # Candidate columns that could stand for each gold column, by their values alone.
    options = []
    for i in range(n_gold):
        g = _col(gold_rows, i, tol)
        if spec.order_matters:
            hits = [
                j
                for j in range(len(cand_cols))
                if all(_eq(x, y, tol) for x, y in zip(_col(cand_rows, j, tol), g, strict=True))
            ]
        else:
            hits = [
                j for j in range(len(cand_cols)) if _same_multiset(_col(cand_rows, j, tol), g, tol)
            ]
        if not hits:
            return Verdict(False, f"no column matches gold column {gold_cols[i]!r}")
        options.append(hits)

    # Pick distinct columns for each gold column and check whole rows line up.
    tried = 0
    for mapping in _assignments(options):
        tried += 1
        if _rows_match(gold_rows, cand_rows, mapping, tol, spec.order_matters):
            return Verdict(True, "match")
        if tried > 500:
            break
    return Verdict(False, "columns match one by one but rows do not line up")


def _assignments(options: list[list[int]]):
    if not options:
        yield []
        return
    first, rest = options[0], options[1:]
    for j in first:
        for tail in _assignments(rest):
            if j not in tail:
                yield [j, *tail]
