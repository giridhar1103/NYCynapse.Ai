"""Write SQL from the pruned context, and repair it when it fails."""

from dataclasses import dataclass

from ..llm.client import LLMError, complete
from ..prompts import prompt

SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": ["string", "null"]},
        "explanation": {"type": "string"},
    },
    "required": ["sql", "explanation"],
}


@dataclass
class Generated:
    sql: str | None
    explanation: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None


def generate(
    question: str,
    as_of: str,
    context: str,
    *,
    previous: str | None = None,
    problem: str | None = None,
    role: str = "sql",
) -> Generated:
    parts = [f"The time now is {as_of} in New York.", context, f"Question: {question}"]
    if previous:
        parts.append(
            f"This query was tried:\n{previous}\n\nIt failed: {problem}\nWrite a corrected query."
        )
    try:
        r = complete(role, prompt("generate"), "\n\n".join(parts), schema=SCHEMA)
        d = r.json()
    except (LLMError, ValueError) as e:
        return Generated(None, error=str(e)[:400])
    return Generated(
        d.get("sql"),
        d.get("explanation", ""),
        r.tokens_in or 0,
        r.tokens_out or 0,
        r.cost_usd or 0.0,
    )
