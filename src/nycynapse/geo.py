"""Map outlines for the site, exported from the lake's own geography.

Boroughs, neighborhoods (NTAs) and subway lines become small GeoJSON files in web/static/geo/,
simplified and rounded so the whole city draws quickly. Subway lines use the MTA's GTFS shapes
and route colors: for each route, the longest shape of the latest feed.
"""

import json
from pathlib import Path

import duckdb

ROUND = 5


def _features(con: duckdb.DuckDBPyConnection, sql: str, props: list[str]) -> dict:
    feats = []
    for row in con.execute(sql).fetchall():
        geom = json.loads(row[-1])
        feats.append(
            {
                "type": "Feature",
                "properties": dict(zip(props, row[:-1], strict=False)),
                "geometry": _round(geom),
            }
        )
    return {"type": "FeatureCollection", "features": feats}


def _round(g):
    if isinstance(g, dict):
        return {k: _round(v) for k, v in g.items()}
    if isinstance(g, list):
        if g and isinstance(g[0], float | int) and not isinstance(g[0], bool):
            return [round(x, ROUND) for x in g]
        return [_round(x) for x in g]
    return g


def export(con: duckdb.DuckDBPyConnection, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "boroughs.json": _features(
            con,
            "SELECT boro_name, ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.0006)) "
            "FROM silver.geo_borough ORDER BY boro_code",
            ["name"],
        ),
        "nta.json": _features(
            con,
            "SELECT nta_name, nta_code, boro_name, nta_kind, "
            "ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.0003)) FROM silver.geo_nta "
            "ORDER BY nta_code",
            ["name", "code", "borough", "kind"],
        ),
        "zips.json": _features(
            con,
            # Each ZIP is labelled with the residential neighborhoods covering most of it, so
            # a reader sees "Coney Island" rather than "11224".
            """
            WITH z AS (
                SELECT modzcta, label, geom FROM silver.geo_modzcta WHERE modzcta <> '99999'
            ),
            overlap AS (
                SELECT z.modzcta, n.nta_name, n.boro_name,
                       ST_Area(ST_Intersection(z.geom, n.geom)) / ST_Area(z.geom) AS share
                FROM z JOIN silver.geo_nta n ON ST_Intersects(z.geom, n.geom)
                WHERE n.nta_kind = 'residential'
            ),
            ranked AS (
                SELECT *, row_number() OVER (PARTITION BY modzcta ORDER BY share DESC) AS k
                FROM overlap WHERE share > 0.2
            ),
            names AS (
                SELECT modzcta, string_agg(nta_name, ' / ' ORDER BY k) AS area,
                       arg_max(boro_name, share) AS borough
                FROM ranked WHERE k <= 2 GROUP BY 1
            )
            SELECT z.modzcta, z.label, coalesce(names.area, ''), coalesce(names.borough, ''),
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(z.geom, 0.0004))
            FROM z LEFT JOIN names USING (modzcta) ORDER BY 1
            """,
            ["name", "zips", "area", "borough"],
        ),
        "subway.json": _features(
            con,
            """
            WITH latest AS (SELECT max(feed_version) AS v FROM silver.gtfs_shape),
            shapes AS (
                SELECT s.shape_id, count(*) AS n FROM silver.gtfs_shape s, latest
                WHERE s.feed_version = latest.v GROUP BY 1
            ),
            route_shape AS (
                SELECT t.route_id, s.shape_id, s.n,
                       row_number() OVER (PARTITION BY t.route_id ORDER BY s.n DESC) AS k
                FROM (SELECT DISTINCT route_id, shape_id FROM silver.gtfs_trip, latest
                      WHERE feed_version = latest.v) t
                JOIN shapes s USING (shape_id)
            ),
            lines AS (
                SELECT r.route_id, ST_MakeLine(list(ST_Point(p.longitude, p.latitude)
                       ORDER BY p.point_sequence)) AS geom
                FROM route_shape r JOIN silver.gtfs_shape p ON p.shape_id = r.shape_id, latest
                WHERE r.k = 1 AND p.feed_version = latest.v
                GROUP BY 1
            )
            SELECT l.route_id, any_value(g.route_short_name),
                   '#' || coalesce(nullif(any_value(g.route_color), ''), '808183'),
                   any_value(g.route_sort_order),
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(any_value(l.geom), 0.0001))
            FROM lines l JOIN silver.gtfs_route g ON g.route_id = l.route_id, latest
            WHERE g.feed_version = latest.v
            GROUP BY l.route_id ORDER BY 4
            """,
            ["route", "name", "color", "order"],
        ),
    }
    sizes = {}
    for name, fc in files.items():
        text = json.dumps(fc, separators=(",", ":"))
        (out / name).write_text(text)
        sizes[name] = {"features": len(fc["features"]), "kb": len(text) // 1024}
    return sizes
