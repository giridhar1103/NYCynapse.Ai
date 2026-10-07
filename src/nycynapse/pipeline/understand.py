"""First stage: what kind of question this is, which domains it touches, what time it means
and which words in it need to be matched to values in the data."""

from dataclasses import dataclass, field

from ..llm.client import LLMError, complete
from ..prompts import prompt
from ..semantic.schema import Catalog
from .timeparse import TimeSpec

SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["answerable", "unclear", "unsupported", "non_data", "refuse"],
        },
        "reason": {"type": "string"},
        "clarification": {"type": ["string", "null"]},
        "workspaces": {"type": "array", "items": {"type": "string"}},
        "time": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": [
                                "all",
                                "now",
                                "date",
                                "month",
                                "year",
                                "between",
                                "since",
                                "relative",
                                "last_n",
                            ],
                        },
                        "date": {"type": ["string", "null"]},
                        "start": {"type": ["string", "null"]},
                        "end": {"type": ["string", "null"]},
                        "year": {"type": ["integer", "null"]},
                        "month": {"type": ["integer", "null"]},
                        "unit": {
                            "type": ["string", "null"],
                            "enum": ["hour", "day", "week", "month", "quarter", "year", None],
                        },
                        "offset": {"type": ["integer", "null"]},
                        "n": {"type": ["integer", "null"]},
                    },
                    "required": ["kind"],
                },
            ]
        },
        "day_part": {"type": ["string", "null"]},
        "mentions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "classification",
        "reason",
        "clarification",
        "workspaces",
        "time",
        "day_part",
        "mentions",
    ],
}

SYSTEM = prompt("understand")


@dataclass
class Understanding:
    classification: str
    reason: str = ""
    clarification: str | None = None
    workspaces: list[str] = field(default_factory=list)
    time: TimeSpec | None = None
    day_part: str | None = None
    mentions: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None


def workspace_brief(catalog: Catalog, coverage: dict[str, str]) -> str:
    lines = []
    for w in catalog.workspaces:
        if w.id in ("geography", "calendar"):
            continue
        cov = coverage.get(w.id)
        lines.append(
            f"- {w.id}: {' '.join(w.description.split())}" + (f" Coverage: {cov}." if cov else "")
        )
    periods = ", ".join(f"{p.name} ({p.description.rstrip('.')})" for p in catalog.periods)
    return "Workspaces:\n" + "\n".join(lines) + f"\n\nNamed periods: {periods}"


def understand(question: str, as_of: str, brief: str, role: str = "router") -> Understanding:
    prompt = f"The time now is {as_of} in New York.\n\n{brief}\n\nQuestion: {question}"
    try:
        r = complete(role, SYSTEM, prompt, schema=SCHEMA)
        d = r.json()
    except (LLMError, ValueError) as e:
        return Understanding("error", error=str(e)[:400])
    time = None
    if d.get("time"):
        try:
            time = TimeSpec(**{k: v for k, v in d["time"].items() if v is not None})
        except ValueError:
            time = None
    return Understanding(
        classification=d.get("classification", "answerable"),
        reason=d.get("reason") or "",
        clarification=d.get("clarification"),
        workspaces=[w for w in d.get("workspaces") or []],
        time=time,
        day_part=d.get("day_part"),
        mentions=d.get("mentions") or [],
        tokens_in=r.tokens_in or 0,
        tokens_out=r.tokens_out or 0,
        cost_usd=r.cost_usd or 0.0,
    )
