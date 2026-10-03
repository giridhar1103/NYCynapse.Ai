"""The query pipeline as a LangGraph state graph.

Stages are plain functions; the graph only decides the order and the branches:

    understand ─┬─ not answerable ──────────────────────────────── done
                └─ retrieve ─ ground* ─ generate ─ guard ─ execute ─┬─ done
                                           ^                          │
                                           └──── repair (max 2) ──────┘

* grounding, time resolution and place coverage are on from E2 up.
"""

import time
from typing import Any, TypedDict

import sqlglot
from langgraph.graph import END, START, StateGraph
from sqlglot import exp

from .. import guard
from ..evals.execute import run
from ..evals.system import Answer
from .cards import instruction_lines, metric_lines, model_card, relationship_lines
from .context import Context
from .generate import generate
from .timeparse import resolve
from .understand import understand, workspace_brief

MAX_REPAIRS = 2


class State(TypedDict, total=False):
    question: str
    as_of: str
    understanding: Any
    window: Any
    models: list[str]
    hits: list[Any]
    groundings: list[Any]
    places: dict
    context: str
    sql: str | None
    explanation: str
    guard: Any
    execution: Any
    problem: str | None
    repairs: int
    cost_usd: float
    tokens_in: int
    tokens_out: int
    calls: int
    log: list[dict]


def _spend(state: State, obj) -> dict:
    return {
        "cost_usd": state.get("cost_usd", 0) + obj.cost_usd,
        "tokens_in": state.get("tokens_in", 0) + obj.tokens_in,
        "tokens_out": state.get("tokens_out", 0) + obj.tokens_out,
        "calls": state.get("calls", 0) + 1,
    }


def _log(state: State, stage: str, **detail) -> list[dict]:
    return [*state.get("log", []), {"stage": stage, "t": time.monotonic(), **detail}]


def build(ctx: Context, *, grounding: bool):
    brief = workspace_brief(ctx.catalog, ctx.coverage)
    values = _enumerated_values(ctx)

    def n_understand(state: State) -> dict:
        u = understand(state["question"], state["as_of"], brief)
        window = resolve(u.time, state["as_of"]) if (grounding and u.time) else None
        return {
            "understanding": u,
            "window": window,
            **_spend(state, u),
            "log": _log(
                state,
                "understand",
                classification=u.classification,
                workspaces=u.workspaces,
                mentions=u.mentions,
                time=u.time.model_dump() if u.time else None,
            ),
        }

    def route(state: State) -> str:
        u = state["understanding"]
        return "retrieve" if u.classification == "answerable" and u.workspaces else END

    def n_retrieve(state: State) -> dict:
        u = state["understanding"]
        workspaces = [w for w in u.workspaces if w in {x.id for x in ctx.catalog.workspaces}]
        models = [m.name for m in ctx.workspace_models(workspaces)]
        hits = ctx.search(state["question"], workspaces)
        return {
            "models": models,
            "hits": hits,
            "log": _log(state, "retrieve", models=models, hits=[h.id for h in hits[:12]]),
        }

    def n_ground(state: State) -> dict:
        u = state["understanding"]
        found = ctx.ground(u.mentions, u.workspaces) if u.mentions else []
        places = {g.value: ctx.place_coverage(g.value) for g in found if g.kind == "place"}
        return {
            "groundings": found,
            "places": places,
            "log": _log(
                state,
                "ground",
                values=[(g.mention, g.model, g.dimension, g.value, g.score) for g in found],
            ),
        }

    def n_context(state: State) -> dict:
        names = set(state["models"])
        u = state["understanding"]
        cards = "\n\n".join(
            model_card(ctx.models[n], ctx.manifest, values) for n in state["models"]
        )
        parts = [
            cards,
            "Governed metrics:\n" + (metric_lines(ctx.catalog, names) or "none"),
            "Relationships:\n" + (relationship_lines(ctx.catalog, names) or "none"),
            "Rules:\n" + instruction_lines(ctx.catalog, u.workspaces, names),
        ]
        if grounding:
            parts.append(_grounding_text(state, ctx))
        return {"context": "\n\n".join(parts)}

    def n_generate(state: State) -> dict:
        g = generate(
            state["question"],
            state["as_of"],
            state["context"],
            previous=state.get("sql") if state.get("problem") else None,
            problem=state.get("problem"),
        )
        return {
            "sql": g.sql,
            "explanation": g.explanation,
            "problem": None,
            **_spend(state, g),
            "log": _log(
                state, "generate", sql=g.sql, error=g.error, repair=bool(state.get("problem"))
            ),
        }

    def n_check_and_run(state: State) -> dict:
        sql = state.get("sql")
        if not sql:
            return {
                "guard": None,
                "execution": None,
                "log": _log(state, "guard", ok=False, reason="no sql"),
            }
        g = guard.check(sql)
        if not g.ok:
            return {
                "guard": g,
                "execution": None,
                "problem": f"rejected: {g.reason}",
                "log": _log(state, "guard", ok=False, reason=g.reason),
            }
        ex = run(ctx.lake, sql, timeout_s=120)
        problem = None
        if not ex.ok:
            problem = f"execution error: {ex.error}"
        elif grounding and (not ex.rows or all(v is None for v in ex.rows[0])):
            problem = (
                "the query returned nothing. Check filter values against the grounded "
                "values and the time window against the coverage."
            )
        return {
            "guard": g,
            "execution": ex,
            "problem": problem,
            "log": _log(state, "execute", ok=ex.ok, rows=len(ex.rows), ms=ex.ms, error=ex.error),
        }

    def after_run(state: State) -> str:
        if state.get("problem") and state.get("repairs", 0) < MAX_REPAIRS:
            return "repair"
        return END

    def n_repair(state: State) -> dict:
        return {"repairs": state.get("repairs", 0) + 1}

    g = StateGraph(State)
    g.add_node("understand", n_understand)
    g.add_node("retrieve", n_retrieve)
    g.add_node("ground", n_ground)
    g.add_node("context", n_context)
    g.add_node("generate", n_generate)
    g.add_node("run", n_check_and_run)
    g.add_node("repair", n_repair)
    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", route, {"retrieve": "retrieve", END: END})
    g.add_edge("retrieve", "ground" if grounding else "context")
    g.add_edge("ground", "context")
    g.add_edge("context", "generate")
    g.add_edge("generate", "run")
    g.add_conditional_edges("run", after_run, {"repair": "repair", END: END})
    g.add_edge("repair", "generate")
    return g.compile()


def _enumerated_values(ctx: Context) -> dict[tuple[str, str], list[str]]:
    rows = ctx.conn.execute(
        "SELECT model, dimension, array_agg(value ORDER BY rows DESC) AS vs "
        "FROM catalog.dimension_values WHERE version = %s GROUP BY 1, 2",
        (ctx.version,),
    ).fetchall()
    enum = {
        (m.name, d.name)
        for m in ctx.catalog.models
        for d in m.dimensions
        if d.values == "enumerate"
    }
    return {
        (r["model"], r["dimension"]): r["vs"] for r in rows if (r["model"], r["dimension"]) in enum
    }


def _grounding_text(state: State, ctx: Context) -> str:
    lines = []
    w = state.get("window")
    if w is not None:
        s, e = w.iso()
        if w.is_now:
            lines.append(
                f"Time: right now, meaning the latest data at or before {e} New York time."
            )
        elif s or e:
            lines.append(
                f"Time window ({w.label}), New York time: from {s} up to but not including {e}."
            )
    u = state["understanding"]
    if u.day_part:
        p = next((p for p in ctx.catalog.periods if p.name == u.day_part), None)
        if p:
            lines.append(
                f"Part of day '{p.name}': {p.description} As SQL: "
                + p.where.replace("{date}", "<local date column>").replace(
                    "{hour}", "<local hour column>"
                )
            )
    by_mention: dict[str, list[str]] = {}
    for g in state.get("groundings", []):
        if g.kind == "value":
            m = ctx.models.get(g.model)
            d = m.dimension(g.dimension) if m else None
            col = f"{m.table}.{d.sql}" if m and d else f"{g.model}.{g.dimension}"
            by_mention.setdefault(g.mention, []).append(f"{col} = '{g.value}'")
    for mention, options in by_mention.items():
        lines.append(f"'{mention}' matches these values in the data: " + "; ".join(options))
    for place, levels in state.get("places", {}).items():
        desc = []
        for level in ("taxi_zone", "neighborhood", "subway_station", "bike_station"):
            items = levels.get(level, [])
            if items:
                desc.append(
                    f"{level} " + ", ".join(f"{code} ({name})" for code, name in items[:20])
                )
        lines.append(f"'{place}' covers: " + "; ".join(desc))
    return "Resolved for this question:\n" + ("\n".join(lines) if lines else "nothing")


class PipelineSystem:
    def __init__(self, ctx: Context, *, grounding: bool, name: str):
        self.ctx = ctx
        self.name = name
        self.graph = build(ctx, grounding=grounding)

    def answer(self, question: str, as_of: str) -> Answer:
        t0 = time.monotonic()
        try:
            s = self.graph.invoke({"question": question, "as_of": as_of, "repairs": 0})
        except Exception as e:  # noqa: BLE001 - a crash is scored as a failed answer
            return Answer(
                "error",
                error=f"{type(e).__name__}: {e}"[:500],
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
        u = s["understanding"]
        sql = s.get("sql") if u.classification == "answerable" else None
        return Answer(
            classification=u.classification if u.classification != "error" else "error",
            sql=sql,
            explanation=s.get("explanation", u.reason),
            workspaces=u.workspaces,
            tables=s["guard"].tables if s.get("guard") and s["guard"].ok else [],
            values=_literals(sql) if sql else [],
            places=list(s.get("places", {})),
            stages={
                "log": s.get("log", []),
                "repairs": s.get("repairs", 0),
                "clarification": u.clarification,
            },
            model_calls=s.get("calls", 0),
            tokens_in=s.get("tokens_in", 0),
            tokens_out=s.get("tokens_out", 0),
            cost_usd=s.get("cost_usd", 0.0),
            latency_ms=int((time.monotonic() - t0) * 1000),
            error=u.error,
        )


def _literals(sql: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, dialect="duckdb")
    except sqlglot.errors.ParseError:
        return []
    return sorted({lit.this for lit in tree.find_all(exp.Literal) if lit.is_string})
