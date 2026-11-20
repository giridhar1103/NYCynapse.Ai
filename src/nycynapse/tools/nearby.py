"""Public amenities near a place, from NYC Open Data: restrooms (with hours, access and
changing tables) and drinking fountains. The kind of thing a visitor needs right now and no
map app lists well."""

from __future__ import annotations

from . import Tool, ToolResult, failed
from .http import get_json
from .trips import _metres, locate

SOCRATA = "https://data.cityofnewyork.us/resource/{}.json"
KINDS = {
    "restroom": {
        "id": "i7jb-7jku",
        "geo": "location_1",
        "where": "status='Operational'",
        "title": "NYC public restrooms",
        "page": "https://data.cityofnewyork.us/d/i7jb-7jku",
    },
    "drinking_fountain": {
        "id": "qnv7-p7a2",
        "geo": "the_geom",
        "where": "featuresta='Active'",
        "title": "NYC Parks drinking fountains",
        "page": "https://data.cityofnewyork.us/d/qnv7-p7a2",
    },
}
RADIUS_M = 1200


def _point(row: dict, geo: str) -> tuple[float, float] | None:
    g = row.get(geo) or {}
    try:
        lon, lat = g["coordinates"][:2]
        return float(lat), float(lon)
    except (KeyError, TypeError, ValueError):
        try:
            return float(row["latitude"]), float(row["longitude"])
        except (KeyError, TypeError, ValueError):
            return None


def _restroom(row: dict) -> tuple[str, str]:
    name = row.get("facility_name") or "Public restroom"
    bits = [row.get("location_type"), row.get("operator")]
    extra = []
    if row.get("hours_of_operation"):
        extra.append("hours: " + " ".join(row["hours_of_operation"].split())[:220])
    if row.get("open") and row["open"] != "Year Round":
        extra.append(f"open {row['open'].lower()}")
    if row.get("accessibility"):
        extra.append(row["accessibility"].lower())
    if row.get("changing_stations") == "Yes":
        extra.append("baby changing table")
    head = f"{name} ({', '.join(b for b in bits if b)})" if any(bits) else name
    return head, "; ".join(extra)


def _fountain(row: dict) -> tuple[str, str]:
    place = row.get("propertyna") or "a park"
    where = (row.get("position") or "").strip()
    return f"Drinking fountain in {place}", where.lower()


def nearby(near: str, what: str = "restroom", n: int = 5) -> ToolResult:
    """The nearest public restrooms or drinking fountains to a place, closest first."""
    kind = KINDS.get((what or "").strip().lower().replace(" ", "_"))
    if not kind:
        return failed(f"unknown kind {what!r}; choose from {', '.join(KINDS)}")
    p = locate(near)
    if not p:
        return failed(f"could not place {near!r}; name a station, landmark, address or area")
    lat, lon = round(p["lat"], 4), round(p["lon"], 4)
    rows = get_json(
        SOCRATA.format(kind["id"]),
        {
            "$where": f"{kind['where']} AND within_circle({kind['geo']}, {lat}, {lon}, {RADIUS_M})",
            "$limit": 60,
        },
        ttl_s=6 * 3600,
    )
    found = []
    for row in rows or []:
        pt = _point(row, kind["geo"])
        if pt:
            found.append((_metres(p, {"lat": pt[0], "lon": pt[1]}), pt, row))
    found.sort(key=lambda x: x[0])
    found = found[: max(1, min(int(n or 5), 8))]
    label = what.replace("_", " ")
    if not found:
        return ToolResult(
            summary=f"No working public {label} is listed within {RADIUS_M / 1000:.1f} km of "
            f"{p['name']} in the city's data. Cafes, department stores and libraries are the "
            "usual fallback.",
            data={"near": p, "results": []},
            sources=[{"kind": "live", "title": kind["title"], "url": kind["page"]}],
        )
    lines = [f"Public {label}s nearest {p['name']}, closest first (city data, listed as working):"]
    points = []
    for d, (plat, plon), row in found:
        head, extra = (_restroom if what == "restroom" else _fountain)(row)
        walk = max(1, round(d * 1.3 / 80))
        lines.append(
            f"- {head}, {round(d)} m, about {walk} min walk" + (f": {extra}" if extra else "")
        )
        points.append({"lat": plat, "lon": plon, "label": head[:60], "detail": extra[:120]})
    lines.append(
        "Hours are as the city lists them; check them against the day and time the reader asked "
        "about, and say when a place may be closed then."
    )
    if what == "restroom":
        lines.append(
            "The city's list leaves out restrooms inside malls, department stores, hotels and big "
            "stations; one of those nearby is often the quickest option."
        )
    return ToolResult(
        summary="\n".join(lines),
        data={"near": p, "results": [r for _, _, r in found]},
        sources=[{"kind": "live", "title": kind["title"], "url": kind["page"]}],
        map=[{"type": "points", "label": f"Public {label}s", "points": points}],
    )


TOOLS = [
    Tool(
        "nearby",
        "live",
        "The nearest public restrooms (with hours, accessibility and baby changing tables) or "
        "drinking fountains to a place, from city data. Use for 'where can I find a bathroom', "
        "'restroom near', 'water fountain' questions.",
        {
            "type": "object",
            "properties": {
                "near": {"type": "string", "description": "station, landmark, address or area"},
                "what": {"type": "string", "enum": list(KINDS)},
                "n": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["near"],
        },
        nearby,
    )
]
