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
from .http import env, get_json

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
EWR_URL = "https://www.newarkairport.com/to-from-airport/air-train"
EWR = {
    "name": "Newark Airport",
    "lat": 40.6895,
    "lon": -74.1745,
    "station": ("Newark Liberty International Airport train station", 40.7046, -74.1906),
}
JFK["code"], EWR["code"] = "jfk", "ewr"
AIRPORT_WORDS = {
    "jfk": JFK,
    "kennedy": JFK,
    "john f kennedy": JFK,
    "ewr": EWR,
    "newark airport": EWR,
    "newark liberty": EWR,
    "newark international": EWR,
}
NAMED = {
    "lga": ("LaGuardia Airport", 40.7746, -73.8719),
    "laguardia": ("LaGuardia Airport", 40.7746, -73.8719),
    "la guardia": ("LaGuardia Airport", 40.7746, -73.8719),
    "jamaica station": ("Jamaica station (LIRR, AirTrain)", 40.6996, -73.8085),
    "atlantic terminal": ("Atlantic Terminal (LIRR)", 40.6840, -73.9776),
    "fulton center": ("Fulton Center", 40.7102, -74.0079),
    "oculus": ("World Trade Center Oculus", 40.7115, -74.0110),
    "hudson yards": ("Hudson Yards", 40.7538, -74.0021),
    "moynihan": ("Moynihan Train Hall", 40.7516, -73.9962),
    # A bare cross street means Manhattan's: 42nd Street is Times Square to Grand Central.
    "42nd street": ("42nd St, Midtown", 40.7553, -73.9844),
    "42nd st": ("42nd St, Midtown", 40.7553, -73.9844),
    "34th street": ("34th St-Herald Square", 40.7497, -73.9878),
    "34th st": ("34th St-Herald Square", 40.7497, -73.9878),
    "14th street": ("14th St-Union Square", 40.7347, -73.9904),
    "14th st": ("14th St-Union Square", 40.7347, -73.9904),
    "125th street": ("125th St, Harlem", 40.8075, -73.9454),
    "125th st": ("125th St, Harlem", 40.8075, -73.9454),
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


_ABBR = {
    "st": "street",
    "av": "avenue",
    "ave": "avenue",
    "blvd": "boulevard",
    "pl": "place",
    "rd": "road",
    "pkwy": "parkway",
    "sq": "square",
    "e": "east",
    "w": "west",
}


def _words(text: str) -> set[str]:
    text = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", text)
    return {_ABBR.get(w, w) for w in text.split()} - {
        "new",
        "york",
        "manhattan",
        "brooklyn",
        "queens",
        "bronx",
    }


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


# PATH stations by the names people use. Grove Street and Harrison also name places in New
# York, so they count only with a word that puts them in New Jersey.
PATH_NAMES = {
    "journal square": ("JSQ", False),
    "exchange place": ("EXP", False),
    "newport": ("NEW", False),
    "pavonia": ("NEW", False),
    "grove": ("GRV", True),
    "harrison": ("HAR", True),
    "newark penn": ("NWK", False),
    "hoboken path": ("HOB", False),
    "hoboken terminal": ("HOB", False),
}


def _path_station(n: str) -> dict | None:
    nj = re.search(r"\b(?:path|jersey|jc|nj)\b", n)
    for key, (code, needs_nj) in PATH_NAMES.items():
        if re.search(rf"\b{key}\b", n) and (nj or not needs_nj):
            name, lat, lon = PATH["stations"][code]
            return {"name": f"{name} PATH station", "lat": lat, "lon": lon}
    return None


# Stations and street corners -------------------------------------------------------------------

GEOCODE = "https://api.transitous.org/api/v1/geocode"
_STREET = {
    "street": "st",
    "avenue": "av",
    "ave": "av",
    "boulevard": "blvd",
    "road": "rd",
    "place": "pl",
    "parkway": "pkwy",
    "drive": "dr",
    "expressway": "expy",
}
_HINT = re.compile(
    r"\b(?:station|stop|subway|lirr|railroad|metro north|metro-north|train|terminal|"
    r"[a-z0-9] line)\b",
    re.I,
)
_DROP = {
    "station",
    "stop",
    "subway",
    "lirr",
    "railroad",
    "train",
    "the",
    "and",
    "at",
    "nyc",
    "queens",
    "brooklyn",
    "manhattan",
    "bronx",
    "staten",
    "island",
    "new",
    "york",
    "ny",
    "metro",
    "north",
    "line",
    "terminal",
    "of",
}


def _key_words(text: str) -> set[str]:
    """Words that identify a stop or street, with street types spelled one way."""
    t = re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("-", " ").replace("/", " "))
    t = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", t)
    return {_STREET.get(w, w) for w in t.split()} - _DROP


def _stop(raw: str) -> dict | None:
    """A station or stop by the name the agencies use ("Flushing-Main St", "Flushing Main
    Street LIRR", "Jamaica"), from the trip planner's own list of stops."""
    name, _, area = raw.partition(",")
    want = _key_words(name)
    if not want:
        return None
    # The area after a comma ("Metropolitan Av, Williamsburg") picks between stops with the
    # same name; it is not part of the name.
    near = _neighborhood(_norm(area)) if area.strip() else None
    try:
        hits = get_json(
            GEOCODE,
            {"text": " ".join(sorted(want)), "type": "STOP", "place": "40.73,-73.93"},
            ttl_s=86400,
        )
    except Exception:  # noqa: BLE001
        return None
    low = raw.lower()
    prefer = (
        "REGIONAL_RAIL"
        if re.search(r"lirr|railroad|metro.north|nj transit", low)
        else "BUS"
        if re.search(r"\bbus\b", low)
        else "SUBWAY"
    )
    best = None
    for h in hits or []:
        lat, lon = h.get("lat"), h.get("lon")
        if lat is None or not _inside(lat, lon):
            continue
        got = _key_words(h.get("name") or "")
        if not want <= got and not (len(want) >= 3 and len(want - got) <= 1):
            continue
        if len(got - want) > 1:  # "42nd Street" is not "28 St/42 Rd"
            continue
        modes = h.get("modes") or []
        rail = "SUBWAY" in modes or "REGIONAL_RAIL" in modes
        dist = -_metres(near, h) if near else 0
        score = (prefer in modes, rail, dist, -len(got - want))
        if best is None or score > best[0]:
            best = (score, h)
    if not best:
        return None
    h = best[1]
    lirr = "LIRR" in str(h.get("id") or "") and "LIRR" not in h["name"]
    return {"name": h["name"] + (" (LIRR)" if lirr else ""), "lat": h["lat"], "lon": h["lon"]}


def _corner(raw: str) -> dict | None:
    """Where two streets meet ("Main St and Roosevelt Ave, Flushing"): a bus stop named for
    the corner, which the agencies place right at it."""
    m = re.match(r"\s*(.+?)\s+(?:and|&|at|/)\s+(.+?)(?:,\s*(.+))?\s*$", raw, re.I)
    if not m:
        return None
    a, b = _key_words(m.group(1)), _key_words(m.group(2))
    if not a or not b or len(a) > 4 or len(b) > 4:
        return None
    try:
        hits = get_json(
            GEOCODE,
            {"text": f"{' '.join(a)}/{' '.join(b)}", "type": "STOP", "place": "40.73,-73.93"},
            ttl_s=86400,
        )
    except Exception:  # noqa: BLE001
        hits = []
    found = [
        h
        for h in hits or []
        if a <= _key_words(h.get("name") or "")
        and b <= _key_words(h.get("name") or "")
        and _inside(h.get("lat", 0), h.get("lon", 0))
    ]
    if not found:
        return None
    # The same corner exists in several boroughs (5th Ave and 42nd St is in Manhattan and in
    # Sunset Park): take the one in the area the reader named, or Manhattan.
    area = m.group(3) or raw
    boro = next(
        (
            b
            for b in ("Brooklyn", "Queens", "Bronx", "Staten Island", "Manhattan")
            if b.lower() in area.lower()
        ),
        None,
    )
    hood = _neighborhood(_norm(m.group(3))) if m.group(3) else None

    def rank(h: dict) -> tuple:
        z = zone_of(h["lat"], h["lon"])
        zb = _zone_borough(z[0]) if z else None
        near = _metres(hood, h) if hood else 0
        return (zb != (boro or "Manhattan") if not hood else 0, near)

    h = min(found, key=rank)
    name = f"{m.group(1).strip()} and {m.group(2).strip()}"
    return {"name": name, "lat": h["lat"], "lon": h["lon"]}


@lru_cache(maxsize=1)
def _landmarks() -> dict[str, dict]:
    """Well-known places with coordinates checked against OpenStreetMap, so the names people
    use most ("Union Square", "MSG", "the Met") never land somewhere else."""
    raw = json.loads((GEO / "landmarks.json").read_text())
    return {_norm(k): v for k, v in raw.items()}


def _landmark(n: str) -> dict | None:
    marks = _landmarks()
    if n in marks:
        return dict(marks[n])
    for key, v in sorted(marks.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(key)}\b", n) and len(n.split()) <= len(key.split()) + 2:
            return dict(v)
    return None


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
    low = " ".join(re.sub(r"[^a-z0-9 ]+", " ", raw.lower()).split())
    for key, ap in AIRPORT_WORDS.items():
        if re.search(rf"\b{key}\b", low):
            return {"name": ap["name"], "lat": ap["lat"], "lon": ap["lon"], "airport": ap["code"]}
    station = _path_station(n)
    if station:
        return station
    corner = _corner(raw)
    if corner:
        return corner
    # "Red Hook, Brooklyn" is Red Hook: a borough or city name after a place adds nothing.
    bare = re.sub(r"\b(?:brooklyn|queens|manhattan|bronx|staten island|new york|nyc)\b", " ", n)
    bare = " ".join(bare.split())
    if bare and bare != n:
        hood = _neighborhood(bare)
        if hood:
            return hood
    for key, (name, lat, lon) in NAMED.items():
        if n == key or re.search(rf"\b{key}\b", n) and len(n.split()) <= len(key.split()) + 2:
            return {"name": name, "lat": lat, "lon": lon}
    mark = _landmark(n)
    if mark:
        return mark
    if _HINT.search(raw) or re.search(r"[A-Za-z]-[A-Z0-9]", raw):
        stop = _stop(raw)
        if stop:
            return stop
    hood = _neighborhood(n)
    if hood:
        return hood
    # A street named without a house number is usually its station ("Metropolitan Av" on the
    # G), not one address somewhere along it.
    if re.search(
        r"\b(?:av|ave|avenue|st|street|blvd|boulevard|pkwy|parkway|rd|road)\b", n
    ) and not (re.search(r"\d+\s*-?\d*\s+\w", raw.strip()[:6]) and raw.strip()[0].isdigit()):
        stop = _stop(raw)
        if stop:
            return stop
    outside = re.search(
        r"\b(?:nj|new jersey|hoboken|jersey city|newark|weehawken|fort lee|union city|"
        r"secaucus|bayonne|yonkers|westchester|long island|nassau)\b",
        n,
    )
    f = None
    try:
        d = (
            {}
            if outside
            else get_json(
                "https://geosearch.planninglabs.nyc/v2/search",
                {"text": raw, "size": 1},
                ttl_s=86400,
            )
        )
        f = (d.get("features") or [None])[0]
    except Exception:  # noqa: BLE001 - fall through to the regional search
        f = None
    if f:
        label = f["properties"].get("label") or ""
        lon, lat = f["geometry"]["coordinates"]
        want, got = _words(n), _words(_norm(label.split(",")[0]))
        if not any(w[0].isdigit() for w in want):  # "Grove St" may come back as "2 Grove St"
            got = {w for w in got if not w[0].isdigit()}
        # The same name both ways: "Journal Square" is not "Todd Square", and "Newark" is not
        # "63B Newark Avenue". A long address may differ by one word.
        if want and (want == got or len(want) >= 3 and len(want ^ got) <= 1):
            return {"name": label.split(",")[0], "lat": lat, "lon": lon}
    stop = _stop(raw)
    if stop:
        return stop
    generic = {
        "town",
        "of",
        "city",
        "village",
        "borough",
        "township",
        "nj",
        "ny",
        "street",
        "avenue",
        "road",
        "boulevard",
        "place",
        "terminal",
        "station",
    }
    queries = [raw]
    # An intersection outside the city ("Washington & 8th, Hoboken"): the first street in that
    # town is close enough to plan from, and the name says it is approximate.
    m = re.match(r"\s*(.+?)\s+(?:&|and|at)\s+(.+?)(?:,\s*(.+))?$", raw, re.I)
    if outside and m:
        town = m.group(3) or re.sub(
            r"^.*?\b(hoboken|jersey city|newark|weehawken|fort lee|union "
            r"city|secaucus|bayonne|yonkers)\b.*$",
            r"\1",
            raw,
            flags=re.I,
        )
        queries.append(f"{m.group(1)}, {town}")
    for k, q in enumerate(queries):
        try:
            hits = get_json(
                "https://nominatim.openstreetmap.org/search",
                {
                    "q": q,
                    "format": "jsonv2",
                    "limit": 1,
                    "viewbox": ",".join(str(x) for x in BOX),
                    "bounded": 1,
                },
                ttl_s=86400,
            )
        except Exception:  # noqa: BLE001
            hits = []
        if not hits:
            continue
        h = hits[0]
        lat, lon = float(h["lat"]), float(h["lon"])
        got = _words(_norm(h.get("name") or ""))
        want = _words(_norm(q))
        # Every word asked for, and nothing more than a generic word: "Fort Lee" or "Town of
        # Huntington", not "Juniors Barber Shop" for "Juniors". Outside the city, the name found
        # may be part of what was asked ("Hoboken" for "Washington St, Hoboken").
        extra = got - want - generic
        fits = (want <= got or bool(outside)) and not extra
        if _inside(lat, lon) and fits:
            name = h.get("name") or q
            return {"name": f"near {name}" if k else name, "lat": lat, "lon": lon}
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


_AGENCY = {
    "Long Island Rail Road": "LIRR",
    "Metro-North Railroad": "Metro-North",
    "NJ TRANSIT RAIL": "NJ Transit",
    "NY Waterway": "NY Waterway",
    "NYC Ferry": "NYC Ferry",
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
    its = list(d.get("itineraries") or [])
    if its and all(it.get("transfers", 0) >= 2 for it in its):
        # Every option has two or more changes: ask for the simplest ones too.
        try:
            d2 = get_json(
                TRANSITOUS,
                {
                    "fromPlace": f"{a['lat']:.5f},{a['lon']:.5f}",
                    "toPlace": f"{b['lat']:.5f},{b['lon']:.5f}",
                    "time": when.astimezone(NY).isoformat(timespec="minutes"),
                    "arriveBy": "true" if arrive_by else "false",
                    "numItineraries": n,
                    "maxTransfers": 1,
                },
                ttl_s=120,
            )
            its += d2.get("itineraries") or []
        except Exception:  # noqa: BLE001
            pass
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
        its,
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


def _coords(lg: dict) -> list[list[float]]:
    """A leg's path: the planner's geometry when it has a real one, otherwise the stops it
    passes in order (or a straight walk), so no leg is missing from the map."""
    if "_coords" in lg:
        return lg["_coords"]
    g = lg.get("legGeometry")
    line = _decode(g["points"], g.get("precision", 6)) if g and g.get("points") else []
    stops = [lg.get("from") or {}, *(lg.get("intermediateStops") or []), lg.get("to") or {}]
    by_stop = [[round(x["lon"], 5), round(x["lat"], 5)] for x in stops if "lat" in x]
    return line if len(line) > len(by_stop) else by_stop


def _tidy(legs: list[dict]) -> list[dict]:
    """A planner chasing the earliest arrival will ride a bus one stop to save a minute. A
    person walks it, so short hops become walking, and walks next to each other are merged
    (with their paths joined, so the map shows the whole walk)."""
    out: list[dict] = []
    for lg in legs:
        lg = {**lg, "_coords": _coords(lg)}
        hop = (
            lg.get("mode") == "BUS"
            and not lg.get("intermediateStops")
            and lg.get("duration", 0) <= 300
        )
        if hop:
            lg = {**lg, "mode": "WALK", "duration": _metres(lg["from"], lg["to"]) * 1.3 / 1.3}
        if lg.get("mode") == "WALK" and out and out[-1].get("mode") == "WALK":
            out[-1] = {
                **out[-1],
                "to": lg["to"],
                "duration": out[-1]["duration"] + lg["duration"],
                "_coords": out[-1]["_coords"] + lg["_coords"],
            }
            continue
        out.append(lg)
    return out


def _itinerary(it: dict, end: str, extra: tuple[str, int, int] | None = None) -> dict:
    it = {**it, "legs": _tidy(it["legs"])}
    it["transfers"] = max(0, sum(lg["mode"] != "WALK" for lg in it["legs"]) - 1)
    legs = [x for x in (_leg_text(lg, end) for lg in it["legs"]) if x]
    # Leaving and arriving follow the walks as merged above, so "leave 9:29, walk 14 min, train
    # at 9:40" cannot happen: leave = first ride minus the walk to it.
    rides = [i for i, lg in enumerate(it["legs"]) if lg["mode"] != "WALK"]

    def ts(x: str) -> float:
        return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp()

    if rides:
        first, last = rides[0], rides[-1]
        leave_ts = ts(it["legs"][first]["startTime"]) - sum(
            lg["duration"] for lg in it["legs"][:first]
        )
        end_ts = ts(it["legs"][last]["endTime"]) + sum(
            lg["duration"] for lg in it["legs"][last + 1 :]
        )
    else:
        leave_ts, end_ts = ts(it["startTime"]), ts(it["endTime"])
    lo = hi = round((end_ts - leave_ts) / 60)
    if extra:
        legs.append(extra[0])
        lo, hi = lo + extra[1], hi + extra[2]
    walk = round(sum(lg["duration"] for lg in it["legs"] if lg["mode"] == "WALK") / 60)
    return {
        "minutes": (lo, hi),
        "arrive_ts": end_ts + 60 * (hi - lo) + 60 * (extra[1] if extra else 0),
        "leave": datetime.fromtimestamp(leave_ts, NY).strftime("%-I:%M %p"),
        "arrive": datetime.fromtimestamp(end_ts, NY).strftime("%-I:%M %p"),
        "transfers": it.get("transfers", 0),
        "walk_minutes": walk,
        "steps": legs,
        "live": any(lg.get("realTime") for lg in it["legs"]),
        "lines": [
            _line(lg, end)
            for lg in it["legs"]
            if round(lg.get("duration", 0) / 60) >= 1 or lg["mode"] != "WALK"
        ],
    }


def _line(lg: dict, end: str) -> dict:
    """One leg as the page draws and lists it: mode, line name and colors, where it boards
    and gets off, when, how long, how many stops, and its path."""
    mode = _MODE.get(lg["mode"], "walk" if lg["mode"] == "WALK" else "other")
    name = (
        lg.get("routeShortName")
        if mode in ("subway", "bus")
        else _AGENCY.get(lg.get("agencyName") or "", lg.get("agencyName") or "")
    )
    to = end if lg["to"].get("name") == "END" else lg["to"].get("name")
    start = lg["from"].get("name")
    return {
        "mode": mode,
        "name": name or "",
        "color": "#" + lg["routeColor"] if lg.get("routeColor") else None,
        "text_color": "#" + lg["routeTextColor"] if lg.get("routeTextColor") else None,
        "from": None if start == "START" else start,
        "to": to,
        "depart": _hm(lg["startTime"]) if lg.get("startTime") and mode != "walk" else None,
        "minutes": max(1, round(lg.get("duration", 0) / 60)),
        "stops": len(lg.get("intermediateStops") or []) + 1 if mode != "walk" else None,
        "headsign": lg.get("headsign"),
        "live": bool(lg.get("realTime")),
        "coords": _coords(lg),
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


@lru_cache(maxsize=1)
def _airtrain_paths() -> dict:
    """AirTrain track from each rail station to the terminals, from OpenStreetMap."""
    return json.loads((GEO / "airtrain.json").read_text())


def _add_airtrain(option: dict, key: str, to_airport: bool) -> None:
    coords = _airtrain_paths().get(key)
    if not coords:
        return
    station = key.split(":", 1)[1].replace(" station", "")
    if station == "station":
        station = "Newark Airport station"
    line = {
        "mode": "train",
        "name": "AirTrain",
        "color": "#7b5cc4",
        "text_color": "#ffffff",
        "from": station if to_airport else "Terminals",
        "to": "Terminals" if to_airport else station,
        "depart": None,
        "minutes": AIRTRAIN["minutes"][1] if key.startswith("jfk") else 11,
        "stops": None,
        "headsign": None,
        "live": False,
        "coords": coords if to_airport else coords[::-1],
    }
    if to_airport:
        option["lines"].append(line)
    else:
        option["lines"].insert(0, line)


def _newark(a: dict, b: dict, when: datetime, arrive_by: bool) -> tuple[list[dict], list[str]]:
    """Trips to and from Newark Airport go by rail to the airport's train station, then the
    AirTrain. The planner has the trains; the AirTrain leg is from the Port Authority."""
    to_airport = b.get("airport") == "ewr"
    city = a if to_airport else b
    name, lat, lon = EWR["station"]
    st = {"name": name, "lat": lat, "lon": lon}
    pair = (city, st) if to_airport else (st, city)
    t0 = when if to_airport else when + timedelta(minutes=12)
    leg = (
        "AirTrain between the train station and the terminals: about 7 min to Terminal C, "
        "11 min to Terminal B; Terminal A is about 20 min (AirTrain then a walk or shuttle)"
    )
    options = [
        _itinerary(it, name, (leg, 7, 20))
        for it in _plan(*pair, t0, arrive_by, n=4)
        if any(lg.get("mode") in ("REGIONAL_RAIL", "RAIL", "SUBURBAN") for lg in it["legs"])
    ][:3]
    for o in options:
        _add_airtrain(o, "ewr:station", to_airport)
    notes = [
        "The AirTrain leg is from the Port Authority. Its $8.75 fee is included in an NJ "
        "Transit or Amtrak ticket to the airport station; riders arriving another way pay it at "
        "the station. From 11 PM to 5 AM AirTrains run every 15 minutes."
    ]
    if not options:
        notes.append("No rail itinerary to the airport station was found for that time.")
    return options, notes


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
                o = _itinerary(it, short, (leg, *AIRTRAIN["minutes"]))
                _add_airtrain(o, f"jfk:{short}", to_airport)
                options.append(o)
        notes.append(
            "The AirTrain leg is from the Port Authority: any terminal in under 20 minutes "
            "from Jamaica or Howard Beach, trains every 5 to 15 minutes, $8.75 one way."
        )
        if status:
            notes.append(f"AirTrain alert: {status}.")
        options.sort(key=lambda o: o["minutes"][1])
        return options[:4], notes
    if a.get("airport") == "ewr" or b.get("airport") == "ewr":
        return _newark(a, b, when, arrive_by)
    options = [_itinerary(it, b["name"]) for it in _plan(a, b, when, arrive_by)[:4]]
    try:
        by_path = _path_options(a, b, when, arrive_by)
    except Exception:  # noqa: BLE001 - the planner's own options still stand
        by_path = []
    if by_path:
        notes.append(
            "PATH lines: on weekdays from about 6 AM to 11 PM all four lines run direct; from "
            "about 11 PM to 6 AM on weeknights, and on weekends outside 10 AM to 9 PM, the "
            "Journal Square-33rd Street line runs via Hoboken. These options already use the "
            "right pattern for the trip time."
        )
        notes.append(
            "PATH times marked live use PATH's own next-train times; the others use its "
            "scheduled ride times between stations plus a typical wait."
        )
    options.sort(key=lambda o: o["arrive_ts"] + 300 * o["transfers"])
    by_path.sort(key=lambda o: o["arrive_ts"] + 300 * o["transfers"])
    merged = sorted(options[:3] + by_path, key=lambda o: o["arrive_ts"] + 300 * o["transfers"])
    return merged[:4], notes


# PATH ------------------------------------------------------------------------------------------
# The trip planner does not carry PATH, so PATH trips are built here: station order and ride
# times between stations from PATH's schedule data, next trains from PATH's own live feed, and
# walking or the trip planner for the rest of the way.

PATH = json.loads((Path(__file__).with_name("path_lines.json")).read_text())
RIDEPATH = "https://www.panynj.gov/bin/portauthority/ridepath.json"
PATH_COLOR = {
    frozenset(("NWK", "WTC")): "#d93a30",
    frozenset(("HOB", "33S")): "#4d92fb",
    frozenset(("HOB", "WTC")): "#65c100",
    frozenset(("JSQ", "33S")): "#ff9900",
    frozenset(("WTC", "33S")): "#65c100",
}
NJ_SIDE = {"NWK", "HAR", "JSQ", "GRV", "NEW", "EXP", "HOB"}
WALK_MAX_M = 1500


def _station(code: str) -> dict:
    name, lat, lon = PATH["stations"][code]
    return {"name": f"{name} PATH", "lat": lat, "lon": lon, "code": code}


def _walk_min(a: dict, b: dict) -> int:
    return max(1, round(_metres(a, b) * 1.3 / 80))


def in_nj(p: dict) -> bool:
    return p["lon"] < -73.995 and zone_of(p["lat"], p["lon"]) is None


def _path_live() -> dict[str, list[tuple[str, str, int]]]:
    """Next trains at each PATH station: (destination code, headsign, seconds away)."""
    try:
        d = get_json(RIDEPATH, ttl_s=30)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list] = {}
    for r in d.get("results") or []:
        for dest in r.get("destinations") or []:
            for m in dest.get("messages") or []:
                try:
                    out.setdefault(r["consideredStation"], []).append(
                        (m["target"], m.get("headSign") or "", int(m["secondsToArrival"]))
                    )
                except (KeyError, ValueError):
                    continue
    return out


def _rides(o: str, d: str) -> list[tuple[dict, float]]:
    """Patterns that run from o to d without changing trains, with the ride time."""
    out = []
    for p in PATH["patterns"]:
        st = p["stops"]
        if o in st and d in st and st.index(o) < st.index(d):
            out.append((p, sum(p["minutes"][st.index(o) : st.index(d)])))
    return out


def _runs(p: dict, at: datetime) -> bool:
    """Whether a PATH line runs at that hour on that kind of day, from its schedule."""
    day = "weekend" if at.weekday() >= 5 else "weekday"
    return at.hour in p["hours"][day]


def _next_train(live: dict, at: str, p: dict, after_s: float) -> int | None:
    """Seconds until the first live train at a station that runs this pattern."""
    target = p["stops"][-1]
    for tgt, head, secs in sorted(live.get(at, []), key=lambda x: x[2]):
        via = "via hoboken" in head.lower()
        same = target != "33S" or via == p["via_hoboken"] or at not in ("JSQ", "GRV", "NEW")
        if tgt == target and secs >= after_s and same:
            return secs
    return None


def _path_options(a: dict, b: dict, when: datetime, arrive_by: bool) -> list[dict]:
    if not (in_nj(a) or in_nj(b)):
        return []
    live = _path_live() if abs((when - _now()).total_seconds()) < 900 and not arrive_by else {}
    starts = sorted(PATH["stations"], key=lambda c: _metres(a, _station(c)))[:3]
    ends = sorted(PATH["stations"], key=lambda c: _metres(b, _station(c)))[:3]
    options = []
    for o in starts:
        so = _station(o)
        if _metres(a, so) > WALK_MAX_M:
            continue
        for d in ends:
            sd = _station(d)
            if o == d:
                continue
            walk1 = _walk_min(a, so)
            best = None
            for p, ride in _rides(o, d):
                wait_s = None
                if not live and not _runs(p, when):
                    continue
                if live:
                    if not _next_train(live, o, p, 0) and o != p["stops"][-1]:
                        continue  # no train on that pattern is coming: it is not running now
                    secs = _next_train(live, o, p, walk1 * 60 + 60)
                    # The live list shows the next two trains; past that, use the schedule.
                    wait_s = None if secs is None else secs - walk1 * 60
                total = walk1 + (wait_s or 0) / 60 + ride
                if best is None or total < best[0]:
                    best = (total, p, ride, wait_s)
            if not best:
                continue
            _, p, ride, wait_s = best
            board = when + timedelta(minutes=walk1, seconds=wait_s or 0)
            off = board + timedelta(minutes=ride)
            stops = p["stops"][p["stops"].index(o) : p["stops"].index(d) + 1]
            head = PATH["stations"][p["stops"][-1]][0] + (
                " via Hoboken (one train, no change)" if p["via_hoboken"] else ""
            )
            n = len(stops) - 1
            steps = [
                f"walk {walk1} min to {so['name']}",
                f"PATH train toward {head} from {PATH['stations'][o][0]} "
                + (
                    f"at {board.strftime('%-I:%M %p')} (live)"
                    if wait_s is not None
                    else "(next train; departure time not known)"
                )
                + f" to {PATH['stations'][d][0]}, {n} stop{'s' if n > 1 else ''}, "
                f"{round(ride)} min",
            ]
            color = PATH_COLOR.get(frozenset((p["stops"][0], p["stops"][-1])), "#d93a30")
            lines = [
                {
                    "mode": "walk",
                    "name": "",
                    "color": None,
                    "from": None,
                    "to": so["name"],
                    "minutes": walk1,
                    "coords": [[a["lon"], a["lat"]], [so["lon"], so["lat"]]],
                },
                {
                    "mode": "train",
                    "name": "PATH",
                    "color": color,
                    "text_color": "#ffffff",
                    "from": PATH["stations"][o][0],
                    "to": PATH["stations"][d][0],
                    "depart": board.strftime("%-I:%M %p") if wait_s is not None else None,
                    "minutes": round(ride),
                    "stops": n,
                    "headsign": head,
                    "live": wait_s is not None,
                    "coords": [[PATH["stations"][c][2], PATH["stations"][c][1]] for c in stops],
                },
            ]
            # Without live times, allow for the wait: a few minutes by day, more at night.
            spare = 0 if wait_s is not None else (20 if when.hour < 6 else 10)
            if _metres(sd, b) <= WALK_MAX_M:
                walk2 = _walk_min(sd, b)
                steps.append(f"walk {walk2} min to {b['name']}")
                lines.append(
                    {
                        "mode": "walk",
                        "name": "",
                        "color": None,
                        "from": sd["name"],
                        "to": b["name"],
                        "minutes": walk2,
                        "coords": [[sd["lon"], sd["lat"]], [b["lon"], b["lat"]]],
                    }
                )
                end = off + timedelta(minutes=walk2)
            else:
                rest = _plan(sd, b, off, False, n=2)
                if not rest:
                    continue
                tail = _itinerary(rest[0], b["name"])
                steps += [x for x in tail["steps"] if not x.startswith("walk 0")]
                lines += tail["lines"]
                end = datetime.fromtimestamp(tail["arrive_ts"], NY)
            mins = round((end - when).total_seconds() / 60)
            options.append(
                {
                    "minutes": (mins, mins + spare),
                    "arrive_ts": end.timestamp() + spare * 60,
                    "leave": when.strftime("%-I:%M %p"),
                    "arrive": end.strftime("%-I:%M %p"),
                    "transfers": sum(x["mode"] != "walk" for x in lines) - 1,
                    "walk_minutes": walk1,
                    "steps": steps,
                    "live": wait_s is not None,
                    "lines": lines,
                }
            )
    options.sort(key=lambda o: o["arrive_ts"])
    seen, out = set(), []
    for o in options:
        if o["steps"][1] not in seen:
            seen.add(o["steps"][1])
            out.append(o)
    return out[:2]


def bike(a: dict, b: dict) -> dict | None:
    return direct(a, b, "BIKE")


def direct(a: dict, b: dict, how: str) -> dict | None:
    """A walk or a bike ride the whole way, on the planner's street network."""
    d = get_json(
        TRANSITOUS,
        {
            "fromPlace": f"{a['lat']:.5f},{a['lon']:.5f}",
            "toPlace": f"{b['lat']:.5f},{b['lon']:.5f}",
            "directModes": how,
            "transitModes": "WALK",
            "maxDirectTime": 7200,
        },
        ttl_s=600,
    )
    it = (d.get("direct") or [None])[0]
    if not it:
        return None
    legs = it["legs"]
    coords = [
        c
        for lg in legs
        if lg.get("legGeometry")
        for c in _decode(lg["legGeometry"]["points"], lg["legGeometry"].get("precision", 6))
    ]
    names = []
    for lg in legs:
        for step in lg.get("steps") or []:
            nm = step.get("streetName")
            if nm and nm not in names and step.get("distance", 0) > 250:
                names.append(nm)
    return {
        "minutes": round(it["duration"] / 60),
        "km": sum(lg.get("distance") or 0 for lg in legs) / 1000,
        "coords": coords,
        "streets": "mostly along " + ", ".join(names[:5]) if names else "street names not given",
    }


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


@lru_cache(maxsize=1)
def _boroughs() -> dict[int, str]:
    feats = json.loads((GEO / "taxi_zones.json").read_text())["features"]
    return {f["properties"]["id"]: f["properties"]["borough"] for f in feats}


def _zone_borough(zone_id: int) -> str | None:
    return _boroughs().get(zone_id)


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
            select ride_kind, pickup_hour, trips, minutes_p25, minutes_median, minutes_p75,
                   fare_usd_median, since_month
            from gold.agg_trip_times_zone_pair
            where pickup_zone_id = ? and dropoff_zone_id = ? and day_type = ?
            """,
            [za[0], zb[0], day],
        )
        .fetchall()
    )
    if not rows:
        return None

    def near(hrs: list[int]) -> int:
        return min(hrs, key=lambda h: min(abs(h - when.hour), 24 - abs(h - when.hour)))

    hour = near(sorted({r[1] for r in rows}))
    at = [r for r in rows if r[1] == hour]
    # Trip time from all rides at that hour, weighted by how many there were.
    n = sum(r[2] for r in at)
    minutes = tuple(round(sum(r[k] * r[2] for r in at) / n) for k in (3, 4, 5))
    fares = {r[0]: r[6] for r in at}
    by_hour: dict[int, list] = {}
    for r in rows:
        by_hour.setdefault(r[1], []).append(r)
    slowest = max(
        by_hour, key=lambda h: sum(r[4] * r[2] for r in by_hour[h]) / sum(r[2] for r in by_hour[h])
    )
    slow_rows = by_hour[slowest]
    return {
        "from_zone": za[1],
        "to_zone": zb[1],
        "day": day,
        "hour": hour,
        "trips": n,
        "minutes": minutes,
        "taxi_fare_usd": fares.get("taxi"),
        "app_fare_usd": fares.get("app"),
        "since": str(at[0][7])[:7],
        "slowest_hour": slowest,
        "slowest_minutes": round(
            sum(r[4] * r[2] for r in slow_rows) / sum(r[2] for r in slow_rows)
        ),
    }


_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_when(text: str | None) -> datetime | None:
    """A travel time from an ISO string or plain words: "now", "at 10", "tonight 11:30pm",
    "Saturday 10 PM", "tomorrow morning". The next such time from now, in New York."""
    now = _now()
    raw = (text or "now").strip().lower()
    if raw in ("", "now", "right now", "asap"):
        return now
    rel = re.search(r"\bin\s+(\d+|an|a|half an)\s*(min|minute|minutes|hr|hour|hours)\b", raw)
    if rel:
        n = {"an": 1, "a": 1, "half an": 0.5}.get(rel.group(1)) or float(rel.group(1))
        return now + timedelta(minutes=n * (60 if rel.group(2).startswith(("h", "hr")) else 1))
    try:
        t = datetime.fromisoformat(
            raw.upper().replace(" ", "T") if re.match(r"\d{4}-", raw) else "x"
        )
        return t.replace(tzinfo=NY) if t.tzinfo is None else t.astimezone(NY)
    except ValueError:
        pass
    day, named = now.date(), False
    for i, name in enumerate(_DAYS):
        if re.search(rf"\b{name[:3]}\w*\b", raw):
            day, named = day + timedelta(days=(i - now.weekday()) % 7), True
            break
    else:
        if "tomorrow" in raw:
            day = day + timedelta(days=1)
    words = {
        "noon": (12, 0),
        "midnight": (0, 0),
        "morning": (8, 0),
        "afternoon": (14, 0),
        "evening": (18, 0),
        "tonight": (20, 0),
    }
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", raw)
    if m and int(m.group(1)) <= 24:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if not ap and "tonight" in raw or not ap and "evening" in raw:
            h = h + 12 if h < 12 else h
    else:
        key = next((k for k in words if k in raw), None)
        if key is None:
            return None
        h, mi = words[key]
        ap = "x"
    t = datetime(day.year, day.month, day.day, h % 24, mi, tzinfo=NY)
    if t < now - timedelta(minutes=10):
        if not m or m.group(3) or h >= 12:
            if day == now.date():  # "at 9 PM" said at 10 PM means tomorrow, or next week
                t += timedelta(days=7 if named else 1)
        else:
            t += timedelta(hours=12)  # "at 10" said at 11 AM means 10 PM
            if t < now:
                t += timedelta(hours=12)
    return t


def car_route(a: dict, b: dict, when: datetime) -> dict | None:
    """Driving time with traffic from TomTom: live traffic for trips leaving now, TomTom's
    traffic forecast for later ones, plus the road path for the map."""
    key = env("TOMTOM_API_KEY")
    if not key:
        return None
    params = {
        "key": key,
        "traffic": "true",
        "travelMode": "car",
        "routeRepresentation": "polyline",
        "computeTravelTimeFor": "all",
    }
    later = (when - _now()).total_seconds() > 900
    if later:
        params["departAt"] = when.isoformat(timespec="seconds")
    try:
        d = get_json(
            "https://api.tomtom.com/routing/1/calculateRoute/"
            f"{a['lat']:.5f},{a['lon']:.5f}:{b['lat']:.5f},{b['lon']:.5f}/json",
            params,
            ttl_s=300,
        )
        r = d["routes"][0]
    except Exception:  # noqa: BLE001
        return None
    sm = r["summary"]
    pts = [p for lg in r["legs"] for p in lg.get("points") or []]
    step = max(1, len(pts) // 400)  # enough points for a smooth line, not a heavy one
    return {
        "minutes": round(sm["travelTimeInSeconds"] / 60),
        "delay_minutes": round((sm.get("trafficDelayInSeconds") or 0) / 60),
        "free_minutes": round((sm.get("noTrafficTravelTimeInSeconds") or 0) / 60) or None,
        "miles": sm["lengthInMeters"] / 1609.34,
        "forecast": later,
        "coords": [[round(p["longitude"], 5), round(p["latitude"], 5)] for p in pts[::step]]
        + ([[round(pts[-1]["longitude"], 5), round(pts[-1]["latitude"], 5)]] if pts else []),
    }


def _free_flow(a: dict, b: dict) -> tuple[int, float] | None:
    """Driving minutes and miles with no traffic, for trips the lake has no record of."""
    try:
        d = get_json(
            f"https://router.project-osrm.org/route/v1/driving/"
            f"{a['lon']:.5f},{a['lat']:.5f};{b['lon']:.5f},{b['lat']:.5f}",
            {"overview": "false"},
            ttl_s=86400,
        )
        r = d["routes"][0]
    except Exception:  # noqa: BLE001
        return None
    return round(r["duration"] / 60), r["distance"] / 1609.34


def _hour(h: int) -> str:
    return datetime(2000, 1, 1, h).strftime("%-I %p")


# The tool --------------------------------------------------------------------------------------


def trip_plan(
    origin: str,
    destination: str,
    mode: str = "all",
    when: str | None = None,
    arrive_by: bool = False,
) -> ToolResult:
    """Real routes and times between two places in and around NYC: transit itineraries from
    the agencies' schedules (with live times where published) and typical taxi or car times
    for that hour from recent trips."""
    mode = (mode or "both").strip().lower()
    mode = {"all": "both", "car": "drive", "walking": "walk"}.get(mode, mode)
    if mode not in ("both", "transit", "drive", "bike", "walk"):
        return failed("mode must be all, transit, drive, walk or bike")
    a, b = locate(origin), locate(destination)
    notes_placed: list[str] = []
    if not a or not b:
        missing = origin if not a else destination
        return failed(f"could not place {missing!r}; name a station, landmark, address or area")
    t = parse_when(when)
    if t is None:
        return failed("when must be a New York time like 2026-10-16T17:30, 'Saturday 10 PM' or now")
    if t.tzinfo is None:
        t = t.replace(tzinfo=NY)
    for asked, got, end in ((origin, a, "start"), (destination, b, "end")):
        # Also when a street was named without a number and one point on it was picked.
        picked = re.match(r"\d", got["name"]) and not re.search(r"\d", asked)
        if (
            not (_words(_norm(asked)) & _words(_norm(got["name"])))
            or got["name"].startswith("near")
            or picked
        ):
            notes_placed.append(
                f"The {end} '{asked}' was placed at {got['name']}. If that is not where the reader "
                "means, say so and ask for the address or a nearby station rather than treating it "
                "as theirs."
            )
    lines = [
        f"Trip from {a['name']} to {b['name']}, "
        + ("arriving by " if arrive_by else "leaving ")
        + f"{t:%a %b %-d %-I:%M %p}."
    ]
    lines += notes_placed
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
            # Name the quickest and the simplest, so a reader who wants fewer changes (luggage,
            # a stroller, stairs) is not handed the bus-bus-train option because it saves a minute.
            quick = min(options, key=lambda o: o["arrive_ts"])
            easy = min(options, key=lambda o: (o["transfers"], o["arrive_ts"]))
            for i, o in enumerate(options, 1):
                lo, hi = o["minutes"]
                span = f"{lo} min" if lo == hi else f"{lo} to {hi} min"
                tag = ", ".join(
                    t for t, x in (("arrives first", quick), ("fewest changes", easy)) if o is x
                )
                lines.append(
                    f"{i}.{f' [{tag}]' if tag else ''} {span} door to door, leave {o['leave']}, "
                    f"arrive {o.get('arrive', '?')}, {o['transfers']} transfer"
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
        elif not notes:
            lines.append("No transit itinerary was found for that time.")
        lines += notes
        if b.get("airport") or "airport" in (b.get("name") or "").lower():
            lines.append(
                "For a flight: the TSA advises being at the airport at least 2 hours before a "
                "domestic departure, and airlines advise 3 hours for international. The times "
                "above end at the terminal entrance, before check-in, security and the walk to "
                "the gate; count back from the flight with that margin."
            )
            sources.append(
                {
                    "kind": "official",
                    "title": "TSA: arrive early for your flight",
                    "url": "https://www.tsa.gov/travel/frequently-asked-questions",
                }
            )
        if any(x.get("airport") == "jfk" for x in (a, b)):
            sources.append(
                {"kind": "official", "title": "AirTrain JFK (Port Authority)", "url": AIRTRAIN_URL}
            )
        if any("PATH train" in step for o in options for step in o["steps"]):
            sources.append(
                {
                    "kind": "official",
                    "title": "PATH schedules and live train times (Port Authority)",
                    "url": "https://www.panynj.gov/path/en/schedules-maps/weekend-schedules.html",
                }
            )
        if any(x.get("airport") == "ewr" for x in (a, b)):
            sources.append(
                {"kind": "official", "title": "AirTrain Newark (Port Authority)", "url": EWR_URL}
            )
    gap = _metres(a, b)
    for how, limit, label in (("WALK", 5000, "walk"), ("BIKE", 16000, "bike")):
        if not (mode == label or (mode == "both" and gap <= limit)):
            continue
        try:
            r = direct(a, b, how)
        except Exception:  # noqa: BLE001
            r = None
        data[label] = r
        if r:
            lines.append(
                f"{'Walking' if label == 'walk' else 'By bike'}: about {r['minutes']} min for "
                f"{r['km']:.1f} km"
                + (
                    " on the route a bike planner picks (bike lanes and quieter streets where "
                    f"it can), at an easy pace; {r['streets']}."
                    if label == "bike"
                    else "."
                )
            )
        elif mode == label:
            lines.append(f"No {label} route was found between these places.")
    if mode in ("both", "drive"):
        car = car_route(a, b, t)
        data["car"] = car
        if car:
            delay = (
                f", about {car['delay_minutes']} min of it from traffic"
                if car["delay_minutes"] >= 2
                else ", traffic is light"
            )
            lines.append(
                f"By car {'(traffic forecast for that time)' if car['forecast'] else 'right now'}"
                f": about {car['minutes']} min for {car['miles']:.0f} miles{delay} (TomTom)."
            )
            sources.append(
                {
                    "kind": "live",
                    "title": "TomTom traffic and routing",
                    "url": "https://www.tomtom.com",
                }
            )
        try:
            d = drive(a, b, t)
        except Exception:  # noqa: BLE001
            d = None
        data["drive"] = d
        if d:
            p25, med, p75 = d["minutes"]
            fares = "; ".join(
                f"{label} about ${d[k]:.0f}"
                for label, k in (
                    ("yellow or green taxi", "taxi_fare_usd"),
                    ("Uber or Lyft", "app_fare_usd"),
                )
                if d.get(k)
            )
            lines.append(
                f"By taxi or car: usually {med} min ({p25} to {p75} min for most trips) from "
                f"{d['from_zone']} to {d['to_zone']} around {_hour(d['hour'])} on a {d['day']}, "
                f"from {d['trips']} taxi and app trips since {d['since']}. Typical paid before "
                f"tip, with tolls and fees: {fares or 'not known'}. The slowest hour is around "
                f"{_hour(d['slowest_hour'])} at about {d['slowest_minutes']} min. These are past "
                "trips; use them for fares and for what is normal at that hour."
            )
            sources.append(
                {
                    "kind": "lake",
                    "title": "NYC TLC trip records (typical trip times)",
                    "url": "https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page",
                }
            )
        elif car:
            lines.append(
                "No record of past taxi trips covers this pair, so there is no typical fare."
            )
        else:
            free = _free_flow(a, b)
            if free:
                lines.append(
                    f"By car: at least {free[0]} min for {free[1]:.0f} miles with no traffic "
                    "(OpenStreetMap routing). That is a floor, not a typical time: no record of "
                    "past trips covers this pair, and rush hour adds more."
                )
                sources.append(
                    {
                        "kind": "live",
                        "title": "OSRM routing on OpenStreetMap",
                        "url": "https://project-osrm.org",
                    }
                )
            else:
                lines.append("No car trip time is available for this pair of places.")
    layers.append(_trip_layer(a, b, t, arrive_by, mode, data))
    return ToolResult(summary="\n".join(lines), data=data, sources=sources, map=layers)


def _trip_layer(a: dict, b: dict, t: datetime, arrive_by: bool, mode: str, data: dict) -> dict:
    """Everything the page needs to show the trip the way map apps do: a switch between
    transit, car, walking and bike with each one's time, route cards for transit, and every
    path drawn from start to finish."""
    modes: dict = {}
    options = data.get("transit") or []
    if options:
        quick = min(options, key=lambda o: o["arrive_ts"])
        easy = min(options, key=lambda o: (o["transfers"], o["arrive_ts"]))
        modes["transit"] = {
            "minutes": options[0]["minutes"][0],
            "options": [
                {
                    "minutes": list(o["minutes"]),
                    "leave": o["leave"],
                    "arrive": o.get("arrive"),
                    "transfers": o["transfers"],
                    "walk_minutes": o["walk_minutes"],
                    "live": o.get("live", False),
                    "tags": [
                        t for t, x in (("Arrives first", quick), ("Fewest changes", easy)) if o is x
                    ],
                    "legs": [x for x in o["lines"] if x.get("coords")],
                }
                for o in options
            ],
        }
    car, typical = data.get("car"), data.get("drive")
    if car or typical:
        modes["drive"] = {
            "minutes": car["minutes"] if car else typical["minutes"][1],
            "live": bool(car) and not car.get("forecast"),
            "delay": car["delay_minutes"] if car else None,
            "miles": round(car["miles"], 1) if car else None,
            "coords": car["coords"] if car else [],
            "typical": typical["minutes"][1] if typical else None,
            "taxi_fare": typical.get("taxi_fare_usd") if typical else None,
            "app_fare": typical.get("app_fare_usd") if typical else None,
        }
    for label in ("walk", "bike"):
        r = data.get(label)
        if r:
            modes[label] = {"minutes": r["minutes"], "km": round(r["km"], 1), "coords": r["coords"]}
    default = mode if mode in modes else next(iter(modes), None)
    return {
        "type": "trip",
        "label": f"{a['name']} to {b['name']}",
        "from": {"name": a["name"], "lat": a["lat"], "lon": a["lon"]},
        "to": {"name": b["name"], "lat": b["lat"], "lon": b["lon"]},
        "when": ("Arrive by " if arrive_by else "Leave ") + t.strftime("%a %-I:%M %p"),
        "modes": modes,
        "default": default,
    }


TOOLS = [
    Tool(
        "trip_plan",
        "live",
        "Routes and travel times between two places in and around NYC. Transit: real "
        "itineraries from the agencies' schedules with live times where published (subway, "
        "bus, LIRR, Metro-North, NJ Transit; trips to JFK include the AirTrain leg). Car or "
        "taxi: typical time and fare for that hour from recent taxi and app trips (not live "
        "traffic). Use for every 'how do I get to', 'how long does it take', 'fastest way' or "
        "airport question, and with mode bike for bike rides; never estimate routes or times "
        "yourself. Set `when` to the time the reader will travel.",
        {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "description": "station, landmark, address or area"},
                "destination": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["all", "transit", "drive", "walk", "bike"],
                    "description": "all (default) gives every way at once, for the reader to "
                    "switch between",
                },
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


# Citi Bike right now -------------------------------------------------------------------------

GBFS = "https://gbfs.lyft.com/gbfs/1.1/bkn/en"


def citibike_now(near: str, n: int = 5) -> ToolResult:
    """Bikes, e-bikes and open docks at the Citi Bike stations nearest a place, live from
    Citi Bike's own feed."""
    p = locate(near)
    if not p:
        return failed(f"could not place {near!r}; name a station, landmark, address or area")
    try:
        info = get_json(f"{GBFS}/station_information.json", ttl_s=3600)["data"]["stations"]
        status = get_json(f"{GBFS}/station_status.json", ttl_s=60)
    except Exception as e:  # noqa: BLE001
        return failed(f"Citi Bike's live feed did not answer: {str(e)[:80]}")
    live = {s["station_id"]: s for s in status["data"]["stations"]}
    rows = []
    for st in info:
        s = live.get(st["station_id"])
        if not s or not s.get("is_installed"):
            continue
        d = _metres(p, {"lat": st["lat"], "lon": st["lon"]})
        if d > 1200:
            continue
        rows.append((d, st, s))
    rows.sort(key=lambda r: r[0])
    rows = rows[: max(1, min(int(n or 5), 8))]
    if not rows:
        return failed(f"no Citi Bike station within 1.2 km of {p['name']}")
    asof = datetime.fromtimestamp(status.get("last_updated") or time.time(), NY)
    lines = [f"Citi Bike right now near {p['name']} (live, {asof:%-I:%M %p}):"]
    points = []
    for d, st, s in rows:
        bikes, ebikes = s.get("num_bikes_available", 0), s.get("num_ebikes_available", 0)
        docks = s.get("num_docks_available", 0)
        renting = "" if s.get("is_renting") else " (not renting right now)"
        lines.append(
            f"- {st['name']}, {round(d * 1.3 / 80)} min walk: {bikes} bikes ({ebikes} e-bikes), "
            f"{docks} open docks{renting}"
        )
        points.append(
            {
                "lat": st["lat"],
                "lon": st["lon"],
                "label": st["name"],
                "value": bikes,
                "detail": f"{bikes} bikes, {ebikes} e-bikes, {docks} docks",
            }
        )
    return ToolResult(
        summary="\n".join(lines),
        data=[{"name": st["name"], **s} for _, st, s in rows],
        sources=[
            {
                "kind": "live",
                "title": "Citi Bike live station status",
                "url": "https://citibikenyc.com/system-data",
                "updated_at": asof.isoformat(),
            }
        ],
        map=[{"type": "points", "label": "Citi Bike now", "unit": "bikes", "points": points}],
    )


TOOLS.append(
    Tool(
        "citibike_now",
        "live",
        "Bikes, e-bikes and open docks at the Citi Bike stations nearest a place, live. Use "
        "for any 'is there a bike' or 'can I dock' question about now; the lake's Citi Bike "
        "data is history.",
        {
            "type": "object",
            "properties": {
                "near": {"type": "string", "description": "station, landmark, address or area"},
                "n": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["near"],
        },
        citibike_now,
    )
)
