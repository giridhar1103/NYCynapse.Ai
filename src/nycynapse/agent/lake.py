"""The lake as tools for the guide agent.

`ask_lake` runs the governed text-to-SQL pipeline on one question and returns its rows without
writing a prose answer (the agent writes one answer for the whole turn). `freshness` reports how
current each lake table is, at no model cost. Results are turned into map hints: boroughs and
neighborhoods become shaded areas, and stations, zones, bike docks and road links become points,
using the lake's own reference tables.
"""

from __future__ import annotations

import copy
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from ..pipeline.context import Context
from ..pipeline.graph import build, coverage_gap, local_now
from ..tools import Tool, ToolResult, failed

NY = ZoneInfo("America/New_York")
ROWS_FOR_MODEL = 25
COVERAGE_TTL_S = 600

STAGES = {
    "understand": "Understand",
    "retrieve": "Find tables",
    "ground": "Match names",
    "context": "Gather context",
    "plan": "Plan",
    "compile": "Compile SQL",
    "generate": "Write SQL",
    "run": "Run query",
    "repair": "Repair",
}
BOROUGHS = {"manhattan", "brooklyn", "queens", "bronx", "staten island"}


class Lake:
    """Shared lake state for many concurrent questions. Each question gets its own DuckDB
    cursor; the catalog, embeddings and reference data are shared."""

    def __init__(self, ctx: Context):
        self.base = ctx
        self._lock = threading.Lock()
        self._coverage_at = 0.0
        self._places: dict[str, dict] | None = None
        self._run_lock = threading.BoundedSemaphore(2)  # lake queries at once

    def fork(self) -> Context:
        with self._lock:
            if time.time() - self._coverage_at > COVERAGE_TTL_S:
                # Coverage of live tables moves all the time; the pipeline caches it per context.
                for k in ("coverage", "table_coverage"):
                    self.base.__dict__.pop(k, None)
                _ = self.base.coverage, self.base.table_coverage
                self._coverage_at = time.time()
            ctx = copy.copy(self.base)
        cur = self.base.lake.cursor()
        cur.execute("USE lake")
        ctx.lake = cur
        return ctx

    # places for the map

    def places(self) -> dict[str, dict]:
        with self._lock:
            if self._places is None:
                self._places = self._load_places()
            return self._places

    def _load_places(self) -> dict[str, dict]:
        cur = self.base.lake.cursor()
        cur.execute("USE lake")
        out: dict[str, dict] = {}
        queries = [
            (
                "nta",
                "SELECT nta_name, centroid_latitude, centroid_longitude, borough "
                "FROM gold.dim_neighborhood",
            ),
            (
                "zone",
                "SELECT zone_name, centroid_latitude, centroid_longitude, borough "
                "FROM gold.dim_taxi_zone",
            ),
            (
                "station",
                "SELECT complex_name, latitude, longitude, borough FROM gold.dim_subway_complex",
            ),
            (
                "station",
                "SELECT station_name, latitude, longitude, borough FROM gold.dim_subway_station",
            ),
            (
                "bike",
                "SELECT station_name, latitude, longitude, NULL FROM gold.dim_bike_station "
                "WHERE NOT is_new_jersey",
            ),
            ("link", "SELECT link_name, latitude, longitude, borough FROM gold.dim_traffic_link"),
        ]
        for kind, sql in queries:
            try:
                for name, lat, lon, boro in cur.execute(sql).fetchall():
                    if name and lat and lon:
                        out.setdefault(
                            str(name).lower(),
                            {
                                "kind": kind,
                                "name": name,
                                "lat": float(lat),
                                "lon": float(lon),
                                "borough": boro,
                            },
                        )
            except Exception:  # noqa: BLE001 - a missing reference table only loses map points
                continue
        return out


def _number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return float(v)
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


_NOT_VALUES = re.compile(
    r"(zip|_id$|^id$|code|year|month|^lat|^lon|latitude|longitude|hour|date|"
    r"rank|number$)"
)
_GOOD_VALUES = re.compile(
    r"(count|total|trips|rides|complaints|requests|crashes|injur|^n$|value|"
    r"avg|average|share|rate|minutes|speed|riders|ridership|delay|wait|pct|"
    r"percent|hours|sum)"
)


def _value_column(columns: list[str], rows: list[list]) -> int | None:
    numeric = [
        i
        for i, c in enumerate(columns)
        if not _NOT_VALUES.search(c.lower())
        and all(_number(r[i]) is not None for r in rows[:20] if r[i] is not None)
        and any(r[i] is not None for r in rows[:20])
    ]
    good = [i for i in numeric if _GOOD_VALUES.search(columns[i].lower())]
    return (good or numeric or [None])[-1] if (good or numeric) else None


def _title(column: str) -> str:
    return column.replace("_", " ").strip().capitalize()


def map_from_rows(lake: Lake, columns: list[str], rows: list[list], label: str) -> list[dict]:
    """Guess how a result can be drawn: shade boroughs or neighborhoods, or place points."""
    if not columns or not rows:
        return []
    vi = _value_column(columns, rows)
    title = _title(columns[vi]) if vi is not None else "Result"
    out: list[dict] = []
    places = lake.places()
    for i in range(len(columns)):
        if i == vi:
            continue
        vals = [str(r[i]) for r in rows if r[i] is not None]
        if not vals:
            continue
        is_zip = "zip" in columns[i].lower() and all(re.fullmatch(r"\d{5}", v) for v in vals[:20])
        if not is_zip and _number(rows[0][i]) is not None:
            continue
        if is_zip:
            if vi is not None:
                out.append(
                    {
                        "type": "areas",
                        "level": "zip",
                        "label": title,
                        "unit": "",
                        "values": {v: _number(r[vi]) for v, r in zip(vals, rows, strict=False)},
                    }
                )
            break
        if all(v.lower() in BOROUGHS for v in vals[:20]):
            if len(set(v.lower() for v in vals)) == len(vals) and vi is not None:
                out.append(
                    {
                        "type": "areas",
                        "level": "borough",
                        "label": title,
                        "unit": "",
                        "values": {
                            v.title(): _number(r[vi]) for v, r in zip(vals, rows, strict=False)
                        },
                    }
                )
            continue  # a borough column next to finer places: keep looking
        hits = [(places.get(v.lower()), r) for v, r in zip(vals, rows, strict=False)]
        found = [(p, r) for p, r in hits if p]
        if found and len(found) >= max(1, len(vals[:30]) // 2):
            pts = [
                {
                    "lat": p["lat"],
                    "lon": p["lon"],
                    "label": p["name"],
                    "value": _number(r[vi]) if vi is not None else None,
                }
                for p, r in found[:60]
            ]
            if found[0][0]["kind"] == "nta":
                out.append(
                    {
                        "type": "areas",
                        "level": "nta",
                        "label": title,
                        "unit": "",
                        "values": {p["label"]: p["value"] for p in pts},
                    }
                )
            else:
                out.append({"type": "points", "points": pts, "label": title, "unit": ""})
            break
    lat = next((i for i, c in enumerate(columns) if c.lower() in ("latitude", "lat")), None)
    lon = next((i for i, c in enumerate(columns) if c.lower() in ("longitude", "lon", "lng")), None)
    if not out and lat is not None and lon is not None:
        pts = [
            {"lat": _number(r[lat]), "lon": _number(r[lon]), "label": str(r[0])[:60]}
            for r in rows[:100]
            if _number(r[lat]) and _number(r[lon])
        ]
        if pts:
            out.append({"type": "points", "points": pts, "label": title})
    return out


def _table(columns: list[str], rows: list[list]) -> str:
    head = " | ".join(columns)
    body = "\n".join(" | ".join(str(v)[:40] for v in r) for r in rows[:ROWS_FOR_MODEL])
    more = f"\n({len(rows) - ROWS_FOR_MODEL} more rows)" if len(rows) > ROWS_FOR_MODEL else ""
    return f"{head}\n{body}{more}"


def ask_lake(
    lake: Lake, question: str, as_of: str, emit: Callable[[dict], None] | None = None
) -> ToolResult:
    """Run the governed pipeline on one question, without writing prose."""
    question = " ".join(question.split())[:300]
    if not question:
        return failed("empty question")
    ctx = lake.fork()
    try:
        graph = build(ctx, grounding=True, planning=True, examples=True)
        state: dict = {"question": question, "as_of": as_of, "repairs": 0}
        t0 = time.monotonic()
        with lake._run_lock:
            for update in graph.stream(state, stream_mode="updates"):
                for node, change in update.items():
                    state.update(change or {})
                    if emit:
                        emit(
                            {
                                "stage": node,
                                "label": STAGES.get(node, node),
                                "ms": int((time.monotonic() - t0) * 1000),
                                "problem": (change or {}).get("problem"),
                            }
                        )
    finally:
        try:
            ctx.lake.close()
        except Exception:  # noqa: BLE001
            pass
    u = state.get("understanding")
    cost = state.get("cost_usd", 0) or 0
    calls = state.get("calls", 0) or 0
    src = [{"kind": "lake", "title": "NYCynapse lake (governed metrics)", "sql": state.get("sql")}]
    if state.get("abstain"):
        r = failed(f"the lake cannot answer this: {state['abstain']}")
        r.cost_usd, r.data = cost, {"model_calls": calls}
        return r
    if not u or u.classification != "answerable":
        reason = (u.clarification or u.reason) if u else "could not read the question"
        r = failed(
            f"the lake pipeline did not answer ({u.classification if u else 'error'}): {reason}"
        )
        r.cost_usd, r.data = cost, {"model_calls": calls}
        return r
    ex = state.get("execution")
    if ex is None or not ex.ok:
        problem = (ex.error if ex is not None else None) or state.get("problem") or "no query"
        r = failed(f"the lake query did not run: {problem}")
        r.cost_usd, r.data = cost, {"model_calls": calls, "sql": state.get("sql")}
        return r
    columns, rows = list(ex.columns), [list(r) for r in ex.rows[:200]]
    rows = [
        [v if isinstance(v, int | float | str | bool) or v is None else str(v) for v in r]
        for r in rows
    ]
    window = state.get("window")
    notes = []
    gap = coverage_gap(ctx, state["guard"].tables, window, local_now(as_of))
    if gap:
        notes.append(gap.message)
    if state.get("problem"):
        notes.append(state["problem"])
    summary = (
        f"Lake answer to {question!r}"
        + (f" for {window.label}" if window else "")
        + f": {len(ex.rows)} rows\n{_table(columns, rows)}"
        + ("\nNotes: " + "; ".join(notes) if notes else "")
    )
    return ToolResult(
        summary=summary,
        data={
            "columns": columns,
            "rows": rows,
            "sql": state.get("sql"),
            "window": window.label if window else None,
            "model_calls": calls,
            "tables": state["guard"].tables,
        },
        sources=src,
        map=map_from_rows(lake, columns, rows, question),
        cost_usd=cost,
    )


def freshness(lake: Lake) -> ToolResult:
    cur = lake.base.lake.cursor()
    cur.execute("USE lake")
    rows = cur.execute(
        "SELECT table_name, domain, cadence, coverage_start, coverage_end, is_stale, "
        "hours_since_latest_event FROM gold.ops_source_freshness ORDER BY domain, table_name"
    ).fetchall()
    lines = []
    for t, d, cad, start, end, stale, _hrs in rows:
        if start is None:
            continue
        end_ny = end.astimezone(NY).strftime("%Y-%m-%d %H:%M") if end else "?"
        lines.append(
            f"{d}/{t}: {str(start)[:10]} to {end_ny} NY ({cad})" + (" STALE" if stale else "")
        )
    return ToolResult(
        summary="What the lake holds, by table (first to latest event, New York time):\n"
        + "\n".join(lines),
        data=[list(map(str, r)) for r in rows],
        sources=[{"kind": "lake", "title": "Lake freshness"}],
    )


def tools(lake: Lake, as_of: Callable[[], str], emit_for: Callable[[], Callable | None]):
    return [
        Tool(
            "ask_lake",
            "lake",
            "Ask the NYC data lake one precise question in plain English. It plans and runs "
            "a governed SQL query over taxi and app rides, subway (ridership, live arrivals "
            "and delays, alerts), Citi Bike (trips, live dock status), 311, crashes, road "
            "speeds and past weather since January 2024, and returns rows. Cheapest and most "
            "trusted source; ask one thing per call.",
            {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
            lambda question: ask_lake(lake, question, as_of(), emit_for()),
        ),
        Tool(
            "lake_freshness",
            "lake",
            "How current each lake table is. Free; use when unsure whether the lake covers a date.",
            {"type": "object", "properties": {}},
            lambda: freshness(lake),
        ),
    ]


def now_as_of() -> str:
    return datetime.now(NY).replace(microsecond=0).isoformat()
