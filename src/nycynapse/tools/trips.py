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
from datetime import date, datetime, timedelta
from contextvars import ContextVar
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
LGA_TERMINALS = {"A": (40.7728, -73.8862), "B": (40.7729, -73.8725), "C": (40.7703, -73.8645)}
# JFK's AirTrain stops at each terminal, from OpenStreetMap.
JFK["terminals"] = {
    "1": (40.64307, -73.78907), "4": (40.64392, -73.78241), "5": (40.64681, -73.77986),
    "7": (40.64823, -73.78319), "8": (40.64675, -73.78886),
}
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
    "jamaica center": ("Jamaica Center-Parsons/Archer", 40.7022, -73.8010),
    "bed stuy": ("Bedford-Stuyvesant", 40.6872, -73.9418),
    "bedstuy": ("Bedford-Stuyvesant", 40.6872, -73.9418),
    # Said alone, Grand Army Plaza is Brooklyn's, at the top of Prospect Park.
    "grand army plaza": ("Grand Army Plaza, Brooklyn", 40.6744, -73.9701),
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
    "centre": "center",
}


def _words(text: str) -> set[str]:
    text = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", text)
    text = re.sub(r"\bnew york\b", " ", text)  # "New Haven" keeps its "new"
    return {_ABBR.get(w, w) for w in text.split()} - {
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
    # Manhattan PATH stations only when PATH is named: "World Trade Center" alone is the tower.
    "world trade center path": ("WTC", False),
    "wtc path": ("WTC", False),
    "33rd street path": ("33S", False),
    "33 street path": ("33S", False),
    "christopher street path": ("CHR", False),
}


def _path_station(n: str) -> dict | None:
    nj = re.search(r"\b(?:path|jersey|jc|nj)\b", n)
    for key, (code, needs_nj) in PATH_NAMES.items():
        # "Red Bull Arena, Harrison, NJ" is the arena, not Harrison's PATH station.
        rest = set(re.sub(rf"\b{key}\b", " ", n).split()) - {
            "path", "station", "nj", "jersey", "city", "jc", "new", "the", "train", "street",
            "st", "terminal", "square", "sq",
        }
        if re.search(rf"\b{key}\b", n) and (nj or not needs_nj) and not rest:
            name, lat, lon = PATH["stations"][code]
            if code == "NWK":  # also the NJ Transit and Amtrak station
                return {"name": "Newark Penn Station", "lat": lat, "lon": lon}
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
    near = None
    for part in [p for p in area.split(",") if p.strip()]:
        near = near or _neighborhood(_norm(part))
    boro = _borough_named(area)
    if area.strip() and not near and not boro:
        return None  # a town outside the city ("Bloomfield Ave, Montclair"): not a city stop
    if not near and boro:
        lat, lon = _BORO_CENTER[boro]
        near = {"lat": lat, "lon": lon}
    try:
        hits = get_json(
            GEOCODE,
            {"text": " ".join(sorted(want)), "type": "STOP", "place": _bias(raw)},
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
        if {w for w in want if w.isdigit()} - got:  # Beach 108 St is not Beach 105 St
            continue
        if len(got - want) > 1:  # "42nd Street" is not "28 St/42 Rd"
            continue
        if len(want) == 1 and got - want - _DROP:  # "Montclair" is not "Upper Montclair"
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


# What people call an avenue, as the agencies name it on stops.
_NICK = {"lex": "lexington"}


_BORO_CENTER = {
    "brooklyn": (40.65, -73.95),
    "queens": (40.72, -73.82),
    "bronx": (40.84, -73.88),
    "staten island": (40.58, -74.14),
    "manhattan": (40.77, -73.97),
}
# Neighborhoods people name instead of the borough.
_BORO_OF = {"sunset park": "brooklyn", "bay ridge": "brooklyn", "park slope": "brooklyn",
            "williamsburg": "brooklyn", "bushwick": "brooklyn", "flatbush": "brooklyn",
            "astoria": "queens", "flushing": "queens", "jamaica": "queens",
            "harlem": "manhattan", "riverdale": "bronx"}


def _borough_named(text: str) -> str | None:
    low = text.lower()
    for b in _BORO_CENTER:
        if re.search(rf"\b{b}\b", low):
            return b
    return next((b for k, b in _BORO_OF.items() if re.search(rf"\b{k}\b", low)), None)


def _bias(text: str) -> str:
    """Where the stop search should look first: the borough the reader named, or the city."""
    lat, lon = _BORO_CENTER.get(_borough_named(text) or "", (40.73, -73.93))
    return f"{lat},{lon}"


def _said(words: set[str]) -> str:
    """Key words back in the order a street name is said: numbers and names, then the type."""
    kinds = set(_STREET.values())
    return " ".join(sorted(words, key=lambda w: (w in kinds, not w.isdigit(), w)))


def _corner(raw: str) -> dict | None:
    """Where two streets meet ("Main St and Roosevelt Ave, Flushing"): a bus stop named for
    the corner, which the agencies place right at it."""
    m = re.match(r"\s*(.+?)\s+(?:and|&|at|/)\s+(.+?)(?:(?:,|\s+in)\s+(.+))?\s*$", raw, re.I)
    if not m:
        return None
    a, b = _key_words(m.group(1)), _key_words(m.group(2))
    a, b = ({_NICK.get(w, w) for w in x} for x in (a, b))
    # "14th and 8th": in Manhattan the small number is the avenue and the other the street.
    if len(a) == len(b) == 1 and all(w.isdigit() for w in a | b):
        x, y = int(next(iter(a))), int(next(iter(b)))
        if (x <= 12) != (y <= 12):
            a, b = ({str(n), "av" if n <= 12 else "st"} for n in (x, y))
    if not a or not b or len(a) > 4 or len(b) > 4:
        return None
    def search(a: set[str], b: set[str]) -> list[dict]:
        try:
            hits = get_json(
                GEOCODE,
                {"text": f"{_said(a)}/{_said(b)}", "type": "STOP", "place": _bias(raw)},
                ttl_s=86400,
            )
        except Exception:  # noqa: BLE001
            hits = []
        return [
            h
            for h in hits or []
            if a <= _key_words(h.get("name") or "")
            and b <= _key_words(h.get("name") or "")
            and _inside(h.get("lat", 0), h.get("lon", 0))
        ]

    # The same corner exists in several boroughs (5th Ave and 42nd St is in Manhattan and in
    # Sunset Park): take the one in the area the reader named, or Manhattan.
    area = m.group(3) or raw
    boro = (_borough_named(area) or "").title() or None
    hood = _neighborhood(_norm(m.group(3))) if m.group(3) else None

    def borough(h: dict) -> str | None:
        z = zone_of(h["lat"], h["lon"])
        return _zone_borough(z[0]) if z else None

    found, near_by = search(a, b), False
    if boro and not any(borough(h) == boro for h in found):
        # No stop at that very corner in the borough named: a block or two along the
        # numbered street is close enough, and the name says so.
        num = next((x for x in (b, a) if any(w.isdigit() for w in x)), None)
        if num is not None:
            n = int(next(w for w in num if w.isdigit()))
            other = a if num is b else b
            for k in (1, -1, 2, -2):
                moved = {w for w in num if not w.isdigit()} | {str(n + k)}
                more = [h for h in search(other, moved) if borough(h) == boro]
                if more:
                    found, near_by = more, True
                    break
    if not found:
        return None

    def rank(h: dict) -> tuple:
        near = _metres(hood, h) if hood else 0
        return (borough(h) != (boro or "Manhattan") if not hood else 0, near)

    h = min(found, key=rank)
    name = f"{m.group(1).strip()} and {m.group(2).strip()}"
    return {"name": f"near {name}" if near_by else name, "lat": h["lat"], "lon": h["lon"]}


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
            # "Brooklyn Bridge Park" is not the Brooklyn Bridge: a place word after the name
            # makes it a different place.
            if set(n.split()) - set(key.split()) & _PLACE_WORDS:
                continue
            return dict(v)
    return None


_PLACE_WORDS = {"park", "pier", "plaza", "station", "garden", "gardens", "museum", "hotel",
                "school", "hospital", "market", "mall", "beach", "playground", "library",
                "church", "terminal", "tower", "college", "university", "field"}


def locate(text: str) -> dict | None:
    """A trip end from what the reader wrote: coordinates, a hub, an NYC address or landmark,
    or a place nearby (Hoboken, Newark Airport). None when it cannot be placed in the region.
    "Church Av station on the F" is that line's station; "Wave Hill, 4900 Independence Ave"
    or "Jackson Heights, 74th St and 37th Ave" falls back to its address or corner part."""
    raw = (text or "").strip()[:120]
    m = re.match(
        r"(.+?)\s+(?:station\s+|stop\s+)?on the\s+([A-Z0-9](?:(?:\s*(?:,|/|and|&)\s*|\s+)[A-Z0-9])*)"
        r"(?:\s+(?:lines?|trains?))?\b(.*)$",
        raw,
        re.I,
    )
    # "Nostrand Av C station", "Court Sq (G)": the line written next to the name. The letter
    # must be a capital as written, and a station of that name must serve that line, so
    # "Avenue J" or "Pier 6" stays what it is.
    m2 = None if m else re.match(
        r"(.+?)\s+(?:\(\s*)?([A-Z0-9](?:\s*(?:/|,|and|&)\s*[A-Z0-9])*)(?:\s*\))?"
        r"(?:\s+(?:station|stop|train|line)s?\b|\s*$|(?=\s*,))(.*)$",
        raw,
    )
    if m or m2:
        mm = m or m2
        lines = set(re.findall(r"\b[A-Z0-9]\b", mm.group(2).upper()))
        hit = _line_station(mm.group(1), lines, mm.group(3))
        if hit:
            return hit
        if m:
            raw = m.group(1) + m.group(3)
    # "Hempstead Turnpike entrance to UBS Arena" is the arena's Hempstead Turnpike entrance.
    to = re.match(r"(.+?)\s+(?:entrance|gate|entry)\s+(?:to|of|for)\s+(?:the\s+)?(.+)$", raw, re.I)
    if to:
        raw = f"{to.group(2)}, {to.group(1)} entrance"
    # "Rockaway ferry landing at Beach 108th St", "St. George Ferry Terminal": a ferry stop.
    ferry = re.match(r"(?:the\s+)?(.+?)\s+ferry\b", raw, re.I)
    if ferry:
        hit = _ferry_stop(ferry.group(1), raw)
        if hit:
            return hit
    # "Prospect Park, Grand Army Plaza entrance", "Bronx Zoo, Southern Blvd gate": a big
    # place's named entrance is where the trip ends, so try that first.
    gate = re.match(r"(.+?),\s*(.+?)\s+(?:entrance|gate|entry)\s*$", raw, re.I)
    if gate and re.fullmatch(r"(?:the\s+)?(?:main|front|primary)", gate.group(2).strip(), re.I):
        return _locate(gate.group(1))  # the main entrance is the place itself
    if gate:
        whole = _locate(gate.group(1))
        tries = [f"{gate.group(2)}, {gate.group(1)}"]
        if not re.search(
            r"\b(?:st|street|ave?|avenue|blvd|boulevard|rd|road|pkwy|parkway)\b",
            gate.group(2),
            re.I,
        ):
            tries.append(gate.group(2))  # a bare street names no point on it
        # The gate's name alone may match a road of that name instead (Mosholu Parkway for
        # the garden's Mosholu gate): a street the reader did not say is not the gate.
        road = re.compile(r"\b(?:st|street|ave?|avenue|blvd|boulevard|rd|road|pkwy|parkway)\b", re.I)
        doors = [
            d
            for d in map(_locate, tries)
            if d
            and (not whole or _metres(d, whole) < 2500)
            and not (road.search(d["name"]) and not road.search(gate.group(2)))
        ]
        if doors:
            # Entrances are at the edge: the city's address search puts every place inside a
            # big park at the park's middle, so the candidate farthest from it is the gate.
            door = max(doors, key=lambda d: _metres(d, whole) if whole else 0)
            if not whole or _metres(door, whole) > 50:  # else no gate was found, only the place
                return {**door, "name": f"{gate.group(1)} ({gate.group(2)} entrance)"}
    # "Astoria, Broadway and 31st St", "Montclair, Walnut Street station": the exact part
    # after the area is where the trip ends, looked up in that area.
    head, _, tail = raw.partition(",")
    if tail and not _EXACT.search(head) and _EXACT.search(tail.split(",")[0]):
        precise = tail.split(",")[0].strip()
        found = _locate(f"{precise}, {head.strip()}")
        area = _locate(head.strip())
        if found and (not area or _metres(found, area) < 4000):
            return {**found, "name": f"{head.strip()} ({found['name']})"}
    found = _locate(raw)
    if found:
        return found
    # "The Met on Fifth Avenue", "Washington Square in Larchmont": the part after on or in
    # says which one, like a comma would.
    parts = [p.strip() for p in re.split(r",|\s+(?:on|in)\s+(?=[A-Z0-9])", raw) if p.strip()]
    if len(parts) < 2:
        return None
    boroughs = {"brooklyn", "queens", "manhattan", "bronx", "staten island", "new york", "nj",
                "new jersey", "ny", "long island", "li", "westchester", "connecticut", "ct",
                "nassau", "suffolk", "upstate"}
    # A region after the town ("Huntington, Long Island", "Harrison, NJ") names no place on
    # its own, but says which town: keep it with the town.
    while len(parts) > 2 and _norm(parts[-1]) in boroughs:
        parts = parts[:-2] + [f"{parts[-2]}, {parts[-1]}"]
    streetish = re.compile(r"\b(?:st|street|ave?|avenue|blvd|boulevard|rd|road|pl|place)\b", re.I)
    last = parts[-1]
    # A town given with the name (not a street, not a borough) must agree with what is found.
    town = (
        _locate(last)
        if not streetish.search(last) and _norm(last) not in boroughs
        else None
    )

    def checked(found: dict, p: str) -> dict:
        if town and p != last and _metres(found, town) > 3000:
            # Another place of that name (Washington Square in Manhattan, not in Larchmont):
            # the town is all that is known.
            return {**town, "name": f"near {town['name']}"}
        if p is not parts[0] and parts[0] not in exact:
            return {**found, "name": f"{parts[0]} ({found['name']})"}
        return found

    # "Brooklyn Bridge Park, Pier 6": a numbered pier, gate or hall inside a place, found
    # with the place's name and kept only if it is there.
    inner = [
        p
        for p in parts[1:]
        if re.search(r"\b(?:pier|gate|building|hall|terminal)\s+\w+|\w+\s+pier\b", p, re.I)
    ]
    if inner:
        whole = _locate(parts[0])
        hit = _osm(f"{inner[0]}, {parts[0]}")
        if whole and hit and _words(_norm(inner[0])) <= _words(_norm(hit["name"])) and (
            _metres(hit, whole) < 2500
        ):
            return {**hit, "name": f"{parts[0]}, {hit['name']}"}
    # An address or corner part is the most exact; a borough or town adds nothing on its own.
    exact = [p for p in parts if _EXACT.search(p)]
    if " in " in raw and "," not in raw:
        found = _locate(", ".join(parts))
        if found:
            return checked(found, parts[0])
    # A place in a town outside the city ("Red Bull Arena, Harrison, NJ"): OpenStreetMap
    # knows it, maybe by a newer name, and it must be in that town.
    if town and not zone_of(town["lat"], town["lon"]) and not _EXACT.search(parts[0]):
        there = _osm(f"{parts[0]}, {last}")
        if there and _metres(there, town) < 5000:
            return {**there, "name": parts[0]}
    for p in exact + [p for p in parts if p not in exact]:
        if _norm(p) in boroughs or (streetish.search(p) and p not in exact):
            continue  # a bare street ("Fifth Avenue") says nothing about where on it
        found = _locate(p)
        if found and town and p in exact and _metres(found, town) > 3000 and not zone_of(
            town["lat"], town["lon"]
        ):
            # "14 Wall Street, Huntington": the address in that town, not the one in the city.
            there = _osm(f"{p}, {last}") or _osm(f"{p}, {last.split(',')[0]}")
            if there and _metres(there, town) < 8000:
                return {**there, "name": f"{p}, {last}"}
        if found:
            return checked(found, p)
    return town and {**town, "name": f"near {town['name']}"}


def _osm(q: str) -> dict | None:
    """The first OpenStreetMap match for a query, inside the region."""
    try:
        hits = get_json(
            "https://nominatim.openstreetmap.org/search",
            {"q": q, "format": "jsonv2", "limit": 1,
             "viewbox": ",".join(str(x) for x in BOX), "bounded": 1},
            ttl_s=86400,
        )
    except Exception:  # noqa: BLE001
        return None
    if not hits:
        return None
    h = hits[0]
    return {"name": h.get("name") or q, "lat": float(h["lat"]), "lon": float(h["lon"])}


# A part of a place name that pins it down: an address, a corner, a station or stop.
_EXACT = re.compile(r"^\s*\d+\s+\w|\band\b|&|\bat\b|\b(?:station|stop)\b", re.I)


def _ferry_stop(name: str, asked: str = "") -> dict | None:
    """A ferry landing by the name of the place it serves, from the trip planner's stops."""
    want = _key_words(name) - {"ferry", "landing", "terminal", "dock", "pier"}
    if not want:
        return None
    said = _key_words(asked or name)
    hits = []
    # With "terminal" when it was asked for: the Staten Island Ferry's St. George Terminal.
    for words in dict.fromkeys([_said(want | ({"ferry", "terminal"} & said)), _said(want)]):
        try:
            hits += get_json(
                GEOCODE, {"text": words, "type": "STOP", "place": _bias(name)}, ttl_s=86400
            ) or []
        except Exception:  # noqa: BLE001
            pass
    fits = [
        h
        for h in hits or []
        if "FERRY" in (h.get("modes") or [])
        and want <= _key_words(h.get("name") or "")
        and _inside(h["lat"], h["lon"])
    ]
    if not fits:
        return None
    # "St. George Ferry Terminal" is the Staten Island Ferry's terminal, not the NYC Ferry
    # landing next to it: the stop sharing most words with what was asked.
    h = max(fits, key=lambda x: len(_key_words(x["name"]) & said))
    label = h["name"] if re.search(r"terminal|ferry", h["name"], re.I) else f"{h['name']} ferry landing"
    return {"name": label, "lat": h["lat"], "lon": h["lon"]}


def _line_station(name: str, lines: set[str], area: str = "") -> dict | None:
    """The subway station of that name served by those lines, from the MTA's station list."""
    from . import access

    want = _key_words(name)
    core = want - set(_STREET.values())  # "Avenue" alone names no station
    try:
        rows = access._stations()
    except Exception:  # noqa: BLE001
        return None
    boro = next(
        (c for w, c in (("brooklyn", "Bk"), ("queens", "Q"), ("bronx", "Bx"),
                        ("staten island", "SI"), ("manhattan", "M")) if w in area.lower()),
        None,
    )
    fits = []
    for r in rows:
        got = _key_words(r.get("stop_name") or "")
        routes = set((r.get("daytime_routes") or "").split())
        if core and core <= got and len(got - want) <= 2 and lines & routes:
            fits.append(((boro is not None and r.get("borough") != boro), len(got ^ want), r))
    if not fits:
        return None
    r = min(fits, key=lambda x: x[:2])[2]
    routes = sorted(set((r.get("daytime_routes") or "").split()))
    return {
        "name": f"{r['stop_name']} ({' '.join(routes)})",
        "lat": float(r["gtfs_latitude"]),
        "lon": float(r["gtfs_longitude"]),
    }


def _locate(text: str) -> dict | None:
    raw = (text or "").strip()[:120]
    if not raw:
        return None
    m = re.fullmatch(r"\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*", raw)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        return {"name": raw, "lat": lat, "lon": lon} if _inside(lat, lon) else None
    n = _norm(raw)
    low = " ".join(re.sub(r"[^a-z0-9 ]+", " ", raw.lower()).split())
    term = re.search(r"\bterminal\s*([a-c1-8])\b|\bt([1-8])\b", low)
    term = (term.group(1) or term.group(2)).upper() if term else None
    for key, ap in AIRPORT_WORDS.items():
        if re.search(rf"\b{key}\b", low):
            out = {"name": ap["name"], "lat": ap["lat"], "lon": ap["lon"], "airport": ap["code"]}
            if term and term in ap.get("terminals", {}):
                out["terminal"] = term
                out["name"] = f"{ap['name']} Terminal {term}"
            return out
    # LaGuardia's terminals are far apart (B to C is a 15 minute walk): the bus stops at each.
    if re.search(r"\b(?:lga|la ?guardia)\b", low) and term in LGA_TERMINALS:
        lat, lon = LGA_TERMINALS[term]
        return {"name": f"LaGuardia Airport Terminal {term}", "lat": lat, "lon": lon}
    station = _path_station(n)
    if station:
        return station
    corner = _corner(raw)
    if corner:
        return corner
    # "Red Hook, Brooklyn" is Red Hook: a borough or city name after a place adds nothing.
    bare = re.sub(r"\b(?:brooklyn|queens|manhattan|bronx|staten island|new york|nyc)\b", " ", n)
    bare = " ".join(bare.split())
    # Well-known places before neighborhoods: Union Square is the park, not the middle of the
    # "Midtown South-Flatiron-Union Square" neighborhood.
    mark = _landmark(n) or (_landmark(bare) if bare else None)
    if mark:
        return mark
    if bare and bare != n:
        hood = _neighborhood(bare)
        if hood:
            return hood
    said = _borough_named(raw)
    for key, (name, lat, lon) in NAMED.items():
        if n == key or re.search(rf"\b{key}\b", n) and len(n.split()) <= len(key.split()) + 2:
            z = zone_of(lat, lon)
            if said and z and (_zone_borough(z[0]) or "").lower() != said:
                continue  # "Grand Army Plaza, Manhattan" is not Brooklyn's
            return {"name": name, "lat": lat, "lon": lon}
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
    # A bare street address ("1 Centre St") is found more often with the city named.
    texts = [raw] + ([f"{raw}, New York"] if re.match(r"\d", raw) and "," not in raw else [])
    for text in [] if outside else texts:
        try:
            d = get_json(
                "https://geosearch.planninglabs.nyc/v2/search", {"text": text, "size": 1},
                ttl_s=86400,
            )
            f = (d.get("features") or [None])[0]
        except Exception:  # noqa: BLE001 - fall through to the regional search
            f = None
        if f and _words(n) <= _words(_norm(f["properties"].get("label", "").split(",")[0])) | {
            "new", "york"
        }:
            break
    if f:
        label = f["properties"].get("label") or ""
        lon, lat = f["geometry"]["coordinates"]
        want, got = _words(n), _words(_norm(label.split(",")[0]))
        if not any(w[0].isdigit() for w in want):  # "Grove St" may come back as "2 Grove St"
            got = {w for w in got if not w[0].isdigit()}
        # The same name both ways: "Journal Square" is not "Todd Square", and "Newark" is not
        # "63B Newark Avenue". A long address may differ by one word, but not by the town
        # ("Bloomfield Ave, Montclair" is not 73 Bloomfield Avenue on Staten Island).
        town = raw.partition(",")[2].strip()
        foreign = town and not _borough_named(town) and not _neighborhood(_norm(town))
        if want and not foreign and (want == got or len(want) >= 3 and len(want ^ got) <= 1):
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
        streetish = {"street", "avenue", "road", "boulevard"} & (got - want)
        fits = (want <= got or bool(outside)) and not extra and not streetish
        if _inside(lat, lon) and fits:
            name = h.get("name") or q
            return {"name": f"near {name}" if k else name, "lat": lat, "lon": lon}
    return _rail_stop(raw)


# Commuter rail reaches well past the city: Montauk, New Haven, Poughkeepsie, Trenton.
RAIL_BOX = (-75.0, 40.1, -71.8, 41.75)
_RAIL_WORDS = {"lirr", "station", "stop", "train", "rail", "railroad", "metro", "north", "mnr",
               "njt", "nj", "transit", "amtrak", "ny", "the"}


_RAIL_MODES = {"REGIONAL_RAIL", "RAIL", "SUBURBAN", "HIGHSPEED_RAIL", "LONG_DISTANCE", "FERRY"}


def _rail_stop(raw: str) -> dict | None:
    """A train or bus stop in the wider region by name ("Ronkonkoma", "Hicksville LIRR",
    "Princeton Junction"), from the trip planner's own stop search. A station is preferred
    over a bus stop of the same name."""
    want = _words(_norm(raw.split(",")[0])) - _RAIL_WORDS
    for text in dict.fromkeys([raw, " ".join(sorted(want, key=_norm(raw).find))]):
        if not want:
            continue
        try:
            hits = get_json(
                TRANSITOUS.replace("/api/v5/plan", "/api/v1/geocode"), {"text": text}, ttl_s=86400
            )
        except Exception:  # noqa: BLE001
            return None
        fits = [
            h
            for h in (hits or [])[:8]
            if (h.get("type") == "STOP" or re.search(r"\bstation\b", h.get("name") or "", re.I))
            and h.get("lat") is not None
            and RAIL_BOX[0] <= h["lon"] <= RAIL_BOX[2]
            and RAIL_BOX[1] <= h["lat"] <= RAIL_BOX[3]
            and want <= _words(_norm(h.get("name") or ""))
            # "Upper Montclair" is another station than "Montclair"
            and not (_words(_norm(h.get("name") or "")) - want - _RAIL_WORDS - {"union", "junction"})
        ]
        # A train station only: a bus stop called "NYU Langone Hospital" out on Long Island is
        # not the hospital the reader means.
        rail = [
            h
            for h in fits
            if set(h.get("modes") or []) & _RAIL_MODES
            or re.search(r"lirr|amtrak|\brail|(?<!bus )\bstation\b", h["name"], re.I)
        ]
        if rail:
            h = rail[0]
            return {"name": h["name"], "lat": h["lat"], "lon": h["lon"]}
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


# A trip for someone who cannot rush or use stairs gets more time at every change.
_STEP_FREE: ContextVar[bool] = ContextVar("step_free", default=False)


def _slack() -> dict:
    """Time allowed to change trains: the planner's own minimum is a sprint between
    platforms, so at least 3 minutes, and 5 at double pace for a step-free trip."""
    if _STEP_FREE.get():
        return {"minTransferTime": 5, "transferTimeFactor": 2}
    return {"minTransferTime": 3}


def _plan(a: dict, b: dict, when: datetime, arrive_by: bool, n: int = 5) -> list[dict]:
    d = get_json(
        TRANSITOUS,
        {
            "fromPlace": f"{a['lat']:.5f},{a['lon']:.5f}",
            "toPlace": f"{b['lat']:.5f},{b['lon']:.5f}",
            "time": when.astimezone(NY).isoformat(timespec="minutes"),
            "arriveBy": "true" if arrive_by else "false",
            # Step-free trips drop routes with stairs, so they need more to choose from.
            "numItineraries": n * (4 if _STEP_FREE.get() else 2),
            **_slack(),
        },
        ttl_s=120,
    )
    its = list(d.get("itineraries") or [])
    # When every option changes trains, ask for the simplest ones too: one seat all the way (a
    # reader with bags or a stroller takes the direct F over a faster change), or one change.
    fewest = min((it.get("transfers", 0) for it in its), default=0)
    for cap in ([0] if fewest >= 1 else []) + ([1] if fewest >= 2 else []):
        try:
            d2 = get_json(
                TRANSITOUS,
                {
                    "fromPlace": f"{a['lat']:.5f},{a['lon']:.5f}",
                    "toPlace": f"{b['lat']:.5f},{b['lon']:.5f}",
                    "time": when.astimezone(NY).isoformat(timespec="minutes"),
                    "arriveBy": "true" if arrive_by else "false",
                    "numItineraries": n,
                    "maxTransfers": cap,
                    **_slack(),
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
            lg = {**lg, "mode": "WALK", "duration": _metres(lg["from"], lg["to"]) * 1.25 / 1.35}
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


def _itinerary(
    it: dict, end: str, extra: tuple[str, int, int] | None = None, before: bool = False
) -> dict:
    """One planner itinerary as the guide reads it. `extra` is a leg the planner does not carry
    (the AirTrain): its text and its shortest and longest minutes, ridden after the planned trip,
    or before it when `before`. Leave, arrive and the door to door minutes all include it."""
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
    arrive = datetime.fromtimestamp(end_ts, NY).strftime("%-I:%M %p")
    if extra:
        lo, hi = lo + extra[1], hi + extra[2]
        if before:  # from the terminals: leave early enough for the slow end of the AirTrain
            legs.insert(0, extra[0])
            leave_ts -= 60 * extra[2]
        else:  # to the terminals: arrival is a window
            legs.append(extra[0])
            a, b = (datetime.fromtimestamp(end_ts + 60 * m, NY) for m in extra[1:])
            arrive = (
                f"{a.strftime('%-I:%M')} to {b.strftime('%-I:%M %p')} at the terminals "
                f"(train station {arrive})"
            )
            end_ts += 60 * extra[2]
    walk = round(sum(lg["duration"] for lg in it["legs"] if lg["mode"] == "WALK") / 60)
    return {
        "minutes": (lo, hi),
        "arrive_ts": end_ts,
        "leave_ts": leave_ts,
        "leave": datetime.fromtimestamp(leave_ts, NY).strftime("%-I:%M %p")
        + (" from the terminals" if extra and before else ""),
        "arrive": arrive,
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


def _airtrain_status(at: datetime) -> list[tuple[bool, str]]:
    """The AirTrain's own MTA alerts, each with whether it applies at the time of the trip."""
    from .live import MTA_ALERTS, _alert_hours, _plain

    try:
        d = get_json(MTA_ALERTS, ttl_s=120)
    except Exception:  # noqa: BLE001
        return []
    now, t = time.time(), at.timestamp()
    out = []
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
        out.append((during, f"{head} ({hours})"))
    return out


def _shuttle_at(alerts: list[tuple[bool, str]], station: str) -> bool:
    """Whether a shuttle bus replaces the AirTrain from this station at the trip time."""
    key = "howard beach" if "howard" in station.lower() else "jamaica"
    return any(on and key in head.lower() and "shuttle" in head.lower() for on, head in alerts)


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


def _for_deadline(options: list[dict], when: datetime, arrive_by: bool) -> list[dict]:
    """Arriving by a time: drop options that get in after it, latest leave first."""
    if not arrive_by:
        return options
    ok = [o for o in options if o["arrive_ts"] <= when.timestamp() + 60]
    if not ok:
        return options
    ok.sort(key=lambda o: -o["leave_ts"])
    # A train hours before the one that still makes it is no plan (1:44 AM for 8:30 AM).
    return [o for o in ok if o["leave_ts"] >= ok[0]["leave_ts"] - 45 * 60]


def _newark(a: dict, b: dict, when: datetime, arrive_by: bool) -> tuple[list[dict], list[str]]:
    """Trips to and from Newark Airport go by rail to the airport's train station, then the
    AirTrain. The planner has the trains; the AirTrain leg is from the Port Authority."""
    to_airport = b.get("airport") == "ewr"
    city = a if to_airport else b
    name, lat, lon = EWR["station"]
    st = {"name": name, "lat": lat, "lon": lon}
    pair = (city, st) if to_airport else (st, city)
    air = timedelta(minutes=20)
    t0 = when - air if to_airport and arrive_by else when
    t0 = when + air if not to_airport and not arrive_by else t0
    leg = (
        "AirTrain between the train station and the terminals: about 7 min to Terminal C, "
        "11 min to Terminal B; Terminal A is about 20 min (AirTrain then a walk or shuttle)"
    )
    options = [
        _itinerary(it, name if to_airport else city["name"], (leg, 7, 20), before=not to_airport)
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
    return _for_deadline(options, when, arrive_by), notes


def transit(a: dict, b: dict, when: datetime, arrive_by: bool) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    if a.get("airport") == "jfk" or b.get("airport") == "jfk":
        to_airport = b.get("airport") == "jfk"
        city = a if to_airport else b
        airport_end = b if to_airport else a
        status = _airtrain_status(when)
        options = []
        for name, lat, lon in JFK["stations"]:
            st = {"name": name, "lat": lat, "lon": lon}
            pair = (city, st) if to_airport else (st, city)
            # Plan the rail part against the slow end of the AirTrain: arriving by a time means
            # reaching the station that much earlier, leaving the terminals that much later.
            air = timedelta(minutes=AIRTRAIN["minutes"][1])
            t0 = when - air if to_airport and arrive_by else when
            t0 = when + air if not to_airport and not arrive_by else t0
            short = name.split(" (")[0]
            leg = (
                f"free shuttle bus replacing the AirTrain between {short} and the terminals "
                "(AirTrain suspended at this time; the bus takes longer than the AirTrain's "
                f"{AIRTRAIN['minutes'][0]} to {AIRTRAIN['minutes'][1]} min)"
                if _shuttle_at(status, short)
                else f"AirTrain between {short} and "
                + (f"Terminal {airport_end['terminal']}" if airport_end.get("terminal")
                   else "the terminals")
                + f", {AIRTRAIN['minutes'][0]} to {AIRTRAIN['minutes'][1]} min, "
                f"${AIRTRAIN['fare_usd']:.2f}"
            )
            for it in _plan(*pair, t0, arrive_by, n=3)[:2]:
                end = short if to_airport else city["name"]
                o = _itinerary(it, end, (leg, *AIRTRAIN["minutes"]), before=not to_airport)
                _add_airtrain(o, f"jfk:{short}", to_airport)
                options.append(o)
        notes.append(
            "The AirTrain leg is from the Port Authority: any terminal in under 20 minutes "
            "from Jamaica or Howard Beach, trains every 5 to 15 minutes, $8.75 one way."
        )
        for on, head in status:
            notes.append(
                f"AirTrain alert, {'in effect at the time of this trip' if on else 'not in effect then'}"
                f": {head}."
            )
        options.sort(key=lambda o: o["minutes"][1])
        return _for_deadline(options, when, arrive_by)[:4], notes
    if a.get("airport") == "ewr" or b.get("airport") == "ewr":
        return _newark(a, b, when, arrive_by)
    options = [_itinerary(it, b["name"]) for it in _plan(a, b, when, arrive_by)[:8]]
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
    # Best first: soonest arrival, or with a deadline the latest departure that makes it.
    # Each change costs five minutes either way, since people prefer one seat.
    def best(o: dict) -> float:
        if arrive_by and o["arrive_ts"] <= when.timestamp() + 60:
            return -o.get("leave_ts", 0) + 300 * o["transfers"]
        return o["arrive_ts"] + 300 * o["transfers"] + (1e9 if arrive_by else 0)

    options.sort(key=best)
    by_path.sort(key=best)
    merged = sorted(options[:3] + by_path, key=best)
    out = _for_deadline(merged, when, arrive_by)[:4]
    # The simplest ride always makes the list (the direct B, not only faster changes), unless
    # it is far slower than the best.
    pool = _for_deadline(options + by_path, when, arrive_by)
    if out and pool:
        easy = min(pool, key=lambda o: (o["transfers"], o["arrive_ts"]))
        slowest_ok = out[0]["minutes"][1] + 20
        if easy["transfers"] < min(o["transfers"] for o in out) and easy["minutes"][0] <= slowest_ok:
            out = out[:3] + [easy]
    return out, notes


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


def _path_rides(
    o: str, d: str, when: datetime, live: dict, walk1: int
) -> tuple[float, list[tuple[dict, str, str, float]], float | None] | None:
    """The quickest PATH ride from o to d running at that time: one train, or one change
    between lines (Hoboken to World Trade Center late at night changes at Grove Street).
    Returns the total minutes, the rides as (pattern, from, to, minutes), and the live wait."""
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
            best = (total, [(p, o, d, ride)], wait_s)
    if best:
        return best
    change = 15 if when.hour < 6 else 7
    for x in PATH["stations"]:
        if x in (o, d):
            continue
        first = [(p, r) for p, r in _rides(o, x) if _runs(p, when)]
        second = [(p, r) for p, r in _rides(x, d) if _runs(p, when)]
        if first and second:
            (p1, r1), (p2, r2) = min(first, key=lambda z: z[1]), min(second, key=lambda z: z[1])
            total = walk1 + r1 + change + r2
            if best is None or total < best[0]:
                best = (total, [(p1, o, x, r1), (p2, x, d, r2)], None)
    return best


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
            best = _path_rides(o, d, when, live, walk1)
            if not best:
                continue
            total, rides, wait_s = best
            # Without live times, allow for the wait: a few minutes by day, more at night,
            # and again at a change of trains.
            spare = 0 if wait_s is not None else (20 if when.hour < 6 else 10)
            change = (15 if when.hour < 6 else 7) if len(rides) > 1 else 0
            ride_min = sum(r[3] for r in rides) + change
            near_end = _metres(sd, b) <= WALK_MAX_M
            tail = None
            if not near_end:
                # The rest of the way by the trip planner: from when PATH gets in, or, for a
                # deadline, the latest trip that still makes it.
                t_off = (
                    when
                    if arrive_by
                    else when + timedelta(minutes=walk1 + ride_min, seconds=wait_s or 0)
                )
                rest = _plan(sd, b, t_off, arrive_by, n=2)
                if not rest:
                    continue
                tail = _itinerary(rest[0], b["name"])
            walk2 = _walk_min(sd, b) if near_end else 0
            if arrive_by:
                # Work back from the deadline: the end of the trip, then the PATH ride, the
                # wait for it and the walk to the station.
                end = (
                    datetime.fromtimestamp(tail["leave_ts"], NY)
                    if tail
                    else when - timedelta(minutes=walk2)
                )
                off = end
                board = off - timedelta(minutes=ride_min)
                leave = board - timedelta(minutes=walk1 + spare)
            else:
                leave = when
                board = when + timedelta(minutes=walk1, seconds=wait_s or 0)
                off = board + timedelta(minutes=ride_min)
            steps = [f"walk {walk1} min to {so['name']}"]
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
            ]
            for k, (p, x, y, ride) in enumerate(rides):
                stops = p["stops"][p["stops"].index(x) : p["stops"].index(y) + 1]
                head = PATH["stations"][p["stops"][-1]][0] + (
                    " via Hoboken (one train, no change)" if p["via_hoboken"] else ""
                )
                n = len(stops) - 1
                when_txt = (
                    f"at {board.strftime('%-I:%M %p')} (live)"
                    if wait_s is not None and k == 0
                    else "(next train; departure time not known)"
                )
                steps.append(
                    ("change to the " if k else "")
                    + f"PATH train toward {head} from {PATH['stations'][x][0]} {when_txt} to "
                    f"{PATH['stations'][y][0]}, {n} stop{'s' if n > 1 else ''}, {round(ride)} min"
                )
                lines.append(
                    {
                        "mode": "train",
                        "name": "PATH",
                        "color": PATH_COLOR.get(
                            frozenset((p["stops"][0], p["stops"][-1])), "#d93a30"
                        ),
                        "text_color": "#ffffff",
                        "from": PATH["stations"][x][0],
                        "to": PATH["stations"][y][0],
                        "depart": board.strftime("%-I:%M %p")
                        if wait_s is not None and k == 0
                        else None,
                        "minutes": round(ride),
                        "stops": n,
                        "headsign": head,
                        "live": wait_s is not None and k == 0,
                        "coords": [[PATH["stations"][c][2], PATH["stations"][c][1]] for c in stops],
                    }
                )
            if near_end:
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
                steps += [x for x in tail["steps"] if not x.startswith("walk 0")]
                lines += tail["lines"]
                end = datetime.fromtimestamp(tail["arrive_ts"], NY)
            mins = round((end - leave).total_seconds() / 60)
            arrive_ts = end.timestamp() + (0 if arrive_by else spare * 60)
            options.append(
                {
                    "minutes": (mins - (spare if arrive_by else 0), mins + (0 if arrive_by else spare)),
                    "arrive_ts": arrive_ts,
                    "leave_ts": leave.timestamp(),
                    "leave": leave.strftime("%-I:%M %p"),
                    "arrive": end.strftime("%-I:%M %p"),
                    "transfers": sum(x["mode"] != "walk" for x in lines) - 1,
                    "walk_minutes": walk1 + walk2,
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


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _take_date(raw: str, today) -> tuple[str, object]:
    """Pull a calendar date ("2026-10-24", "October 24", "Oct 24th, 2026", "10/24") out of the
    text, so its digits are not read as the clock time. Returns the rest and the date, or None.
    A date without a year that has already passed this year means next year."""
    pats = [
        (r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", lambda g: (int(g[0]), int(g[1]), int(g[2]))),
        (r"\b(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?",
         lambda g: (int(g[2]) if g[2] else None, _MONTHS.index(g[0]) + 1, int(g[1]))),
        (r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b",
         lambda g: ((int(g[2]) + 2000 if len(g[2]) == 2 else int(g[2])) if g[2] else None,
                    int(g[0]), int(g[1]))),
    ]
    for pat, parts in pats:
        m = re.search(pat, raw)
        if not m:
            continue
        y, mo, d = parts(m.groups())
        try:
            dt = date(y or today.year, mo, d)
            if y is None and dt < today - timedelta(days=1):
                dt = date(today.year + 1, mo, d)
        except ValueError:
            continue
        return raw[: m.start()] + " " + raw[m.end():], dt
    return raw, None


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
    raw, dated = _take_date(raw, now.date())
    small = re.search(r"\b([0-4])(?::\d\d)?\s*a\.?m\b|\bmidnight\b", raw)
    if not dated and small and re.search(r"\btonight\b|after midnight", raw) and now.hour >= 5:
        # "tonight 12:30 AM", "after midnight into Friday": the small hours after this evening
        dated = now.date() + timedelta(days=1)
    if dated:
        day = dated
    else:
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
    if dated or re.search(r"\btoday\b", raw):  # a day the reader named is kept as said
        return t
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


def _line_changes(option: dict, at: datetime) -> list[str]:
    """MTA service changes on the subway lines an option rides that are in effect at the time
    of the trip (planned work the schedules may not show)."""
    from .live import MTA_ALERTS, _plain

    routes = {lg.get("name") for lg in option.get("lines") or [] if lg.get("mode") == "subway"}
    routes.discard(None)
    if not routes:
        return []
    try:
        d = get_json(MTA_ALERTS, ttl_s=120)
    except Exception:  # noqa: BLE001
        return []
    t, out = at.timestamp(), []
    for ent in d.get("entity", []):
        al = ent.get("alert", {})
        hit = {x.get("route_id") for x in al.get("informed_entity") or []} & routes
        # An alert with no end is about now (delays): it says nothing about a trip hours away.
        soon = t - time.time() < 90 * 60
        during = any(
            int(p.get("start") or 0) <= t and (t < int(p["end"]) if p.get("end") else soon)
            for p in al.get("active_period") or [{}]
        )
        head = _plain(al.get("header_text", {}).get("translation"))[:220]
        if "AirTrain" in head:
            continue  # the AirTrain's own alerts are read against its leg, not the subway line
        if hit and during:
            line = f"Service change on the {'/'.join(sorted(hit))} at that time: {head}"
            if head and line not in out:
                out.append(line)
    return out[:3]


def _step_free_first(options: list[dict]) -> list[dict]:
    """Check every subway station an option boards or leaves at, and put the options with no
    stairs (and no elevator out) first."""
    from . import access

    def check(o: dict) -> int:
        seen, texts, bad = set(), [], 0
        for lg in o.get("lines") or []:
            if lg.get("mode") != "subway" or not lg.get("coords"):
                continue
            for lon, lat in (lg["coords"][0], lg["coords"][-1]):
                try:
                    rec = access.station("", lat, lon, lg.get("name"))
                except Exception:  # noqa: BLE001
                    rec = None
                if not rec or rec["name"] in seen:
                    continue
                seen.add(rec["name"])
                texts.append(access.describe(rec))
                bad += rec["step_free"] != "yes" or bool(rec["out"])
        o["access"] = "; ".join(texts) if texts else "no subway stations on this option"
        return bad

    bad = {id(o): check(o) for o in options}
    good = [o for o in options if not bad[id(o)]]
    # Options with stairs are left out when a step-free one exists.
    return good or sorted(options, key=lambda o: bad[id(o)])


# The tool --------------------------------------------------------------------------------------


def trip_plan(
    origin: str,
    destination: str,
    mode: str = "all",
    when: str | None = None,
    arrive_by: bool = False,
    step_free: bool = False,
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
    if _metres(a, b) < 150:
        return failed(
            f"both ends were placed at {a['name']}; name the destination more exactly "
            "(its address or a nearby station)"
        )
    t = parse_when(when)
    if t is None:
        return failed("when must be a New York time like 2026-10-16T17:30, 'Saturday 10 PM' or now")
    if t.tzinfo is None:
        t = t.replace(tzinfo=NY)
    if t < _now() - timedelta(minutes=10):
        return failed(
            f"{t:%a %b %-d %-I:%M %p} has already passed (it is now {_now():%-I:%M %p}); "
            "tell the reader, and ask whether they mean a later time"
        )
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
        token = _STEP_FREE.set(step_free)
        try:
            options, notes = transit(a, b, t, arrive_by)
        except Exception as e:  # noqa: BLE001 - the drive half can still answer
            options, notes = [], [f"Transit planner unavailable: {str(e)[:80]}"]
        finally:
            _STEP_FREE.reset(token)
        # Never offer a plan that should already have left. For a deadline that nothing
        # leaving now can make, say so and give the soonest arrival instead.
        gone = _now().timestamp() - 120
        if options and any(o.get("leave_ts", gone + 1) < gone for o in options):
            still = [o for o in options if o.get("leave_ts", gone + 1) >= gone]
            if still:
                options = still
            elif arrive_by:
                try:
                    soon, more = transit(a, b, _now(), False)
                except Exception:  # noqa: BLE001
                    soon, more = [], []
                notes = [
                    f"Leaving now, no transit option arrives by {t:%-I:%M %p}: every one "
                    "would have had to leave already. The options below leave now and arrive "
                    "as soon as possible."
                ] + more
                options = soon
        if options and step_free:
            options = _step_free_first(options)
        data["transit"] = options
        if options:
            lines.append("Transit options (best first):")
            # Name the quickest and the simplest, so a reader who wants fewer changes (luggage,
            # a stroller, stairs) is not handed the bus-bus-train option because it saves a minute.
            # With a deadline the useful one is the latest to leave, not the first to arrive.
            quick = (
                max(options, key=lambda o: o.get("leave_ts", 0))
                if arrive_by
                else min(options, key=lambda o: o["arrive_ts"])
            )
            first = "leaves latest" if arrive_by else "arrives first"
            easy = min(options, key=lambda o: (o["transfers"], o["arrive_ts"]))
            for i, o in enumerate(options, 1):
                lo, hi = o["minutes"]
                span = f"{lo} min" if lo == hi else f"{lo} to {hi} min"
                tag = ", ".join(
                    t for t, x in ((first, quick), ("fewest changes", easy)) if o is x
                )
                lines.append(
                    f"{i}.{f' [{tag}]' if tag else ''} {span} door to door, leave {o['leave']}, "
                    f"arrive {o.get('arrive', '?')}, {o['transfers']} transfer"
                    f"{'s' if o['transfers'] != 1 else ''}, {o['walk_minutes']} min walking: "
                    + "; ".join(o["steps"])
                )
                if step_free and o.get("access") is not None:
                    lines.append("   Step-free check: " + o["access"])
                for warn in _line_changes(o, t):
                    lines.append("   " + warn)
            if step_free:
                lines.append(
                    "Step-free checks use the MTA's list of accessible subway stations and its "
                    "live elevator outages. Every MTA bus kneels or has a ramp. LIRR, Metro-North, "
                    "NJ Transit and PATH stations are not checked here."
                )
                sources.append(
                    {
                        "kind": "live",
                        "title": "MTA accessible stations and elevator status",
                        "url": "https://new.mta.info/elevator-escalator-status",
                    }
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
        if arrive_by and (car or d):
            # A deadline needs a time to leave, from the slow end of a normal trip.
            slow = max(([car["minutes"]] if car else []) + ([d["minutes"][2]] if d else []))
            leave = t - timedelta(minutes=slow + 10)
            lines.append(
                f"By car, to arrive by {t:%-I:%M %p}: leave by about {leave:%-I:%M %p} (the slow "
                "end of a normal trip at that hour, plus 10 minutes)."
            )
        if d:
            p25, med, p75 = d["minutes"]
            fares = "; ".join(
                f"{label} about ${d[k]:.0f}"
                for label, k in (
                    ("taxi", "taxi_fare_usd"),
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
                "step_free": {
                    "type": "boolean",
                    "description": "the reader cannot use stairs (wheelchair, stroller, heavy "
                    "luggage): checks each subway station and elevator outages",
                },
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
