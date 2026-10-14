"""Live public sources about New York City, for questions the lake cannot answer fresh enough.

None of these need a paid key. Weather comes from Open-Meteo and the National Weather Service,
road speeds from NYC DOT's real-time feed, subway alerts from the MTA, addresses from NYC
Planning's GeoSearch, and a short allowlist of NYC Open Data datasets can be queried directly.
511NY road events are used only when 511NY feed access is configured, and are shown as NYSDOT
published them, with their update time and attribution.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx

from ..config import REPO
from . import Tool, ToolResult, failed
from .http import UA, cached, env, get_json

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


def _icon(code: int | None) -> str:
    """A small set of weather pictures the page knows how to draw."""
    code = code or 0
    if code >= 95:
        return "storm"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code >= 61 or code in (80, 81, 82):
        return "rain"
    if code >= 51:
        return "drizzle"
    if code in (45, 48):
        return "fog"
    if code == 3:
        return "cloudy"
    if code in (1, 2):
        return "partly"
    return "clear"


def weather(hours: int = 6, days: int = 0) -> ToolResult:
    """Weather now, the next hours by borough, and (with `days`) a daily forecast up to a week
    ahead, plus active weather alerts."""
    hours = max(1, min(int(hours or 6), 48))
    days = max(0, min(int(days or 0), 7))
    lats = ",".join(str(v[0]) for v in BOROUGHS.values())
    lons = ",".join(str(v[1]) for v in BOROUGHS.values())
    params = {
        "latitude": lats,
        "longitude": lons,
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m,is_day",
        "hourly": "precipitation_probability,precipitation,weather_code",
        "forecast_hours": hours,
        "timezone": "America/New_York",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "wind_speed_unit": "mph",
    }
    if days:
        params["daily"] = (
            "weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum,wind_speed_10m_max"
        )
        params["forecast_days"] = days
    data = get_json("https://api.open-meteo.com/v1/forecast", params, ttl_s=600)
    rows, lines = [], []
    for name, d in zip(BOROUGHS, data, strict=True):
        cur, hr = d["current"], d["hourly"]
        p_max = max(x or 0 for x in hr["precipitation_probability"] or [0])
        total = round(sum(x or 0 for x in hr["precipitation"]), 2)
        wet_hours = [
            t[11:16]
            for t, x in zip(hr["time"], hr["precipitation"], strict=True)
            if (x or 0) >= 0.01
        ]
        row = {
            "borough": name,
            "temp_f": round(cur["temperature_2m"]),
            "now": WMO.get(cur["weather_code"], "unknown"),
            "icon": _icon(cur["weather_code"]),
            "day": bool(cur.get("is_day", 1)),
            "rain_now_in": cur["precipitation"],
            "wind_mph": round(cur["wind_speed_10m"]),
            "rain_chance_max_pct": p_max,
            "rain_next_in": total,
            "wet_hours": wet_hours,
        }
        rows.append(row)
        lines.append(
            f"{name}: {row['temp_f']}F, {row['now']}, wind {row['wind_mph']} mph, "
            f"{row['rain_now_in']} in falling now; next {hours} h: up to {p_max}% chance of "
            f"rain, {total} in expected"
            + (f" (wet at {', '.join(wet_hours[:6])})" if wet_hours else "")
        )
    as_of = data[0]["current"]["time"]
    daily = []
    if days:
        per_boro = [d["daily"] for d in data]
        city = per_boro[0]  # Manhattan, near Central Park
        for i, day in enumerate(city["time"]):
            chances = [b["precipitation_probability_max"][i] or 0 for b in per_boro]
            daily.append(
                {
                    "date": day,
                    "label": datetime.fromisoformat(day).strftime("%a %b %-d"),
                    "short": datetime.fromisoformat(day).strftime("%a"),
                    "icon": _icon(city["weather_code"][i]),
                    "sky": WMO.get(city["weather_code"][i], "unknown"),
                    "high_f": round(city["temperature_2m_max"][i]),
                    "low_f": round(city["temperature_2m_min"][i]),
                    "rain_chance_pct": max(chances),
                    "rain_chance_low_pct": min(chances),
                    "rain_in": round(city["precipitation_sum"][i] or 0, 2),
                    "wind_max_mph": round(city["wind_speed_10m_max"][i] or 0),
                }
            )
        lines.append(
            "Daily forecast (Manhattan; rain chance is the range across the five boroughs):\n"
            + "\n".join(
                f"{x['label']}: {x['sky']}, high {x['high_f']}F, low {x['low_f']}F, rain chance "
                f"{x['rain_chance_low_pct']}-{x['rain_chance_pct']}%, {x['rain_in']} in, "
                f"wind up to {x['wind_max_mph']} mph"
                for x in daily
            )
        )
    alerts = weather_alerts()
    summary = (
        f"Weather in New York City at {as_of} local time (Open-Meteo model data):\n"
        + "\n".join(lines)
        + "\n"
        + alerts.summary
    )
    raining = any(r["rain_now_in"] > 0 or r["icon"] in ("rain", "drizzle", "storm") for r in rows)
    return ToolResult(
        summary=summary,
        data={
            "boroughs": rows,
            "daily": daily,
            "alerts": alerts.data,
            "as_of": as_of,
            "hours": hours,
        },
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
                "type": "weather",
                "label": "Weather now" + (f" and the next {days} days" if days else ""),
                "as_of": as_of,
                "hours": hours,
                "boroughs": [
                    {
                        "name": r["borough"],
                        "temp_f": r["temp_f"],
                        "icon": r["icon"],
                        "day": r["day"],
                        "sky": r["now"],
                        "rain_chance_pct": r["rain_chance_max_pct"],
                    }
                    for r in rows
                ],
                "daily": daily,
                "radar": True,
                "raining": raining,
                "alerts": [a["event"] for a in alerts.data][:3],
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


_WHOLE_CITY = {"", "all", "any", "nyc", "new york", "new york city", "citywide", "city"}


def _borough(name: str | None) -> str | None:
    """A borough name, or None for the whole city; raises ValueError for anything else."""
    b = (name or "").strip().lower()
    if b in _WHOLE_CITY:
        return None
    b = {"the bronx": "bronx", "si": "staten island", "bk": "brooklyn"}.get(b, b)
    for k in BOROUGHS:
        if k.lower() == b:
            return k
    raise ValueError(f"unknown borough {name}; use one of {', '.join(BOROUGHS)} or leave it empty")


def _socrata_headers() -> dict:
    token = env("SOCRATA_APP_TOKEN")
    return {"X-App-Token": token} if token else {}


def _speeds_live() -> bool:
    """Whether NYC DOT's sensors have reported in the last hour. When they have not, the tool
    is left out, so an answer never leans on hours-old speeds."""

    def check() -> bool:
        try:
            head = get_json(DOT_SPEEDS, {"$select": "max(data_as_of) AS t"}, _socrata_headers(), 60)
            latest = datetime.fromisoformat(head[0]["t"]).replace(tzinfo=NY)
        except Exception:  # noqa: BLE001 - if the check fails, let the tool report it
            return True
        return (datetime.now(UTC) - latest.astimezone(UTC)).total_seconds() < 3600

    return cached(("speeds_live",), 300, check)


def road_speeds(borough: str | None = None) -> ToolResult:
    """Latest NYC DOT sensor speeds on major roads, slowest first, and how fresh they are."""
    head = get_json(DOT_SPEEDS, {"$select": "max(data_as_of) AS t"}, _socrata_headers(), 60)
    if not head or not head[0].get("t"):
        return failed("the DOT speed feed returned nothing")
    latest = datetime.fromisoformat(head[0]["t"]).replace(tzinfo=NY)
    since = datetime.fromtimestamp(latest.timestamp() - 20 * 60, NY).strftime("%Y-%m-%dT%H:%M:%S")
    where = f"data_as_of >= '{since}'"  # speed is text in this dataset; filtered below
    try:
        b = _borough(borough)
    except ValueError as e:
        return failed(str(e))
    if b:
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
    if stale_min > 60:
        # Hours-old speeds read as current on a map, so they are not shown at all.
        return ToolResult(
            summary=(
                "Live road speeds are not available right now (NYC DOT's sensors last reported "
                f"{_ago(latest)}). Do not mention speeds or this gap unless the reader asked "
                "how fast traffic is moving; then say in one short clause that live traffic "
                "speeds aren't available right now."
            ),
            data={"fresh": False, "updated_at": latest.isoformat()},
        )
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


NY511_EVENTS = "https://nysdot.carsprogram.org/carsapi_v1/api/events"
NY511_COUNTIES = {
    "New York": "Manhattan",
    "Kings": "Brooklyn",
    "Queens": "Queens",
    "Bronx": "Bronx",
    "Richmond": "Staten Island",
}
NY511_KINDS = {
    "closure": ("closure",),
    "roadwork": ("roadwork",),
    "incident": ("incident", "obstruction", "restriction"),
    "event": ("special-event", "sporting-event"),
}
NY511_ATTRIBUTION = "powered by 511NY"
NY511_DISCLAIMER = (
    "511NY data is provided as is by NYSDOT and may not match conditions in the field; "
    "posted restrictions take precedence."
)
_ny511: dict = {"at": 0.0, "rows": None, "busy": False}
_ny511_lock = threading.Lock()
_ny511_done = threading.Event()


def _ny511_configured() -> bool:
    return bool(env("NY511_USER") and env("NY511_PASSWORD"))


def _ny511_fetch() -> None:
    """The statewide feed is about 9 MB and takes several seconds, so only the NYC part is kept,
    refreshed in the background every 5 minutes (the polling rate 511NY asks for)."""
    try:
        r = httpx.get(
            NY511_EVENTS,
            auth=(env("NY511_USER"), env("NY511_PASSWORD")),
            headers={"User-Agent": UA},
            timeout=httpx.Timeout(45.0, connect=8.0),
        )
        r.raise_for_status()
        rows = [e for e in r.json() if set(e.get("counties") or []) & NY511_COUNTIES.keys()]
        with _ny511_lock:
            _ny511.update(at=time.time(), rows=rows)
    finally:
        with _ny511_lock:
            _ny511["busy"] = False
        _ny511_done.set()


def _ny511_events() -> tuple[list[dict], float]:
    with _ny511_lock:
        age = time.time() - _ny511["at"]
        start = (_ny511["rows"] is None or age > 300) and not _ny511["busy"]
        if start:
            _ny511["busy"] = True
            _ny511_done.clear()
    if start:
        threading.Thread(target=_ny511_fetch, daemon=True).start()
    if _ny511["rows"] is None or age > 1800:
        _ny511_done.wait(timeout=40)
    if _ny511["rows"] is None or time.time() - _ny511["at"] > 1800:
        raise RuntimeError("511NY did not answer in time")
    return _ny511["rows"], _ny511["at"]


def warm_road_events() -> None:
    if _ny511_configured():
        with suppress(Exception):
            _ny511_events()


def _ms(t: dict | None) -> float | None:
    return t["time"] / 1000 if t and t.get("time") else None


def _local(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, NY).strftime("%a %b %-d %-I:%M %p") if ts else None


def _shape(e: dict) -> dict | None:
    """The event's point, or the stretch of road it covers, thinned to keep the page light."""
    g = e.get("geometry") or {}
    coords = g.get("coordinates") or []
    if g.get("type") == "LineString" and len(coords) > 1:
        step = max(1, len(coords) // 80)
        return {"type": "LineString", "coordinates": coords[::step] + coords[-1:]}
    if g.get("type") == "Point" and len(coords) == 2:
        return {"type": "Point", "coordinates": coords}
    return None


_MAP_KIND = {"closure": "closure", "roadwork": "roadwork", "special event": "event"}
_MAP_KIND |= {"sporting event": "event", "incident": "incident", "obstruction": "incident"}


# Names people use for roads, mapped to how the 511NY text spells them.
ROAD_ALIASES = {
    "gwb": ("george washington bridge",),
    "bqe": ("bklyn/queens expressway", "brooklyn-queens expressway", "brooklyn queens"),
    "brooklyn queens": ("bklyn/queens expressway",),
    "lie": ("long island expressway",),
    "fdr": ("fdr drive", "franklin d. roosevelt"),
    "rfk": ("rfk bridge", "robert f. kennedy", "triborough"),
    "triborough": ("rfk bridge", "robert f. kennedy"),
    "verrazzano": ("verrazano",),
    "west side highway": ("ny 9a", "west side hwy"),
    "henry hudson": ("henry hudson pkwy", "henry hudson parkway"),
    "hhp": ("henry hudson",),
    "major deegan": ("i-87",),
    "cross bronx": ("i-95", "cross bronx"),
    "gowanus": ("i-278", "gowanus"),
}
_FILLER = re.compile(
    r"^(look out for|there is an? |.* work is in progress\.?$)",
    re.IGNORECASE,
)


def _road_terms(road: str) -> re.Pattern:
    q = re.sub(r"\s+", " ", road.lower().replace("expwy", "expressway")).strip()
    terms = [q]
    for alias, spelled in ROAD_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", q):
            terms.extend(spelled)
    return re.compile("|".join(rf"\b{re.escape(t)}\b" for t in terms), re.IGNORECASE)


def _digest(e: dict, lanes: list[dict]) -> str:
    """The published text without its filler sentences, plus what only the agency comment says
    (which deck of a bridge, detours) and the lane count. Nothing is reworded."""
    text, _, comment = (e.get("description") or "").partition(" Comment:")
    text = re.sub(r" \((?:near )?New York City\)", "", text)
    kept = [t for t in re.split(r"(?<=\.) (?=[A-Z])", text) if not _FILLER.match(t.strip())]
    extra = []
    # "on George Washington Bridge Lower Level westbound", not a far end of some other road.
    level = re.search(r"\b(Upper|Lower) Level ((?:east|west|north|south)bound)", comment)
    if level:
        extra.append(f"{level.group(1)} Level {level.group(2)}")
    side = re.search(r"\bto (New Jersey|New York) Side\b", comment)
    if side:
        extra.append(f"toward {side.group(1)}")
    if re.search(r"detour", comment, re.IGNORECASE):
        extra.append("detoured")
    for ln in lanes[:1]:
        hit, total = ln.get("total-affected"), ln.get("total-original")
        if hit and total:
            extra.append(f"all {total} lanes closed" if hit >= total else f"{hit} of {total} lanes")
    # The deck and real direction go first: on the GWB, I-95 "southbound" heads to New Jersey.
    return (f"[{'; '.join(extra)}] " if extra else "") + " ".join(kept)


def road_events(
    borough: str | None = None, kind: str | None = None, road: str | None = None
) -> ToolResult:
    """Closures, roadwork, incidents and planned events on NYC roads from 511NY, word for word."""
    if not _ny511_configured():
        return failed("511NY road events are not set up on this site yet")
    try:
        wanted = _borough(borough)
    except ValueError as err:
        return failed(str(err))
    kind = (kind or "").strip().lower().rstrip("s") or None
    if kind and kind not in NY511_KINDS:
        return failed(f"unknown kind {kind}; use one of {', '.join(NY511_KINDS)} or leave it empty")
    terms = _road_terms(road) if road and road.strip() else None
    rows, fetched = _ny511_events()
    now, week = time.time(), time.time() + 7 * 86400
    items = []
    for e in rows:
        cat = (e.get("headline") or {}).get("category", "")
        if cat == "nysdot-road-conditions" or (kind and cat not in NY511_KINDS[kind]):
            continue
        boroughs = [NY511_COUNTIES[c] for c in e.get("counties") or [] if c in NY511_COUNTIES]
        if wanted and wanted not in boroughs:
            continue
        if terms and not terms.search((e.get("description") or "").split(" Comment:")[0]):
            continue
        details = e.get("details") or [{}]
        start = min((_ms(d.get("start-time")) or 0 for d in details), default=0) or None
        end = max((_ms(d.get("end-time")) or 0 for d in details), default=0) or None
        if (end and end < now) or (start and start > week):
            continue
        loc = ((details[0].get("locations") or [{}])[0]) or {}
        lanes = [ln for d in details for ln in d.get("lanes") or []]
        items.append(
            {
                "id": e.get("event-id"),
                "type": cat.replace("-", " "),
                "what": (e.get("headline") or {}).get("code"),
                "roadway": loc.get("route-designator") or loc.get("landmark-name"),
                "direction": (loc.get("link-alignment") or "").lower() or None,
                "boroughs": boroughs,
                "description": e.get("description"),  # kept word for word, as published
                "digest": _digest(e, lanes),
                "status": "upcoming"
                if start and start > now
                else "scheduled"
                if any(d.get("recurrent-times") for d in details)
                else "active",
                "start": start or 0,
                "starts": _local(start),
                "ends": _local(end) if end and end - now < 5 * 365 * 86400 else None,
                "priority": e.get("priority"),
                "last_updated": _local(_ms(e.get("update-time"))),
                "shape": _shape(e),
            }
        )
    status = {"active": 0, "scheduled": 1, "upcoming": 2}
    order = {"incident": 0, "obstruction": 0, "closure": 1, "restriction": 1}
    items.sort(key=lambda i: (status[i["status"]], order.get(i["type"], 2), i["priority"] or 9))
    counts = {k: sum(i["status"] == k for i in items) for k in status}

    def line(i: dict) -> str:
        when = f" from {i['starts']}" if i["status"] == "upcoming" else ""
        where = ", ".join(i["boroughs"])
        return f"- [{i['status']}{when}] {i['type']}, {where}: {i['digest']}"

    soon = sorted((i for i in items if i["status"] == "upcoming"), key=lambda i: i["start"])
    if terms:
        # One road: what shuts it comes first, then everything else by when it starts.
        listed = sorted(items, key=lambda i: (order.get(i["type"], 2), i["start"]))[:14]
    else:
        listed = (
            [i for i in items if i["status"] == "active"][:6]
            + [i for i in items if i["status"] == "scheduled"][:3]
            + soon[:5]
        )
    head = (
        f"511NY road events in {wanted or 'New York City'}"
        f"{f' on {road.strip()}' if terms else ''}{f' ({kind})' if kind else ''}: "
        f"{counts['active']} active, {counts['scheduled']} on a recurring schedule (they apply "
        f"only on the days and hours in their text) and {counts['upcoming']} starting in the "
        f"next 7 days. Now: {_now_ny():%a %b %-d %-I:%M %p}. Feed fetched "
        f"{_ago(datetime.fromtimestamp(fetched, UTC))} ({NY511_ATTRIBUTION}). Text is as "
        "published, minus filler; brackets add the bridge deck, detours and lane counts from "
        "the agency's comment."
    )
    lines, size = [], len(head)
    for i in listed:
        ln = line(i)
        if size + len(ln) > 3700:
            lines.append(f"- {len(listed) - len(lines)} more not shown; narrow by road or kind.")
            break
        lines.append(ln)
        size += len(ln) + 1
    if not items:
        lines.append("- none found" + (" (try the road's number, like I-95)" if terms else ""))
    summary = head + "\n" + "\n".join(lines)
    # The map shows what the answer can talk about, not every closure in the city.
    shown = [i for i in listed if i["shape"]]

    def pin(i: dict) -> dict:
        title, _, rest = i["description"].partition(": ")
        what, _, _ = rest.partition(". ")
        when = {"active": "Now", "scheduled": "On set days and hours"}.get(i["status"])
        detail = i["digest"].split(". ", 2)[-1]
        return {
            "kind": _MAP_KIND.get(i["type"], "roadwork"),
            "title": title[:90],
            "what": what[:60],
            "when": when or f"From {i['starts']}",
            "detail": detail if len(detail) <= 240 else detail[:240].rsplit(" ", 1)[0] + " ...",
            "geometry": i["shape"],
        }

    pins = [pin(i) for i in shown]
    return ToolResult(
        summary=summary,
        data=[
            {k: v for k, v in i.items() if k not in ("shape", "start", "digest")}
            for i in items[:40]
        ],
        sources=[
            {
                "kind": "live",
                "title": "511NY road events (NYSDOT)",
                "url": "https://511ny.org",
                "attribution": NY511_ATTRIBUTION,
                "note": NY511_DISCLAIMER,
            }
        ],
        map=[{"type": "road_events", "label": "Road events", "events": pins}] if pins else [],
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


# Neighborhood statistics ---------------------------------------------------------------------

# American Community Survey 5-year estimates by ZIP code area, from the Census Bureau through
# Census Reporter's public API (no key). Five-year figures are averages over the period, so they
# describe a neighborhood, not this month's market.
CENSUS = "https://api.censusreporter.org/1.0/data/show/latest"
NYC_PLACE = "16000US3651000"
MEASURES = {
    "median_rent": (
        "median gross rent",
        "$ a month",
        "Median gross rent paid by renter households (rent plus utilities), including "
        "rent-stabilized and public housing; asking rents for new leases run higher. Values "
        "of 3,501 mean $3,500 or more.",
    ),
    "median_income": ("median household income", "$ a year", "Median household income."),
    "population": ("population", "people", "Total population."),
    "renter_share": ("share of homes rented", "%", "Share of occupied homes that are rented."),
}
_ZIP_AREAS: dict[str, dict] | None = None


def _zip_areas() -> dict[str, dict]:
    global _ZIP_AREAS
    if _ZIP_AREAS is None:
        geo = json.loads((REPO / "web" / "static" / "geo" / "zips.json").read_text())
        _ZIP_AREAS = {f["properties"]["name"]: f["properties"] for f in geo["features"]}
    return _ZIP_AREAS


def _acs(geo_ids: str) -> dict:
    return get_json(
        CENSUS, {"table_ids": "B25064,B19013,B01003,B25003", "geo_ids": geo_ids}, ttl_s=7 * 86400
    )


def _acs_values(d: dict) -> dict:
    def est(table: str, col: str):
        return ((d.get(table) or {}).get("estimate") or {}).get(col)

    occupied, rented = est("B25003", "B25003001"), est("B25003", "B25003003")
    return {
        "median_rent": est("B25064", "B25064001"),
        "median_income": est("B19013", "B19013001"),
        "population": est("B01003", "B01003001"),
        "renter_share": round(100 * rented / occupied, 1) if occupied and rented else None,
    }


def area_stats(
    measure: str = "median_rent", borough: str | None = None, order: str = "low", n: int = 5
) -> ToolResult:
    """Rank NYC ZIP code areas by rent, income, population or share of renters (Census ACS)."""
    if measure not in MEASURES:
        return failed(f"measure must be one of {', '.join(MEASURES)}")
    what, unit, note = MEASURES[measure]
    n = max(1, min(int(n or 5), 10))
    boro = _borough(borough)
    zips = _zip_areas()
    d = _acs(f"860|{NYC_PLACE}")
    city = _acs_values(_acs(NYC_PLACE)["data"][NYC_PLACE])
    release = d["release"]["name"]
    rows = []
    for geoid, tables in d["data"].items():
        z = geoid[-5:]
        meta = zips.get(z)
        if not meta or (boro and meta.get("borough") != boro):
            continue
        v = _acs_values(tables)
        if not v[measure] or (v["population"] or 0) < 1000:
            continue
        rows.append(
            {
                "zip": z,
                "area": meta.get("area") or z,
                "borough": meta.get("borough"),
                "value": v[measure],
                **{k: v[k] for k in MEASURES},
            }
        )
    if not rows:
        return failed("no ZIP code areas matched")
    rows.sort(key=lambda r: r["value"], reverse=order == "high")
    top = rows[:n]

    def show(x) -> str:
        if x is None:
            return "n/a"
        if measure in ("median_rent", "median_income"):
            return f"${x:,.0f}" + ("+" if measure == "median_rent" and x >= 3501 else "")
        return f"{x:,.1f}%" if measure == "renter_share" else f"{x:,.0f}"

    where = boro or "New York City"
    lines = [
        f"{i + 1}. {r['area']} (ZIP {r['zip']}, {r['borough']}): {what} {show(r['value'])}"
        + (
            f"; median rent ${r['median_rent']:,.0f}"
            if measure != "median_rent" and r["median_rent"]
            else ""
        )
        for i, r in enumerate(top)
    ]
    summary = (
        f"{'Lowest' if order != 'high' else 'Highest'} {what} by ZIP code area in {where} "
        f"({release}, {d['release']['years']}; US Census Bureau American Community Survey). "
        f"{note}\nCitywide: {what} {show(city[measure])}.\n"
        + "\n".join(lines)
        + f"\n{len(rows)} ZIP code areas compared."
    )
    return ToolResult(
        summary=summary,
        data={"measure": measure, "release": release, "city": city, "rows": rows},
        sources=[
            {
                "kind": "data",
                "title": f"US Census Bureau, {release}",
                "url": "https://censusreporter.org/tables/"
                + {
                    "median_rent": "B25064",
                    "median_income": "B19013",
                    "population": "B01003",
                    "renter_share": "B25003",
                }[measure],
                "updated_at": d["release"]["years"],
            }
        ],
        map=[
            {
                "type": "areas",
                "level": "zip",
                "values": {r["zip"]: r["value"] for r in rows},
                "unit": unit,
                "label": f"{what.capitalize()}, {d['release']['years']}",
                "scale": "value",
            },
            {
                "type": "highlight",
                "label": f"{'Lowest' if order != 'high' else 'Highest'} {what}",
                "items": [
                    {
                        "name": r["area"].split(" / ")[0],
                        "level": "zip",
                        "key": r["zip"],
                        "note": f"{show(r['value'])} {unit.replace('$ ', '')}".strip(),
                    }
                    for r in top
                ],
            },
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
        "chance and amount), a daily forecast up to 7 days ahead when `days` is set, and "
        "active National Weather Service alerts. Use for current or upcoming weather; the lake "
        "only has past weather. For 'this week', 'Friday' or 'the next 5 days' set days.",
        {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "minimum": 1, "maximum": 48},
                "days": {"type": "integer", "minimum": 0, "maximum": 7},
            },
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
        enabled=_speeds_live,
    ),
    Tool(
        "road_events",
        "live",
        "511NY closures, roadwork, incidents and planned events (parades, games, concerts) on "
        "New York City roads, active now or starting in the next 7 days.",
        {
            "type": "object",
            "properties": {
                "borough": {"type": "string"},
                "kind": {"type": "string", "enum": list(NY511_KINDS)},
                "road": {
                    "type": "string",
                    "description": "a road, bridge or tunnel, like GWB, BQE, I-278 or 5th Ave",
                },
            },
        },
        road_events,
        enabled=_ny511_configured,
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
        "area_stats",
        "live",
        "Rank New York City ZIP code areas (named by neighborhood) by Census figures: "
        "median_rent, median_income, population or renter_share, lowest or highest first, "
        "optionally within one borough. American Community Survey 5-year estimates. Use for "
        "cheapest or priciest areas to rent, richest or poorest areas, most populated.",
        {
            "type": "object",
            "properties": {
                "measure": {"type": "string", "enum": list(MEASURES)},
                "borough": {"type": "string"},
                "order": {"type": "string", "enum": ["low", "high"]},
                "n": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["measure"],
        },
        area_stats,
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
