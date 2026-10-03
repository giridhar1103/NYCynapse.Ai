"""Run a system over evaluation cases and score it.

Every run happens against one pinned DuckLake snapshot, so gold and system SQL see the same
data however much has been ingested since. Results are written per case, with the summary
broken down by category so a change that helps one kind of question and hurts another shows.
"""

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb

from .. import guard
from .cases import Case
from .compare import compare
from .execute import Execution, run
from .system import Answer


def split_of(case: Case) -> str:
    """dev, regression or holdout. Fixed by paraphrase group, so a group never spans splits."""
    key = (case.group or case.id).encode()
    bucket = int(hashlib.sha256(key).hexdigest(), 16) % 10
    return "dev" if bucket < 5 else "regression" if bucket < 7 else "holdout"


@dataclass
class CaseResult:
    id: str
    category: str
    group: str | None
    split: str
    question: str
    expected: str
    predicted: str
    classification_ok: bool
    sql: str | None = None
    guard_ok: bool | None = None
    guard_reason: str = ""
    executed: bool = False
    exec_error: str | None = None
    result_match: bool | None = None
    match_reason: str = ""
    tables: list[str] = field(default_factory=list)
    table_recall: float | None = None
    workspace_recall: float | None = None
    metric_recall: float | None = None
    value_recall: float | None = None
    latency_ms: int = 0
    exec_ms: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None

    @property
    def correct(self) -> bool:
        if self.expected == "answerable":
            return bool(self.classification_ok and self.result_match)
        return self.classification_ok


def _recall(expected: list[str], got: list[str]) -> float | None:
    if not expected:
        return None
    got_l = {g.lower() for g in got}
    return sum(e.lower() in got_l for e in expected) / len(expected)


def score(
    case: Case, answer: Answer, gold: Execution | None, con: duckdb.DuckDBPyConnection
) -> CaseResult:
    r = CaseResult(
        id=case.id,
        category=case.category,
        group=case.group,
        split=split_of(case),
        question=case.question,
        expected=case.expect.classification,
        predicted=answer.classification,
        classification_ok=answer.classification == case.expect.classification,
        sql=answer.sql,
        latency_ms=answer.latency_ms,
        cost_usd=answer.cost_usd,
        tokens_in=answer.tokens_in,
        tokens_out=answer.tokens_out,
        error=answer.error,
        workspace_recall=_recall(case.expect.workspaces, answer.workspaces),
        metric_recall=_recall(case.expect.metrics, answer.metrics),
        value_recall=_recall(case.expect.values, answer.values),
    )
    if answer.sql:
        g = guard.check(answer.sql)
        r.guard_ok, r.guard_reason, r.tables = g.ok, g.reason, g.tables
        r.table_recall = _recall(case.expect.tables, g.tables)
        if g.ok:
            ex = run(con, answer.sql, timeout_s=120)
            r.executed, r.exec_error, r.exec_ms = ex.ok, ex.error, ex.ms
            if ex.ok and gold is not None and gold.ok:
                v = compare(gold.columns, gold.rows, ex.columns, ex.rows, case.compare)
                r.result_match, r.match_reason = v.match, v.reason
            elif ex.ok and case.gold_sql:
                r.match_reason = "gold failed"
            elif ex.ok:
                r.match_reason = "answered a question that should not be answered"
    if case.expect.classification == "answerable" and r.result_match is None:
        r.result_match = False
    return r


def summarize(results: list[CaseResult]) -> dict:
    def rate(xs):
        xs = list(xs)
        return round(sum(xs) / len(xs), 3) if xs else None

    answerable = [r for r in results if r.expected == "answerable"]
    others = [r for r in results if r.expected != "answerable"]
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r.category].append(r)
    groups = defaultdict(list)
    for r in results:
        if r.group:
            groups[r.group].append(r)
    consistent = [len({x.correct for x in g}) == 1 for g in groups.values() if len(g) > 1]
    unsafe_ran = [r for r in results if r.sql and r.guard_ok is False]
    return {
        "cases": len(results),
        "overall_correct": rate(r.correct for r in results),
        "execution_accuracy": rate(bool(r.result_match) for r in answerable),
        "classification_accuracy": rate(r.classification_ok for r in results),
        "abstention_recall": rate(r.classification_ok for r in others),
        "false_abstention_rate": rate(r.predicted != "answerable" for r in answerable),
        "sql_rejected_by_guard": len(unsafe_ran),
        "executed_rate": rate(r.executed for r in answerable),
        "paraphrase_consistency": rate(consistent),
        "table_recall": rate(r.table_recall for r in results if r.table_recall is not None),
        "by_category": {
            k: {"n": len(v), "correct": rate(x.correct for x in v)}
            for k, v in sorted(by_cat.items())
        },
        "latency_ms_p50": _pct([r.latency_ms for r in results], 50),
        "latency_ms_p95": _pct([r.latency_ms for r in results], 95),
        "cost_usd": round(sum(r.cost_usd for r in results), 4),
        "tokens_in": sum(r.tokens_in for r in results),
    }


def _pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))] if xs else None


def evaluate(
    system,
    cases: list[Case],
    con: duckdb.DuckDBPyConnection,
    out: Path,
    *,
    meta: dict,
    progress=print,
) -> dict:
    gold_cache: dict[str, Execution] = {}
    results: list[CaseResult] = []
    for i, case in enumerate(cases, 1):
        if case.gold_sql and case.id not in gold_cache:
            gold_cache[case.id] = run(con, case.gold_sql, timeout_s=300)
        answer = system.answer(case.question, case.as_of)
        res = score(case, answer, gold_cache.get(case.id), con)
        results.append(res)
        progress(
            f"[{i}/{len(cases)}] {case.id:8} {'ok ' if res.correct else 'MISS'} "
            f"{res.predicted:11} {res.match_reason or res.guard_reason or res.error or ''}"
        )
    summary = summarize(results)
    report = {
        "meta": {**meta, "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
        "summary": summary,
        "results": [asdict(r) for r in results],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str))
    return summary
