"""Postgres store for the semantic catalog."""

from importlib import resources

import psycopg
from psycopg.rows import dict_row

MIGRATIONS = "nycynapse.catalog.migrations"


def connect(dsn: str) -> psycopg.Connection:
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET TimeZone = 'UTC'")
    return conn


def migrate(conn: psycopg.Connection) -> list[str]:
    conn.execute("CREATE SCHEMA IF NOT EXISTS catalog")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS catalog.schema_migrations "
        "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    done = {r["version"] for r in conn.execute("SELECT version FROM catalog.schema_migrations")}
    applied = []
    for f in sorted(x for x in resources.files(MIGRATIONS).iterdir() if x.name.endswith(".sql")):
        version = f.name.removesuffix(".sql")
        if version in done:
            continue
        with conn.transaction():
            conn.execute(f.read_text())
            conn.execute("INSERT INTO catalog.schema_migrations (version) VALUES (%s)", (version,))
        applied.append(version)
    return applied


def current_version(conn: psycopg.Connection) -> str | None:
    row = conn.execute("SELECT version FROM catalog.versions WHERE is_current").fetchone()
    return row["version"] if row else None
