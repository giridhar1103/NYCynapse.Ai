"""Publish the semantic layer to Postgres as a new catalog version.

The build happens in one transaction: documents with embeddings, the value index read from
the lake, and place coverage. The new version only becomes current once all of it is in, so
the query pipeline never sees half a catalog. Older versions are kept for a while because
traces and evaluation runs refer to the version they used.
"""

import hashlib
import json
import logging
import re
from pathlib import Path

import duckdb
import psycopg
from psycopg.types.json import Jsonb

from ..semantic.schema import Catalog
from .documents import Document, build

log = logging.getLogger(__name__)
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
KEEP_VERSIONS = 10
VALUE_LIMIT = 20_000


def content_hash(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.yaml")) + [root / "gold_manifest.json"]:
        if path.exists():
            h.update(path.relative_to(root).as_posix().encode())
            h.update(path.read_bytes())
    return h.hexdigest()[:10]


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def embed(texts: list[str]) -> list[list[float]]:
    from fastembed import TextEmbedding

    model = TextEmbedding(EMBEDDING_MODEL)
    return [v.tolist() for v in model.embed(texts, batch_size=64)]


def dimension_values(con: duckdb.DuckDBPyConnection, catalog: Catalog) -> list[tuple]:
    """Distinct values with row counts for every dimension that needs grounding."""
    rows = []
    for m in catalog.models:
        recent = ""
        if m.partition and m.partition.granularity == "month":
            # Big trip tables: recent months are enough to learn the vocabulary.
            recent = (
                f"WHERE {m.partition.column} >= "
                f"date_trunc('month', current_date) - INTERVAL 6 MONTH"
            )
        for d in m.dimensions:
            if d.values == "none" or d.kind == "list":
                continue
            sql = (
                f"SELECT CAST({d.sql} AS VARCHAR) AS v, count(*) AS n FROM lake.{m.table} "
                f"{recent} GROUP BY 1 HAVING v IS NOT NULL ORDER BY n DESC LIMIT {VALUE_LIMIT}"
            )
            try:
                found = con.execute(sql).fetchall()
            except duckdb.Error as e:
                log.warning("values for %s.%s skipped: %s", m.name, d.name, e)
                continue
            rows += [(m.name, d.name, v, normalize(v), n) for v, n in found]
            log.info("values %s.%s: %d", m.name, d.name, len(found))
    return rows


def place_coverage(con: duckdb.DuckDBPyConnection, catalog: Catalog) -> list[tuple]:
    rows = []
    for p in catalog.places:
        point = f"ST_Point({p.longitude}, {p.latitude})"
        # Buffer in degrees of latitude. East-west it reaches a little further, which is fine
        # for "near".
        circle = f"ST_Buffer({point}, {p.radius_m / 111_320.0})"
        queries = {
            "neighborhood": f"SELECT nta_code, nta_name FROM lake.silver.geo_nta "
            f"WHERE ST_Intersects(geom, {circle})",
            "taxi_zone": f"SELECT CAST(location_id AS VARCHAR), zone_name "
            f"FROM lake.silver.geo_taxi_zone "
            f"WHERE ST_Intersects(geom, {circle})",
            "borough": f"SELECT CAST(boro_code AS VARCHAR), boro_name "
            f"FROM lake.silver.geo_borough "
            f"WHERE ST_Intersects(geom, {circle})",
            "subway_station": f"SELECT station_id, station_name FROM lake.gold.dim_subway_station "
            f"WHERE ST_Distance_Sphere(ST_Point({p.latitude}, {p.longitude}), "
            f"ST_Point(latitude, longitude)) <= {p.radius_m}",
            "bike_station": f"SELECT station_id, station_name FROM lake.gold.dim_bike_station "
            f"WHERE ST_Distance_Sphere(ST_Point({p.latitude}, {p.longitude}), "
            f"ST_Point(latitude, longitude)) <= {p.radius_m}",
        }
        for level, sql in queries.items():
            for code, name in con.execute(sql).fetchall():
                rows.append((p.name, level, code, name))
    return rows


def publish(
    conn: psycopg.Connection,
    catalog: Catalog,
    gold: dict,
    *,
    version: str,
    lake: duckdb.DuckDBPyConnection | None,
    git_sha: str | None = None,
) -> dict:
    docs: list[Document] = build(catalog, gold)
    vectors = embed([f"{d.label or d.name}. {d.body}" for d in docs])
    values = dimension_values(lake, catalog) if lake is not None else []
    places = place_coverage(lake, catalog) if lake is not None else []
    stats = {"objects": len(docs), "values": len(values), "place_rows": len(places)}

    with conn.transaction():
        conn.execute("DELETE FROM catalog.versions WHERE version = %s", (version,))
        conn.execute(
            "INSERT INTO catalog.versions (version, git_sha, stats) VALUES (%s, %s, %s)",
            (version, git_sha, Jsonb(stats)),
        )
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO catalog.objects (version, id, kind, workspace, model, name, label,
                                             body, payload, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
                """,
                [
                    (
                        version,
                        d.id,
                        d.kind,
                        d.workspace,
                        d.model,
                        d.name,
                        d.label,
                        d.body,
                        Jsonb(json.loads(json.dumps(d.payload, default=str))),
                        str(v),
                    )
                    for d, v in zip(docs, vectors, strict=True)
                ],
            )
            cur.executemany(
                "INSERT INTO catalog.dimension_values VALUES (%s, %s, %s, %s, %s, %s)",
                [(version, *row) for row in values],
            )
            cur.executemany(
                "INSERT INTO catalog.place_coverage VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                [(version, *row) for row in places],
            )
        conn.execute("UPDATE catalog.versions SET is_current = false WHERE is_current")
        conn.execute("UPDATE catalog.versions SET is_current = true WHERE version = %s", (version,))
        conn.execute(
            """
            DELETE FROM catalog.versions WHERE version NOT IN (
                SELECT version FROM catalog.versions ORDER BY loaded_at DESC LIMIT %s)
            """,
            (KEEP_VERSIONS,),
        )
    return stats
