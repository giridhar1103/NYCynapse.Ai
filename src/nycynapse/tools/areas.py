"""Neighborhoods and ZIP codes by name, as the city draws them.

A count for "Washington Heights" should not depend on which ZIP codes somebody thinks of, so a
name becomes the city's own neighborhood outlines. The outlines are finer than the ones the map
draws; what happens on a boundary street still falls on either side.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from ..config import REPO

GEO = REPO / "web" / "static" / "geo"
MAX_OUTLINE = 12_000  # characters of a query's address, which the city still answers

BOROUGHS = ("Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island")
_ALIASES = {
    "fidi": "financial district",
    "uws": "upper west side",
    "ues": "upper east side",
    "les": "lower east side",
    "lic": "long island city",
    "bed stuy": "bedford stuyvesant",
    "bedstuy": "bedford stuyvesant",
    "plg": "prospect lefferts gardens",
    "stuytown": "stuyvesant town",
    "stuy town": "stuyvesant town",
    "spanish harlem": "east harlem",
    "el barrio": "east harlem",
    "midtown east": "east midtown",
    "flatiron district": "flatiron",
    "saint albans": "st albans",
    "saint george": "st george",
}


def norm(s: str) -> str:
    s = re.sub(r"\(.*?\)", " ", s.lower().replace("'", "").replace("’", ""))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(w for w in s.split() if w not in {"the", "neighborhood", "area"})


@lru_cache(maxsize=1)
def _hoods() -> list[tuple[str, str, str, list[str]]]:
    """(name, borough, whole name, its parts): "Astoria (North)" is Astoria as well."""
    return [
        (p["name"], p["borough"], norm(p["name"]), [norm(x) for x in re.split(r"[-/]", p["name"])])
        for p in (f["properties"] for f in json.loads((GEO / "nta.json").read_text())["features"])
        if p.get("kind") == "residential"
    ]


def _named(n: str) -> list[tuple[str, str]]:
    return [
        (name, boro)
        for name, boro, whole, parts in _hoods()
        if n == whole or n in parts or f"old {n}" in parts
    ]


@lru_cache(maxsize=1)
def _outlines() -> dict[str, dict]:
    fine = json.loads((GEO / "outlines.json").read_text())["features"]
    return {f["properties"]["name"]: f["geometry"] for f in fine}


@lru_cache(maxsize=1)
def _coarse() -> dict[str, dict]:
    feats = json.loads((GEO / "nta.json").read_text())["features"]
    return {f["properties"]["name"]: f["geometry"] for f in feats}


@lru_cache(maxsize=1)
def _zips() -> dict[str, dict]:
    feats = json.loads((GEO / "zips.json").read_text())["features"]
    # One outline can stand for several ZIP codes.
    return {z.strip(): f for f in feats for z in f["properties"]["zips"].split(",")}


def _wkt(geometries: list[dict]) -> str:
    polys = []
    for g in geometries:
        for poly in g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]:
            rings = (",".join(f"{x:.5f} {y:.5f}" for x, y, *_ in ring) for ring in poly)
            polys.append("(" + ",".join(f"({r})" for r in rings) + ")")
    return f"MULTIPOLYGON({','.join(polys)})"


def outline(text: str) -> dict:
    """{wkt, names, note, item} for one neighborhood or ZIP code, or {error}. The borough may
    follow the name ("Murray Hill, Queens")."""
    z = re.fullmatch(r"\D*(1[01]\d{3})\D*", text)
    if z:
        f = _zips().get(z.group(1))
        if not f:
            return {"error": f"no area is drawn for ZIP code {z.group(1)}; use the ZIP column"}
        p = f["properties"]
        note = f"ZIP code {z.group(1)}"
        if "," in p["zips"]:
            note += f" (drawn together: {p['zips']})"
        wkt = _wkt([_outlines()[p["name"]]])
        return {
            "wkt": wkt if len(wkt) <= MAX_OUTLINE else _wkt([f["geometry"]]),
            "names": [z.group(1)],
            "note": note,
            "item": {"name": f"ZIP {z.group(1)}", "level": "zip", "key": p["name"]},
        }
    name, _, rest = text.partition(",")
    boro = next((b for b in BOROUGHS if b.lower() in rest.lower()), None)
    n = norm(name)
    n = _ALIASES.get(n, n)
    if n in {b.lower() for b in BOROUGHS} and not boro:
        return {"error": f"{name.strip()} is a borough; use the borough column in where"}
    hits = [h for h in _named(n) if boro in (None, h[1])] if n else []
    if not hits:
        return {
            "error": f"the city has no neighborhood named {text.strip()!r}; use a neighborhood "
            "it does have, a ZIP code, or within_circle around a point in where"
        }
    where = sorted({b for _, b in hits})
    if len(where) > 1:
        return {"error": f"{name.strip()} is in {' and in '.join(where)}; name the borough"}
    names = [h[0] for h in hits]
    wkt = _wkt([_outlines()[x] for x in names])
    if len(wkt) > MAX_OUTLINE:  # a long shoreline: the map's outline is close enough
        wkt = _wkt([_coarse()[x] for x in names])
    if len(wkt) > MAX_OUTLINE:
        return {"error": f"{name.strip()} is too large to draw in one query; use ZIP codes"}
    note = "the city's neighborhood outlines of " + "; ".join(names)
    joined = [x for x in names if norm(x) != n]
    if joined:
        note += " (an outline that carries other names too covers those as well: say so)"
    near = [
        x
        for x, b, whole, _ in _hoods()
        if b == where[0] and x not in names and f" {n} " in f" {whole} "
    ]
    if near:
        note += f". Not counted, though the name is in theirs: {', '.join(near)}"
    return {
        "wkt": wkt,
        "names": names,
        "note": note,
        "item": {"name": name.strip(), "level": "nta", "key": names},
    }
