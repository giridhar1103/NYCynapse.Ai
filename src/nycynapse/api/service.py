"""Answering one question end to end for the web: run the pipeline stage by stage, write the
answer, store the trace."""

import hashlib
import json
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from ..catalog.sync import normalize
from ..llm.client import ProviderLimit
from ..pipeline.answer import write_answer
from ..pipeline.context import Context
from ..pipeline.graph import build, coverage_gap

NY = ZoneInfo("America/New_York")
MAX_ROWS = 200

PAUSE_S = 1800
PAUSED = (
    "New questions are paused for a while: the model's usage allowance has run out. "
    "Example answers and recent questions still work."
)

STAGE_LABELS = {
    "understand": "Understand",
    "retrieve": "Find tables",
    "ground": "Match names",
    "context": "Gather context",
    "plan": "Plan",
    "compile": "Compile SQL",
    "generate": "Write SQL",
    "run": "Check and run",
    "repair": "Repair",
}


def now_as_of() -> str:
    return datetime.now(NY).replace(microsecond=0).isoformat()


def client_hash(ip: str) -> str:
    return hashlib.sha256(f"nycynapse:{ip}".encode()).hexdigest()[:16]


def _jsonable(v):
    if isinstance(v, int | float | str | bool) or v is None:
        return v
    return str(v)


class Answerer:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.graph = build(ctx, grounding=True, planning=True, examples=True)

    def recent(self, question: str, hours: int = 6) -> dict | None:
        row = self.ctx.conn.execute(
            "SELECT id FROM app.traces WHERE lower(question) = lower(%s) AND error IS NULL "
            "AND answer IS NOT NULL AND asked_at > now() - make_interval(hours => %s) "
            "ORDER BY asked_at DESC LIMIT 1",
            (question.strip(), hours),
        ).fetchone()
        return self.trace(str(row["id"])) if row else None

    def trace(self, trace_id: str) -> dict | None:
        row = self.ctx.conn.execute(
            "SELECT * FROM app.traces WHERE id = %s", (trace_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            k: (str(v) if k in ("id", "asked_at") else v)
            for k, v in row.items()
            if k != "client_hash"
        }

    def spent_today(self) -> float:
        row = self.ctx.conn.execute(
            "SELECT coalesce(sum(cost_usd), 0) AS c FROM app.traces "
            "WHERE asked_at > date_trunc('day', now())"
        ).fetchone()
        return float(row["c"])

    def asked_recently(self, client: str, minutes: int = 60) -> int:
        row = self.ctx.conn.execute(
            "SELECT count(*) AS n FROM app.traces WHERE client_hash = %s "
            "AND asked_at > now() - make_interval(mins => %s)",
            (client, minutes),
        ).fetchone()
        return int(row["n"])

    paused_until: float = 0.0

    def paused(self) -> bool:
        return time.time() < self.paused_until

    def stream(self, question: str, client: str):
        """Yield (event, data) pairs as the pipeline moves, then store the trace.

        When the model provider's allowance runs out, new questions pause for a while instead
        of failing one by one; nothing is stored, since the question itself was fine.
        """
        try:
            yield from self._stream(question, client)
        except ProviderLimit:
            self.paused_until = time.time() + PAUSE_S
            yield "error", {"message": PAUSED}

    def _stream(self, question: str, client: str):
        trace_id = str(uuid.uuid4())
        as_of = now_as_of()
        t0 = time.monotonic()
        state: dict = {"question": question, "as_of": as_of, "repairs": 0}
        stages = []
        error = None
        try:
            for update in self.graph.stream(state, stream_mode="updates"):
                for node, change in update.items():
                    state.update(change or {})
                    detail = _stage_detail(node, change or {}, state)
                    stages.append(
                        {
                            "stage": node,
                            "label": STAGE_LABELS.get(node, node),
                            "ms": int((time.monotonic() - t0) * 1000),
                            **detail,
                        }
                    )
                    yield "stage", stages[-1]
        except ProviderLimit:
            raise
        except Exception as e:  # noqa: BLE001 - reported to the reader, kept in the trace
            error = f"{type(e).__name__}: {e}"[:400]
            yield "error", {"message": "Something went wrong answering that. Try rephrasing."}

        u = state.get("understanding")
        classification = u.classification if u else "error"
        if state.get("abstain"):
            classification = "unsupported"
        ex = state.get("execution")
        columns, rows = (ex.columns, ex.rows[:MAX_ROWS]) if ex is not None and ex.ok else ([], [])
        answer_text, chart, caveats, unsupported = None, None, [], []
        window = state.get("window")
        if error is None and classification == "answerable" and ex is not None and ex.ok:
            yield (
                "result",
                {
                    "columns": columns,
                    "rows": [[_jsonable(v) for v in r] for r in rows],
                    "sql": state.get("sql"),
                },
            )
            warnings = []
            gap = coverage_gap(self.ctx, state["guard"].tables, window)
            if gap:
                warnings.append(gap)
            w = write_answer(question, window.label if window else None, columns, rows, warnings)
            state["cost_usd"] = state.get("cost_usd", 0) + w.cost_usd
            answer_text, chart, caveats = w.answer, w.chart, w.caveats
            unsupported = w.unsupported_numbers
            if unsupported:
                caveats = [
                    *caveats,
                    "Some numbers in this answer could not be traced to the "
                    "result; check the table.",
                ]
        elif error is None:
            answer_text = (
                state.get("abstain")
                or (u.clarification if u else None)
                or (u.reason if u else "I could not answer that.")
            )
        payload = {
            "trace_id": trace_id,
            "classification": classification,
            "answer": answer_text,
            "chart": chart,
            "caveats": caveats,
            "window": window.label if window else None,
            "evidence": _evidence(state, unsupported),
            "cost_usd": round(state.get("cost_usd", 0), 4),
            "latency_ms": int((time.monotonic() - t0) * 1000),
        }
        yield "answer", payload
        self.ctx.conn.execute(
            """
            INSERT INTO app.traces (id, question, as_of, classification, workspaces, sql, answer,
                result, stages, catalog_version, model_calls, cost_usd, latency_ms, client_hash,
                error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                trace_id,
                question,
                as_of,
                classification,
                u.workspaces if u else None,
                state.get("sql"),
                answer_text,
                Jsonb(
                    {
                        "columns": columns,
                        "rows": [[_jsonable(v) for v in r] for r in rows],
                        "chart": chart,
                        "caveats": caveats,
                        "evidence": payload["evidence"],
                    }
                ),
                Jsonb(json.loads(json.dumps(stages, default=str))),
                self.ctx.version,
                state.get("calls", 0),
                state.get("cost_usd", 0),
                payload["latency_ms"],
                client,
                error,
            ),
        )


def _stage_detail(node: str, change: dict, state: dict) -> dict:
    if node == "understand":
        u = change.get("understanding")
        w = change.get("window")
        return {
            "classification": u.classification,
            "workspaces": u.workspaces,
            "mentions": u.mentions,
            "window": w.label if w else None,
        }
    if node == "retrieve":
        return {"models": change.get("models", [])}
    if node == "ground":
        return {
            "matches": [
                {"mention": g.mention, "value": g.value, "where": g.dimension or g.kind}
                for g in change.get("groundings", [])[:12]
            ],
            "places": list(change.get("places", {})),
        }
    if node == "plan":
        return {"plan": change.get("plan_raw"), "problem": change.get("problem")}
    if node in ("compile", "generate"):
        return {"sql": change.get("sql"), "problem": change.get("problem")}
    if node == "run":
        ex = change.get("execution")
        g = change.get("guard")
        return {
            "guard": (g.ok if g else None),
            "rows": len(ex.rows) if ex else 0,
            "query_ms": ex.ms if ex else None,
            "problem": change.get("problem"),
            "abstain": change.get("abstain"),
        }
    return {}


def _evidence(state: dict, unsupported: list[str]) -> list[str]:
    out = []
    if state.get("compiled"):
        out.append("Compiled from governed metrics")
    elif state.get("sql"):
        out.append("Generated SQL")
    if state.get("groundings"):
        out.append("Names matched to values in the data")
    if state.get("repairs"):
        out.append(f"Repaired {state['repairs']} time(s)")
    if state.get("abstain"):
        out.append("Data does not cover the period")
    if unsupported:
        out.append("Answer has numbers not found in the result")
    return out


def normalized(question: str) -> str:
    return normalize(question)
