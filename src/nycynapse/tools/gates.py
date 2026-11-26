"""Big fenced places entered by their gates.

The address search pins the Bronx Zoo at its middle, so a bus trip there ended with a twenty minute
walk across the zoo. A trip goes to the gate nearest where it comes from (bridges.nearer picks it),
or to the gate the reader named. Gates are OpenStreetMap's named entrances (2026-11-26).
"""

from __future__ import annotations

import re

_GATES: dict[str, list[tuple[str, float, float]]] = {
    # The zoo's gate points are on its inside paths, which a walk reaches the long way round:
    # the street corner outside each gate (50 and 70 m). The Bronx River Gate is for parking.
    "Bronx Zoo": [
        ("Asia Gate, Boston Rd and Bronx Park South", 40.84400, -73.87734),
        ("Southern Boulevard Gate, at E 183 St", 40.85107, -73.88232),
    ],
    "Brooklyn Botanic Garden": [
        ("Eastern Parkway entrance", 40.671937, -73.964966),
        ("Washington Avenue entrance", 40.669463, -73.962646),
        ("Flatbush Avenue entrance", 40.663448, -73.962429),
    ],
    "Green-Wood Cemetery": [
        ("Main Entrance, 5th Ave and 25th St", 40.659010, -73.995613),
        ("Prospect Park West entrance", 40.656503, -73.984115),
        ("Sunset Park entrance", 40.655025, -74.003332),
        ("Fort Hamilton Parkway entrance", 40.647087, -73.982304),
    ],
}
_NAMES = {
    r"bronx zoo": "Bronx Zoo",
    r"brooklyn botanic(?:al)? gardens?": "Brooklyn Botanic Garden",
    r"green-? ?wood(?: cemetery)?": "Green-Wood Cemetery",
}
_BOROUGH = r"(?:\s*,?\s*(?:in\s+)?(?:the\s+)?(?:bronx|brooklyn|ny|nyc|new york))*"


def _key(s: str) -> set[str]:
    return set(re.findall(r"[a-z]+", s.lower())) - {"gate", "entrance", "the", "and", "st", "ave"}


def find(text: str) -> dict | None:
    """The place with its gates to pick from, or the one gate the text names."""
    t = re.sub(r"^\s*the\s+", "", (text or "").strip().lower())
    for pattern, title in _NAMES.items():
        m = re.match(pattern, t)
        if not m:
            continue
        rest = t[m.end() :]
        gates = [
            {"name": f"{title}, {g}", "lat": lat, "lon": lon, "gate": True}
            for g, lat, lon in _GATES[title]
        ]
        if re.fullmatch(_BOROUGH + r"\s*", rest):
            return {**gates[0], "name": title, "ends": gates}
        said = _key(rest)
        named = [g for g in gates if said and said <= _key(g["name"])]
        if re.search(r"\b(gate|entrance)\b", rest) and len(named) == 1:
            return named[0]
        return None
    return None
