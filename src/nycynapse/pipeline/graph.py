"""The query pipeline as a LangGraph state graph.

Stages are plain functions; the graph only decides the order and the branches:

    understand ─┬─ not answerable ──────────────────────────────── done
                └─ retrieve ─ ground* ─ generate ─ guard ─ execute ─┬─ done
                                           ^                          │
                                           └──── repair (max 2) ──────┘

* grounding, time resolution and place coverage are on from E2 up.

From E3 the model plans instead of writing SQL, and code compiles the plan:

    ... ground ─ plan ─ compile ─ guard ─ execute
                  ^        │         │
                  └────────┴─────────┘  failed plans and runs go back to the planner,
                                        then to SQL generation as a last resort
"""

import json
import time
from datetime import datetime
from typing import Any, TypedDict

import sqlglot
from langgraph.graph import END, START, StateGraph
from sqlglot import exp

from .. import guard
from ..evals.execute import run
from ..evals.system import Answer
from .cards import (
    instruction_lines,
    metric_catalog,
    metric_lines,
    model_card,
    relationship_catalog,
    relationship_lines,
    semantic_card,
)
from .compiler import CompileError, Compiler
from .context import Context
from .generate import generate
from .plan import make_plan
from .timeparse import resolve
from .understand import understand, workspace_brief

MAX_REPAIRS = 2
HISTORY_START = datetime(2024, 1, 1)


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
    semantic_context: str
    plan: Any
    plan_raw: Any
    plan_attempts: int
    compiled: bool
    abstain: str | None
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


def build(ctx: Context, *, grounding: bool, planning: bool = False, examples: bool = False):
    brief = workspace_brief(ctx.catalog, ctx.coverage)
    compiler = Compiler(ctx.catalog)
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
        rules = "Rules:\n" + instruction_lines(ctx.catalog, u.workspaces, names)
        resolved = _grounding_text(state, ctx) if grounding else ""
        cards = "\n\n".join(
            model_card(ctx.models[n], ctx.manifest, values) for n in state["models"]
        )
        parts = [
            cards,
            "Governed metrics:\n" + (metric_lines(ctx.catalog, names) or "none"),
            "Relationships:\n" + (relationship_lines(ctx.catalog, names) or "none"),
            rules,
            resolved,
        ]
        out = {"context": "\n\n".join(p for p in parts if p)}
        if planning:
            sem = "\n\n".join(
                semantic_card(ctx.models[n], ctx.manifest, values, ctx.catalog)
                for n in state["models"]
            )
            out["semantic_context"] = "\n\n".join(
                p
                for p in [
                    sem,
                    "Metrics:\n" + (metric_catalog(ctx.catalog, names) or "none"),
                    "Relationships:\n" + (relationship_catalog(ctx.catalog, names) or "none"),
                    rules,
                    resolved,
                    _examples_text(ctx, state["question"], u.workspaces) if examples else "",
                ]
                if p
            )
        return out

    def n_plan(state: State) -> dict:
        retry = state.get("problem")
        p = make_plan(
            state["question"],
            state["as_of"],
            state["semantic_context"],
            previous=state.get("plan_raw") if retry else None,
            problem=retry,
        )
        return {
            "plan": p.plan,
            "plan_raw": p.raw,
            "problem": p.error,
            "plan_attempts": state.get("plan_attempts", 0) + 1,
            **_spend(state, p),
            "log": _log(state, "plan", plan=p.raw, error=p.error),
        }

    def n_compile(state: State) -> dict:
        if state.get("plan") is None:
            return {"sql": None, "compiled": False}
        try:
            sql = compiler.compile(state["plan"], state.get("window"), state.get("places"))
        except CompileError as e:
            return {
                "sql": None,
                "compiled": False,
                "problem": f"compile: {e}",
                "log": _log(state, "compile", ok=False, error=str(e)),
            }
        return {
            "sql": sql,
            "compiled": True,
            "problem": None,
            "log": _log(state, "compile", ok=True, sql=sql),
        }

    def after_compile(state: State) -> str:
        if state.get("sql"):
            return "run"
        if state.get("plan_attempts", 0) < 2:
            return "plan"
        return "generate"

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
        if grounding:
            gap = coverage_gap(ctx, g.tables, state.get("window"))
            if gap:
                return {
                    "guard": g,
                    "execution": None,
                    "problem": None,
                    "abstain": gap,
                    "log": _log(state, "coverage", ok=False, reason=gap),
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
        elif state.get("compiled") and state.get("plan") is not None:
            problem = out_of_bounds(ctx, state["plan"].metrics, ex)
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

    def after_repair(state: State) -> str:
        return "plan" if planning and state.get("compiled") else "generate"

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
    if planning:
        g.add_node("plan", n_plan)
        g.add_node("compile", n_compile)
        g.add_edge("context", "plan")
        g.add_edge("plan", "compile")
        g.add_conditional_edges(
            "compile", after_compile, {"run": "run", "plan": "plan", "generate": "generate"}
        )
        g.add_conditional_edges("repair", after_repair, {"plan": "plan", "generate": "generate"})
    else:
        g.add_edge("context", "generate")
        g.add_edge("repair", "generate")
    g.add_edge("generate", "run")
    g.add_conditional_edges("run", after_run, {"repair": "repair", END: END})
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
    def __init__(
        self,
        ctx: Context,
        *,
        grounding: bool,
        name: str,
        planning: bool = False,
        examples: bool = False,
    ):
        self.ctx = ctx
        self.name = name
        self.graph = build(ctx, grounding=grounding, planning=planning, examples=examples)

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
        classification = u.classification
        if s.get("abstain"):
            classification, sql = "unsupported", None
        return Answer(
            classification=classification,
            sql=sql,
            explanation=s.get("abstain") or s.get("explanation", u.reason),
            workspaces=u.workspaces,
            tables=s["guard"].tables if s.get("guard") and s["guard"].ok else [],
            values=_literals(sql) if sql else [],
            metrics=list(s["plan"].metrics) if s.get("plan") is not None else [],
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


def _examples_text(ctx: Context, question: str, workspaces: list[str]) -> str:
    found = ctx.verified_examples(question, workspaces)
    if not found:
        return ""
    lines = ["Verified plans for similar questions (follow their style, not their values):"]
    for _, ex in found:
        plan = {k: v for k, v in ex["plan"].items() if v not in (None, [], {}, True, "aggregate")}
        lines.append(f"Q: {ex['question']}\nplan: {json.dumps(plan)}")
    return "\n".join(lines)


def out_of_bounds(ctx: Context, metric_ids: list[str], ex) -> str | None:
    """A governed metric outside its plausible range means the query is wrong, not the city."""
    for mid in metric_ids:
        m = next((x for x in ctx.catalog.metrics if x.id == mid), None)
        if m is None or mid not in ex.columns or m.bounds == (None, None):
            continue
        i = ex.columns.index(mid)
        lo, hi = m.bounds
        for row in ex.rows:
            v = row[i]
            if isinstance(v, int | float) and (
                (lo is not None and v < lo) or (hi is not None and v > hi)
            ):
                return (
                    f"{mid} = {v} is outside the plausible range {lo} to {hi}; "
                    "check filters, joins and units"
                )
    return None


def coverage_gap(ctx: Context, tables: list[str], window) -> str | None:
    """Say why not, when most of the time window is outside what the data covers.

    A model's coverage is the union of its source tables, so weather counts both the settled
    archive and the recent model hours.
    """
    if window is None or window.is_now or window.start is None or window.end is None:
        return None
    span = (window.end - window.start).total_seconds()
    for m in ctx.models_for_tables(tables):
        spans = [ctx.table_coverage[t] for t in m.coverage if ctx.table_coverage.get(t, (None,))[0]]
        if not spans or not span:
            continue
        # A few source rows carry impossible dates; the lake's history starts in 2024.
        start = max(min(s for s, _ in spans), HISTORY_START)
        end = max(e for _, e in spans)
        missing = max(0.0, (window.end - max(end, window.start)).total_seconds())
        missing += max(0.0, (min(start, window.end) - window.start).total_seconds())
        if missing / span > 0.1:
            return (
                f"{m.label} covers {start:%-d %b %Y} to {end:%-d %b %Y}, which does not "
                f"cover most of {window.label}."
            )
    return None


def _literals(sql: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, dialect="duckdb")
    except sqlglot.errors.ParseError:
        return []
    return sorted({lit.this for lit in tree.find_all(exp.Literal) if lit.is_string})
