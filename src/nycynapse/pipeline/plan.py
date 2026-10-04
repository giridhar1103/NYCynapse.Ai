"""The planner: the model fills in a semantic query plan instead of writing SQL."""

from dataclasses import dataclass

from pydantic import ValidationError

from ..llm.client import LLMError, complete
from ..prompts import prompt
from .ir import Plan


@dataclass
class Planned:
    plan: Plan | None
    raw: dict | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    declined: str | None = None


def plan_schema() -> dict:
    schema = Plan.model_json_schema()
    schema["properties"]["model"]["description"] = "semantic model name, or none"
    schema["properties"]["reason"] = {
        "type": "string",
        "description": "when model is none: what the data lacks, in one sentence for the reader",
    }
    return schema


def make_plan(
    question: str,
    as_of: str,
    context: str,
    *,
    previous: dict | None = None,
    problem: str | None = None,
    role: str = "planner",
) -> Planned:
    parts = [f"The time now is {as_of} in New York.", context, f"Question: {question}"]
    if previous is not None:
        parts.append(f"This plan was tried:\n{previous}\n\nIt failed: {problem}\nFix the plan.")
    try:
        r = complete(role, prompt("plan"), "\n\n".join(parts), schema=plan_schema())
        raw = r.json()
    except (LLMError, ValueError) as e:
        return Planned(None, error=str(e)[:400])
    spent = {
        "tokens_in": r.tokens_in or 0,
        "tokens_out": r.tokens_out or 0,
        "cost_usd": r.cost_usd or 0.0,
    }
    reason = raw.pop("reason", None)
    if raw.get("model") in (None, "none"):
        # A deliberate "the data cannot answer this", not a broken plan: say so instead of
        # pushing for another attempt, which tends to produce a confident wrong answer.
        why = (reason or "").strip() or "the data available does not cover this question."
        return Planned(None, raw, error="planner found no model", declined=why, **spent)
    try:
        return Planned(Plan(**raw), raw, **spent)
    except ValidationError as e:
        return Planned(None, raw, error=f"invalid plan: {str(e)[:300]}", **spent)
