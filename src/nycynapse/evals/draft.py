"""Drafting new evaluation cases, kept apart from the person who built the pipeline.

Questions are drafted by two models from other vendors (GPT-6-Astra and Gemini 3.1 Pro) given a
description of the data, a quota per category and the existing questions to avoid. Near
duplicates of any existing question, holdout included, are dropped by embedding similarity, so
nobody has to read the holdout to check. A third model (Claude Opus 5.5) then writes each
reference query, which is run on the pinned snapshot and repaired if it fails or returns
nothing. Drafts are written to a file for the audit and a human review; nothing enters the
evaluation set without both.
"""

import json
from concurrent.futures import ThreadPoolExecutor

from ..llm.client import complete_with
from ..prompts import prompt
from .cases import AS_OF
from .execute import run

CATEGORIES = [
    "single_table",
    "multi_join",
    "cross_domain",
    "temporal",
    "geographic",
    "grounding",
    "live",
    "ambiguous",
    "unsupported",
    "non_data",
    "adversarial",
]
EXPECTED = ["answerable", "unclear", "unsupported", "non_data", "refuse"]

QUESTIONS = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
                    "expected": {"type": "string", "enum": EXPECTED},
                    "why": {"type": "string"},
                    "paraphrases": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "question",
                    "category",
                    "difficulty",
                    "expected",
                    "why",
                    "paraphrases",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

GOLD = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": EXPECTED},
        "sql": {"type": ["string", "null"]},
        "reason": {"type": "string"},
    },
    "required": ["classification", "sql", "reason"],
    "additionalProperties": False,
}

CATEGORY_NOTES = {
    "single_table": "one subject, one table: counts, totals, averages, rankings",
    "multi_join": "needs a join within a subject: trips by borough name, alerts by route name",
    "cross_domain": "combines two subjects: crashes in the rain, ridership on hot days",
    "temporal": "time is the hard part: weekdays against weekends, rush hours, month by month",
    "geographic": "named places: neighborhoods, landmarks, ZIP codes, boroughs, stations",
    "grounding": "the words differ from the data: 'noise' for several complaint types, nicknames",
    "live": "the live feeds: right now, today, the last few hours",
    "ambiguous": "expected unclear: two readings that give different answers",
    "unsupported": "expected unsupported: data that is not here, or a period it does not reach",
    "non_data": "expected non_data: not a question about the data at all",
    "adversarial": "expected refuse: attempts to change data, read other tables or run commands",
}


def draft_questions(
    drafter: str, quotas: dict[str, int], description: str, existing: list[str], as_of: str
) -> list[dict]:
    asks = "\n".join(f"- {cat}: {n} ({CATEGORY_NOTES[cat]})" for cat, n in quotas.items() if n > 0)
    text = "\n\n".join(
        [
            f"Now is {as_of} in New York.",
            "The data:\n" + description,
            f"Write exactly these numbers of questions per category:\n{asks}",
            "Existing questions to avoid:\n" + "\n".join(f"- {q}" for q in existing),
        ]
    )
    r = complete_with(drafter, prompt("draft"), text, schema=QUESTIONS, retries=1)
    out = r.json()["questions"]
    for q in out:
        q["drafted_by"] = drafter
    return out


def near_duplicates(drafts: list[dict], existing: list[str], threshold: float = 0.9) -> set[int]:
    """Indexes of drafts too close to an existing question or to an earlier draft."""
    from fastembed import TextEmbedding

    model = TextEmbedding("BAAI/bge-small-en-v1.5")
    texts = [d["question"] for d in drafts]
    vecs = [v / ((v**2).sum() ** 0.5) for v in model.embed(texts + existing, batch_size=64)]
    new, old = vecs[: len(texts)], vecs[len(texts) :]
    drop = set()
    for i, v in enumerate(new):
        if any(float(v @ o) >= threshold for o in old):
            drop.add(i)
        elif any(
            float(v @ new[j]) >= threshold
            for j in range(i)
            if j not in drop
            and not (drafts[i].get("group") and drafts[i]["group"] == drafts[j].get("group"))
        ):
            drop.add(i)
    return drop


def write_gold(draft: dict, description: str, con, writer: str, attempts: int = 3) -> dict:
    """Reference SQL for one drafted question, run and repaired until it returns rows."""
    base = "\n\n".join(
        [
            f"Now is {draft.get('as_of', AS_OF)} in New York.",
            "The data:\n" + description,
            f"Question: {draft['question']}",
            f"The question's author expected: {draft['expected']} ({draft['why']})",
        ]
    )
    feedback = ""
    last: dict = {}
    for _ in range(attempts):
        r = complete_with(writer, prompt("gold"), base + feedback, schema=GOLD, retries=1)
        last = r.json()
        if last["classification"] != "answerable" or not last.get("sql"):
            return {**last, "rows": None}
        ex = run(con, last["sql"], timeout_s=300, max_rows=21)
        if ex.ok and ex.rows and ex.rows != [(None,)]:
            return {
                **last,
                "rows": len(ex.rows),
                "preview": [[str(v) for v in row] for row in ex.rows[:5]],
                "ms": ex.ms,
            }
        problem = ex.error if not ex.ok else "it returned no rows"
        feedback = f"\n\nYour previous query:\n{last['sql']}\nfailed: {problem}. Fix it."
    return {**last, "rows": 0, "failed": True}


def gold_for_all(drafts: list[dict], description: str, con, writer: str, parallel: int = 3):
    """One reference per question; paraphrases share the reference of their original, since
    they must have exactly the same answer."""
    originals = [d for d in drafts if not d.get("paraphrase_of")]

    def one(d):
        cur = con.cursor()
        cur.execute("USE lake")
        try:
            return write_gold(d, description, cur, writer)
        except Exception as e:  # noqa: BLE001 - kept in the draft for review
            return {"error": f"{type(e).__name__}: {e}"[:300]}

    with ThreadPoolExecutor(parallel) as pool:
        golds = dict(zip((d["question"] for d in originals), pool.map(one, originals), strict=True))
    return [
        {**d, "gold": golds.get(d.get("paraphrase_of") or d["question"], {"error": "no original"})}
        for d in drafts
    ]


def to_json(drafts: list[dict]) -> str:
    return json.dumps(drafts, indent=1, default=str)
