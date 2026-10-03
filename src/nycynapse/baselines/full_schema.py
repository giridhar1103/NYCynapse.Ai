"""E0: the naive baseline. Every gold table and column goes into one prompt.

This is what QueryGPT set out to replace: no routing, no retrieval, no grounding, no plan.
The model sees the whole schema with its documentation and writes SQL in one shot. Its
scores are the floor every later stage has to beat.
"""

import time

from ..evals.system import Answer
from ..llm.client import LLMError, complete

SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["answerable", "unclear", "unsupported", "non_data", "refuse"],
        },
        "sql": {"type": ["string", "null"]},
        "explanation": {"type": "string"},
    },
    "required": ["classification", "sql", "explanation"],
    "additionalProperties": False,
}

SYSTEM = """You answer questions about New York City data by writing one DuckDB SELECT query
over the tables below. Reply with JSON only.

classification:
- answerable: the tables can answer it; write the SQL
- unclear: the question is too vague to answer without asking what the person means
- unsupported: the data needed is not in these tables or not in the time range they cover
- non_data: not a question about this data
- refuse: it asks to change data, read files, change settings or anything other than a SELECT

Write SQL only for answerable questions. Tables are in schema gold, for example gold.dim_date.
Times: columns ending in _at are instants. Local New York date and hour columns are provided.
"""


def schema_text(manifest: dict) -> str:
    out = []
    for name, t in sorted(manifest["tables"].items()):
        if name.startswith("_"):
            continue
        out.append(f"TABLE gold.{name}: {t['description']}")
        for col, c in t["columns"].items():
            desc = f" -- {c['description']}" if c["description"] else ""
            out.append(f"  {col} {c['type']}{desc}")
        out.append("")
    return "\n".join(out)


class FullSchemaBaseline:
    name = "E0 full schema"

    def __init__(self, manifest: dict, role: str = "sql"):
        self.schema = schema_text(manifest)
        self.role = role

    def answer(self, question: str, as_of: str) -> Answer:
        prompt = f"The time now is {as_of} (New York).\n\n{self.schema}\n\nQuestion: {question}"
        t0 = time.monotonic()
        try:
            r = complete(self.role, SYSTEM, prompt, schema=SCHEMA)
            d = r.json()
        except (LLMError, ValueError) as e:
            return Answer(
                "error", error=str(e)[:500], latency_ms=int((time.monotonic() - t0) * 1000)
            )
        return Answer(
            classification=d.get("classification", "answerable"),
            sql=d.get("sql"),
            explanation=d.get("explanation", ""),
            model_calls=1,
            tokens_in=r.tokens_in or 0,
            tokens_out=r.tokens_out or 0,
            cost_usd=r.cost_usd or 0.0,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
