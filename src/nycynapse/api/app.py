"""HTTP API and static site.

GET only, so the site can sit behind the same read-only proxy as the other projects. The guide
itself is in `chat.py` (one conversation turn as a stream of events, with its limits and its
line). This module holds the rest: the pages, the catalog, freshness, evaluation results,
stored answers, health checks and the security headers.
"""

import json
import logging
import os
import threading
from pathlib import Path

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import lake
from ..catalog import store
from ..config import REPO, Settings
from ..pipeline.context import Context
from . import chat, pulse
from .service import Answerer

WEB = REPO / "web"
RESULTS = REPO / "evals" / "results"


def _log_to_stderr() -> None:
    """The service log (journald) gets this package's lines; it adds the time itself."""
    pkg = logging.getLogger("nycynapse")
    if not pkg.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        pkg.addHandler(handler)
        pkg.setLevel(os.environ.get("NYCYNAPSE_LOG_LEVEL", "INFO"))
        pkg.propagate = False


_log_to_stderr()
log = logging.getLogger(__name__)
settings = Settings.from_env()
app = FastAPI(title="NYCynapse", docs_url=None, redoc_url=None)
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
    # Database changes that came with this version of the code are applied before it serves.
    try:
        applied = store.migrate(store.connect(settings.app_pg_dsn))
        if applied:
            log.info("applied database migrations: %s", ", ".join(applied))
    except Exception:  # noqa: BLE001 - the site still answers; the log says what is missing
        log.exception("could not apply database migrations")
    # Loading the embedding model takes several seconds; do it before the first reader asks.
    threading.Thread(target=lambda: answerer().ctx.embed("warm up"), daemon=True).start()
    threading.Thread(target=_warm_tools, daemon=True).start()


def _warm_tools() -> None:
    """Start the tools server and make the calls whose first run is slow (station lists,
    schedules, alert feeds), so the first reader after a restart does not wait for them."""
    if os.environ.get("NYCYNAPSE_WARM", "1") == "0":
        return
    try:
        host = chat.agent().host
        for name, args in (
            ("subway_alerts", {}),
            ("transit_changes", {"system": "subway"}),
            (
                "trip_plan",
                {
                    "origin": "Times Square",
                    "destination": "Grand Central Terminal",
                    "mode": "transit",
                },
            ),
        ):
            res, _ = host.call(name, args)
            log.info("warmed %s: %s", name, "ok" if res.ok else (res.error or "")[:120])
    except Exception:  # noqa: BLE001 - warming is a courtesy, never a reason not to start
        log.exception("could not warm the tools")


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


@app.get("/api/health/deep")
def health_deep():
    """Each part an answer depends on, checked for real. 503 names what is down."""
    parts: dict[str, bool] = {}

    def check(name: str, fn) -> None:
        try:
            parts[name] = bool(fn())
        except Exception as e:  # noqa: BLE001 - a failed check is the finding
            log.warning("health: %s failed: %s", name, str(e)[:160])
            parts[name] = False

    check("store", lambda: answerer().ctx.conn.execute("SELECT 1").fetchone() is not None)
    check(
        "lake",
        lambda: (
            answerer()
            .ctx.lake.cursor()
            .execute("SELECT count(*) FROM lake.gold.ops_source_freshness")
            .fetchone()[0]
            > 0
        ),
    )
    check("tools_server", lambda: chat.agent().host.connected())
    check("models", lambda: chat.agent().available())
    stale: list[str] = []
    try:
        stale = [
            r[0]
            for r in answerer()
            .ctx.lake.cursor()
            .execute("SELECT table_name FROM lake.gold.ops_source_freshness WHERE is_stale")
            .fetchall()
        ]
    except Exception:  # noqa: BLE001 - reported by the lake check above
        pass
    ok = all(parts.values())
    return JSONResponse(
        {"ok": ok, **parts, "stale_tables": stale, "chat": chat.gate.stats()},
        status_code=200 if ok else 503,
    )


chat.setup(settings, lambda: answerer().ctx)
app.include_router(chat.router)
pulse.setup(lambda: answerer().ctx.lake)
app.include_router(pulse.router)

# Only this site's own scripts, styles and data, plus the bot check and the map tiles.
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com "
    "https://static.cloudflareinsights.com; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://tiles.openfreemap.org "
    "https://mesonet.agron.iastate.edu; "
    "font-src 'self' data: https://tiles.openfreemap.org; "
    "connect-src 'self' https://tiles.openfreemap.org https://mesonet.agron.iastate.edu "
    "https://cloudflareinsights.com; "
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
    h.setdefault(
        "Permissions-Policy", "camera=(), microphone=(self), geolocation=(self), payment=()"
    )
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    path = request.url.path
    if "/static/" in path:
        # Scripts and styles carry a version in their address and the libraries are pinned, so
        # those never change under the same address. Map outlines change rarely.
        if "/static/vendor/" in path or request.url.query:
            keep = "public, max-age=31536000, immutable"
        elif "/static/geo/" in path:
            keep = "public, max-age=86400"
        else:
            keep = "public, max-age=300"
        h.setdefault("Cache-Control", keep)
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
