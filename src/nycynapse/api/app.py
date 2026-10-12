"""HTTP API and static site.

GET only, so the site can sit behind the same read-only proxy as the other projects. A question
is answered as a stream of server-sent events: one per pipeline stage, then the result and the
answer. Questions run one at a time; a few more may wait. Each reader gets a small hourly
allowance and the whole site a daily spending cap, and a question asked recently is answered
from its stored trace without calling a model.
"""

import asyncio
import json
import os
import threading
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import lake
from ..catalog import store
from ..config import REPO, Settings
from ..pipeline.context import Context
from . import chat, pulse
from .service import PAUSED, Answerer, client_hash

PER_HOUR = int(os.environ.get("NYCYNAPSE_PER_HOUR", "8"))
DAILY_BUDGET = float(os.environ.get("NYCYNAPSE_DAILY_BUDGET_USD", "4"))
WAITING = int(os.environ.get("NYCYNAPSE_QUEUE", "3"))
KEEPALIVE_S = 10
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
        a = _state["answerer"]
        # A Postgres restart (an unattended upgrade did this once) closes the connection;
        # reconnect on the next request instead of failing until the service restarts.
        try:
            a.ctx.conn.execute("SELECT 1")
        except psycopg.Error:
            a.ctx.conn = store.connect(settings.app_pg_dsn)
    return a


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


async def _relay(a: Answerer, question: str, client: str):
    """Run the question in its own thread and pass its events on.

    The thread owns the queue places and gives them back when the question is done, even if
    the reader left. A reader who leaves early still gets the stored answer on asking again.
    While waiting, a comment line goes out now and then so proxies keep the stream open.
    """
    loop = asyncio.get_running_loop()
    events: asyncio.Queue = asyncio.Queue()

    def work():
        try:
            with _busy:
                loop.call_soon_threadsafe(
                    events.put_nowait, _sse("stage", {"stage": "start", "label": "Start", "ms": 0})
                )
                for event, data in a.stream(question, client):
                    loop.call_soon_threadsafe(events.put_nowait, _sse(event, data))
        except Exception as e:  # noqa: BLE001 - reported to the reader
            msg = {"message": f"Something went wrong answering that ({type(e).__name__})."}
            loop.call_soon_threadsafe(events.put_nowait, _sse("error", msg))
        finally:
            _queue.release()
            loop.call_soon_threadsafe(events.put_nowait, None)

    threading.Thread(target=work, daemon=True).start()
    while True:
        try:
            chunk = await asyncio.wait_for(events.get(), KEEPALIVE_S)
        except TimeoutError:
            yield b": still working\n\n"
            continue
        if chunk is None:
            return
        yield chunk


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

    return StreamingResponse(
        _relay(a, question, client),
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
    path = RESULTS / "summary.json"
    return json.loads(path.read_text()) if path.exists() else {"groups": [], "paired": []}


@app.get("/api/health")
def health():
    return {"ok": True, "catalog": answerer().ctx.version, "chat": chat.gate.stats()}


chat.setup(settings, lambda: answerer().ctx)
app.include_router(chat.router)
pulse.setup(lambda: answerer().ctx.lake)
app.include_router(pulse.router)

# Only this site's own scripts, styles and data, plus the bot check and the map tiles.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com "
    "https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://tiles.openfreemap.org; "
    "font-src 'self' data: https://tiles.openfreemap.org; "
    "connect-src 'self' https://tiles.openfreemap.org https://cloudflareinsights.com; "
    "worker-src 'self' blob:; "
    "frame-src https://challenges.cloudflare.com; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'; object-src 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h.setdefault("Content-Security-Policy", CSP)
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    path = request.url.path
    if "/static/" in path:
        # Map outlines and libraries change rarely; scripts and styles are versioned in their URLs.
        long = "/static/vendor/" in path or "/static/geo/" in path or request.url.query
        h.setdefault("Cache-Control", f"public, max-age={86400 if long else 300}")
    elif not path.startswith("/api/"):
        h.setdefault("Cache-Control", "no-cache")
    return response


if WEB.exists():
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")

    @app.get("/{page:path}")
    def page(page: str):
        if page.startswith("api/"):
            raise HTTPException(404, "Not found")
        name = page.strip("/").split("/")[0] or "index"
        target = WEB / f"{name}.html"
        if not target.exists() or not Path(target).resolve().is_relative_to(WEB.resolve()):
            target = WEB / "index.html"
        return FileResponse(target)
