"""Bridges people walk, at the ends of their walkways.

A bridge has one pin, in the middle of the water: a trip to it ended there, and one from its
Manhattan side to Chinatown began with a twenty minute walk to Brooklyn. Each end is a corner next
to where the walkway begins (tools/corners.py).
"""

from __future__ import annotations

import re

from . import corners

M, B, Q, J = "Manhattan", "Brooklyn", "Queens", "New Jersey"
_ENDS: dict[str, dict[str, tuple]] = {
    "Brooklyn Bridge": {M: ("Centre St", "Chambers St"), B: ("Washington St", "Prospect St")},
    "Manhattan Bridge": {M: ("Bowery", "Canal St"), B: ("Jay St", "Sands St")},
    "Williamsburg Bridge": {M: ("Delancey St", "Clinton St"), B: ("Bedford Ave", "S 5 St")},
    "Queensboro Bridge": {M: ("1 Ave", "E 60 St"), Q: ("Queens Plaza N", "Crescent St")},
    "George Washington Bridge": {M: ("W 178 St", "Cabrini Blvd"), J: (40.8541, -73.9665)},
}
_NAMES = {
    r"brooklyn bridge": "Brooklyn Bridge",
    r"manhattan bridge": "Manhattan Bridge",
    r"williamsburg bridge": "Williamsburg Bridge",
    r"(?:ed koch )?queensboro(?:ugh)? bridge|(?:59th|59) (?:st|street) bridge": "Queensboro Bridge",
    r"george washington bridge|gwb": "George Washington Bridge",
}
_SIDES = {"manhattan": M, "brooklyn": B, "queens": Q, "new jersey": J, "nj": J, "fort lee": J}
_TAIL = re.compile(
    r"(?:\s+(?:walkway|pedestrian (?:path|walkway|entrance)|bike path|promenade))?"
    r"(?:[\s,(]+(?:on |at |from )?(?:the )?"
    r"(?P<side>manhattan|brooklyn|queens|new jersey|nj|fort lee)"
    r"(?:[\s-]+(?:side|end|tower|entrance))?(?:\s+(?:entrance|walkway|of the bridge))?\)?)?"
    r"(?:,?\s*(?:ny|nyc|new york(?: city)?))?\s*$",
    re.I,
)


def _spot(title: str, side: str) -> dict | None:
    at = _ENDS[title][side]
    if isinstance(at[0], float):
        lat, lon = at
    else:
        hit = next((c for c in corners.find(*at) if c["borough"] == side), None)
        if not hit:
            return None
        lat, lon = hit["lat"], hit["lon"]
    return {"name": f"{title}, {side} side", "lat": lat, "lon": lon}


def find(text: str) -> dict | None:
    """The end of a bridge the text names, or its Manhattan end with both `ends` to pick from."""
    t = re.sub(r"^\s*the\s+", "", text.strip(), flags=re.I)
    for pattern, title in _NAMES.items():
        m = re.match(pattern, t, re.I)
        tail = _TAIL.match(t[m.end() :]) if m else None
        if not tail:
            continue
        side = _SIDES.get((tail.group("side") or "").lower())
        if side:
            return _spot(title, side) if side in _ENDS[title] else None
        ends = [e for e in (_spot(title, s) for s in _ENDS[title]) if e]
        return {**ends[0], "name": title, "ends": ends} if ends else None
    return None


def nearer(place: dict, other: dict, metres) -> dict:
    """The end of a bridge on the other place's side."""
    ends = place.get("ends")
    return min(ends, key=lambda e: metres(e, other)) if ends else place
