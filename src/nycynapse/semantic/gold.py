"""What gold actually contains, captured from the live lake and dbt's documentation.

The export is committed as semantic/gold_manifest.json. The semantic layer is checked
against it, so CI catches a column that was renamed or dropped in the lake without needing
access to the lake itself.
"""

import json
from pathlib import Path

import duckdb


def export(con: duckdb.DuckDBPyConnection, dbt_manifest: Path) -> dict:
    docs = {}
    if dbt_manifest.exists():
        manifest = json.loads(dbt_manifest.read_text())
        for node in manifest.get("nodes", {}).values():
            if node.get("resource_type") in {"model", "seed"} and node.get("schema") == "gold":
                docs[node["name"]] = {
                    "description": " ".join((node.get("description") or "").split()),
                    "meta": node.get("meta") or node.get("config", {}).get("meta") or {},
                    "columns": {
                        c: " ".join((v.get("description") or "").split())
                        for c, v in node.get("columns", {}).items()
                    },
                }
    tables = {}
    rows = con.execute(
        "SELECT table_name, column_name, data_type FROM duckdb_columns() "
        "WHERE database_name = 'lake' AND schema_name = 'gold' ORDER BY table_name, column_index"
    ).fetchall()
    for table, column, dtype in rows:
        t = tables.setdefault(
            table,
            {
                "description": docs.get(table, {}).get("description", ""),
                "meta": docs.get(table, {}).get("meta", {}),
                "columns": {},
            },
        )
        t["columns"][column] = {
            "type": dtype,
            "description": docs.get(table, {}).get("columns", {}).get(column, ""),
        }
    return {"tables": tables}


def load(path: Path) -> dict:
    return json.loads(path.read_text())
