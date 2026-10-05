"""HTTP API and static site.

GET only, so the site can sit behind the same read-only proxy as the other projects. A question
is answered as a stream of server-sent events: one per pipeline stage, then the result and the
answer. Questions run one at a time; a few more may wait. Each reader gets a small hourly
allowance and the whole site a daily spending cap, and a question asked recently is answered
from its stored trace without calling a model.
"""

import json
import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import lake
from ..catalog import store
from ..config import REPO, Settings
from ..pipeline.context import Context
from .service import PAUSED, Answerer, client_hash

PER_HOUR = int(os.environ.get("NYCYNAPSE_PER_HOUR", "8"))
DAILY_BUDGET = float(os.environ.get("NYCYNAPSE_DAILY_BUDGET_USD", "4"))
WAITING = int(os.environ.get("NYCYNAPSE_QUEUE", "3"))
WEB = REPO / "web"
RESULTS = REPO / "evals" / "results"

settings = Settings.from_env()
app = FastAPI(title="NYCynapse", docs_url=None, redoc_url=None)
_busy = threading.Semaphore(1)
_queue = threading.Semaphore(WAITING + 1)
_state: dict = {}
_setup = threading.Lock()


def answerer() -> Answerer:
    with _setup:
        if "answerer" not in _state:
            con = lake.connect(settings)
            ctx = Context(settings, con, store.connect(settings.app_pg_dsn))
            _state["answerer"] = Answerer(ctx)
    return _state["answerer"]


@app.on_event("startup")
def warm_up():
    # Loading the embedding model takes several seconds; do it before the first reader asks.
    threading.Thread(target=lambda: answerer().ctx.embed("warm up"), daemon=True).start()


def _client(request: Request) -> str:
    # Behind the site's proxy every request comes from the same few addresses, so the proxy
    # passes the reader's address along.
    ip = (
        request.headers.get("x-client-ip")
        or request.headers.get("x-real-ip")
        or (request.client.host if request.client else "?")
    )
    return client_hash(ip)


def _sse(event: str, data) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


@app.get("/api/ask")
def ask(request: Request, q: str = Query(min_length=3, max_length=400)):
    a = answerer()
    question = " ".join(q.split())
    cached = a.recent(question)
    if cached:

        def replay():
            for s in cached["stages"]:
                yield _sse("stage", s)
            r = cached["result"] or {}
            if r.get("columns"):
                yield _sse(
                    "result", {"columns": r["columns"], "rows": r["rows"], "sql": cached["sql"]}
                )
            yield _sse(
                "answer",
                {
                    "trace_id": cached["id"],
                    "classification": cached["classification"],
                    "answer": cached["answer"],
                    "chart": r.get("chart"),
                    "caveats": r.get("caveats", []),
                    "evidence": r.get("evidence", []),
                    "cached": True,
                    "cost_usd": 0,
                    "latency_ms": cached["latency_ms"],
                },
            )

        return StreamingResponse(replay(), media_type="text/event-stream")

    if a.paused():
        raise HTTPException(503, PAUSED)
    client = _client(request)
    if a.asked_recently(client) >= PER_HOUR:
        raise HTTPException(
            429,
            f"That is {PER_HOUR} questions this hour. Try again later, or "
            "open one of the example answers.",
        )
    if a.spent_today() >= DAILY_BUDGET:
        raise HTTPException(503, "Today's question budget is used up. Example answers still work.")
    if not _queue.acquire(blocking=False):
        raise HTTPException(503, "Busy answering other questions. Try again in a minute.")

    def run():
        try:
            with _busy:
                yield _sse("stage", {"stage": "start", "label": "Start", "ms": 0})
                for event, data in a.stream(question, client):
                    yield _sse(event, data)
        finally:
            _queue.release()

    return StreamingResponse(
        run(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/trace/{trace_id}")
def trace(trace_id: str):
    try:
        t = answerer().trace(trace_id)
    except Exception as e:  # noqa: BLE001 - malformed ids
        raise HTTPException(404, "No such answer") from e
    if t is None:
        raise HTTPException(404, "No such answer")
    return t


@app.get("/api/recent")
def recent(limit: int = 12):
    rows = (
        answerer()
        .ctx.conn.execute(
            "SELECT id, question, classification, asked_at, latency_ms FROM app.traces "
            "WHERE error IS NULL AND answer IS NOT NULL ORDER BY asked_at DESC LIMIT %s",
            (min(limit, 50),),
        )
        .fetchall()
    )
    return [{**r, "id": str(r["id"]), "asked_at": str(r["asked_at"])} for r in rows]


@app.get("/api/freshness")
def freshness():
    # A cursor of its own: a question may be using the main connection right now.
    con = answerer().ctx.lake.cursor()
    rows = con.execute(
        "SELECT table_name, domain, cadence, coverage_start, coverage_end, row_count, "
        "last_success_at, is_stale, hours_since_latest_event FROM lake.gold.ops_source_freshness "
        "ORDER BY domain, table_name"
    ).fetchall()
    cols = [
        "table",
        "domain",
        "cadence",
        "coverage_start",
        "coverage_end",
        "rows",
        "last_success_at",
        "is_stale",
        "hours_since_latest_event",
    ]
    gaps = con.execute(
        "SELECT source, feed, gap_start, gap_end, gap_minutes, reason "
        "FROM lake.gold.ops_feed_gap ORDER BY gap_start DESC LIMIT 20"
    ).fetchall()
    return JSONResponse(
        json.loads(
            json.dumps(
                {
                    "tables": [dict(zip(cols, r, strict=True)) for r in rows],
                    "gaps": [
                        dict(
                            zip(
                                ["source", "feed", "start", "end", "minutes", "reason"],
                                g,
                                strict=True,
                            )
                        )
                        for g in gaps
                    ],
                },
                default=str,
            )
        )
    )


@app.get("/api/catalog")
def catalog():
    c = answerer().ctx.catalog
    return {
        "version": answerer().ctx.version,
        "workspaces": [
            {
                "id": w.id,
                "label": w.label,
                "description": " ".join(w.description.split()),
                "models": w.models,
                "questions": w.sample_questions,
            }
            for w in c.workspaces
        ],
        "models": [
            {
                "name": m.name,
                "label": m.label,
                "grain": m.grain,
                "table": m.table,
                "workspace": m.workspace,
                "dimensions": [d.name for d in m.dimensions],
            }
            for m in c.models
        ],
        "metrics": [
            {
                "id": m.id,
                "label": m.label,
                "unit": m.unit,
                "description": " ".join(m.description.split()),
                "additivity": m.additivity,
                "sources": [s.model for s in m.sources],
            }
            for m in c.metrics
        ],
        "relationships": len(c.relationships),
    }


@app.get("/api/evals")
def evals():
    out = []
    for path in sorted(RESULTS.glob("*.json")):
        out.append(json.loads(path.read_text()))
    return out


@app.get("/api/health")
def health():
    return {"ok": True, "catalog": answerer().ctx.version}


if WEB.exists():
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")

    @app.get("/{page:path}")
    def page(page: str):
        name = page.strip("/").split("/")[0] or "index"
        target = WEB / f"{name}.html"
        if not target.exists() or not Path(target).resolve().is_relative_to(WEB.resolve()):
            target = WEB / "index.html"
        return FileResponse(target)
