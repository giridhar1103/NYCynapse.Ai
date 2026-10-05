"""Checking the grader: a panel reviews the cases where result comparison is least reliable.

Comparing results can fail a right answer that is shaped differently and pass a wrong one that
matches by coincidence. For a finished run, three judges from three vendors look at every
answerable case whose result did not match, and every match that rests on a single value, and
say whether the answer is equivalent to the reference. Their verdicts are stored beside the run
and reported as the grader's error rates. Scores are never changed by the panel.
"""

from concurrent.futures import ThreadPoolExecutor

from ..llm.client import LLMError, ProviderLimit, complete_with
from ..prompts import prompt
from .cases import Case
from .execute import run

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["equivalent", "not_equivalent", "both_reasonable"],
        },
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
    "additionalProperties": False,
}


def needs_review(r: dict) -> bool:
    if r["expected"] != "answerable" or not r.get("executed") or not r.get("sql"):
        return False
    if not r.get("result_match"):
        return True
    return (r.get("gold_rows") or 0) <= 1  # a match on a single row can be a coincidence


def _preview(con, sql: str) -> str:
    ex = run(con, sql, timeout_s=300, max_rows=11)
    if not ex.ok:
        return f"error: {ex.error}"
    rows = "\n".join(str(tuple(str(v) for v in row)) for row in ex.rows[:10])
    more = " (more rows follow)" if len(ex.rows) > 10 else ""
    return f"columns {ex.columns}{more}:\n{rows}"


def review(case: Case, result: dict, con, judges: list[str], conventions: str) -> dict:
    text = "\n\n".join(
        [
            f"Question: {case.question}",
            f"Asked at: {case.as_of} (New York)",
            f"Reference SQL:\n{case.gold_sql.strip()}",
            f"Reference result: {_preview(con, case.gold_sql)}",
            f"System SQL:\n{result['sql'].strip()}",
            f"System result: {_preview(con, result['sql'])}",
            f"Automatic comparison said: {result.get('match_reason')}",
            "Conventions:\n" + conventions,
        ]
    )

    def one(judge):
        try:
            r = complete_with(judge, prompt("judge"), text, schema=SCHEMA, retries=1)
            return {"judge": judge, **r.json()}
        except ProviderLimit:
            raise
        except (LLMError, ValueError) as e:
            return {"judge": judge, "verdict": "error", "reason": str(e)[:300]}

    with ThreadPoolExecutor(len(judges)) as pool:
        verdicts = list(pool.map(one, judges))
    counted = [v["verdict"] for v in verdicts if v["verdict"] != "error"]
    majority = max(set(counted), key=counted.count) if counted else None
    if majority and counted.count(majority) * 2 <= len(counted):
        majority = "split"
    return {
        "id": case.id,
        "comparison": result.get("result_match"),
        "panel": majority,
        "verdicts": verdicts,
    }


def grader_error_rates(reviews: list[dict]) -> dict:
    """How often the panel disagreed with the automatic comparison."""
    failed = [r for r in reviews if r["comparison"] is False and r["panel"] not in (None, "split")]
    passed = [r for r in reviews if r["comparison"] is True and r["panel"] not in (None, "split")]
    return {
        "mismatches_reviewed": len(failed),
        "mismatches_panel_says_right": sum(r["panel"] == "equivalent" for r in failed),
        "mismatches_panel_says_either": sum(r["panel"] == "both_reasonable" for r in failed),
        "single_value_matches_reviewed": len(passed),
        "single_value_matches_panel_says_wrong": sum(
            r["panel"] == "not_equivalent" for r in passed
        ),
        "split_or_unjudged": sum(r["panel"] in (None, "split") for r in reviews),
    }
