"""Turn the places an answer names into something the map can highlight.

The model lists the places its answer is about ("Mott Haven", "11224", "Brooklyn", "Barclays
Center"). Each name is matched against the map's own outlines first (bridges and tunnels,
boroughs, neighborhoods, ZIP code areas), then against what the tools already highlighted,
and only then looked up with NYC GeoSearch as a point. Anything that cannot be placed in the
city is left off the map.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from ..config import REPO
from ..tools import live
from ..tools.areas import norm as _norm

GEO = REPO / "web" / "static" / "geo"
MAX_PLACES = 6
_BOROUGH_ALIASES = {
    "the bronx": "Bronx",
    "bronx": "Bronx",
    "brooklyn": "Brooklyn",
    "manhattan": "Manhattan",
    "queens": "Queens",
    "staten island": "Staten Island",
}
_AIRPORTS = {"jfk": "John F. Kennedy International Airport", "lga": "LaGuardia Airport"}
_COMMON = {"the", "nyc", "new", "york", "and", "of", "at", "in", "ny"}
_index: dict | None = None


_ABBR = {
    "ave": "avenue",
    "av": "avenue",
    "st": "street",
    "blvd": "boulevard",
    "pl": "place",
    "rd": "road",
    "pkwy": "parkway",
    "sq": "square",
}


def _load() -> dict:
    global _index
    if _index is None:
        nta = json.loads((GEO / "nta.json").read_text())["features"]
        zips = json.loads((GEO / "zips.json").read_text())["features"]
        _index = {
            "nta": [
                (
                    f["properties"]["name"],
                    _norm(f["properties"]["name"]),
                    [_norm(p) for p in re.split(r"[-/]", f["properties"]["name"])],
                )
                for f in nta
                if f["properties"].get("kind") in (None, "residential", "park", "airport")
            ],
            "zips": {f["properties"]["name"]: f["properties"] for f in zips},
            "crossings": [
                (f["properties"]["name"], [_norm(a) for a in f["properties"]["aliases"]])
                for f in json.loads((GEO / "crossings.json").read_text())["features"]
            ],
        }
    return _index


def _match_area(name: str) -> dict | None:
    idx = _load()
    n = _norm(name)
    if not n:
        return None
    # Bridges and tunnels, drawn from their real outlines ("GWB lower level" is the GWB).
    for full, aliases in idx["crossings"]:
        if any(re.search(rf"\b{re.escape(a)}\b", n) for a in aliases):
            return {"level": "crossing", "key": full}
    if n in _BOROUGH_ALIASES:
        return {"level": "borough", "key": _BOROUGH_ALIASES[n]}
    z = re.search(r"\b(1\d{4})\b", name)
    if z and z.group(1) in idx["zips"]:
        return {"level": "zip", "key": z.group(1)}
    for short, full in _AIRPORTS.items():
        if short in n.split() or n == _norm(full):
            return {"level": "nta", "key": [full]}
    # Neighborhoods split in parts ("Astoria (North)", "Astoria (Central)") all count.
    scored: dict[int, list[str]] = {}
    for full, whole, parts in idx["nta"]:
        if n == whole:
            s = 4
        elif n in parts:
            s = 3
        elif any(p.startswith(n + " ") for p in parts):
            s = 2
        elif len(n) > 5 and re.search(rf"\b{re.escape(n)}\b", whole):
            s = 1
        else:
            continue
        scored.setdefault(s, []).append(full)
    if not scored:
        return None
    if 4 in scored or 3 in scored:
        return {"level": "nta", "key": scored.get(4, []) + scored.get(3, [])}
    return {"level": "nta", "key": scored[max(scored)][:1]}


def _words(text: str) -> set[str]:
    text = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", r"\1", _norm(text))
    return {_ABBR.get(w, w) for w in text.split()} - _COMMON


def _lookup(name: str, near: tuple[float, float] | None = None) -> dict | None:
    from ..tools import placenames  # verified well-known places first ("Union Square", "MSG")

    mark = placenames._landmark(placenames._norm(name))
    if mark:
        return {"level": "point", "lat": mark["lat"], "lon": mark["lon"]}
    try:
        r = live.find_place(name)
    except Exception:  # noqa: BLE001 - a place that cannot be found is simply not drawn
        return None
    if not r.ok or not r.data:
        return None
    # GeoSearch always returns something; keep it only when every word asked for is in the
    # name it found ("Union City" must not become "Industry City").
    # The match must be the same name both ways: "Weehawken" is not "Weehawken Street".
    want = _words(name)
    same = [d for d in r.data if want and _words((d.get("label") or "").split(",")[0]) == want]
    if not same or not (want == _words((r.data[0].get("label") or "").split(",")[0]) or near):
        return None
    # "65 4th Avenue" is in Manhattan and in Brooklyn: the one near what the tools found.
    if near and len(same) > 1:
        same.sort(key=lambda d: (d["lat"] - near[0]) ** 2 + ((d["lon"] - near[1]) * 0.76) ** 2)
    return {"level": "point", "lat": same[0]["lat"], "lon": same[0]["lon"]}


def resolve(
    places: list[dict], hints: list[dict] | None = None, near: list[tuple] | None = None
) -> list[dict]:
    """[{name, note}] from the model -> highlight items the map can draw. `near` holds points
    the tools found, so an address in two boroughs is taken where the answer is."""
    hinted = {_norm(h.get("name", "")): h for h in hints or [] if h.get("name")}
    items: list[dict | None] = []
    todo: list[int] = []
    for p in places[:MAX_PLACES]:
        name = str(p.get("name") or "").strip()[:80]
        if not name:
            continue
        note = str(p.get("note") or "").strip()[:80]
        h = hinted.get(_norm(name))
        hit = (
            {k: h[k] for k in ("level", "key", "lat", "lon") if k in h} if h else _match_area(name)
        )
        items.append({"name": name, "note": note, **hit} if hit else {"name": name, "note": note})
        if not hit:
            todo.append(len(items) - 1)
    # Named places first, then street addresses ("65 4th Avenue" is in Manhattan and in
    # Brooklyn), each taken near the middle of what the tools and the named places found.
    first = [i for i in todo if not re.match(r"\d", items[i]["name"])]
    later = [i for i in todo if i not in first]
    for group in (first, later):
        if not group:
            continue
        spots = list(near or []) + [
            (i["lat"], i["lon"]) for i in items if i and i.get("level") == "point" and "lat" in i
        ]
        if group is later and not spots:  # "Union Square" is drawn as its area: its point
            from ..tools import placenames

            for i in items:
                mark = i and i.get("level") and placenames._landmark(placenames._norm(i["name"]))
                if mark:
                    spots.append((mark["lat"], mark["lon"]))
        middle = (
            (
                sorted(p[0] for p in spots)[len(spots) // 2],
                sorted(p[1] for p in spots)[len(spots) // 2],
            )
            if spots
            else None
        )
        with ThreadPoolExecutor(max_workers=min(4, len(group))) as pool:
            for i, hit in zip(
                group,
                pool.map(lambda i, m=middle: _lookup(items[i]["name"], m), group),
                strict=True,
            ):
                if hit:
                    items[i].update(hit)
    return [i for i in items if i and "level" in i]
