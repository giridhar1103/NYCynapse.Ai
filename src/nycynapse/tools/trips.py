"""Trip planning: real itineraries instead of a model's sense of the map.

Transit trips come from Transitous, a public trip planner built on the agencies' own schedules
(MTA subway, buses, LIRR and Metro-North, NJ Transit) with live arrivals where the agency
publishes them. It does not carry the JFK AirTrain or PATH, so trips to and from JFK are
planned to the AirTrain stations and the AirTrain leg is added from the Port Authority's own
figures, and PATH is named as not covered rather than left out silently.

Car trips use what taxi and app rides between the same two taxi zones took at the same hour,
from the lake (agg_trip_times_zone_pair). That is history, not live traffic, and the summary
says so.
"""

from __future__ import annotations

import json
import math
import re
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from . import Tool, ToolResult, failed
from .http import get_json

NY = ZoneInfo("America/New_York")
TRANSITOUS = "https://api.transitous.org/api/v5/plan"
GEO = Path(__file__).resolve().parents[3] / "web" / "static" / "geo"
# The region a trip may start or end in: the five boroughs and the nearby suburbs and airports.
BOX = (-74.6, 40.35, -73.4, 41.2)

AIRTRAIN_URL = "https://www.jfkairport.com/to-from-airport/air-train"
JFK = {
    "name": "JFK Airport",
    "lat": 40.6441,
    "lon": -73.7823,
    "stations": [
        ("Howard Beach-JFK Airport station", 40.6604, -73.8303),
        ("Jamaica station (Sutphin Blvd-Archer Av, LIRR)", 40.7003, -73.8080),
    ],
}
# From the Port Authority's AirTrain page: any terminal in under 20 minutes from Jamaica or
# Howard Beach, trains every 5 to 15 minutes, $8.75 one way.
AIRTRAIN = {"minutes": (8, 20), "fare_usd": 8.75}
AIRPORT_WORDS = {
    "jfk": JFK,
    "kennedy": JFK,
    "john f kennedy": JFK,
}
NAMED = {
    "lga": ("LaGuardia Airport", 40.7746, -73.8719),
    "laguardia": ("LaGuardia Airport", 40.7746, -73.8719),
    "la guardia": ("LaGuardia Airport", 40.7746, -73.8719),
    "ewr": ("Newark Liberty International Airport", 40.6895, -74.1745),
    "newark airport": ("Newark Liberty International Airport", 40.6895, -74.1745),
    "midtown": ("Midtown Manhattan", 40.7549, -73.9840),
    "times square": ("Times Square", 40.7580, -73.9855),
    "penn station": ("Penn Station", 40.7506, -73.9935),
    "grand central": ("Grand Central Terminal", 40.7527, -73.9772),
    "port authority": ("Port Authority Bus Terminal", 40.7570, -73.9903),
    "downtown brooklyn": ("Downtown Brooklyn", 40.6928, -73.9903),
    "lower manhattan": ("Lower Manhattan", 40.7075, -74.0113),
    "financial district": ("Financial District", 40.7075, -74.0113),
    "hoboken": ("Hoboken, NJ", 40.7440, -74.0324),
    "jersey city": ("Jersey City, NJ", 40.7178, -74.0431),
}


def _now() -> datetime:
    return datetime.now(NY)


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower().replace("'", ""))
    return " ".join(w for w in s.split() if w not in {"the", "airport", "nyc", "ny"})


def _inside(lat: float, lon: float) -> bool:
    return BOX[0] <= lon <= BOX[2] and BOX[1] <= lat <= BOX[3]


@lru_cache(maxsize=1)
def _hoods() -> list[tuple[str, list[str], float, float]]:
    """Neighborhood names (and their parts, "Astoria (North)" is Astoria) with a middle point."""
    out = []
    for f in json.loads((GEO / "nta.json").read_text())["features"]:
        name = f["properties"]["name"]
        g = f["geometry"]
        ring = (
            g["coordinates"][0]
            if g["type"] == "Polygon"
            else max((p[0] for p in g["coordinates"]), key=len)
        )
        lon = sum(c[0] for c in ring) / len(ring)
        lat = sum(c[1] for c in ring) / len(ring)
        parts = [_norm(re.sub(r"\(.*?\)", "", x)) for x in re.split(r"[-/]", name)]
        out.append((name, parts, lat, lon))
    return out


def _neighborhood(n: str) -> dict | None:
    hits = [(name, lat, lon) for name, parts, lat, lon in _hoods() if n in parts]
    if not hits:
        return None
    lat = sum(h[1] for h in hits) / len(hits)
    lon = sum(h[2] for h in hits) / len(hits)
    return {"name": n.title(), "lat": lat, "lon": lon}


def locate(text: str) -> dict | None:
    """A trip end from what the reader wrote: coordinates, a hub, an NYC address or landmark,
    or a place nearby (Hoboken, Newark Airport). None when it cannot be placed in the region."""
    raw = (text or "").strip()[:120]
    if not raw:
        return None
    m = re.fullmatch(r"\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*", raw)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return {"name": raw, "lat": lat, "lon": lon} if _inside(lat, lon) else None
    n = _norm(raw)
    for key, ap in AIRPORT_WORDS.items():
        if re.search(rf"\b{key}\b", n):
            return {"name": ap["name"], "lat": ap["lat"], "lon": ap["lon"], "airport": "jfk"}
    for key, (name, lat, lon) in NAMED.items():
        if n == key or re.search(rf"\b{key}\b", n) and len(n.split()) <= len(key.split()) + 2:
            return {"name": name, "lat": lat, "lon": lon}
    hood = _neighborhood(n)
    if hood:
        return hood
    try:
        d = get_json(
            "https://geosearch.planninglabs.nyc/v2/search", {"text": raw, "size": 1}, ttl_s=86400
        )
        f = (d.get("features") or [None])[0]
    except Exception:  # noqa: BLE001 - fall through to the regional search
        f = None
    if f:
        label = f["properties"].get("label") or ""
        lon, lat = f["geometry"]["coordinates"]
        want = set(n.split()) - {"st", "street", "ave", "avenue", "new", "york"}
        got = set(_norm(label).split())
        if want and len(want & got) >= max(1, len(want) - 1):
            return {"name": label.split(",")[0], "lat": lat, "lon": lon}
    try:
        hits = get_json(
            "https://nominatim.openstreetmap.org/search",
            {
                "q": raw,
                "format": "jsonv2",
                "limit": 1,
                "viewbox": ",".join(str(x) for x in BOX),
                "bounded": 1,
            },
            ttl_s=86400,
        )
    except Exception:  # noqa: BLE001
        hits = []
    if hits:
        h = hits[0]
        lat, lon = float(h["lat"]), float(h["lon"])
        if _inside(lat, lon):
            return {"name": h.get("name") or raw, "lat": lat, "lon": lon}
    return None


# Transit ---------------------------------------------------------------------------------------


def _decode(points: str, precision: int) -> list[list[float]]:
    """Google encoded polyline to [lon, lat] pairs."""
    coords, idx, lat, lon, f = [], 0, 0, 0, 10**precision
    while idx < len(points):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(points[idx]) - 63
                idx += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append([round(lon / f, 5), round(lat / f, 5)])
    return coords


def _hm(iso: str) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(NY)
    return t.strftime("%-I:%M %p")


_MODE = {
    "SUBWAY": "subway",
    "METRO": "subway",
    "BUS": "bus",
    "REGIONAL_RAIL": "train",
    "RAIL": "train",
    "SUBURBAN": "train",
    "FERRY": "ferry",
    "TRAM": "tram",
    "COACH": "bus",
}


def _leg_text(leg: dict, end: str) -> str:
    mode = leg.get("mode", "")
    mins = round(leg.get("duration", 0) / 60)
    if mode == "WALK":
        to = end if leg["to"]["name"] == "END" else leg["to"]["name"]
        return f"walk {mins} min to {to}" if mins >= 1 else ""
    kind = _MODE.get(mode, mode.lower())
    name = leg.get("routeShortName") or leg.get("displayName") or ""
    agency = leg.get("agencyName") or ""
    if kind == "subway":
        what = f"{name} train"
    elif kind == "bus":
        what = f"{name} bus"
    elif kind == "train":
        what = f"{agency} train" + (
            f" ({leg.get('routeLongName')})" if leg.get("routeLongName") else ""
        )
    else:
        what = f"{agency} {kind}".strip()
    stops = len(leg.get("intermediateStops") or []) + 1
    toward = f" toward {leg['headsign']}" if leg.get("headsign") else ""
    live = ", live times" if leg.get("realTime") else ""
    return (
        f"{what}{toward} from {leg['from']['name']} at {_hm(leg['startTime'])} to "
        f"{leg['to']['name']}, {stops} stop{'s' if stops > 1 else ''}, {mins} min{live}"
    )


def _plan(a: dict, b: dict, when: datetime, arrive_by: bool, n: int = 5) -> list[dict]:
    d = get_json(
        TRANSITOUS,
        {
            "fromPlace": f"{a['lat']:.5f},{a['lon']:.5f}",
            "toPlace": f"{b['lat']:.5f},{b['lon']:.5f}",
            "time": when.astimezone(NY).isoformat(timespec="minutes"),
            "arriveBy": "true" if arrive_by else "false",
            "numItineraries": n * 2,
        },
        ttl_s=120,
    )
    out, seen = [], set()

    # Best first, where each transfer counts as five extra minutes: people prefer one seat.
    def rides(it: dict) -> list[dict]:
        return [lg for lg in _tidy(it["legs"]) if lg.get("mode") != "WALK"]

    def arrival(it: dict) -> float:
        return datetime.fromisoformat(it["endTime"].replace("Z", "+00:00")).timestamp()

    def departure(it: dict) -> float:
        return datetime.fromisoformat(it["startTime"].replace("Z", "+00:00")).timestamp()

    # Arriving by a time: leave as late as possible. Otherwise arrive as early as possible.
    # Either way each ride after the first costs five minutes, since people prefer one seat.
    ranked = sorted(
        d.get("itineraries") or [],
        key=lambda it: (-departure(it) if arrive_by else arrival(it)) + 300 * len(rides(it)),
    )
    for it in ranked:
        rides_ = rides(it)
        sig = tuple(lg.get("routeShortName") or lg.get("agencyName") for lg in rides_)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(it)
    return out


def _metres(a: dict, b: dict) -> float:
    dy = (a["lat"] - b["lat"]) * 111_320
    dx = (a["lon"] - b["lon"]) * 111_320 * math.cos(math.radians(a["lat"]))
    return math.hypot(dx, dy)


def _tidy(legs: list[dict]) -> list[dict]:
    """A planner chasing the earliest arrival will ride a bus one stop to save a minute. A
    person walks it, so short hops become walking, and walks next to each other are merged."""
    out: list[dict] = []
    for lg in legs:
        hop = (
            lg.get("mode") == "BUS"
            and not lg.get("intermediateStops")
            and lg.get("duration", 0) <= 300
        )
        if hop:
            lg = {**lg, "mode": "WALK", "duration": _metres(lg["from"], lg["to"]) * 1.3 / 1.3}
        if lg.get("mode") == "WALK" and out and out[-1].get("mode") == "WALK":
            out[-1] = {**out[-1], "to": lg["to"], "duration": out[-1]["duration"] + lg["duration"]}
            continue
        out.append(lg)
    return out


def _itinerary(it: dict, end: str, extra: tuple[str, int, int] | None = None) -> dict:
    it = {**it, "legs": _tidy(it["legs"])}
    it["transfers"] = max(0, sum(lg["mode"] != "WALK" for lg in it["legs"]) - 1)
    legs = [x for x in (_leg_text(lg, end) for lg in it["legs"]) if x]
    lo = hi = round(it["duration"] / 60)
    if extra:
        legs.append(extra[0])
        lo, hi = lo + extra[1], hi + extra[2]
    walk = round(sum(lg["duration"] for lg in it["legs"] if lg["mode"] == "WALK") / 60)
    return {
        "minutes": (lo, hi),
        "leave": _hm(it["startTime"]),
        "arrive": _hm(it["endTime"]),
        "transfers": it.get("transfers", 0),
        "walk_minutes": walk,
        "steps": legs,
        "live": any(lg.get("realTime") for lg in it["legs"]),
        "lines": [
            {
                "mode": _MODE.get(lg["mode"], "walk" if lg["mode"] == "WALK" else "other"),
                "name": lg.get("routeShortName") or "",
                "color": "#" + lg["routeColor"] if lg.get("routeColor") else None,
                "coords": _decode(
                    lg["legGeometry"]["points"], lg["legGeometry"].get("precision", 6)
                )
                if lg.get("legGeometry")
                else [],
            }
            for lg in it["legs"]
        ],
    }


def _airtrain_status(at: datetime) -> str | None:
    """The AirTrain's own MTA alert, if it applies at the time of the trip."""
    from .live import MTA_ALERTS, _alert_hours, _plain

    try:
        d = get_json(MTA_ALERTS, ttl_s=120)
    except Exception:  # noqa: BLE001
        return None
    now, t = time.time(), at.timestamp()
    for ent in d.get("entity", []):
        a = ent.get("alert", {})
        head = _plain(a.get("header_text", {}).get("translation"))
        if "AirTrain" not in head:
            continue
        periods = a.get("active_period") or [{}]
        hours, _ = _alert_hours(periods, now)
        during = any(
            int(p.get("start") or 0) <= t + 3600 and (not p.get("end") or t < int(p["end"]))
            for p in periods
        )
        if during:
            return f"{head}, at the time of this trip ({hours})"
        return f"Not in effect at the time of this trip: {head} ({hours})"
    return None


def transit(a: dict, b: dict, when: datetime, arrive_by: bool) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    if a.get("airport") == "jfk" or b.get("airport") == "jfk":
        to_airport = b.get("airport") == "jfk"
        city = a if to_airport else b
        status = _airtrain_status(when)
        shuttle = bool(status and status.endswith(")") and "at the time of this trip" in status)
        options = []
        for name, lat, lon in JFK["stations"]:
            st = {"name": name, "lat": lat, "lon": lon}
            pair = (city, st) if to_airport else (st, city)
            t0 = when if to_airport else when + timedelta(minutes=AIRTRAIN["minutes"][1])
            short = name.split(" (")[0]
            leg = (
                f"free shuttle bus replacing the AirTrain between {short} and the terminals "
                "(AirTrain suspended at this time; the bus takes longer than the AirTrain's "
                f"{AIRTRAIN['minutes'][0]} to {AIRTRAIN['minutes'][1]} min)"
                if shuttle
                else f"AirTrain between {short} and the terminals, {AIRTRAIN['minutes'][0]} "
                f"to {AIRTRAIN['minutes'][1]} min, ${AIRTRAIN['fare_usd']:.2f}"
            )
            for it in _plan(*pair, t0, arrive_by, n=3)[:2]:
                options.append(_itinerary(it, short, (leg, *AIRTRAIN["minutes"])))
        notes.append(
            "The AirTrain leg is from the Port Authority: any terminal in under 20 minutes "
            "from Jamaica or Howard Beach, trains every 5 to 15 minutes, $8.75 one way."
        )
        if status:
            notes.append(f"AirTrain alert: {status}.")
        options.sort(key=lambda o: o["minutes"][1])
        return options[:4], notes
    return [_itinerary(it, b["name"]) for it in _plan(a, b, when, arrive_by)[:4]], notes


# Driving ---------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _zones() -> list[tuple[int, str, list]]:
    feats = json.loads((GEO / "taxi_zones.json").read_text())["features"]
    out = []
    for f in feats:
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        out.append((f["properties"]["id"], f["properties"]["name"], polys))
    return out


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=False):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def zone_of(lat: float, lon: float) -> tuple[int, str] | None:
    for zid, name, polys in _zones():
        for poly in polys:
            if _in_ring(lon, lat, poly[0]) and not any(_in_ring(lon, lat, h) for h in poly[1:]):
                return zid, name
    return None


_lake_con = None


def _lake():
    global _lake_con
    if _lake_con is None:
        from .. import lake
        from ..config import Settings

        s = Settings.from_env()
        _lake_con = lake.connect(s, threads=1)
    return _lake_con


def drive(a: dict, b: dict, when: datetime) -> dict | None:
    za, zb = zone_of(a["lat"], a["lon"]), zone_of(b["lat"], b["lon"])
    if not za or not zb or za[0] == zb[0]:
        return None
    day = "weekend" if when.weekday() >= 5 else "weekday"
    rows = (
        _lake()
        .execute(
            """
            select pickup_hour, trips, minutes_p25, minutes_median, minutes_p75, fare_usd_median,
                   since_month
            from gold.agg_trip_times_zone_pair
            where pickup_zone_id = ? and dropoff_zone_id = ? and day_type = ?
            """,
            [za[0], zb[0], day],
        )
        .fetchall()
    )
    if not rows:
        return None
    by_hour = {r[0]: r for r in rows}
    hour = when.hour
    r = by_hour.get(hour) or min(rows, key=lambda r: min(abs(r[0] - hour), 24 - abs(r[0] - hour)))
    busiest = max(rows, key=lambda r: r[3])
    return {
        "from_zone": za[1],
        "to_zone": zb[1],
        "day": day,
        "hour": r[0],
        "trips": r[1],
        "minutes": (r[2], r[3], r[4]),
        "fare_usd": r[5],
        "since": str(r[6])[:7],
        "slowest_hour": busiest[0],
        "slowest_minutes": busiest[3],
    }


def _hour(h: int) -> str:
    return datetime(2000, 1, 1, h).strftime("%-I %p")


# The tool --------------------------------------------------------------------------------------


def trip_plan(
    origin: str,
    destination: str,
    mode: str = "both",
    when: str | None = None,
    arrive_by: bool = False,
) -> ToolResult:
    """Real routes and times between two places in and around NYC: transit itineraries from
    the agencies' schedules (with live times where published) and typical taxi or car times
    for that hour from recent trips."""
    mode = (mode or "both").strip().lower()
    if mode not in ("both", "transit", "drive"):
        return failed("mode must be transit, drive or both")
    a, b = locate(origin), locate(destination)
    if not a or not b:
        missing = origin if not a else destination
        return failed(f"could not place {missing!r}; name a station, landmark, address or area")
    try:
        t = datetime.fromisoformat(when).replace(tzinfo=NY) if when and when != "now" else _now()
    except ValueError:
        return failed("when must be an ISO date and time in New York time, like 2026-10-16T17:30")
    if t.tzinfo is None:
        t = t.replace(tzinfo=NY)
    lines = [
        f"Trip from {a['name']} to {b['name']}, "
        + ("arriving by " if arrive_by else "leaving ")
        + f"{t:%a %b %-d %-I:%M %p}."
    ]
    data: dict = {"from": a, "to": b, "when": t.isoformat()}
    sources: list[dict] = []
    layers: list[dict] = []
    if mode in ("both", "transit"):
        try:
            options, notes = transit(a, b, t, arrive_by)
        except Exception as e:  # noqa: BLE001 - the drive half can still answer
            options, notes = [], [f"Transit planner unavailable: {str(e)[:80]}"]
        data["transit"] = options
        if options:
            lines.append("Transit options (best first):")
            for i, o in enumerate(options, 1):
                lo, hi = o["minutes"]
                span = f"{lo} min" if lo == hi else f"{lo} to {hi} min"
                lines.append(
                    f"{i}. {span} door to door, leave {o['leave']}, {o['transfers']} transfer"
                    f"{'s' if o['transfers'] != 1 else ''}, {o['walk_minutes']} min walking: "
                    + "; ".join(o["steps"])
                )
            sources.append(
                {
                    "kind": "live",
                    "title": "Transitous trip planner (agency schedules and live times)",
                    "url": "https://transitous.org",
                }
            )
            best = options[0]
            layers.append(
                {
                    "type": "route",
                    "label": f"Transit, {best['minutes'][1]} min",
                    "legs": [x for x in best["lines"] if x["coords"]],
                }
            )
        elif not notes:
            lines.append("No transit itinerary was found for that time.")
        lines += notes
        if any(x.get("airport") == "jfk" for x in (a, b)):
            sources.append(
                {"kind": "official", "title": "AirTrain JFK (Port Authority)", "url": AIRTRAIN_URL}
            )
        if re.search(
            r"\b(?:nj|new jersey|hoboken|jersey city|newark)\b", f"{origin} {destination}", re.I
        ):
            lines.append(
                "PATH trains are not in this planner; between Manhattan and Hoboken or Jersey "
                "City PATH may be quicker than the options above."
            )
    if mode in ("both", "drive"):
        try:
            d = drive(a, b, t)
        except Exception:  # noqa: BLE001
            d = None
        data["drive"] = d
        if d:
            p25, med, p75 = d["minutes"]
            lines.append(
                f"By taxi or car: usually {med} min ({p25} to {p75} min for most trips) from "
                f"{d['from_zone']} to {d['to_zone']} around {_hour(d['hour'])} on a {d['day']}, "
                f"typical fare about ${d['fare_usd']:.0f} before tip, from {d['trips']} taxi and "
                f"app trips since {d['since']}. The slowest hour is around "
                f"{_hour(d['slowest_hour'])} at about {d['slowest_minutes']} min. These are "
                "past trips, not live traffic."
            )
            sources.append(
                {
                    "kind": "lake",
                    "title": "NYC TLC trip records (typical trip times)",
                    "url": "https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page",
                }
            )
        else:
            lines.append("No typical car trip time is available for this pair of places.")
    layers.append(
        {
            "type": "points",
            "label": "Trip",
            "points": [
                {"lat": a["lat"], "lon": a["lon"], "label": a["name"]},
                {"lat": b["lat"], "lon": b["lon"], "label": b["name"]},
            ],
        }
    )
    return ToolResult(summary="\n".join(lines), data=data, sources=sources, map=layers)


TOOLS = [
    Tool(
        "trip_plan",
        "live",
        "Routes and travel times between two places in and around NYC. Transit: real "
        "itineraries from the agencies' schedules with live times where published (subway, "
        "bus, LIRR, Metro-North, NJ Transit; trips to JFK include the AirTrain leg). Car or "
        "taxi: typical time and fare for that hour from recent taxi and app trips (not live "
        "traffic). Use for every 'how do I get to', 'how long does it take', 'fastest way' or "
        "airport question; never estimate routes or times yourself.",
        {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "station, landmark, address or area"},
                "destination": {"type": "string"},
                "mode": {"type": "string", "enum": ["both", "transit", "drive"]},
                "when": {
                    "type": "string",
                    "description": "New York time like 2026-10-16T17:30, or now (default)",
                },
                "arrive_by": {"type": "boolean"},
            },
            "required": ["origin", "destination"],
        },
        trip_plan,
    ),
]
