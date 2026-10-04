"""Turn a result into a short answer, and check the answer only states numbers that are in it."""

import re
from dataclasses import dataclass, field

from ..llm.client import LLMError, complete
from ..prompts import prompt

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "chart": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["bar", "line", "table"]},
                        "x": {"type": "string"},
                        "y": {"type": "array", "items": {"type": "string"}},
                        "title": {"type": "string"},
                    },
                    "required": ["type", "x", "y", "title"],
                },
            ]
        },
        "caveats": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "chart", "caveats"],
}


@dataclass
class Written:
    answer: str
    chart: dict | None = None
    caveats: list[str] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None


def write_answer(
    question: str,
    window_label: str | None,
    columns: list[str],
    rows: list,
    warnings: list[str],
    role: str = "answer",
) -> Written:
    table = "\n".join([" | ".join(columns)] + [" | ".join(str(v) for v in r) for r in rows[:40]])
    more = f"\n({len(rows) - 40} more rows)" if len(rows) > 40 else ""
    msg = (
        f"Question: {question}\nPeriod: {window_label or 'as asked'}\n\nResult:\n{table}{more}"
        f"\n\nWarnings: {'; '.join(warnings) or 'none'}"
    )
    try:
        r = complete(role, prompt("answer"), msg, schema=SCHEMA)
        d = r.json()
    except (LLMError, ValueError) as e:
        return Written("", error=str(e)[:300])
    w = Written(
        d.get("answer", ""),
        d.get("chart"),
        d.get("caveats", []),
        cost_usd=r.cost_usd or 0.0,
        tokens_in=r.tokens_in or 0,
        tokens_out=r.tokens_out or 0,
    )
    w.unsupported_numbers = unsupported_numbers(w.answer, rows, window_label or "")
    return w


# Commas only count as thousands separators, so "in September 2026, the A" reads as 2026.
NUMBER = re.compile(r"(?<![\w.])-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def unsupported_numbers(text: str, rows: list, context: str = "") -> list[str]:
    """Numbers in the answer that cannot be traced to the result, allowing for rounding,
    percentages and thousands. Years and day numbers that appear in the period are fine."""
    values = []
    for r in rows:
        for v in r:
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, int | float):
                values.append(float(v))
            else:
                values += [float(x.replace(",", "")) for x in NUMBER.findall(str(v))]
    allowed_text = set(NUMBER.findall(context))
    bad = []
    for raw in NUMBER.findall(text):
        if raw in allowed_text:
            continue
        x = float(raw.replace(",", ""))
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10 ** (-decimals) if decimals else 0.5
        if not any(_close(x, v, tol) for v in values):
            bad.append(raw)
    return bad


def _close(x: float, v: float, tol: float) -> bool:
    for scale in (1, 100, 1 / 1000, 1 / 1_000_000):
        if abs(x - v * scale) <= tol * max(1, abs(v * scale)) * 0.01 + tol:
            return True
    return False
