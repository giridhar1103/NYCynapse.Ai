"""Live public sources about New York City, for questions the lake cannot answer fresh enough.

None of these need a paid key. Weather comes from Open-Meteo and the National Weather Service,
road speeds from NYC DOT's real-time feed, subway alerts from the MTA, addresses from NYC
Planning's GeoSearch, and a short allowlist of NYC Open Data datasets can be queried directly.
511NY road events are used only when an access key is configured, and are shown as NYSDOT
published them, with their update time and attribution.
"""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from . import Tool, ToolResult, failed
from .http import env, get_json

NY = ZoneInfo("America/New_York")

# Rough centres of the five boroughs, for weather by borough.
BOROUGHS = {
    "Manhattan": (40.7831, -73.9712),
    "Brooklyn": (40.6501, -73.9496),
    "Queens": (40.7282, -73.7949),
    "Bronx": (40.8448, -73.8648),
    "Staten Island": (40.5795, -74.1502),
}
NWS_ZONES = "NYZ072,NYZ073,NYZ074,NYZ075,NYZ176,NYZ178"  # the five boroughs

WMO = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "heavy showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorms",
    96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


def _now_ny() -> datetime:
    return datetime.now(NY)


def _ago(ts: datetime) -> str:
    mins = int((datetime.now(UTC) - ts.astimezone(UTC)).total_seconds() // 60)
    if mins < 2:
        return "just now"
    if mins < 90:
        return f"{mins} minutes ago"
    if mins < 48 * 60:
        return f"{mins // 60} hours ago"
    return f"{mins // 1440} days ago"


def _fmt(ts: datetime) -> str:
    return ts.astimezone(NY).strftime("%a %b %-d, %-I:%M %p")


# Weather ---------------------------------------------------------------------------------------


def weather(hours: int = 6) -> ToolResult:
    """Current weather and the next hours by borough, plus active weather alerts."""
    hours = max(1, min(int(hours), 48))
    lats = ",".join(str(v[0]) for v in BOROUGHS.values())
    lons = ",".join(str(v[1]) for v in BOROUGHS.values())
    data = get_json(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": lats,
            "longitude": lons,
            "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
            "hourly": "precipitation_probability,precipitation,weather_code",
            "forecast_hours": hours,
            "timezone": "America/New_York",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "wind_speed_unit": "mph",
        },
        ttl_s=600,
    )
    rows, chance, lines = [], {}, []
    for name, d in zip(BOROUGHS, data, strict=True):
        cur, hr = d["current"], d["hourly"]
        p_max = max(hr["precipitation_probability"] or [0])
        total = round(sum(x or 0 for x in hr["precipitation"]), 2)
        wet_hours = [
            t[11:16]
            for t, x in zip(hr["time"], hr["precipitation"], strict=True)
            if (x or 0) >= 0.01
        ]
        row = {
            "borough": name,
            "temp_f": cur["temperature_2m"],
            "now": WMO.get(cur["weather_code"], "unknown"),
            "rain_now_in": cur["precipitation"],
            "wind_mph": cur["wind_speed_10m"],
            "rain_chance_max_pct": p_max,
            "rain_next_in": total,
            "wet_hours": wet_hours,
        }
        rows.append(row)
        chance[name] = p_max
        lines.append(
            f"{name}: {row['temp_f']:.0f}F, {row['now']}, {row['rain_now_in']} in falling now; "
            f"next {hours} h: up to {p_max}% chance of rain, {total} in expected"
            + (f" (wet at {', '.join(wet_hours[:6])})" if wet_hours else "")
        )
    as_of = data[0]["current"]["time"]
    alerts = weather_alerts()
    summary = (
        f"Weather in New York City at {as_of} local time (Open-Meteo model data):\n"
        + "\n".join(lines)
        + "\n"
        + alerts.summary
    )
    return ToolResult(
        summary=summary,
        data={"boroughs": rows, "alerts": alerts.data, "as_of": as_of, "hours": hours},
        sources=[
            {
                "kind": "live",
                "title": "Open-Meteo forecast",
                "url": "https://open-meteo.com",
                "updated_at": as_of,
            },
            *alerts.sources,
        ],
        map=[
            {
                "type": "areas",
                "level": "borough",
                "values": chance,
                "unit": "%",
                "label": f"Chance of rain, next {hours} h",
                "scale": "rain",
            }
        ],
    )


def weather_alerts() -> ToolResult:
    d = get_json(
        "https://api.weather.gov/alerts/active",
        {"zone": NWS_ZONES},
        headers={"Accept": "application/geo+json"},
        ttl_s=300,
    )
    seen, items = set(), []
    for f in d.get("features", []):
        p = f["properties"]
        if p.get("id") in seen:
            continue
        seen.add(p.get("id"))
        items.append(
            {
                "event": p.get("event"),
                "headline": p.get("headline"),
                "areas": p.get("areaDesc"),
                "severity": p.get("severity"),
                "starts": p.get("onset") or p.get("effective"),
                "ends": p.get("ends") or p.get("expires"),
                "instruction": (p.get("instruction") or "")[:400],
            }
        )
    if items:
        text = "Active National Weather Service alerts: " + "; ".join(
            f"{a['event']} ({a['areas']}), until {a['ends']}" for a in items[:6]
        )
    else:
        text = "No active National Weather Service alerts for the five boroughs."
    return ToolResult(
        summary=text,
        data=items,
        sources=[
            {
                "kind": "live",
                "title": "National Weather Service alerts",
                "url": "https://www.weather.gov/okx/",
            }
        ],
    )


# Roads -----------------------------------------------------------------------------------------

DOT_SPEEDS = "https://data.cityofnewyork.us/resource/i4gi-tjb9.json"


def _socrata_headers() -> dict:
    token = env("SOCRATA_APP_TOKEN")
    return {"X-App-Token": token} if token else {}


def road_speeds(borough: str | None = None) -> ToolResult:
    """Latest NYC DOT sensor speeds on major roads, slowest first, and how fresh they are."""
    head = get_json(DOT_SPEEDS, {"$select": "max(data_as_of) AS t"}, _socrata_headers(), 60)
    if not head or not head[0].get("t"):
        return failed("the DOT speed feed returned nothing")
    latest = datetime.fromisoformat(head[0]["t"]).replace(tzinfo=NY)
    since = datetime.fromtimestamp(latest.timestamp() - 20 * 60, NY).strftime("%Y-%m-%dT%H:%M:%S")
    where = f"data_as_of >= '{since}'"  # speed is text in this dataset; filtered below
    if borough:
        b = borough.strip().title()
        if b not in BOROUGHS:
            return failed(f"unknown borough {borough}")
        where += f" AND borough = '{b}'"
    rows = get_json(
        DOT_SPEEDS,
        {
            "$select": "link_id,link_name,borough,speed,travel_time,data_as_of,link_points",
            "$where": where,
            "$order": "data_as_of DESC",
            "$limit": 2000,
        },
        _socrata_headers(),
        60,
    )
    by_link: dict[str, dict] = {}
    for r in rows:
        try:
            if float(r.get("speed") or 0) <= 0:  # 0 means no reading
                continue
        except ValueError:
            continue
        by_link.setdefault(r["link_id"], r)  # newest reading per link
    links = list(by_link.values())
    stale_min = (datetime.now(UTC) - latest.astimezone(UTC)).total_seconds() / 60
    fresh = stale_min <= 30
    by_boro: dict[str, list[float]] = {}
    for r in links:
        by_boro.setdefault(r["borough"], []).append(float(r["speed"]))
    avg = {b: round(sum(v) / len(v), 1) for b, v in by_boro.items()}
    slow = sorted(links, key=lambda r: float(r["speed"]))[:10]
    lines_map = []
    for r in sorted(links, key=lambda r: float(r["speed"]))[:60]:
        coords = []
        for pair in (r.get("link_points") or "").split():
            try:
                lat, lon = (float(x) for x in pair.split(","))
            except ValueError:
                continue
            if 40.4 < lat < 41.0 and -74.3 < lon < -73.6:
                coords.append([round(lon, 5), round(lat, 5)])
        if len(coords) >= 2:
            lines_map.append(
                {"coords": coords, "label": r["link_name"], "value": round(float(r["speed"]), 1)}
            )
    status = (
        f"Feed last updated {_fmt(latest)} ({_ago(latest)})."
        if fresh
        else f"The DOT speed feed has not updated since {_fmt(latest)} ({_ago(latest)}), so these "
        "are the last known speeds, not current ones."
    )
    summary = (
        f"NYC DOT real-time road speeds, {len(links)} road segments. {status}\n"
        + "Average speed by borough (mph): "
        + ", ".join(f"{b} {v}" for b, v in sorted(avg.items()))
        + "\nSlowest segments: "
        + "; ".join(f"{r['link_name']} {float(r['speed']):.1f} mph" for r in slow)
    )
    return ToolResult(
        summary=summary,
        data={
            "fresh": fresh,
            "updated_at": latest.isoformat(),
            "avg_by_borough": avg,
            "slowest": [{k: r[k] for k in ("link_name", "borough", "speed")} for r in slow],
        },
        sources=[
            {
                "kind": "live",
                "title": "NYC DOT real-time traffic speeds",
                "url": "https://data.cityofnewyork.us/d/i4gi-tjb9",
                "updated_at": latest.isoformat(),
                "stale": not fresh,
            }
        ],
        map=[
            {
                "type": "lines",
                "lines": lines_map,
                "unit": "mph",
                "label": "Road speed",
                "scale": "speed",
            }
        ],
    )


NYC_COUNTIES = ("new york", "kings", "queens", "bronx", "richmond")
NY511_ATTRIBUTION = "powered by 511NY"
NY511_DISCLAIMER = (
    "511NY data is provided as is by NYSDOT and may not match conditions in the field; "
    "posted restrictions take precedence."
)


def road_events(borough: str | None = None) -> ToolResult:
    """Incidents, construction and closures from 511NY, as NYSDOT published them."""
    key = env("NY511_API_KEY")
    if not key:
        return failed("511NY road events are not set up on this site yet")
    rows = get_json("https://511ny.org/api/getevents", {"key": key, "format": "json"}, ttl_s=180)
    items = []
    for e in rows:
        county = str(e.get("CountyName") or "").lower()
        if not any(c in county for c in NYC_COUNTIES):
            continue
        if borough and borough.lower() not in (str(e.get("RegionName", "")) + county).lower():
            continue
        items.append(
            {
                "type": e.get("EventType"),
                "roadway": e.get("RoadwayName"),
                "direction": e.get("DirectionOfTravel"),
                "description": e.get("Description"),  # kept word for word, as published
                "lanes": e.get("LanesAffected"),
                "full_closure": e.get("IsFullClosure"),
                "severity": e.get("Severity"),
                "last_updated": e.get("LastUpdated"),
                "lat": e.get("Latitude"),
                "lon": e.get("Longitude"),
            }
        )
    items.sort(key=lambda x: str(x.get("last_updated") or ""), reverse=True)
    summary = (
        f"{len(items)} 511NY road events in New York City ({NY511_ATTRIBUTION}).\n"
        + "\n".join(
            f'- {i["type"]}: {i["roadway"]} {i["direction"] or ""}: "{i["description"]}" '
            f"(updated {i['last_updated']})"
            for i in items[:15]
        )
    )
    return ToolResult(
        summary=summary,
        data=items[:50],
        sources=[
            {
                "kind": "live",
                "title": "511NY traffic events",
                "url": "https://511ny.org",
                "attribution": NY511_ATTRIBUTION,
                "note": NY511_DISCLAIMER,
            }
        ],
        map=[
            {
                "type": "points",
                "label": "Road events (511NY)",
                "points": [
                    {
                        "lat": i["lat"],
                        "lon": i["lon"],
                        "label": i["roadway"],
                        "detail": i["description"],
                        "updated_at": i["last_updated"],
                    }
                    for i in items[:50]
                    if i["lat"] and i["lon"]
                ],
            }
        ],
    )


# Subway ----------------------------------------------------------------------------------------

MTA_ALERTS = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fsubway-alerts.json"


def _plain(translations: list[dict]) -> str:
    for t in translations or []:
        if t.get("language") == "en":
            return re.sub(r"\s+", " ", html.unescape(t.get("text", ""))).strip()
    return ""


def subway_alerts(line: str | None = None) -> ToolResult:
    """Subway service alerts in effect right now, from the MTA."""
    d = get_json(MTA_ALERTS, ttl_s=120)
    now = time.time()
    items = []
    for ent in d.get("entity", []):
        a = ent.get("alert", {})
        periods = a.get("active_period") or [{}]
        active = any(int(p.get("start", 0)) <= now < int(p.get("end") or now + 1) for p in periods)
        if not active:
            continue
        routes = sorted(
            {e.get("route_id") for e in a.get("informed_entity", []) if e.get("route_id")}
        )
        if line and line.upper() not in routes:
            continue
        meta = a.get("transit_realtime.mercury_alert", {})
        items.append(
            {
                "routes": routes,
                "type": meta.get("alert_type"),
                "text": _plain(a.get("header_text", {}).get("translation")),
                "updated_at": datetime.fromtimestamp(
                    meta.get("updated_at") or meta.get("created_at") or now, NY
                ).isoformat(),
            }
        )
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    delays = [i for i in items if (i["type"] or "").lower() in ("delays", "severe delays")]
    summary = (
        f"{len(items)} MTA subway alerts in effect now"
        + (f" for the {line.upper()}" if line else "")
        + (f", {len(delays)} about delays" if delays else "")
        + ":\n"
        + "\n".join(
            f"- [{','.join(i['routes'])}] {i['type']}: {i['text'][:220]}"
            for i in (delays + [i for i in items if i not in delays])[:15]
        )
    )
    touched = sorted({r for i in (delays if not line else items) for r in i["routes"]})
    return ToolResult(
        summary=summary,
        data=items[:60],
        map=[
            {
                "type": "routes",
                "routes": touched,
                "label": "Lines with delays now" if not line else f"{line.upper()} line alerts",
            }
        ]
        if touched
        else [],
        sources=[
            {
                "kind": "live",
                "title": "MTA subway service alerts",
                "url": "https://new.mta.info/alerts",
                "updated_at": _now_ny().isoformat(),
            }
        ],
    )


# Places ----------------------------------------------------------------------------------------


def find_place(text: str) -> ToolResult:
    """Find an address, landmark or venue in New York City and where it is."""
    text = text.strip()[:120]
    if not text:
        return failed("nothing to look up")
    d = get_json(
        "https://geosearch.planninglabs.nyc/v2/search", {"text": text, "size": 3}, ttl_s=86400
    )
    out = []
    for f in d.get("features", []):
        p = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        out.append(
            {
                "label": p.get("label"),
                "borough": p.get("borough"),
                "neighborhood": p.get("neighbourhood"),
                "lat": lat,
                "lon": lon,
                "kind": p.get("layer"),
            }
        )
    if not out:
        return failed(f"no place in New York City matched {text!r}")
    top = out[0]
    return ToolResult(
        summary="Places found: "
        + "; ".join(
            f"{o['label']} (neighborhood {o['neighborhood']}, {o['borough']}, "
            f"{o['lat']:.5f},{o['lon']:.5f})"
            for o in out
        ),
        data=out,
        sources=[
            {
                "kind": "live",
                "title": "NYC Planning GeoSearch",
                "url": "https://geosearch.planninglabs.nyc",
            }
        ],
        map=[
            {
                "type": "points",
                "label": "Places",
                "points": [
                    {"lat": o["lat"], "lon": o["lon"], "label": o["label"]} for o in out[:1]
                ],
            },
            {"type": "focus", "lat": top["lat"], "lon": top["lon"], "zoom": 14},
        ],
    )


# NYC Open Data ---------------------------------------------------------------------------------

DATASETS = {
    "311": (
        "erm2-nwe9",
        "311 service requests, 2020 to now, updated daily",
        "created_date, closed_date, agency, complaint_type, descriptor, incident_zip, "
        "incident_address, borough, status, latitude, longitude",
    ),
    "collisions": (
        "h9gi-nx95",
        "Motor vehicle crashes reported by NYPD",
        "crash_date, crash_time, borough, zip_code, on_street_name, "
        "number_of_persons_injured, number_of_persons_killed, "
        "contributing_factor_vehicle_1, vehicle_type_code1, latitude, longitude",
    ),
    "events": (
        "tvpp-9vvx",
        "Permitted events: street fairs, parades, races, block parties",
        "event_name, start_date_time, end_date_time, event_type, event_borough, "
        "event_location, street_closure_type",
    ),
    "film_permits": (
        "tg4x-b46p",
        "Film and TV shoot permits",
        "eventtype, startdatetime, enddatetime, borough, category, "
        "subcategoryname, parkingheld, zipcode_s",
    ),
    "restaurant_inspections": (
        "43nn-pn8j",
        "Restaurant inspection results and grades",
        "dba, boro, building, street, zipcode, cuisine_description, "
        "inspection_date, grade, score, violation_description, "
        "latitude, longitude",
    ),
    "farmers_markets": (
        "8vwk-6iz2",
        "Farmers markets, days and hours",
        "marketname, borough, streetaddress, daysoperation, hoursoperations, "
        "season_begin, accepts_ebt, latitude, longitude",
    ),
}
_SOQL_BAD = re.compile(r"[;\\]|--|/\*")


def open_data(
    dataset: str,
    select: str = "",
    where: str = "",
    group: str = "",
    order: str = "",
    limit: int = 20,
) -> ToolResult:
    """Query one NYC Open Data dataset from a fixed list with SoQL clauses."""
    if dataset not in DATASETS:
        return failed(f"unknown dataset {dataset}; choose from {', '.join(DATASETS)}")
    for clause in (select, where, group, order):
        if len(clause) > 400 or _SOQL_BAD.search(clause):
            return failed("query clause not allowed")
    ds, title, _ = DATASETS[dataset]
    params = {"$limit": max(1, min(int(limit), 50))}
    for k, v in (("$select", select), ("$where", where), ("$group", group), ("$order", order)):
        if v.strip():
            params[k] = v.strip()
    rows = get_json(
        f"https://data.cityofnewyork.us/resource/{ds}.json", params, _socrata_headers(), ttl_s=300
    )
    pts = [
        {
            "lat": float(r["latitude"]),
            "lon": float(r["longitude"]),
            "label": str(next(iter(r.values())))[:60],
        }
        for r in rows
        if r.get("latitude") and r.get("longitude")
    ][:50]
    preview = "\n".join(
        " | ".join(f"{k}={str(v)[:60]}" for k, v in r.items() if k not in ("location",))
        for r in rows[:20]
    )
    return ToolResult(
        summary=f"{title}: {len(rows)} rows\n{preview}",
        data=rows,
        sources=[
            {
                "kind": "live",
                "title": f"NYC Open Data: {title}",
                "url": f"https://data.cityofnewyork.us/d/{ds}",
            }
        ],
        map=[{"type": "points", "label": title, "points": pts}] if pts else [],
    )


TOOLS = [
    Tool(
        "weather",
        "live",
        "Weather now and for the next hours in each borough (temperature, conditions, rain "
        "chance and amount) plus active National Weather Service alerts. Use for current or "
        "upcoming weather; the lake only has past weather.",
        {
            "type": "object",
            "properties": {"hours": {"type": "integer", "minimum": 1, "maximum": 48}},
        },
        weather,
    ),
    Tool(
        "road_speeds",
        "live",
        "Latest NYC DOT sensor speeds on highways and major roads, average by borough and the "
        "slowest segments, with how fresh the feed is. Use for traffic right now.",
        {"type": "object", "properties": {"borough": {"type": "string"}}},
        road_speeds,
    ),
    Tool(
        "road_events",
        "live",
        "511NY incidents, construction and closures on New York City roads right now.",
        {"type": "object", "properties": {"borough": {"type": "string"}}},
        road_events,
        enabled=lambda: bool(env("NY511_API_KEY")),
    ),
    Tool(
        "subway_alerts",
        "live",
        "MTA subway service alerts in effect right now (delays, suspensions, reroutes), "
        "optionally for one line such as A or 7.",
        {"type": "object", "properties": {"line": {"type": "string"}}},
        subway_alerts,
    ),
    Tool(
        "find_place",
        "live",
        "Find an address, landmark or venue in New York City: coordinates, neighborhood, "
        "borough. Use before asking about a specific place.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        find_place,
    ),
    Tool(
        "open_data",
        "live",
        "Query a NYC Open Data dataset directly with SoQL (select, where, group, order, limit "
        "up to 50). Datasets: "
        + "; ".join(f"{k}: {v[1]} (columns: {v[2]})" for k, v in DATASETS.items())
        + ". Use when the lake does not cover the subject or is not fresh enough.",
        {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": list(DATASETS)},
                "select": {"type": "string"},
                "where": {"type": "string"},
                "group": {"type": "string"},
                "order": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["dataset"],
        },
        open_data,
    ),
]
