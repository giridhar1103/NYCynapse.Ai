"""Run SQL against the lake with a time limit."""

import threading
import time
from dataclasses import dataclass

import duckdb


@dataclass
class Execution:
    ok: bool
    columns: list[str]
    rows: list[tuple]
    error: str | None
    ms: int


def run(
    con: duckdb.DuckDBPyConnection, sql: str, *, timeout_s: float = 120, max_rows: int = 10_000
) -> Execution:
    timer = threading.Timer(timeout_s, con.interrupt)
    started = time.monotonic()
    timer.start()
    try:
        cur = con.execute(sql)
        rows = cur.fetchmany(max_rows)
        cols = [d[0] for d in cur.description] if cur.description else []
        return Execution(True, cols, rows, None, int((time.monotonic() - started) * 1000))
    except duckdb.InterruptException:
        return Execution(
            False,
            [],
            [],
            f"timed out after {timeout_s:.0f}s",
            int((time.monotonic() - started) * 1000),
        )
    except duckdb.Error as e:
        return Execution(
            False,
            [],
            [],
            f"{type(e).__name__}: {e}"[:500],
            int((time.monotonic() - started) * 1000),
        )
    finally:
        timer.cancel()
