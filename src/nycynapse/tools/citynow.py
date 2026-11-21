"""Two current-conditions tools: street closures for construction (NYC DOT permits, now and
coming up) near a place, and air quality now and for the day (Open-Meteo, free and keyless)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import Tool, ToolResult, failed
from .http import get_json

NY = ZoneInfo("America/New_York")
CLOSURES = "https://data.cityofnewyork.us/resource/i6b5-j7bu.json"
AIR = "https://air-quality-api.open-meteo.com/v1/air-quality"
BOROUGH = {
    "M": "Manhattan",
    "X": "Bronx",
    "B": "Brooklyn",
    "K": "Brooklyn",
    "Q": "Queens",
    "S": "Staten Island",
}


def _street(s: str) -> str:
    return " ".join((s or "").split()).title()


def street_closures(near: str, radius_m: int = 500, days_ahead: int = 7) -> ToolResult:
    """Streets closed for construction near a place, in effect now or starting within
    `days_ahead` days, from NYC DOT's street closure permits."""
    from .trips import _metres, locate

    p = locate(near)
    if not p:
        return failed(f"could not place {near!r}; name a station, landmark, address or area")
    radius_m = max(100, min(int(radius_m or 500), 2000))
    days_ahead = max(0, min(int(days_ahead or 7), 60))
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(p["lat"])))
    box = (
        f"POLYGON(({p['lon'] - dlon} {p['lat'] - dlat}, {p['lon'] + dlon} {p['lat'] - dlat}, "
        f"{p['lon'] + dlon} {p['lat'] + dlat}, {p['lon'] - dlon} {p['lat'] + dlat}, "
        f"{p['lon'] - dlon} {p['lat'] - dlat}))"
    )
    today = datetime.now(NY).date()
    until = today + timedelta(days=days_ahead)
    rows = get_json(
        CLOSURES,
        {
            "$where": f"intersects(the_geom, '{box}') AND work_end_date >= '{today}' "
            f"AND work_start_date <= '{until}T23:59:59'",
            "$limit": 200,
            "$order": "work_start_date",
        },
        ttl_s=1800,
    )
    seen, found = set(), []
    for r in rows or []:
        key = (r.get("onstreetname"), r.get("fromstreetname"), r.get("tostreetname"))
        if key in seen:
            continue
        seen.add(key)
        coords = [c for line in (r.get("the_geom") or {}).get("coordinates", []) for c in line]
        if not coords:
            continue
        mid = coords[len(coords) // 2]
        d = _metres(p, {"lat": mid[1], "lon": mid[0]})
        found.append((d, r, mid))
    found.sort(key=lambda x: x[0])
    # One job often closes a street block by block: show it once, with how many blocks.
    groups: dict = {}
    for d, r, mid in found:
        k = (
            r.get("onstreetname"),
            r.get("work_start_date"),
            r.get("work_end_date"),
            r.get("purpose"),
        )
        groups.setdefault(k, []).append((d, r, mid))
    found = [(g[0][0], {**g[0][1], "_blocks": len(g)}, g[0][2]) for g in groups.values()][:10]
    head = (
        f"Streets closed for construction within {radius_m} m of {p['name']}, now or starting "
        f"by {until:%a %b %-d} (NYC DOT permits)"
    )
    if not found:
        return ToolResult(
            summary=head + ": none.",
            data={"near": p, "closures": []},
            sources=[
                {
                    "kind": "live",
                    "title": "NYC DOT street closures for construction",
                    "url": "https://data.cityofnewyork.us/d/i6b5-j7bu",
                }
            ],
        )
    lines, points = [head + ":"], []
    for d, r, mid in found:
        start = (r.get("work_start_date") or "")[:10]
        end = (r.get("work_end_date") or "")[:10]
        now = start <= str(today)
        when = f"closed now until {end}" if now else f"from {start} to {end}"
        n = r.get("_blocks", 1)
        span = (
            f"{n} blocks, the nearest between {_street(r.get('fromstreetname'))} and "
            f"{_street(r.get('tostreetname'))}"
            if n > 1
            else f"between {_street(r.get('fromstreetname'))} and {_street(r.get('tostreetname'))}"
        )
        lines.append(
            f"- {_street(r.get('onstreetname'))}, {span}, {round(d)} m away: {when}"
            + (f" ({r['purpose'].strip().lower()})" if r.get("purpose") else "")
        )
        points.append(
            {"lat": mid[1], "lon": mid[0], "label": _street(r.get("onstreetname")), "detail": when}
        )
    lines.append("Permits say when work may happen; crews do not always use every day of one.")
    return ToolResult(
        summary="\n".join(lines),
        data={"near": p, "closures": [r for _, r, _ in found]},
        sources=[
            {
                "kind": "live",
                "title": "NYC DOT street closures for construction",
                "url": "https://data.cityofnewyork.us/d/i6b5-j7bu",
            }
        ],
        map=[{"type": "points", "label": "Construction closures", "points": points}],
    )


AQI_WORDS = [
    (50, "good"),
    (100, "moderate (unusually sensitive people may want to take it easier outdoors)"),
    (
        150,
        "unhealthy for sensitive groups (people with asthma, heart or lung conditions, "
        "children and older adults should cut back on long or hard exertion outdoors)",
    ),
    (200, "unhealthy (everyone should cut back on long or hard exertion outdoors)"),
    (300, "very unhealthy (avoid long or hard exertion outdoors)"),
    (10_000, "hazardous (stay indoors)"),
]


def _aqi_word(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "unknown"
    return next(w for top, w in AQI_WORDS if v <= top)


def air_quality(near: str | None = None) -> ToolResult:
    """Air quality now and through today: the US AQI with what it means for being outside, and
    the fine particles and ozone behind it."""
    lat, lon, name = 40.7128, -74.0060, "New York City"
    if near:
        from .trips import locate

        p = locate(near)
        if not p:
            return failed(f"could not place {near!r}")
        lat, lon, name = p["lat"], p["lon"], p["name"]
    d = get_json(
        AIR,
        {
            "latitude": round(lat, 3),
            "longitude": round(lon, 3),
            "current": "us_aqi,pm2_5,ozone",
            "hourly": "us_aqi",
            "forecast_days": 2,
            "timezone": "America/New_York",
        },
        ttl_s=900,
    )
    cur = d.get("current") or {}
    hours = d.get("hourly") or {}
    now = datetime.now(NY)
    ahead = [
        (datetime.fromisoformat(t).replace(tzinfo=NY), v)
        for t, v in zip(hours.get("time") or [], hours.get("us_aqi") or [], strict=False)
        if v is not None
    ]
    ahead = [(t, v) for t, v in ahead if now <= t <= now + timedelta(hours=18)]
    aqi = cur.get("us_aqi")
    lines = [
        f"Air quality near {name} at {now:%-I:%M %p}: US AQI {aqi}, {_aqi_word(aqi)}. "
        f"Fine particles (PM2.5) {cur.get('pm2_5')} µg/m³, ozone {cur.get('ozone')} µg/m³."
    ]
    if ahead:
        hi = max(ahead, key=lambda x: x[1])
        lo = min(ahead, key=lambda x: x[1])
        lines.append(
            f"Next 18 hours: highest AQI {hi[1]} around {hi[0]:%-I %p} "
            f"({_aqi_word(hi[1]).split(' (')[0]}), "
            f"lowest {lo[1]} around {lo[0]:%-I %p}."
        )
    lines.append(
        "These are model estimates from Open-Meteo (CAMS), not a reading from a sensor on the "
        "block."
    )
    return ToolResult(
        summary="\n".join(lines),
        data={"near": name, "current": cur, "next": [(t.isoformat(), v) for t, v in ahead]},
        sources=[
            {
                "kind": "live",
                "title": "Open-Meteo air quality (CAMS)",
                "url": "https://open-meteo.com/en/docs/air-quality-api",
            }
        ],
    )


TOOLS = [
    Tool(
        "street_closures",
        "live",
        "Streets closed for construction near a place, now or starting in the next days, from "
        "NYC DOT permits. Use for 'is my street being dug up', 'construction near', 'why is "
        "this block closed' and for planning a drive or a move.",
        {
            "type": "object",
            "properties": {
                "near": {"type": "string", "description": "address, corner, landmark or area"},
                "radius_m": {"type": "integer", "minimum": 100, "maximum": 2000},
                "days_ahead": {"type": "integer", "minimum": 0, "maximum": 60},
            },
            "required": ["near"],
        },
        street_closures,
    ),
    Tool(
        "air_quality",
        "live",
        "Air quality now and for the next hours (US AQI with what it means outdoors, PM2.5, "
        "ozone), citywide or near a place. Use for 'is the air OK for a run', smoke, haze, "
        "asthma questions.",
        {"type": "object", "properties": {"near": {"type": "string"}}},
        air_quality,
    ),
]
