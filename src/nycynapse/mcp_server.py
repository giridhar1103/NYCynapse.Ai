"""MCP server: the same pipeline and lake, usable from any MCP client.

Every tool is read-only. `ask` runs the full pipeline and keeps a trace like the website does;
`run_sql` goes through the same guard and read-only connection as generated SQL.
"""

import threading

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from . import guard, lake
from .catalog import store
from .config import Settings
from .evals.execute import run
from .pipeline.context import Context

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
MAX_ROWS = 200

server = MCPServer(
    name="nycynapse",
    title="NYCynapse",
    instructions=(
        "Answers questions about New York City from open data: taxi and app rides, the subway, "
        "Citi Bike, 311, crashes, road speeds and weather, January 2024 to now. Prefer `ask` "
        "for questions in plain English. Use `domains` and `metric` to see what is covered, and "
        "`run_sql` only for SELECT queries on gold tables. Times are New York time."
    ),
)
_lock = threading.Lock()
_state: dict = {}


def _jsonable(v):
    from .api.service import _jsonable as convert

    return convert(v)


def _answerer():
    from .api.service import Answerer

    with _lock:
        if "answerer" not in _state:
            settings = Settings.from_env()
            ctx = Context(settings, lake.connect(settings), store.connect(settings.app_pg_dsn))
            _state["answerer"] = Answerer(ctx)
    return _state["answerer"]


@server.tool(annotations=READ_ONLY)
def ask(question: str) -> dict:
    """Answer a question about New York City data. Returns the answer, the SQL that produced
    it, the result rows and notes on how it was checked."""
    out: dict = {"question": question}
    for event, data in _answerer().stream(" ".join(question.split()), "mcp"):
        if event == "result":
            out["sql"] = data["sql"]
            out["columns"] = data["columns"]
            out["rows"] = data["rows"][:MAX_ROWS]
        elif event == "answer":
            out.update(
                {
                    k: data.get(k)
                    for k in ("answer", "classification", "window", "caveats", "evidence")
                }
            )
            out["trace_id"] = data.get("trace_id")
        elif event == "error":
            out["error"] = data["message"]
    return out


@server.tool(annotations=READ_ONLY)
def domains() -> list[dict]:
    """The subject areas covered, with their tables and example questions."""
    c = _answerer().ctx.catalog
    return [
        {
            "id": w.id,
            "label": w.label,
            "description": " ".join(w.description.split()),
            "models": w.models,
            "example_questions": w.sample_questions,
        }
        for w in c.workspaces
    ]


@server.tool(annotations=READ_ONLY)
def metric(name: str) -> dict:
    """How a governed metric is defined: meaning, unit, how it adds up and where it comes
    from. `name` is a metric id or label; an unknown name lists the ones that exist."""
    c = _answerer().ctx.catalog
    key = name.strip().lower()
    for m in c.metrics:
        if key in (m.id.lower(), m.label.lower()):
            return {
                "id": m.id,
                "label": m.label,
                "unit": m.unit,
                "description": " ".join(m.description.split()),
                "additivity": m.additivity,
                "sources": [{"model": s.model, "expression": s.expression} for s in m.sources],
            }
    return {"error": f"no metric called {name}", "metrics": sorted(m.id for m in c.metrics)}


@server.tool(annotations=READ_ONLY)
def run_sql(sql: str) -> dict:
    """Run one read-only SELECT on the gold tables (DuckDB SQL). Filter timestamps with
    timezone('America/New_York', TIMESTAMP '...') bounds for speed. At most 200 rows."""
    g = guard.check(sql)
    if not g.ok:
        return {"error": f"rejected: {g.reason}"}
    con = _answerer().ctx.lake.cursor()
    con.execute("USE lake")
    ex = run(con, sql, timeout_s=60, max_rows=MAX_ROWS)
    if not ex.ok:
        return {"error": ex.error}
    rows = [[_jsonable(v) for v in r] for r in ex.rows]
    return {"columns": ex.columns, "rows": rows, "ms": ex.ms}


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
