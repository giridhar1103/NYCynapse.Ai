# ruff: noqa: E501
"""One-way adult fares on the LIRR, Metro-North and NJ Transit, from the agencies' own fare charts.

Sources (checked 24 November 2026), all linked from mta.info/fares-tolls/lirr-metro-north:
LIRR document/194866, Harlem and Hudson 194931 and their intermediate fares 194936, New Haven
210536 (New York stations 4 January 2026, Connecticut stations 1 July 2026). NJ Transit: the chart of
all fares to and from New York Penn Station, effective 1 July 2026, linked from
njtransit.com/adjustedfares. It is the same on every train. The schedule files the railroads
publish carry no fares, so these tables are kept by hand: when an agency changes its fares,
replace them and move CHECKED. After VALID_UNTIL no price is given and the trip points to
the fare page, as it does for anything this module cannot price.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
CHECKED = date(2026, 11, 24)
VALID_UNTIL = date(2027, 6, 30)

_LIRR_STATIONS = {
    1: "Penn Station, Grand Central, Grand Central Madison, Hunterspoint Avenue, Long Island City, "
    "Atlantic Terminal, Nostrand Avenue, East New York, Woodside, Forest Hills, Kew Gardens, "
    "Mets-Willets Point",
    3: "Jamaica, Locust Manor, Laurelton, Rosedale, St. Albans, Hollis, Queens Village, Flushing, "
    "Flushing-Main Street, Murray Hill, Broadway, Auburndale, Bayside, Douglaston, Little Neck",
    4: "Gibson, Hewlett, Woodmere, Cedarhurst, Lawrence, Inwood, Valley Stream, Westwood, Malverne, "
    "Lakeview, Hempstead Gardens, West Hempstead, Lynbrook, Elmont-UBS Arena, Bellerose, "
    "Stewart Manor, Nassau Boulevard, Garden City, Country Life Press, Hempstead, Floral Park, "
    "New Hyde Park, Merillon Avenue, Mineola, East Williston, Great Neck, Manhasset, Plandome, "
    "Port Washington",
    7: "Centre Avenue, East Rockaway, Oceanside, Island Park, Long Beach, Rockville Centre, Baldwin, "
    "Freeport, Merrick, Bellmore, Wantagh, Seaford, Massapequa, Massapequa Park, Carle Place, "
    "Westbury, Hicksville, Bethpage, Farmingdale, Syosset, Albertson, Roslyn, Greenvale, Glen Head, "
    "Sea Cliff, Glen Street, Glen Cove, Locust Valley, Oyster Bay",
    9: "Amityville, Copiague, Lindenhurst, Babylon, Pinelawn, Wyandanch, Deer Park, "
    "Cold Spring Harbor, Huntington, Greenlawn, Northport",
    10: "Bay Shore, Islip, Great River, Oakdale, Sayville, Patchogue, Brentwood, Central Islip, "
    "Ronkonkoma, Medford, Kings Park, Smithtown, St. James, Stony Brook, Port Jefferson",
    12: "Bellport, Mastic-Shirley, Speonk, Yaphank, Yaphank-BNL",
    14: "Westhampton, Hampton Bays, Southampton, Bridgehampton, East Hampton, Amagansett, Montauk, "
    "Riverhead, Mattituck, Southold, Greenport",
}
_Z = (1, 3, 4, 7, 9, 10, 12, 14)
_PEAK = {
    1: (7.25, 7.25, 13.50, 15.25, 18.25, 21.50, 25.50, 33.00),
    3: (7.25, 6.00, 9.00, 10.75, 13.25, 16.50, 22.00, 28.25),
    4: (13.50, 9.00, 3.75, 3.75, 6.50, 8.25, 12.25, 19.50),
    7: (15.25, 10.75, 3.75, 3.75, 3.75, 6.50, 10.75, 18.00),
    9: (18.25, 13.25, 6.50, 3.75, 3.75, 3.75, 8.25, 14.75),
    10: (21.50, 16.50, 8.25, 6.50, 3.75, 3.75, 3.75, 10.50),
    12: (25.50, 22.00, 12.25, 10.75, 8.25, 3.75, 3.75, 7.25),
    14: (33.00, 28.25, 19.50, 18.00, 14.75, 10.50, 7.25, 3.75),
}
_OFF = {
    1: (5.25, 5.25, 10.00, 11.25, 13.50, 16.00, 18.75, 24.50),
    3: (5.25, 4.50, 6.75, 8.00, 9.75, 12.25, 16.25, 21.00),
}

_MNR_STATIONS = {
    1: "Grand Central, Grand Central Terminal, Harlem-125 St, Harlem-125th Street",
    2: "Yankees-E 153 St, Morris Heights, University Heights, Marble Hill, Spuyten Duyvil, Riverdale, "
    "Melrose, Tremont, Fordham, Botanical Garden, Williams Bridge, Woodlawn, Wakefield",
    3: "Ludlow, Yonkers, Glenwood, Greystone, Mount Vernon West, Fleetwood, Bronxville, Tuckahoe, "
    "Crestwood",
    4: "Hastings-on-Hudson, Dobbs Ferry, Ardsley-on-Hudson, Irvington, Scarsdale, Hartsdale, "
    "White Plains, North White Plains",
    5: "Tarrytown, Philipse Manor, Scarborough, Ossining, Croton-Harmon, Valhalla, Mount Pleasant, "
    "Hawthorne, Pleasantville, Chappaqua",
    6: "Cortlandt, Peekskill, Mount Kisco, Bedford Hills, Katonah, Goldens Bridge",
    7: "Manitou, Garrison, Cold Spring, Breakneck Ridge, Purdys, Croton Falls, Brewster, Southeast",
    8: "Beacon, New Hamburg, Patterson, Pawling, Appalachian Trail",
    9: "Poughkeepsie, Harlem Valley-Wingdale, Dover Plains",
    10: "Tenmile River, Wassaic",
    12: "Mount Vernon East, Pelham, New Rochelle",
    13: "Larchmont, Mamaroneck, Harrison",
    14: "Rye, Port Chester",
    15: "Greenwich, Cos Cob, Riverside, Old Greenwich",
    16: "Stamford, Noroton Heights, Darien, Rowayton",
    17: "South Norwalk, East Norwalk",
    18: "Westport, Greens Farms, Southport, Fairfield, Fairfield-Black Rock",
    19: "Bridgeport",
    20: "Stratford, Milford",
    21: "West Haven, New Haven, New Haven-State Street",
    31: "Glenbrook, Springdale, Talmadge Hill, New Canaan",
    41: "Merritt 7, Wilton, Cannondale",
    42: "Branchville, Redding, Bethel, Danbury",
    51: "Derby-Shelton, Ansonia, Seymour, Beacon Falls, Naugatuck, Waterbury",
}
# To and from Grand Central or Harlem-125 St: peak, off-peak.
_MNR_CITY = {
    1: (7.25, 5.25), 2: (7.25, 5.25), 3: (12.50, 9.25), 4: (13.75, 10.25), 5: (16.00, 11.75),
    6: (19.00, 14.00), 7: (21.75, 16.00), 8: (25.00, 18.50), 9: (28.25, 21.00), 10: (29.75, 22.00),
    12: (12.25, 9.00), 13: (13.75, 10.25), 14: (15.00, 11.00), 15: (16.00, 11.75),
    16: (17.75, 13.25), 17: (19.00, 14.00), 18: (20.75, 15.25), 19: (22.75, 16.75),
    20: (24.25, 18.00), 21: (27.25, 20.25), 31: (17.75, 13.25), 41: (19.75, 14.50),
    42: (20.75, 15.25), 51: (24.25, 18.00),
}  # fmt: skip
# Harlem and Hudson trips that do not touch Manhattan, the same on every train.
_MNR_BETWEEN = {
    2: (3.50, 3.50, 4.50, 6.25, 8.75, 12.50, 15.75, 18.25, 19.50),
    3: (3.50, 3.50, 3.50, 5.50, 8.00, 9.75, 14.25, 16.75, 17.25),
    4: (4.50, 3.50, 3.50, 3.50, 6.50, 8.50, 12.50, 15.00, 15.75),
    5: (6.25, 5.50, 3.50, 3.50, 3.50, 7.25, 9.00, 13.00, 14.00),
    6: (8.75, 8.00, 6.50, 3.50, 3.50, 3.50, 7.25, 9.00, 10.75),
    7: (12.50, 9.75, 8.50, 7.25, 3.50, 3.50, 4.50, 7.00, 8.00),
    8: (15.75, 14.25, 12.50, 9.00, 7.25, 4.50, 3.50, 4.50, 5.00),
    9: (18.25, 16.75, 15.00, 13.00, 9.00, 7.00, 4.50, 3.50, 3.50),
    10: (19.50, 17.25, 15.75, 14.00, 10.75, 8.00, 5.00, 3.50, 3.50),
}
# NJ Transit to or from New York Penn Station, by the zone of the other station, in the spelling of
# NJ Transit's schedule file. Not here, so not priced: the Metro-North stations past
# Suffern and beyond Montvale, and trips that do not touch Penn Station.
_NJT_STATIONS = {
    5.05: "SECAUCUS UPPER LEVEL, SECAUCUS LOWER LEVEL",
    6.30: "NEWARK PENN STATION, NEWARK BROAD ST, LYNDHURST",
    6.60: "RUTHERFORD, DELAWANNA, WOOD-RIDGE",
    8.15: "ORANGE, BRICK CHURCH, EAST ORANGE, GLEN RIDGE, BLOOMFIELD, WATSESSING AVENUE, GARFIELD, "
    "WESMONT, PASSAIC, TETERBORO",
    8.75: "ELIZABETH, NORTH ELIZABETH, UNION, SOUTH ORANGE, MOUNTAIN STATION, HIGHLAND AVENUE, "
    "UPPER MONTCLAIR, WATCHUNG AVENUE, WALNUT STREET, BAY STREET, PLAUDERVILLE, CLIFTON, "
    "ANDERSON STREET, ESSEX STREET",
    9.40: "ROSELLE PARK, MAPLEWOOD, MSU, MONTCLAIR HEIGHTS, MOUNTAIN AVENUE, RADBURN, BROADWAY, "
    "PATERSON, RIVER EDGE, NEW BRIDGE LANDING",
    10.30: "LINDEN, CRANFORD, SHORT HILLS, MILLBURN, HAWTHORNE, ORADELL",
    11.20: "RAHWAY, WESTFIELD, GARWOOD, LITTLE FALLS, GLEN ROCK BORO HALL, GLEN ROCK MAIN LINE, "
    "EMERSON",
    11.55: "HILLSDALE, WESTWOOD, MONTVALE, PARK RIDGE, WOODCLIFF LAKE",
    11.80: "AVENEL, FANWOOD, NEW PROVIDENCE, SUMMIT, MOUNTAIN VIEW, "
    "WAYNE/ROUTE 23 TRANSIT CENTER [RR], RIDGEWOOD",
    13.05: "WOODBRIDGE, METROPARK, NETHERWOOD, MURRAY HILL, CHATHAM, LINCOLN PARK, WALDWICK, HOHOKUS",
    14.30: "METUCHEN, PLAINFIELD, BERKELEY HEIGHTS, MADISON, TOWACO, ALLENDALE",
    14.80: "SUFFERN, MAHWAH, RAMSEY, RAMSEY ROUTE 17 STATION",
    14.85: "PERTH AMBOY, DUNELLEN, GILLETTE, CONVENT",
    16.10: "SOUTH AMBOY, EDISON STATION",
    # njtransit.com/ewr, undated, read 24 November 2026. It includes the AirTrain fee.
    17.25: "NEWARK AIRPORT RAILROAD STATION",
    17.00: "JERSEY AVE., NEW BRUNSWICK, BOUND BROOK, LYONS, MILLINGTON, STIRLING, MORRISTOWN, "
    "MOUNTAIN LAKES, BOONTON",
    17.95: "ABERDEEN-MATAWAN, BRIDGEWATER",
    18.25: "HAZLET, BERNARDSVILLE, BASKING RIDGE, DENVILLE, MOUNT TABOR, MORRIS PLAINS",
    18.50: "MIDDLETOWN NJ, RARITAN, SOMERVILLE, FAR HILLS, DOVER",
    19.50: "RED BANK, NORTH BRANCH, GLADSTONE, PEAPACK, MONMOUTH PARK, LITTLE SILVER, PRINCETON JCT., "
    "WHITE HOUSE, HACKETTSTOWN, MOUNT OLIVE, NETCONG, LAKE HOPATCONG, MOUNT ARLINGTON",
    19.75: "ELBERON, LONG BRANCH, HAMILTON, ANNANDALE, LEBANON, BRADLEY BEACH, ASBURY PARK, "
    "ALLENHURST, HIGH BRIDGE",
    20.35: "BAY HEAD, POINT PLEASANT, MANASQUAN, SPRING LAKE, BELMAR, TRENTON TRANSIT CENTER",
    21.60: "PRINCETON",
}
# Stations whose ticket depends on more than their zone.
_SPECIAL = {"far rockaway", "belmont park"}
_ABBR = {"mt": "mount", "ave": "avenue", "av": "avenue", "blvd": "boulevard", "hts": "heights",
         "st": "street", "saint": "street", "ctr": "centre", "center": "centre", "e": "east"}  # fmt: skip


def _norm(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower().replace("'", "").replace("’", "")).split()
    drop = {"station", "lirr", "railroad", "metro", "north", "the", "terminal", "th"}
    if words[:1] == ["north"]:  # North White Plains
        drop = drop - {"north"}
    words = [re.sub(r"^(\d+)(?:st|nd|rd|th)$", r"\1", w) for w in words]
    return " ".join(_ABBR.get(w, w) for w in words if w not in drop)


def _index(table: dict[float, str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for zone, names in table.items():
        for n in names.split(","):
            key = _norm(n)
            if out.get(key, zone) != zone:
                raise ValueError(f"{n.strip()} is in two zones")
            out[key] = zone
    return out


_LIRR = _index(_LIRR_STATIONS)
_MNR = _index(_MNR_STATIONS)
_NJT = _index(_NJT_STATIONS)
_NJT_PENN = _norm("NEW YORK PENN STATION")


def _holiday(d: date) -> bool:
    """Off-peak fares are valid all day on these, from mta.info/fares-tolls/lirr-metro-north."""
    if (d.month, d.day) in {(1, 1), (7, 4), (12, 25)}:
        return True
    week = (d.day - 1) // 7 + 1
    if d.weekday() == 0:  # Martin Luther King, Presidents, Labor and Memorial Day
        last = (d + timedelta(days=7)).month != d.month
        return (d.month, week) in {(1, 3), (2, 3), (9, 1)} or d.month == 5 and last
    thursday = d - timedelta(days=1) if d.weekday() == 4 else d  # and the day after Thanksgiving
    return d.weekday() in (3, 4) and d.month == 11 and (thursday.day - 1) // 7 + 1 == 4


def _maybe_holiday(d: date) -> bool:
    """A holiday on a weekend may be kept on the weekday next to it: both prices are given."""
    near = {4: d + timedelta(days=1), 0: d - timedelta(days=1)}.get(d.weekday())
    return near is not None and (near.month, near.day) in {(1, 1), (7, 4), (12, 25)}


_TERMINALS = {
    _norm(n)
    for n in (
        "Penn Station",
        "Grand Central",
        "Grand Central Madison",
        "Atlantic Terminal",
        "Hunterspoint Avenue",
        "Long Island City",
    )
}


def _window(t: datetime, a: int, b: int) -> bool:
    return a * 60 <= t.hour * 60 + t.minute < b * 60 + (1 if b == 10 else 0)


def fare(railroad: str, start: str, end: str, depart: float, arrive: float) -> str | None:
    """What one adult pays in advance, "$13.50", or "$18.25 peak or $13.50 off-peak" when which
    applies cannot be told. None when this table cannot say."""
    day = datetime.fromtimestamp(depart, NY)
    if day.date() > VALID_UNTIL:
        return None
    a, b = _norm(start), _norm(end)
    if a in _SPECIAL or b in _SPECIAL:
        return None
    if railroad == "LIRR":
        za, zb = _LIRR.get(a), _LIRR.get(b)
        if za is None or zb is None:
            return None
        peak = _PEAK[za][_Z.index(zb)]
        off = (
            _OFF[za][_Z.index(zb)] if za in _OFF else _OFF[zb][_Z.index(za)] if zb in _OFF else peak
        )
    elif railroad == "Metro-North":
        za, zb = _MNR.get(a), _MNR.get(b)
        if za is None or zb is None:
            return None
        if 1 not in (za, zb):
            if za > 10 or zb > 10:
                return None  # New Haven Line trips outside Manhattan have their own chart
            return f"${_MNR_BETWEEN[za][zb - 2]:.2f}"
        peak, off = _MNR_CITY[zb if za == 1 else za]
    elif railroad == "NJ Transit":
        if _NJT_PENN not in (a, b):
            return None
        peak = off = _NJT.get(b if a == _NJT_PENN else a)
        if peak is None:
            return None
    else:
        return None
    if peak == off:
        return f"${peak:.2f}"
    if day.weekday() >= 5 or _holiday(day.date()):
        return f"${off:.2f}"
    both = f"${peak:.2f} peak or ${off:.2f} off-peak"
    if _maybe_holiday(day.date()):
        return both
    end_at = datetime.fromtimestamp(arrive, NY)
    inbound, outbound = zb == 1, za == 1
    if inbound and outbound:
        return both
    # The hours are those at the terminal. At a stop short of it the train is there up to half an
    # hour earlier or later, so near the edge of the window either may apply.
    slack = 0 if (b if inbound else a) in _TERMINALS else 30
    if inbound:
        kinds = {_window(end_at + timedelta(minutes=m), 6, 10) for m in (0, slack)}
    elif outbound:
        kinds = {
            _window(x, 16, 20) or (railroad == "Metro-North" and _window(x, 6, 9))
            for x in (day, day - timedelta(minutes=slack))
        }
    else:
        return both  # within Queens or beyond: it follows the train, not the stations
    if len(kinds) > 1:
        return both
    return f"${(peak if kinds.pop() else off):.2f}"


_STATIONS: list | None = None


def station(text: str) -> dict | None:
    """The commuter rail station a reader names: "Cold Spring Metro-North station" is the one on
    the Hudson, not Cold Spring Harbor. Without the railroad's name only stations outside the city
    count, since many in it share a name with a subway stop."""
    global _STATIONS
    low = text.lower()
    rail = (
        "LIRR"
        if re.search(r"\blirr\b|long island rail", low)
        else ("Metro-North" if re.search(r"metro[- ]north|\bmnr\b", low) else None)
    )
    if not rail and (not re.search(r"\bstation\b", low) or "subway" in low):
        return None
    if _STATIONS is None:
        _STATIONS = json.loads((Path(__file__).parent / "rail_stations.json").read_text())
    bare = re.sub(
        r"\b(?:long island rail ?road|lirr|metro[- ]north|mnr|railroad|train)\b", " ", low
    )
    want = _norm(bare.split(",")[0])
    found = []
    for rr, name, lat, lon in _STATIONS:
        if (rail and rr != rail) or _norm(name) != want:
            continue
        zone = (_LIRR if rr == "LIRR" else _MNR).get(want, 99)
        if not rail and zone < (4 if rr == "LIRR" else 3):
            continue
        found.append({"name": f"{name} ({rr})", "lat": lat, "lon": lon})
    return found[0] if len(found) == 1 else None
