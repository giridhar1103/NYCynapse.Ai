"""Read-only access to the lake.

Every connection the query side opens is read-only: DuckLake is attached READ_ONLY, file
access is limited to the lake's data directory, network access and extension loading are off,
and settings are locked so generated SQL cannot change them back.
"""

import os

import duckdb

from .config import Settings


def connect(settings: Settings, *, threads: int = 2) -> duckdb.DuckDBPyConnection:
    if not settings.lake_pg_dsn:
        raise RuntimeError("NYC_LAKE_PG_DSN is not set")
    con = duckdb.connect()
    if not os.environ.get("HOME"):
        con.execute("SET home_directory = '/root'")
    con.execute(f"SET memory_limit = '{settings.memory_limit}'")
    con.execute(f"SET threads = {int(threads)}")
    con.execute("SET TimeZone = 'UTC'")
    for ext in ("ducklake", "postgres", "spatial"):
        try:
            con.execute(f"LOAD {ext}")
        except duckdb.Error:
            con.execute(f"INSTALL {ext}")
            con.execute(f"LOAD {ext}")
    con.execute(
        f"ATTACH 'ducklake:postgres:{settings.lake_pg_dsn}' AS lake "
        "(METADATA_SCHEMA 'ducklake', READ_ONLY)"
    )
    con.execute("USE lake")
    # DuckLake reads its own Parquet files, so file access is limited to the lake's data
    # directory rather than switched off. That still allows COPY TO inside it, which is why
    # the SQL guard only lets SELECT through and the query worker runs as a user that cannot
    # write there.
    con.execute(f"SET allowed_directories = ['{settings.lake_data_path}/']")
    con.execute("SET enable_external_access = false")
    con.execute("SET autoinstall_known_extensions = false")
    con.execute("SET autoload_known_extensions = false")
    con.execute("SET lock_configuration = true")
    return con
