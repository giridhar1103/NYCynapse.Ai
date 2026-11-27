"""Where a reader means: a trip end from what they wrote. Airports and terminals, PATH and
subway stations (with the line named), corners, landmarks with front-door pins, ferry landings,
neighborhoods, NYC addresses, then the region's rail stations and towns.

Moved out of trips.py; constants and helpers that stay there (airports, the taxi zones, the
distance function) are reached through `trips` at call time, which keeps the two modules free of
an import cycle."""

from __future__ import annotations

import json
import re
from functools import lru_cache

from . import bridges, centers, corners, gates, osm, railfares
from . import trips as T
from .http import get_json
from .path_trains import PATH

__all__ = [
    "_norm",
    "_ABBR",
    "_words",
    "_inside",
    "_hoods",
    "_neighborhood",
    "PATH_NAMES",
    "_path_station",
    "GEOCODE",
    "_STREET",
    "_HINT",
    "_DROP",
    "_key_words",
    "_stop",
    "_NICK",
    "_BORO_CENTER",
    "_BORO_OF",
    "_borough_named",
    "_bias",
    "_said",
    "_mapped_corner",
    "_corner",
    "_corner_by_street",
    "_same_address",
    "_landmarks",
    "_landmark",
    "_PLACE_WORDS",
    "locate",
    "_osm",
    "_EXACT",
    "_ferry_stop",
    "_line_station",
    "_locate",
    "RAIL_BOX",
    "_RAIL_WORDS",
    "_RAIL_MODES",
    "_rail_stop",
]


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"['’]", "", s.lower()))
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
    "theatre": "theater",
    "saint": "street",  # as "st" is: St. Patrick's and Saint Patrick's are one
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
    return T.BOX[0] <= lon <= T.BOX[2] and T.BOX[1] <= lat <= T.BOX[3]


def _middle(ring: list) -> tuple[float, float]:
    """The center of an outline's area. The mean of its points leans to the wiggly shore."""
    x0, y0 = ring[0][0], ring[0][1]
    area = x = y = 0.0
    for a, b in zip(ring, ring[1:], strict=False):
        ax, ay, bx, by = a[0] - x0, a[1] - y0, b[0] - x0, b[1] - y0
        k = ax * by - bx * ay
        area, x, y = area + k, x + (ax + bx) * k, y + (ay + by) * k
    return (x0 + x / (3 * area), y0 + y / (3 * area)) if area else (x0, y0)


@lru_cache(maxsize=1)
def _hoods() -> list[tuple[str, list[str], float, float]]:
    """Neighborhood names (and their parts, "Astoria (North)" is Astoria) with a middle point."""
    out = []
    for f in json.loads((T.GEO / "nta.json").read_text())["features"]:
        name = f["properties"]["name"]
        g = f["geometry"]
        ring = (
            g["coordinates"][0]
            if g["type"] == "Polygon"
            else max((p[0] for p in g["coordinates"]), key=len)
        )
        lon, lat = _middle(ring)
        parts = [_norm(re.sub(r"\(.*?\)", "", x)) for x in re.split(r"[-/]", name)]
        out.append((name, parts, lat, lon))
    return out


def _neighborhood(n: str, boro: str | None = None) -> dict | None:
    """The middle of a neighborhood: the corner people there know, or else of the city's outline."""
    known = centers.find(n, boro)
    if known:
        return known
    hits = [(name, lat, lon) for name, parts, lat, lon in _hoods() if n in parts]
    if boro:
        hits = [
            h
            for h in hits
            if (z := T.zone_of(h[1], h[2])) and (T._zone_borough(z[0]) or "").lower() == boro
        ]
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
            "path",
            "station",
            "nj",
            "jersey",
            "city",
            "jc",
            "new",
            "the",
            "train",
            "street",
            "st",
            "terminal",
            "square",
            "sq",
        }
        if re.search(rf"\b{key}\b", n) and (nj or not needs_nj) and not rest:
            name, lat, lon = PATH["stations"][code]
            if code == "NWK":  # also the NJ Transit and Amtrak station
                return {"name": "Newark Penn Station", "lat": lat, "lon": lon}
            return {"name": f"{name} PATH station", "lat": lat, "lon": lon}
    return None


GEOCODE = "https://api.transitous.org/api/v1/geocode"

_STREET = {
    "street": "st",
    "avenue": "av",
    "ave": "av",
    "boulevard": "blvd",
    "road": "rd",
    "place": "pl",
    "parkway": "pkwy",
    "square": "sq",
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
    t = re.sub(r"\bnew york\b", " ", t)  # York St and New Lots Av keep their names
    return {_STREET.get(w, w) for w in t.split()} - _DROP


def _stop(raw: str) -> dict | None:
    """A station or stop by the name the agencies use ("Flushing-Main St", "Flushing Main
    Street LIRR", "Jamaica"), from the trip planner's own list of stops."""
    name, _, area = raw.partition(",")
    m = None if area else re.match(r"(.+?)\s+in\s+([A-Z].*)$", raw)
    if m:  # "Fulton Street station in Manhattan"
        name, area = m.group(1), m.group(2)
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
        if boro:  # a borough the reader named is where the stop is, not a preference
            z = T.zone_of(lat, lon)
            if not z or (T._zone_borough(z[0]) or "").lower() != boro:
                continue
        modes = h.get("modes") or []
        rail = "SUBWAY" in modes or "REGIONAL_RAIL" in modes
        dist = -T._metres(near, h) if near else 0
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
_BORO_OF = {
    "sunset park": "brooklyn",
    "bay ridge": "brooklyn",
    "park slope": "brooklyn",
    "williamsburg": "brooklyn",
    "bushwick": "brooklyn",
    "flatbush": "brooklyn",
    "astoria": "queens",
    "flushing": "queens",
    "jamaica": "queens",
    "harlem": "manhattan",
    "riverdale": "bronx",
}


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


def _with_area(raw: str) -> str:
    """ "Main St and 41st Ave Flushing NY": the area written straight after the corner."""
    raw = re.sub(r"[\s,]+(?:NY|NYC|New York)\s*$", "", corners.strip(raw), flags=re.I)
    if "," not in raw:
        for area in sorted([*_BORO_CENTER, *_BORO_OF], key=len, reverse=True):
            if raw.lower().endswith(" " + area):
                return f"{raw[: -len(area) - 1]}, {raw[-len(area) :]}"
    return raw


def _mapped_corner(raw: str) -> dict | None:
    """The corner on the city's street map: in the area named, or else Manhattan's, or the only
    one there is. Names without an area that meet in several boroughs but not in Manhattan stay
    open."""
    m = re.match(r"(.+?)(?:(?:,|\s+in\s)\s*(.+))?$", _with_area(raw).strip())
    if not m:
        return None
    streets, area = m.groups()
    pair = corners.parse(streets)
    found = corners.find(*pair) if pair else []
    # A street can carry a borough's name (Queens Blvd is in Queens, Bronx Park East not only).
    boro = (_borough_named(area) or "").title() if found and area else ""
    hood = _neighborhood(_norm(area)) if found and area else None
    if hood:
        near = min(found, key=lambda h: T._metres(hood, h))
        found = [near] if T._metres(hood, near) < 4000 else []
    elif boro and any(h["borough"] == boro for h in found):
        found = [h for h in found if h["borough"] == boro]
    elif area and boro:
        found = []  # they do not meet in the borough named
    elif any(h["borough"] == "Manhattan" for h in found):
        found = [h for h in found if h["borough"] == "Manhattan"]
    if not found or len({h["borough"] for h in found}) > 1:
        return None
    h = found[0]
    return {
        "name": f"{pair[0].strip()} and {pair[1].strip()}",
        "lat": h["lat"],
        "lon": h["lon"],
        "corner": h["borough"],
    }


def _corner(raw: str) -> dict | None:
    """Where two streets meet ("Main St and Roosevelt Ave, Flushing"), from the city's street map
    or else a bus stop named for the corner, which the agencies place right at it."""
    raw = _with_area(raw)
    mapped = _mapped_corner(raw)
    if mapped:
        return mapped
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
        # Stops are named either way round ("5 AV/W 32 ST"): ask both.
        hits = []
        for text in (f"{_said(a)}/{_said(b)}", f"{_said(b)}/{_said(a)}"):
            try:
                hits += (
                    get_json(
                        GEOCODE, {"text": text, "type": "STOP", "place": _bias(raw)}, ttl_s=86400
                    )
                    or []
                )
            except Exception:  # noqa: BLE001
                continue
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
        z = T.zone_of(h["lat"], h["lon"])
        return T._zone_borough(z[0]) if z else None

    found, near_by = search(a, b), False
    if boro and not any(borough(h) == boro for h in found):
        # No stop at that very corner in the borough named: a block or two along the
        # numbered street is close enough, and the name says so.
        # Move along a numbered street, never an avenue: from "W 32 St & 5 Av" the nearby
        # corners are on 32nd Street (31st, 33rd), not on 4th or 6th Avenue.
        num = next((x for x in (b, a) if any(w.isdigit() for w in x) and "st" in x), None)
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
        return _corner_by_street(m.group(1), m.group(2), m.group(3))

    def rank(h: dict) -> tuple:
        near = T._metres(hood, h) if hood else 0
        return (borough(h) != (boro or "Manhattan") if not hood else 0, near)

    h = min(found, key=rank)
    name = f"{m.group(1).strip()} and {m.group(2).strip()}"
    return {"name": f"near {name}" if near_by else name, "lat": h["lat"], "lon": h["lon"]}


def _corner_by_street(a: str, b: str, area: str | None) -> dict | None:
    """A corner of two named streets with no bus stop on it ("Ludlow St and Stanton St"):
    the first street, in the area named, is within a short walk, and the name says "near"."""
    if any(ch.isdigit() for ch in a + b) or not re.search(
        r"\b(?:st|street|av|ave|avenue|pl|place|rd|road|blvd|boulevard)\b", a, re.I
    ):
        return None
    where = area or "Manhattan"
    other = _locate(f"{b.strip()}, {where}")
    if not other:
        return None
    # Walk the first street by house number and keep the address nearest the second street.
    want, best = _key_words(a), None
    for n in (1, 40, 80, 120, 160, 200, 260, 320, 400, 500):
        try:
            d = get_json(
                "https://geosearch.planninglabs.nyc/v2/search",
                {"text": f"{n} {a.strip()}, {where}", "size": 1},
                ttl_s=7 * 86400,
            )
        except Exception:  # noqa: BLE001
            continue
        f = (d.get("features") or [None])[0]
        if not f or not want <= _key_words(f["properties"].get("name") or ""):
            continue
        lon, lat = f["geometry"]["coordinates"]
        far = T._metres(other, {"lat": lat, "lon": lon})
        if best is None or far < best[0]:
            best = (far, lat, lon)
    if best is None or best[0] > 350:
        return None
    return {"name": f"near {a.strip()} and {b.strip()}", "lat": best[1], "lon": best[2]}


def _same_address(features: list[dict], said: str | None, name: str = "") -> dict | None:
    """The same street address exists in several boroughs (233 5th Ave is in Manhattan and in
    Park Slope). Of the matches with the same wording, take the one in the borough the reader
    named, or Manhattan, as for street corners."""
    if not features:
        return None
    top = features[0]

    def first(f: dict) -> str:
        return (f["properties"].get("label") or "").split(",")[0].strip().lower()

    exact = [f for f in features if first(f) == (name or "").strip().lower()]
    if exact and not re.match(r"\d", first(top)):
        return exact[0]  # STATEN ISLAND ZOO, listed after STATEN IS ZOO
    if not said and not re.match(r"\d", first(top)):
        return top  # a name, not an address: Pratt Institute's first match is Brooklyn's
    want = said or "manhattan"
    for f in features:
        if first(f) == first(top) and (f["properties"].get("borough") or "").lower() == want:
            return f
    return top


_STREET_KINDS = {"street", "st", "avenue", "ave", "av", "road", "rd", "place", "pl", "drive", "dr",
                 "lane", "terrace", "boulevard", "blvd", "parkway", "court"}  # fmt: skip
_SMALL = {"of", "the", "and", "at", "on"}


def _ordinal(n: str) -> str:
    if n[-2:] in ("11", "12", "13"):
        return n + "th"
    return n + {"1": "st", "2": "nd", "3": "rd"}.get(n[-1], "th")


def tidy_label(name: str) -> str:
    """The city's address search writes in capitals: "151 WEST 34 STREET" is "151 West 34th
    Street", as a reader writes it. A name with small letters is left as it is."""
    if not name.isupper():
        return name
    words = name.split()
    out = []
    for i, w in enumerate(words):
        nxt = words[i + 1].lower() if i + 1 < len(words) else ""
        if w in _KEEP_CAPS:
            out.append(w)
        elif w.isdigit() and i > 0 and nxt in _STREET_KINDS:
            out.append(_ordinal(w))
        elif i and w.lower() in _SMALL:
            out.append(w.lower())
        else:
            out.append("-".join(x.capitalize() for x in w.split("-")))
    return " ".join(out)


# The middles of the biggest parks, which some of their statues and fountains also get.
_PARK_MIDDLES = ((40.78249, -73.96554), (40.7473, -73.8433), (40.6614, -73.9696))


def in_its_park(f: dict) -> dict:
    """The place of an address search match. A thing without a building of its own (a terrace, a
    statue, a whole zoo) has a placeholder building number and the middle of its lot, which for
    Bethesda Terrace is the far end of Central Park: OpenStreetMap knows where it stands."""
    p = f["properties"]
    lon, lat = f["geometry"]["coordinates"]
    raw = (p.get("label") or "").split(",")[0]
    name = tidy_label(raw)
    spot = {"name": name, "lat": lat, "lon": lon}
    number = str(((p.get("addendum") or {}).get("pad") or {}).get("bin") or "")
    middle = any(T._metres(spot, {"lat": a, "lon": b}) < 25 for a, b in _PARK_MIDDLES)
    if p.get("layer") != "venue" or not (middle or number.endswith("000000")):
        return spot
    if _norm(raw) in _landmarks():
        return dict(_landmarks()[_norm(raw)])
    hit = _osm(f"{raw}, {p.get('borough') or 'New York'}")
    if hit and _words(_norm(hit["name"])) == _words(_norm(name)):
        far = T._metres(hit, spot)
        return hit if 120 < far < 4000 else spot
    whole = re.search(
        r"\b(?:park|pk|playground|plgd|plaza|gardens?|field|square|beach|zoo|cemetery)$",
        name.strip(),
        re.I,
    )
    return spot if whole else {**spot, "name": f"near {name}"}


@lru_cache(maxsize=1)
def _landmarks() -> dict[str, dict]:
    """Well-known places with coordinates checked against OpenStreetMap, so the names people
    use most ("Union Square", "MSG", "the Met") never land somewhere else."""
    raw = json.loads((T.GEO / "landmarks.json").read_text())
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
            # "Nitehawk Prospect Park" is a cinema, not the park: the words before the name may
            # only say which part or where.
            if set(re.split(rf"\b{re.escape(key)}\b", n)[0].split()) - _LEAD:
                continue
            return dict(v)
    return None


_LEAD = {
    *("north", "south", "east", "west", "upper", "lower", "central", "downtown", "midtown"),
    *("uptown", "manhattan", "brooklyn", "queens", "bronx", "staten", "island", "new", "york"),
    *("near", "around", "by", "at", "in", "to", "from", "inside", "outside", "of", "old"),
    *("flushing", "harlem", "city", "entrance", "main", "corner", "middle", "end", "side"),
}

_PLACE_WORDS = {
    "park",
    "pier",
    "plaza",
    "station",
    "garden",
    "gardens",
    "museum",
    "hotel",
    "school",
    "hospital",
    "market",
    "mall",
    "beach",
    "playground",
    "library",
    "church",
    "terminal",
    "tower",
    "college",
    "university",
    "field",
    *("cafe", "deli", "diner", "bakery", "restaurant", "pizza", "bar", "grill", "tavern"),
    "bookstore",
    *("zoo", "aquarium", "ps1", "theater", "theatre", "arena", "stadium", "bowl", "cathedral"),
}


def locate(text: str) -> dict | None:
    """A trip end from what the reader wrote: coordinates, a hub, an NYC address or landmark,
    or a place nearby (Hoboken, Newark Airport). None when it cannot be placed in the region.
    "Church Av station on the F" is that line's station; "Wave Hill, 4900 Independence Ave"
    or "Jackson Heights, 74th St and 37th Ave" falls back to its address or corner part."""
    raw = (text or "").strip()[:120]
    bridge = bridges.find(raw) or gates.find(raw)
    if bridge:
        return bridge
    # "Great Lawn, Central Park": a well-known thing in a well-known park.
    thing, _, park = raw.partition(",")
    if not park and (m := re.match(r"(.+?)\s+(?:in|at)\s+(.+\bpark)$", raw, re.I)):
        thing, park = m.groups()  # "the Boathouse in Central Park"
    marks = _landmarks()
    # "Boathouse, Central Park" is listed as central park boathouse.
    inner = next((k for k in (_norm(thing), f"{_norm(park)} {_norm(thing)}") if k in marks), "")
    if _norm(park).endswith("park") and inner and _norm(park) in marks:
        spot, whole = marks[inner], marks[_norm(park)]
        if spot["name"] != whole["name"] and T._metres(spot, whole) < 2500:
            return dict(spot)
    m = re.match(
        r"(.+?)\s+(?:station\s+|stop\s+)?on the\s+"
        r"([A-Z0-9](?:(?:\s*(?:,|/|and|&)\s*|\s+)[A-Z0-9])*)"
        r"(?:\s+(?:lines?|trains?))?\b(.*)$",
        raw,
        re.I,
    )
    # "Nostrand Av C station", "Court Sq (G)": the line written next to the name. The letter
    # must be a capital as written, and a station of that name must serve that line, so
    # "Avenue J" or "Pier 6" stays what it is.
    m2 = (
        None
        if m
        else re.match(
            r"(.+?)\s+(?:\(\s*)?([A-Z0-9](?:\s*(?:/|,|and|&)\s*[A-Z0-9])*)(?:\s*\))?"
            r"(?:\s+(?:station|stop|train|line)s?\b|\s*$|(?=\s*,))(.*)$",
            raw,
        )
    )
    # "96 St (1 2 3)", as the guide's own tools write a station, or "96 St subway station
    # (1 2 3), Manhattan": the lines in brackets after the name, wherever they sit.
    paren = (
        None
        if m
        else re.search(r"\(\s*([A-Z0-9](?:(?:\s*[/,&]\s*|\s+and\s+|\s+)[A-Z0-9])*)\s*\)", raw)
    )
    if paren:
        lines = set(re.findall(r"\b[A-Z0-9]\b", paren.group(1)))
        hit = _line_station(raw[: paren.start()], lines, raw[paren.end() :])
        if hit:
            return hit
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
    # "Wall Street, Pier 11": a numbered pier with a place is its ferry landing.
    if re.search(r"\bpier\s+\d+", raw, re.I):
        hit = _ferry_stop(re.sub(r"[,/]", " ", raw), raw)
        if hit:
            return hit
    # "Rockaway ferry landing at Beach 108th St", "St. George Ferry Terminal": a ferry stop.
    # "South Ferry" is the station at the tip of Manhattan, not a ferry landing called South.
    if re.fullmatch(
        r"\s*(?:the\s+)?south\s+ferry(?:\s+(?:station|stop|subway|terminal))?"
        r"(?:\s*,?\s*(?:manhattan|nyc|new york(?:,? ny)?))?\s*",
        raw,
        re.I,
    ):
        return {"name": "South Ferry", "lat": 40.70207, "lon": -74.01366}
    ferry = re.match(r"(?:the\s+)?(.+?)\s+ferry\b", raw, re.I)
    if ferry:
        hit = _ferry_stop(ferry.group(1), raw)
        if hit:
            return hit
    # "Prospect Park, Grand Army Plaza entrance", "Bronx Zoo, Southern Blvd gate": a big
    # place's named entrance is where the trip ends, so try that first.
    gate = re.match(r"(.+?),\s*(.+?)\s+(?:entrance|gate|entry)\s*$", raw, re.I)
    if gate and re.fullmatch(r"(?:the\s+)?(?:main|front|primary)", gate.group(2).strip(), re.I):
        return locate(gate.group(1))  # the main entrance is the place itself
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
        road = re.compile(
            r"\b(?:st|street|ave?|avenue|blvd|boulevard|rd|road|pkwy|parkway)\b", re.I
        )
        doors = [
            d
            for d in map(_locate, tries)
            if d
            and (not whole or T._metres(d, whole) < 2500)
            and not (road.search(d["name"]) and not road.search(gate.group(2)))
        ]
        if doors:
            # Entrances are at the edge: the city's address search puts every place inside a
            # big park at the park's middle, so the candidate farthest from it is the gate.
            door = max(doors, key=lambda d: T._metres(d, whole) if whole else 0)
            if not whole or T._metres(door, whole) > 50:  # else no gate was found, only the place
                return {**door, "name": f"{gate.group(1)} ({gate.group(2)} entrance)"}
    # "Astoria, Broadway and 31st St", "Montclair, Walnut Street station": the exact part
    # after the area is where the trip ends, looked up in that area.
    head, _, tail = raw.partition(",")
    if tail and not _EXACT.search(head) and _EXACT.search(tail.split(",")[0]):
        precise = tail.split(",")[0].strip()
        # "Beach 108th St boardwalk": the street, without the words about what is on it
        plain = re.sub(r"\s+(?:boardwalk|promenade|side|area|corner)\s*$", "", precise, flags=re.I)
        found = _locate(f"{precise}, {head.strip()}") or (
            _locate(f"{plain}, {head.strip()}") if plain != precise else None
        )
        area = _locate(head.strip())
        if not found and area:  # the precise part alone, if it is in that area
            found = _locate(plain)
        if found and (not area or T._metres(found, area) < 4000):
            return {**found, "name": f"{head.strip()} ({found['name']})"}
    found = _place_in(raw) or _locate(raw)
    # "Juniors Downtown Brooklyn": the search matched only the neighborhood at the end.
    if found and "," not in raw and _norm(raw) != (fn := _norm(found["name"])):
        if _norm(raw).endswith(" " + fn) and _neighborhood(fn):
            found = _area_at_end(raw) or found
    if found:
        return found
    # "The Met on Fifth Avenue", "Washington Square in Larchmont": the part after on or in
    # says which one, like a comma would.
    parts = [p.strip() for p in re.split(r",|\s+(?:on|in)\s+(?=[A-Z0-9])", raw) if p.strip()]
    if len(parts) < 2:
        return _area_at_end(raw) or _only_one(raw)
    boroughs = {
        "brooklyn",
        "queens",
        "manhattan",
        "bronx",
        "staten island",
        "new york",
        "nj",
        "new jersey",
        "ny",
        "long island",
        "li",
        "westchester",
        "connecticut",
        "ct",
        "nassau",
        "suffolk",
        "upstate",
    }
    # A region after the town ("Huntington, Long Island", "Harrison, NJ") names no place on
    # its own, but says which town: keep it with the town.
    while len(parts) > 2 and _norm(parts[-1]) in boroughs:
        parts = parts[:-2] + [f"{parts[-2]}, {parts[-1]}"]
    streetish = re.compile(r"\b(?:st|street|ave?|avenue|blvd|boulevard|rd|road|pl|place)\b", re.I)
    last = parts[-1]
    # A town given with the name (not a street, not a borough) must agree with what is found.
    town = _locate(last) if not streetish.search(last) and _norm(last) not in boroughs else None

    boro = _borough_named(", ".join(parts[1:]))
    if "," not in raw and " in " in raw and not (town or boro or streetish.search(last)):
        return _only_one(raw)  # "Alice in Wonderland" is a name, not an Alice somewhere

    def checked(found: dict, p: str) -> dict | None:
        z = T.zone_of(found["lat"], found["lon"])
        if boro and p is parts[0] and z and (T._zone_borough(z[0]) or "").lower() != boro:
            return None  # "NYU Langone, Brooklyn" is not the one on First Avenue
        if town and p != last and T._metres(found, town) > 3000:
            # Another place of that name (Washington Square in Manhattan, not in Larchmont):
            # the town is all that is known.
            return {**town, "name": f"near {town['name']}"}
        if town and p == last and p is not parts[0]:
            # Only the town was found, not the place in it: its middle must not pass for the place.
            return {**found, "name": f"near {found['name']}"}
        if p is not parts[0] and parts[0] not in exact:
            return {**found, "name": f"{parts[0]} ({found['name']})"}
        return found

    # "Brooklyn Bridge Park, Pier 6": a numbered pier, gate or hall inside a place, found
    # with the place's name and kept only if it is there.
    inner = [
        p
        for p in parts
        if re.search(r"\b(?:pier|gate|building|hall|terminal)\s+\w+|\w+\s+pier\b", p, re.I)
    ]
    outer = [p for p in parts if p not in inner]
    if inner and outer:
        whole = _locate(outer[0])
        hit = _osm(f"{inner[0]}, {outer[0]}")
        if (
            whole
            and hit
            and _words(_norm(inner[0])) <= _words(_norm(hit["name"]))
            and (T._metres(hit, whole) < 2500)
        ):
            return {**hit, "name": f"{outer[0]}, {hit['name']}"}
    # An address or corner part is the most exact; a borough or town adds nothing on its own.
    exact = [p for p in parts if _EXACT.search(p)]
    if " in " in raw and "," not in raw:
        found = _locate(", ".join(parts))
        if found:
            return checked(found, parts[0])
    # A place in a town outside the city ("Red Bull Arena, Harrison, NJ"): OpenStreetMap
    # knows it, maybe by a newer name, and it must be in that town.
    if town and not T.zone_of(town["lat"], town["lon"]) and not _EXACT.search(parts[0]):
        there = _osm(f"{parts[0]}, {last}")
        if there and T._metres(there, town) < 5000:
            return {**there, "name": parts[0]}
    for p in exact + [p for p in parts if p not in exact]:
        if _norm(p) in boroughs or (streetish.search(p) and p not in exact):
            continue  # a bare street ("Fifth Avenue") says nothing about where on it
        found = _locate(p)
        if (
            found
            and town
            and p in exact
            and T._metres(found, town) > 3000
            and not T.zone_of(town["lat"], town["lon"])
        ):
            # "14 Wall Street, Huntington": the address in that town, not the one in the city.
            there = _osm(f"{p}, {last}") or _osm(f"{p}, {last.split(',')[0]}")
            if there and T._metres(there, town) < 8000:
                return {**there, "name": f"{p}, {last}"}
        if found:
            return checked(found, p)
    return town and {**town, "name": f"near {town['name']}"}


# Words a shop's full name may add to what people call it ("Peter Luger" Steak House).
_KIND = {
    *("steak", "house", "steakhouse", "restaurant", "deli", "delicatessen", "bar", "cafe"),
    *("coffee", "pizza", "pizzeria", "bakery", "cinema", "theater", "theatre", "club", "store"),
    *("shop", "bookstore", "books", "grill", "diner", "tavern", "pub", "kitchen", "brooklyn"),
    *("manhattan", "queens", "bronx", "and", "co", "company", "new", "york"),
}

_ROAD = re.compile(
    r"\b(?:st|street|ave?|avenue|blvd|boulevard|rd|road|pl|place|pkwy|parkway|sq|square)\b", re.I
)


def _shops(name: str, at: dict) -> list[dict]:
    """Shops, restaurants and venues of that name, as the trip planner's place search lists
    them around a point, the closest name first."""
    want = set(_norm(name).split())
    if not want - _KIND:  # "the bakery"
        return []
    # One Nitehawk is mapped as "Nitehawk Cinema", the other as Nitehawk: both are asked for.
    core = " ".join(w for w in name.split() if _norm(w) not in _KIND)
    both = bool(core) and core != name
    texts = [name, core] if both else [name]
    if "'" not in name and "\u2019" not in name:  # "Juniors" is written Junior's
        texts.append(re.sub(r"^(\w{3,})s\b", r"\1's", name))
    found: dict[tuple, tuple] = {}
    for text in dict.fromkeys(texts):
        try:
            hits = get_json(
                GEOCODE, {"text": text, "place": f"{at['lat']:.4f},{at['lon']:.4f}"}, ttl_s=86400
            )
        except Exception:  # noqa: BLE001
            break
        for h in hits or []:
            got = set(_norm(h.get("name") or "").split())
            if h.get("type") != "PLACE" or T._metres(h, at) > 40000:
                continue
            # "Nitehawk Cinema" is mapped as Nitehawk: a kind word may be missing from the map's
            # name, but not swapped for another ("Joe's Pizza" is not Joe's Bar).
            gone, extra = want - got, got - want
            if extra <= _KIND and (not gone or gone <= _KIND and not extra):
                key, fit = (round(h["lat"], 4), round(h["lon"], 4)), len(extra) + len(gone)
                if key not in found or fit < found[key][0]:
                    found[key] = (fit, T._metres(h, at), h)
        if found and not (both and text == name):
            break
    return [h for *_, h in sorted(found.values(), key=lambda x: x[:2])]


def _place_in(raw: str) -> dict | None:
    """ "Katz's Deli, Lower East Side", "Joe's Pizza in Greenwich Village": the place of that
    name in that area. Not for streets, stations or places known by their own name."""
    m = re.match(r"(.+?)(?:\s*,\s*|\s+(?:in|at)\s+(?:the\s+)?(?=[A-Z]))(.+)$", raw)
    if not m:
        return None
    name, area = m.group(1).strip(), m.group(2).strip()
    n = _norm(name)
    exact = [x for x in (name, area) if _EXACT.search(x) or _HINT.search(x) or _ROAD.search(x)]
    if exact == [area] and _landmark(_norm(area)):
        exact = []  # Union Square
    if not n or exact or re.match(r"[\d-]|(?:pier|gate|building|hall)\b", area, re.I):
        return None
    if _known(name):
        return None
    if "," not in raw and _corner(raw):
        return None
    boro = _borough_named(area)
    bare = _norm(re.sub(r"\b(?:new york|nyc?|usa)\b", " ", area, flags=re.I))
    if boro and bare in _BORO_CENTER:
        # Only the borough is known: good enough when it has just one place of that name.
        lat, lon = _BORO_CENTER[boro]
        fits = [
            h
            for h in _shops(name, {"lat": lat, "lon": lon})
            if (z := T.zone_of(h["lat"], h["lon"]))
            and (T._zone_borough(z[0]) or "").lower() == boro
        ]
        if not fits or any(T._metres(h, fits[0]) > 200 for h in fits):
            return None
        return {"name": fits[0]["name"], "lat": fits[0]["lat"], "lon": fits[0]["lon"]}
    at = _locate(area)
    if not at or at["name"].startswith("near "):
        return None
    town = not T.zone_of(at["lat"], at["lon"])
    reach = 5000 if town else 800 if _landmark(_norm(area)) else 2200
    apart: list[dict] = []
    for h in _shops(name, at):
        if T._metres(h, at) < reach and all(T._metres(h, x) > 150 for x in apart):
            apart.append(h)
    if not apart:
        return None
    h, around = apart[0], area.split(",")[0]
    # The name says when there are several, and when the closest is in the area next door.
    notes = [f"one of {len(apart)} around {around}"] if len(apart) > 1 else []
    z = None if town or reach == 800 else T.zone_of(h["lat"], h["lon"])
    if (
        notes
        and z
        and T._metres(h, at) > 600
        and not _words(_norm(str(z[1]))) & _words(_norm(around))
    ):
        notes.insert(0, f"in {z[1]}")
    more = f" ({', '.join(notes)})" if notes else ""
    return {"name": h["name"] + more, "lat": h["lat"], "lon": h["lon"]}


def _area_at_end(raw: str) -> dict | None:
    """ "Nitehawk Park Slope": a name with its neighborhood after it and no comma."""
    words = raw.split()
    for k in (3, 2, 1):
        if len(words) <= k:
            continue
        head, tail = " ".join(words[:-k]), " ".join(words[-k:])
        if not tail[0].isupper() or _known(head) or not _neighborhood(_norm(tail)):
            continue
        return _place_in(f"{head}, {tail}")
    return None


def _only_one(name: str) -> dict | None:
    """A place named without its area ("Peter Luger"), when the city has just one."""
    if _ROAD.search(name) or _HINT.search(name) or re.search(r"\d", name) or len(name) < 5:
        return None
    fits = [
        h
        for h in _shops(name, {"lat": 40.73, "lon": -73.95})
        if h.get("category") not in (None, "none") and T.zone_of(h["lat"], h["lon"])
    ]
    if not fits or any(T._metres(h, fits[0]) > 200 for h in fits):
        return None
    return {"name": fits[0]["name"], "lat": fits[0]["lat"], "lon": fits[0]["lon"]}


def _known(name: str) -> bool:
    """A landmark, neighborhood, station or airport in its own right."""
    n = _norm(name)
    low = name.lower()
    return bool(
        _landmark(n)
        or _neighborhood(n)
        or _path_station(n)
        or railfares.station(name)
        or n in _BORO_CENTER
        or n in T.NAMED
        or any(re.search(rf"\b{k}\b", low) for k in T.AIRPORT_WORDS)
    )


def _no_article(q: str) -> str:
    """OpenStreetMap finds nothing for "the Liberty Science Center"."""
    return re.sub(r"^\s*the\s+", "", q, flags=re.I)


def _osm(q: str) -> dict | None:
    """The first OpenStreetMap match for a query, inside the region."""
    q = _no_article(q)
    hits = osm.search(q)
    if not hits:
        return None
    h = hits[0]
    return {"name": h.get("name") or q, "lat": float(h["lat"]), "lon": float(h["lon"])}


# A part of a place name that pins it down: an address, a corner, a station or stop.
_EXACT = re.compile(r"^\s*\d+\s+\w|\band\b|&|\bat\b|\b(?:station|stop)\b|\bbeach\s+\d+", re.I)


def _ferry_stop(name: str, asked: str = "") -> dict | None:
    """A ferry landing by the name of the place it serves, from the trip planner's stops."""
    want = _key_words(name) - {"ferry", "landing", "terminal", "dock", "pier"}
    if not want:
        return None
    said = _key_words(asked or name)
    hits = []
    # With "terminal" when it was asked for: the Staten Island Ferry's St. George Terminal.
    for words in dict.fromkeys([_said(want | ({"ferry", "terminal", "pier"} & said)), _said(want)]):
        try:
            hits += (
                get_json(
                    GEOCODE, {"text": words, "type": "STOP", "place": _bias(name)}, ttl_s=86400
                )
                or []
            )
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
    label = (
        h["name"] if re.search(r"terminal|ferry", h["name"], re.I) else f"{h['name']} ferry landing"
    )
    return {"name": label, "lat": h["lat"], "lon": h["lon"]}


def _line_station(name: str, lines: set[str], area: str = "") -> dict | None:
    """The subway station of that name served by those lines, from the MTA's station list."""
    from . import access

    want = _station_words(name)
    core = want - set(_STREET.values())  # "Avenue" alone names no station
    try:
        rows = access._stations()
    except Exception:  # noqa: BLE001
        return None
    boro = next(
        (
            c
            for w, c in (
                ("brooklyn", "Bk"),
                ("queens", "Q"),
                ("bronx", "Bx"),
                ("staten island", "SI"),
                ("manhattan", "M"),
            )
            if w in area.lower()
        ),
        None,
    )
    fits = []
    for r in rows:
        got = _station_words(r.get("stop_name") or "")
        routes = set((r.get("daytime_routes") or "").split())
        if core and core <= got and len(got - want) <= 2 and lines & routes:
            elsewhere = boro is not None and r.get("borough") != boro
            # "Gun Hill Rd (5)" is the one only the 5 serves, not the one on the 2 and 5.
            fits.append((elsewhere, len(got ^ want), len(routes - lines), r))
    if not fits:
        return None
    r = min(fits, key=lambda x: x[:3])[3]
    routes = sorted(set((r.get("daytime_routes") or "").split()))
    return {
        "name": f"{r['stop_name']} ({' '.join(routes)})",
        "lat": float(r["gtfs_latitude"]),
        "lon": float(r["gtfs_longitude"]),
    }


_RAILROAD = re.compile(
    r"lirr|railroad|metro.?north|nj ?transit|\bpath\b|amtrak|ferry|\bbus\b", re.I
)
# The MTA shortens names ("W 4 St-Wash Sq", "Atlantic Av-Barclays Ctr").
_SHORT = {
    **{"west": "w", "east": "e", "center": "ctr", "centre": "ctr", "washington": "wash"},
    **{"turnpike": "tpke", "highway": "hwy", "heights": "hts", "sts": "st", "plz": "plaza"},
    **{
        w: str(i)
        for i, w in enumerate(
            "first second third fourth fifth sixth seventh eighth ninth tenth".split(), 1
        )
    },
}


def _station_words(text: str) -> set[str]:
    return {_SHORT.get(w, w) for w in _key_words(text)}


def _subway_station(raw: str, loose: bool = False) -> dict | None:
    """ "9 Av station, Brooklyn": the subway station of exactly that name there, or the only one
    with those words in its name ("Union Square station" is 14 St-Union Sq). Loose: also when the
    reader did not write station."""
    if _RAILROAD.search(raw):
        return None
    if not loose and not re.search(r"\b(?:station|subway|stop)\b", raw, re.I):
        return None
    from . import access

    name, _, area = raw.partition(",")
    tail = re.match(r"(.+?\b(?:station|subway|stop))\s+(?:in\s+)?(\S.*)$", name, re.I)
    if tail and not area:  # "96th St station Upper West Side"
        name, area = tail.groups()
    want, boro = _station_words(name), _borough_named(area)
    if not want - set(_STREET.values()) - {"w", "e"}:
        return None
    try:
        rows = [(r, _station_words(r.get("stop_name") or "")) for r in access._stations()]
    except Exception:  # noqa: BLE001 - the other ways of finding it remain
        return None
    codes = {"manhattan": "M", "brooklyn": "Bk", "queens": "Q", "bronx": "Bx"}
    if boro:
        rows = [x for x in rows if x[0].get("borough") == codes.get(boro, "SI")]
    for fits in (
        [r for r, got in rows if got == want],
        [r for r, got in rows if want <= got],
    ):
        hood = _neighborhood(_norm(area)) if area.strip() and fits else None
        if hood and not hood.get("around") and boro:
            hood = None
        if hood:  # the nearest one to the area named, if it is there
            spot = lambda r: {  # noqa: E731
                "lat": float(r["gtfs_latitude"]),
                "lon": float(r["gtfs_longitude"]),
            }
            near = min(fits, key=lambda r: T._metres(hood, spot(r)))
            fits = [near] if T._metres(hood, spot(near)) < 2500 else []
        if len({r.get("complex_id") for r in fits}) == 1:
            routes = sorted({x for r in fits for x in (r.get("daytime_routes") or "").split()})
            return {
                "name": f"{fits[0]['stop_name']} ({' '.join(routes)})",
                "lat": float(fits[0]["gtfs_latitude"]),
                "lon": float(fits[0]["gtfs_longitude"]),
            }
        if fits:
            return None  # several: the reader has to say which
    return None


def _station_in_hood(raw: str) -> dict | None:
    """ "Bay Ridge 86th street": a neighborhood and the station of that name there."""
    words = raw.replace(",", " ").split()
    for k in range(1, min(4, len(words))):
        for hood, rest in ((words[:k], words[k:]), (words[-k:], words[:-k])):
            if centers.find(" ".join(hood)):
                hit = _subway_station(f"{' '.join(rest)}, {' '.join(hood)}", loose=True)
                if hit:
                    return hit
    return None


def _locate(text: str) -> dict | None:
    raw = (text or "").strip()[:120]
    if not raw:
        return None
    m = re.fullmatch(
        r"\s*(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)\s*(?:\((home|work)\))?\s*", raw, re.I
    )
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        # Coordinates come from a reader who shared their location or saved home or work.
        name = m.group(3).capitalize() if m.group(3) else "Your location"
        return {"name": name, "lat": lat, "lon": lon} if _inside(lat, lon) else None
    rail = railfares.station(raw)
    if rail:
        return rail
    n = _norm(raw)
    low = " ".join(re.sub(r"[^a-z0-9 ]+", " ", raw.lower()).split())
    term = re.search(r"\bterminal\s*([a-c1-8])\b|\bt([1-8])\b", low)
    term = (term.group(1) or term.group(2)).upper() if term else None
    station = _subway_station(raw)
    if station:
        return station
    for key, ap in T.AIRPORT_WORDS.items():
        if re.search(rf"\b{key}\b", low):
            out = {"name": ap["name"], "lat": ap["lat"], "lon": ap["lon"], "airport": ap["code"]}
            if term and term in ap.get("terminals", {}):
                out["terminal"] = term
                out["name"] = f"{ap['name']} Terminal {term}"
                out["lat"], out["lon"] = ap["terminals"][term]
            return out
    # LaGuardia's terminals are far apart (B to C is a 15 minute walk): the bus stops at each.
    if re.search(r"\b(?:lga|la ?guardia)\b", low) and term in T.LGA_TERMINALS:
        lat, lon = T.LGA_TERMINALS[term]
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
    said = _borough_named(raw)
    if bare not in T.NAMED:
        hood = centers.find(bare or n, said)
        if hood:  # before landmarks: Battery Park City is not a lawn in the Battery
            return hood
    mark = _landmark(n) or (_landmark(bare) if bare else None)
    # A landmark of that name in another borough. (The Staten Island Ferry leaves Manhattan.)
    if mark and said and n not in _landmarks():
        z = T.zone_of(mark["lat"], mark["lon"])
        if z and (T._zone_borough(z[0]) or "").lower() != said:
            mark = None
    # "Strand Bookstore, Union Square" is the shop, not the park.
    own = re.split(r",|\s(?:in|at)\s", raw, maxsplit=1)
    inside = len(own) > 1 and _norm(own[0]) and not _known(own[0]) and not _borough_named(own[0])
    if mark and not (inside and _landmark(_norm(own[1]))):
        return mark
    if bare and bare != n and bare not in T.NAMED:
        hood = _neighborhood(bare, said)
        if hood:
            return hood
    # "Razza, Jersey City" or "Juniors in Downtown Brooklyn" is a place there, not the area.
    head = _norm(re.split(r",|\s(?:in|at)\s", raw, maxsplit=1)[0])
    for key, (name, lat, lon) in T.NAMED.items():
        if n == key or (
            re.search(rf"\b{key}\b", n)
            and len(n.split()) <= len(key.split()) + 2
            and (not head or key in head)
        ):
            z = T.zone_of(lat, lon)
            if said and z and (T._zone_borough(z[0]) or "").lower() != said:
                continue  # "Grand Army Plaza, Manhattan" is not Brooklyn's
            return {"name": name, "lat": lat, "lon": lon}
    if _HINT.search(raw) or re.search(r"[A-Za-z]-[A-Z0-9]", raw):
        stop = _stop(raw)
        if stop:
            return stop
    hood = _neighborhood(n, said)
    if hood:
        return hood
    # A street named without a house number is usually its station ("Metropolitan Av" on the
    # G), not one address somewhere along it.
    if re.search(
        r"\b(?:av|ave|avenue|st|street|blvd|boulevard|pkwy|parkway|rd|road|plaza)\b", n
    ) and not (re.search(r"\d+\s*-?\d*\s+\w", raw.strip()[:6]) and raw.strip()[0].isdigit()):
        stop = _station_in_hood(raw) or _subway_station(raw, loose=True) or _stop(raw)
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
                "https://geosearch.planninglabs.nyc/v2/search",
                {"text": text, "size": 5},
                ttl_s=86400,
            )
            f = _same_address(d.get("features") or [], said, raw)
        except Exception:  # noqa: BLE001 - fall through to the regional search
            f = None
        if f and _words(n) <= _words(_norm(f["properties"].get("label", "").split(",")[0])) | {
            "new",
            "york",
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
        # A word may be missing from an address, not from a name: "Nitehawk Prospect Park".
        close = len(want) >= 3 and len(want ^ got) <= 1 and (want <= got or re.match(r"\s*\d", raw))
        if want and not foreign and (want == got or close):
            return in_its_park(f)
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
    queries = [_no_article(raw)]
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
        hits = osm.search(q)
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
            # A town outside the city ("White Plains, NY") comes back as the middle of its
            # boundary, which the transit planner may not connect to anything. Its train
            # station is where a trip there starts or ends.
            town = outside or h.get("addresstype") in ("city", "town", "village", "hamlet")
            if town or re.match(r"(?:city|town|village) of\b", name, re.I):
                station = _rail_stop(raw)
                if station and T._metres(station, {"lat": lat, "lon": lon}) < 6000:
                    return {**station, "name": _station_name(station["name"])}
            return {"name": f"near {name}" if k else name, "lat": lat, "lon": lon}
    # "Vernon Jackson", "Atlantic Barclays": the words of one station's name.
    return (_subway_station(raw, loose=True) if not outside else None) or _rail_stop(raw)


# Commuter rail reaches well past the city: Montauk, New Haven, Poughkeepsie, Trenton.
RAIL_BOX = (-75.0, 40.1, -71.8, 41.75)

_RAIL_WORDS = {
    "lirr",
    "station",
    "stop",
    "train",
    "rail",
    "railroad",
    "metro",
    "north",
    "mnr",
    "njt",
    "nj",
    "transit",
    "amtrak",
    "ny",
    "the",
}

_RAIL_MODES = {"REGIONAL_RAIL", "RAIL", "SUBURBAN", "HIGHSPEED_RAIL", "LONG_DISTANCE", "FERRY"}


_KEEP_CAPS = {"NJ", "NY", "MSU", "EWR", "JFK", "PATH", "LIRR", "US"}


def _station_name(name: str) -> str:
    """ "SUMMIT RAIL STATION" (NJ Transit writes names in capitals) is "Summit Rail Station";
    the word station is added only when the name lacks it."""
    if name.isupper():
        name = " ".join(w if w in _KEEP_CAPS else w.capitalize() for w in name.split())
    return name if re.search(r"\bstation\b", name, re.I) else f"{name} station"


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
                T.TRANSITOUS.replace("/api/v5/plan", "/api/v1/geocode"), {"text": text}, ttl_s=86400
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
            and not (
                _words(_norm(h.get("name") or "")) - want - _RAIL_WORDS - {"union", "junction"}
            )
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
            # "White Plains" is White Plains, not North White Plains.
            h = min(rail, key=lambda h: len(_words(_norm(h.get("name") or "")) - want))
            return {"name": h["name"], "lat": h["lat"], "lon": h["lon"]}
    return None
