"""What is happening near a place right now: road incidents and closures (511NY), weather
warnings (National Weather Service) and subway alerts tied to stations (MTA).

One poller (`poll`, run by the nycynapse-alerts service every few minutes) reads the feeds once
and keeps app.city_alerts current, so answering "anything going on near me?" is a quick read for
any number of people, and new alerts near a watched place can be pushed to them.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg

from . import Tool, ToolResult, failed
from .http import get_json

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
ROAD_KINDS = {"incident": "incident", "closure": "closure", "special-events": "event"}
_local = threading.local()


def _db() -> psycopg.Connection:
    from ..catalog import store

    c = getattr(_local, "conn", None)
    if c is None or c.closed:
        c = _local.conn = store.connect(os.environ["NYC_APP_PG_DSN"])
    return c


def _ts(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(ms) / 1000, UTC)
    except (TypeError, ValueError):
        return None


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat1 - lat2) * 111_320
    dx = (lon1 - lon2) * 111_320 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


# ------------------------------------------------------------------------------------ the feeds


def _road() -> list[dict]:
    from . import live

    live._ny511_events()
    for _ in range(30):  # the first fetch after a start runs in the background
        rows, age = live._ny511_events()
        if rows is not None:
            break
        time.sleep(1)
    out = []
    now = time.time() * 1000
    for e in rows or []:
        cat = (e.get("headline") or {}).get("category")
        kind = ROAD_KINDS.get(cat)
        geo = e.get("geometry") or {}
        if not kind or geo.get("type") != "Point":
            continue
        ends = (e.get("update-expiry-time") or {}).get("time")
        if ends and ends < now:
            continue
        lon, lat = geo["coordinates"][:2]
        text = " ".join((e.get("description") or "").split())
        prio = int(e.get("priority") or 10)
        # "W 47TH ST westbound: Road closed. Construction on ... for crane work." : the first
        # sentence is the headline, the rest the details.
        title, _, rest = text.partition(". ")
        out.append(
            {
                "id": f"511ny:{e.get('event-id')}",
                "source": "511ny",
                "kind": kind,
                "title": title.rstrip(".")[:160],
                "detail": rest[:600] or None,
                "lat": lat,
                "lon": lon,
                "area": None,
                "severity": 3 if prio <= 2 else 2 if prio <= 5 else 1,
                "starts": _ts((e.get("update-time") or {}).get("time")),
                "ends": None,
            }
        )
    return out


def _weather() -> list[dict]:
    from .live import NWS_ZONES

    d = get_json(
        "https://api.weather.gov/alerts/active",
        {"zone": NWS_ZONES},
        headers={"Accept": "application/geo+json"},
        ttl_s=120,
    )
    out = []
    sev = {"Extreme": 3, "Severe": 3, "Moderate": 2, "Minor": 1}
    for f in d.get("features", []):
        p = f["properties"]
        out.append(
            {
                "id": f"nws:{p.get('id')}",
                "source": "nws",
                "kind": "weather",
                "title": p.get("headline") or p.get("event") or "Weather alert",
                "detail": " ".join((p.get("description") or "").split())[:600],
                "lat": None,
                "lon": None,
                "area": p.get("areaDesc"),
                "severity": sev.get(p.get("severity"), 1),
                "starts": p.get("onset") or p.get("effective"),
                "ends": p.get("ends") or p.get("expires"),
            }
        )
    return out


def _subway() -> list[dict]:
    from .access import _stations
    from .live import MTA_ALERTS, _plain

    where = {}
    for s in _stations():
        try:
            where[s["gtfs_stop_id"]] = (
                float(s["gtfs_latitude"]),
                float(s["gtfs_longitude"]),
                s["stop_name"],
            )
        except (KeyError, TypeError, ValueError):
            continue
    d = get_json(MTA_ALERTS, ttl_s=60)
    now = time.time()
    out = []
    for ent in d.get("entity", []):
        a = ent.get("alert", {})
        live_now = any(
            int(p.get("start") or 0) <= now <= int(p.get("end") or 9e12)
            for p in (a.get("active_period") or [{}])
        )
        if not live_now:
            continue
        title = _plain((a.get("header_text") or {}).get("translation"))
        detail = _plain((a.get("description_text") or {}).get("translation"))
        stops = {
            (i.get("stop_id") or "").rstrip("NS")
            for i in a.get("informed_entity", [])
            if i.get("stop_id")
        }
        # Station alerts only: a whole-line alert is not "near" one place, and the subway alert
        # tool already covers lines. A long run of stations is a line change, not a local one.
        hits = [where[s] for s in sorted(stops) if s in where]
        if not hits or len(hits) > 6:
            continue
        lat = sum(h[0] for h in hits) / len(hits)
        lon = sum(h[1] for h in hits) / len(hits)
        out.append(
            {
                "id": f"mta:{ent.get('id')}",
                "source": "mta",
                "kind": "subway",
                "title": title[:200] or "Subway alert",
                "detail": (detail or "")[:600] or None,
                "lat": lat,
                "lon": lon,
                "area": ", ".join(sorted({h[2] for h in hits}))[:200],
                "severity": 2,
                "starts": None,
                "ends": None,
            }
        )
    return out


def poll() -> dict:
    """Read every feed once and bring app.city_alerts up to date. Returns counts per source, and
    the ids of alerts seen for the first time (for push notifications)."""
    c = _db()
    counts, new = {}, []
    for source, fn in (("511ny", _road), ("nws", _weather), ("mta", _subway)):
        try:
            rows = fn()
        except Exception as e:  # noqa: BLE001 - one feed down must not stop the others
            log.warning("alerts feed %s failed: %s", source, str(e)[:200])
            continue
        ids = []
        for r in rows:
            got = c.execute(
                """
                INSERT INTO app.city_alerts (id, source, kind, title, detail, lat, lon, area,
                    severity, starts, ends)
                VALUES (%(id)s, %(source)s, %(kind)s, %(title)s, %(detail)s, %(lat)s, %(lon)s,
                    %(area)s, %(severity)s, %(starts)s, %(ends)s)
                ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, detail = EXCLUDED.detail,
                    lat = EXCLUDED.lat, lon = EXCLUDED.lon, area = EXCLUDED.area,
                    severity = EXCLUDED.severity, ends = EXCLUDED.ends, last_seen = now(),
                    active = true
                RETURNING (xmax = 0) AS inserted
                """,
                r,
            ).fetchone()
            ids.append(r["id"])
            if got and got["inserted"]:
                new.append(r["id"])
        c.execute(
            "UPDATE app.city_alerts SET active = false WHERE source = %s AND active "
            "AND NOT (id = ANY(%s))",
            (source, ids),
        )
        counts[source] = len(ids)
    c.execute(
        "DELETE FROM app.city_alerts WHERE NOT active AND last_seen < now() - interval '7 days'"
    )
    return {"counts": counts, "new": new}


# --------------------------------------------------------------------------------------- the tool

KIND_WORD = {
    "incident": "Road incident",
    "closure": "Road closure",
    "event": "Event on the road",
    "weather": "Weather alert",
    "subway": "Subway",
}


def alerts_near(near: str, radius_m: int = 1000, since_minutes: int | None = None) -> ToolResult:
    """Road incidents and closures, subway station alerts and weather warnings in effect now
    near a place, closest first."""
    from .trips import locate

    p = locate(near)
    if not p:
        return failed(f"could not place {near!r}; name a station, landmark, address or area")
    radius_m = max(100, min(int(radius_m or 1000), 5000))
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(p["lat"])))
    since = (
        f"AND first_seen > now() - make_interval(mins => {int(since_minutes)})"
        if since_minutes
        else ""
    )
    rows = (
        _db()
        .execute(
            f"""
        SELECT * FROM app.city_alerts WHERE active {since} AND (
            lat IS NULL OR (lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s))
        ORDER BY severity DESC, first_seen DESC LIMIT 200
        """,
            (p["lat"] - dlat, p["lat"] + dlat, p["lon"] - dlon, p["lon"] + dlon),
        )
        .fetchall()
    )
    found = []
    for r in rows:
        d = None if r["lat"] is None else _metres(p["lat"], p["lon"], r["lat"], r["lon"])
        if d is not None and d > radius_m:
            continue
        found.append((d, dict(r)))
    found.sort(key=lambda x: (x[0] is None, x[0] or 0))
    found = found[:12]
    asof = datetime.now(NY)
    head = f"Active alerts within {radius_m} m of {p['name']} as of {asof:%-I:%M %p}"
    if since_minutes:
        head += f", new in the last {since_minutes} min"
    if not found:
        return ToolResult(
            summary=head + ": none. No road incidents, closures, station alerts or weather "
            "warnings are posted for that area right now.",
            data={"near": p, "alerts": []},
            sources=_SOURCES,
        )
    lines, points = [head + ":"], []
    for d, r in found:
        where = f"{round(d)} m away" if d is not None else f"area-wide ({(r['area'] or '')[:80]})"
        seen = r["first_seen"].astimezone(NY).strftime("%-I:%M %p %b %-d")
        until = ""
        if r.get("ends"):
            until = f", until {r['ends'].astimezone(NY):%a %-I:%M %p}"
        lines.append(
            f"- {KIND_WORD.get(r['kind'], r['kind'])}, {where}, posted {seen}{until}: {r['title']}"
            + (f". {r['detail'][:240]}" if r.get("detail") else "")
        )
        if d is not None:
            points.append(
                {
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "label": r["title"][:60],
                    "detail": KIND_WORD.get(r["kind"], ""),
                }
            )
    return ToolResult(
        summary="\n".join(lines),
        data={"near": p, "alerts": [r for _, r in found]},
        sources=_SOURCES,
        map=[{"type": "points", "label": "Alerts now", "points": points}] if points else [],
    )


_SOURCES = [
    {"kind": "live", "title": "511NY road events (powered by 511NY)", "url": "https://511ny.org"},
    {"kind": "live", "title": "MTA subway alerts", "url": "https://new.mta.info/alerts"},
    {
        "kind": "live",
        "title": "National Weather Service alerts",
        "url": "https://www.weather.gov/okx/",
    },
]

TOOLS = [
    Tool(
        "alerts_near",
        "live",
        "Road incidents and closures, subway station alerts and weather warnings in effect right "
        "now near a place, closest first. Use for 'anything going on near', 'is something "
        "happening around', 'why is traffic stopped near' questions. since_minutes limits to "
        "alerts posted in the last that many minutes.",
        {
            "type": "object",
            "properties": {
                "near": {"type": "string", "description": "station, landmark, address or area"},
                "radius_m": {"type": "integer", "minimum": 100, "maximum": 5000},
                "since_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
            },
            "required": ["near"],
        },
        alerts_near,
    )
]


def main() -> None:
    """The alerts poller: read the feeds every three minutes, forever."""
    from .http import load_keys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_keys()
    from ..catalog import store

    store.migrate(_db())
    every = int(os.environ.get("NYCYNAPSE_ALERTS_EVERY_S", "180"))
    while True:
        t0 = time.time()
        try:
            out = poll()
            log.info("alerts %s", json.dumps({**out["counts"], "new": len(out["new"])}))
            try:
                from ..connect.push import notify

                notify(out["new"])
            except ImportError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("push notifications failed")
        except Exception:  # noqa: BLE001
            log.exception("alerts poll failed")
        time.sleep(max(30, every - (time.time() - t0)))


if __name__ == "__main__":
    main()
