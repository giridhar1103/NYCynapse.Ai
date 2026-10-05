"""Gold audit: the strongest model of each vendor reviews every reference answer.

Each auditor sees the question, when it was asked, the expected response, the reference SQL, a
preview of its result, documentation for the tables it reads, the governed metrics and domain
rules, what the data covers, and the written conventions. Opinions are stored per case and
reviewed by hand; nothing is changed automatically.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import guard
from ..llm.client import LLMError, ProviderLimit, complete_with
from ..prompts import prompt
from .cases import Case
from .execute import run

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["correct", "wrong", "ambiguous"]},
        "error_types": {
            "type": "array",
            "items": {"type": "string", "enum": ["E1", "E2", "E3", "E4"]},
        },
        "problem": {"type": "string"},
        "fixed_sql": {"type": ["string", "null"]},
        "reworded_question": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
    },
    "required": [
        "verdict",
        "error_types",
        "problem",
        "fixed_sql",
        "reworded_question",
        "confidence",
    ],
    "additionalProperties": False,
}
PREVIEW_ROWS = 15


def table_docs(manifest: dict, tables: list[str]) -> str:
    out = []
    for t in tables:
        name = t.removeprefix("gold.")
        doc = manifest["tables"].get(name)
        if not doc:
            continue
        cols = "\n".join(
            f"  {c} ({d.get('type', '')}): {d.get('description', '')}"
            for c, d in doc["columns"].items()
        )
        out.append(f"TABLE gold.{name}: {doc.get('description', '')}\n{cols}")
    return "\n\n".join(out)


def case_prompt(case: Case, context: dict, con) -> tuple[str, dict]:
    parts = [
        f"Question: {case.question}",
        f"Asked at: {case.as_of} (New York)",
        f"Expected response: {case.expect.classification}",
        f"Notes from the author: {case.notes}" if case.notes else "",
        f"Category: {case.category}",
    ]
    facts: dict = {}
    if case.gold_sql:
        g = guard.check(case.gold_sql)
        r = run(con, case.gold_sql, timeout_s=300, max_rows=PREVIEW_ROWS + 1)
        facts = {
            "columns": r.columns,
            "rows": len(r.rows),
            "error": r.error,
            "preview": [[str(v) for v in row] for row in r.rows[:PREVIEW_ROWS]],
        }
        parts += [
            f"Reference SQL:\n{case.gold_sql.strip()}",
            "Reference result: "
            + (
                f"error {r.error}"
                if not r.ok
                else f"columns {r.columns}, "
                f"{'more than ' if len(r.rows) > PREVIEW_ROWS else ''}"
                f"{min(len(r.rows), PREVIEW_ROWS)} rows:\n"
                + "\n".join(str(row) for row in facts["preview"])
            ),
            f"Comparison rules for this case: {case.compare.model_dump()}",
            (
                "Other accepted readings:\n" + "\n---\n".join(s.strip() for s in case.alt_gold_sql)
                if case.alt_gold_sql
                else ""
            ),
            "Documentation of the tables it reads:\n" + table_docs(context["manifest"], g.tables),
        ]
    parts += [
        "What the data covers and the subject areas:\n" + context["brief"],
        "Governed metrics:\n" + context["metrics"],
        "Domain rules:\n" + context["rules"],
        "Conventions:\n" + context["conventions"],
    ]
    return "\n\n".join(p for p in parts if p), facts


def audit_case(case: Case, context: dict, con, auditors: list[str]) -> dict:
    text, facts = case_prompt(case, context, con)

    def one(auditor: str) -> dict:
        try:
            return _ask_as(auditor, text)
        except ProviderLimit:
            raise
        except (LLMError, ValueError) as e:
            return {"auditor": auditor, "verdict": "error", "problem": str(e)[:300]}

    with ThreadPoolExecutor(len(auditors)) as pool:
        opinions = list(pool.map(one, auditors))
    flagged = [o for o in opinions if o.get("verdict") in ("wrong", "ambiguous")]
    return {
        "id": case.id,
        "question": case.question,
        "expected": case.expect.classification,
        "gold_sql": case.gold_sql,
        "result": facts,
        "opinions": opinions,
        "flagged_by": [o["auditor"] for o in flagged],
    }


def _ask_as(auditor: str, text: str) -> dict:
    r = complete_with(auditor, prompt("audit"), text, schema=SCHEMA, retries=1)
    out = r.json()
    out["auditor"] = auditor
    out["tokens_in"], out["tokens_out"] = r.tokens_in, r.tokens_out
    return out


def save(result: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{result['id']}.json"
    path.write_text(json.dumps(result, indent=1, default=str) + "\n")
    return path
