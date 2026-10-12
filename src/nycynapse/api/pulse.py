"""What the city looks like right now, for the landing page: bike docks reporting, trains seen
in the last minutes, the weather, and how much the lake holds. Cached for a minute and cheap:
no model is called."""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter

router = APIRouter()
TTL_S = 60
_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()
_state: dict = {}


def setup(lake_fn) -> None:
    _state["lake_fn"] = lake_fn


def _compute() -> dict:
    con = _state["lake_fn"]().cursor()
    con.execute("USE lake")
    out: dict = {}
    bikes = con.execute(
        """
        WITH latest AS (
            SELECT station_id, arg_max(bikes_available, reported_at) AS bikes,
                   arg_max(ebikes_available, reported_at) AS ebikes, max(reported_at) AS at
            FROM gold.fct_bike_station_status
            WHERE reported_date >= current_date - 1
              AND reported_at > now() - INTERVAL 45 MINUTE
            GROUP BY 1
        )
        SELECT l.station_id, s.latitude, s.longitude, l.bikes, l.ebikes, l.at
        FROM latest l JOIN gold.dim_bike_station s USING (station_id)
        WHERE NOT s.is_new_jersey
        """
    ).fetchall()
    out["bikes"] = {
        "stations": len(bikes),
        "bikes": int(sum(b[3] or 0 for b in bikes)),
        "ebikes": int(sum(b[4] or 0 for b in bikes)),
        "at": str(max((b[5] for b in bikes), default="")),
        "points": [[round(b[2], 4), round(b[1], 4), int(b[3] or 0)] for b in bikes[::3]],
    }
    trains = con.execute(
        """
        WITH recent AS (
            SELECT a.route_id, a.station_id, a.arrived_at
            FROM gold.fct_subway_arrival a
            WHERE a.arrived_date >= current_date - 1
              AND a.arrived_at > (SELECT max(arrived_at) FROM gold.fct_subway_arrival
                                  WHERE arrived_date >= current_date - 1) - INTERVAL 20 MINUTE
        )
        SELECT r.route_id, s.latitude, s.longitude, count(*) AS n, max(r.arrived_at)
        FROM recent r JOIN gold.dim_subway_station s USING (station_id)
        GROUP BY 1, 2, 3
        """
    ).fetchall()
    out["subway"] = {
        "arrivals": int(sum(t[3] for t in trains)),
        "stations": len({(t[1], t[2]) for t in trains}),
        "at": str(max((t[4] for t in trains), default="")),
        "points": [[round(t[2], 4), round(t[1], 4), t[0]] for t in trains],
    }
    size = con.execute(
        "SELECT sum(row_count), count(*) FILTER (WHERE NOT is_stale) , count(*) "
        "FROM gold.ops_source_freshness"
    ).fetchone()
    out["lake"] = {"rows": int(size[0] or 0), "fresh_tables": size[1], "tables": size[2]}
    try:
        from ..tools.live import subway_alerts, weather

        w = weather(3)
        m = next(b for b in w.data["boroughs"] if b["borough"] == "Manhattan")
        out["weather"] = {
            "temp_f": m["temp_f"],
            "now": m["now"],
            "rain_chance": m["rain_chance_max_pct"],
        }
        out["alerts"] = len(subway_alerts().data or [])
    except Exception:  # noqa: BLE001 - the page shows what it has
        out["weather"], out["alerts"] = None, None
    out["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return out


@router.get("/api/pulse")
def pulse():
    with _lock:
        if _cache["data"] is None or time.time() - _cache["at"] > TTL_S:
            _cache["data"], _cache["at"] = _compute(), time.time()
        return _cache["data"]
