"""Decide whether generated SQL may run. Deterministic: no model is consulted.

Allowed: one SELECT (WITH and UNION included) over tables in the gold schema. Rejected:
anything that writes, changes settings, attaches, installs or loads extensions, reads files or
URLs through table functions, or reaches the catalog's internals.
"""

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

ALLOWED_SCHEMAS = {"gold"}
BLOCKED_FUNCTIONS = {
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "read_json",
    "read_json_auto",
    "read_text",
    "read_blob",
    "parquet_scan",
    "csv_scan",
    "json_scan",
    "glob",
    "st_read",
    "sniff_csv",
    "ducklake_snapshots",
    "ducklake_table_info",
    "ducklake_list_files",
    "postgres_scan",
    "postgres_query",
    "query",
    "query_table",
    "getenv",
    "current_setting",
    "duckdb_settings",
    "duckdb_secrets",
    "duckdb_extensions",
    "pragma_database_list",
    "which_secret",
}
BLOCKED_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Copy,
    exp.Command,
    exp.Set,
    exp.Pragma,
    exp.Use,
    exp.TruncateTable,
    exp.Attach,
    exp.Detach,
    exp.Install,
)


@dataclass
class GuardResult:
    ok: bool
    reason: str = ""
    tables: list[str] = field(default_factory=list)


def check(sql: str) -> GuardResult:
    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except sqlglot.errors.ParseError as e:
        return GuardResult(False, f"does not parse: {str(e).splitlines()[0][:200]}")
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        return GuardResult(False, f"expected one statement, got {len(statements)}")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        return GuardResult(False, f"only SELECT is allowed, got {type(tree).__name__}")
    for node in tree.walk():
        if isinstance(node, BLOCKED_NODES):
            return GuardResult(False, f"{type(node).__name__} is not allowed")
        if isinstance(node, exp.Func):
            name = (node.sql_name() if not isinstance(node, exp.Anonymous) else node.name).lower()
            if name in BLOCKED_FUNCTIONS:
                return GuardResult(False, f"function {name} is not allowed")

    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = []
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Func):  # table function in FROM
            fname = (t.this.name or t.this.sql_name()).lower()
            if fname not in {"unnest", "range", "generate_series"}:
                return GuardResult(False, f"table function {fname} is not allowed")
            continue
        name = t.name.lower()
        if not t.db and name in ctes:
            continue
        schema = (t.db or "").lower()
        catalog = (t.catalog or "").lower()
        if catalog not in ("", "lake") or schema not in ALLOWED_SCHEMAS:
            return GuardResult(False, f"table {t.sql()} is outside the gold schema")
        tables.append(f"gold.{name}")
    if not tables:
        return GuardResult(False, "no gold table referenced")
    return GuardResult(True, tables=sorted(set(tables)))
