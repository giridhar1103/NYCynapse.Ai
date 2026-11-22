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
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import date, datetime, timedelta
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
    "1": (40.64307, -73.78907),
    "4": (40.64392, -73.78241),
    "5": (40.64681, -73.77986),
    "7": (40.64823, -73.78319),
    "8": (40.64675, -73.78886),
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


# Stations and street corners -------------------------------------------------------------------


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

    its = [it for it in its if not _cancelled(it)]
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


def _cancelled(it: dict) -> bool:
    """The planner carries live cancellations: a ride that is cancelled, or whose boarding or
    drop-off stop is, is not a plan. Only rides count: the planner also puts the flag on the
    walks between them, and reading those threw out every itinerary for some trips."""
    return any(
        lg.get("cancelled")
        or (lg.get("from") or {}).get("cancelled")
        or (lg.get("to") or {}).get("cancelled")
        for lg in it.get("legs") or []
        if lg.get("mode") != "WALK"
    )


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
            lo = hi  # leave and arrive are both fixed, so the trip takes what lies between
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
        "agency": lg.get("agencyName") or "",
        "fare_url": lg.get("agencyFareUrl")
        or FARE_PAGES.get(_AGENCY.get(lg.get("agencyName") or "", "")),
        "color": "#" + lg["routeColor"] if lg.get("routeColor") else None,
        "text_color": "#" + lg["routeTextColor"] if lg.get("routeTextColor") else None,
        "from": None if start == "START" else start,
        "to": to,
        "depart": _hm(lg["startTime"]) if lg.get("startTime") and mode != "walk" else None,
        "depart_ts": (
            datetime.fromisoformat(lg["startTime"].replace("Z", "+00:00")).timestamp()
            if lg.get("startTime") and mode != "walk"
            else None
        ),
        "minutes": max(1, round(lg.get("duration", 0) / 60)),
        "stops": len(lg.get("intermediateStops") or []) + 1 if mode != "walk" else None,
        "headsign": lg.get("headsign"),
        "live": bool(lg.get("realTime")),
        "coords": _coords(lg),
        "via": [x.get("name") for x in lg.get("intermediateStops") or [] if x.get("name")],
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


def _first_ride_ts(o: dict) -> float:
    """When the first vehicle of an itinerary leaves (its stated leave time can be a guess)."""
    for x in o.get("lines") or []:
        if x.get("mode") != "walk" and x.get("depart_ts"):
            return x["depart_ts"]
    return o.get("leave_ts", 0)


def _hours_away(options: list[dict], when: datetime, arrive_by: bool) -> bool:
    """Every option is hours from the time asked: the sign of ends the planner cannot link."""
    if not options:
        return False
    if arrive_by:
        return min(o["arrive_ts"] for o in options) - when.timestamp() > 2 * 3600
    return min(_first_ride_ts(o) for o in options) - when.timestamp() > 2.5 * 3600


def _sane(options: list[dict]) -> list[dict]:
    """Drop itineraries that take several times as long as the best one (a ride glued to a
    connection the next morning)."""
    if len(options) < 2:
        return options
    best = min(o["minutes"][0] for o in options)
    return [o for o in options if o["minutes"][0] <= max(180, 3 * best)]


def _nearest_station(p: dict) -> dict | None:
    """The subway station nearest a point, within a 20 minute walk."""
    from .access import _stations

    best = None
    for s in _stations():
        try:
            q = {"lat": float(s["gtfs_latitude"]), "lon": float(s["gtfs_longitude"])}
        except (KeyError, TypeError, ValueError):
            continue
        d = _metres(p, q)
        if d < 1500 and (best is None or d < best[0]):
            best = (d, {**q, "name": f"{s['stop_name']} station"})
    return best[1] if best else None


def transit(a: dict, b: dict, when: datetime, arrive_by: bool) -> tuple[list[dict], list[str]]:
    options, notes = _transit(a, b, when, arrive_by)
    options = _sane(options)
    if not _hours_away(options, when, arrive_by) or a.get("airport") or b.get("airport"):
        return options, notes
    # Ends the planner cannot reach on foot (inside a gated yard, a pier, a park) send it to
    # whatever ride reaches them, often a ferry the next morning. From the nearest stations:
    a2 = (_nearest_station(a) or a) if not a.get("name", "").endswith("station") else a
    b2 = (_nearest_station(b) or b) if not b.get("name", "").endswith("station") else b
    if a2 is a and b2 is b:
        return options, notes
    try:
        again, more = _transit(a2, b2, when, arrive_by)
    except Exception:  # noqa: BLE001 - keep what there is
        return options, notes
    again = _sane(again)
    if again and not _hours_away(again, when, arrive_by):
        moved = [f"{p['name']} for {q['name']}" for p, q in ((a2, a), (b2, b)) if p is not q]
        return again, more + [
            "Planned from or to the nearest station (" + "; ".join(moved) + "), since the "
            "places themselves could not be linked at that hour; add the walk at that end."
        ]
    return options, notes


def _transit(a: dict, b: dict, when: datetime, arrive_by: bool) -> tuple[list[dict], list[str]]:
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
                + (
                    f"Terminal {airport_end['terminal']}"
                    if airport_end.get("terminal")
                    else "the terminals"
                )
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
                "AirTrain alert, "
                f"{'in effect at the time of this trip' if on else 'not in effect then'}"
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
            "PATH runs 24 hours a day, every day. "
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
        if (
            easy["transfers"] < min(o["transfers"] for o in out)
            and easy["minutes"][0] <= slowest_ok
        ):
            out = out[:3] + [easy]
    return out, notes


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
    if min(abs(hour - when.hour), 24 - abs(hour - when.hour)) > 2:
        return None  # nothing near that hour: 7 AM trips say little about a 3 AM one
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
        (
            r"\b("
            + "|".join(_MONTHS)
            + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?",
            lambda g: (int(g[2]) if g[2] else None, _MONTHS.index(g[0]) + 1, int(g[1])),
        ),
        (
            r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b",
            lambda g: (
                (int(g[2]) + 2000 if len(g[2]) == 2 else int(g[2])) if g[2] else None,
                int(g[0]),
                int(g[1]),
            ),
        ),
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
        return raw[: m.start()] + " " + raw[m.end() :], dt
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
    weekday = any(re.search(rf"\b{d[:3]}\w*\b", raw) for d in _DAYS)
    if (
        not dated
        and not weekday  # "after midnight into Friday": Friday's small hours, said below
        and small
        and re.search(r"\btonight\b|after midnight", raw)
        and now.hour >= 5
    ):
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


def _station_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("street", "st"))


def _line_changes(option: dict, at: datetime) -> list[str]:
    """MTA service changes on the subway lines an option rides that are in effect at the time
    of the trip (planned work the schedules may not show). One that names a station this ride
    uses ("no 7 at Flushing-Main St") is marked as affecting it, and the option is flagged."""
    from .live import MTA_ALERTS, _plain

    legs = [lg for lg in option.get("lines") or [] if lg.get("mode") == "subway"]
    routes = {lg.get("name") for lg in legs} - {None}
    if not routes:
        return []
    stations = {
        _station_key(n)
        for lg in legs
        for n in [lg.get("from"), lg.get("to"), *(lg.get("via") or [])]
        if n
    } - {""}
    try:
        d = get_json(MTA_ALERTS, ttl_s=120)
    except Exception:  # noqa: BLE001
        return []
    # The ride's own window, from leaving to arriving, not the time asked about: an arrive-by
    # trip rides well before its deadline.
    t0 = option.get("leave_ts") or at.timestamp()
    t1 = option.get("arrive_ts") or t0
    out = []
    for ent in d.get("entity", []):
        al = ent.get("alert", {})
        hit = {x.get("route_id") for x in al.get("informed_entity") or []} & routes
        # An alert with no end is about now (delays): it says nothing about a trip hours away.
        soon = t0 - time.time() < 90 * 60
        during = any(
            int(p.get("start") or 0) <= t1 and (t0 < int(p["end"]) if p.get("end") else soon)
            for p in al.get("active_period") or [{}]
        )
        head = _plain(al.get("header_text", {}).get("translation"))[:220]
        if "AirTrain" in head:
            continue  # the AirTrain's own alerts are read against its leg, not the subway line
        if hit and during and head:
            text = re.sub(r"[^a-z0-9]", "", head.lower().replace("street", "st"))
            touches = any(len(k) >= 4 and k in text for k in stations)
            line = (
                f"{'Affects this ride' if touches else 'Service change'} on the "
                f"{'/'.join(sorted(hit))} at that time: {head}"
            )
            if touches:
                option["affected"] = True
            if line not in out:
                out.append(line)
    out.sort(key=lambda x: not x.startswith("Affects"))
    return out[:3]


def _step_free_first(options: list[dict]) -> list[dict]:
    """Check every subway station an option boards or leaves at, and put the options with no
    stairs (and no elevator out) first."""
    from . import access

    def check(o: dict) -> int:
        seen, texts, bad, subway = set(), [], 0, False
        for lg in o.get("lines") or []:
            if lg.get("mode") != "subway":
                continue
            subway = True
            ends = (lg["coords"][0], lg["coords"][-1]) if lg.get("coords") else ()
            if not ends:
                bad += 1
                texts.append(f"{lg.get('from') or 'a station'}: could not be checked")
                continue
            for lon, lat in ends:
                try:
                    rec = access.station("", lat, lon, lg.get("name"))
                except Exception:  # noqa: BLE001
                    rec = None
                if not rec:
                    # Not verified is not step-free: an unchecked station counts against it.
                    bad += 1
                    texts.append("a station on this ride could not be checked")
                    continue
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                texts.append(access.describe(rec))
                bad += rec["step_free"] != "yes" or bool(rec["out"]) or not rec["outages_known"]
        o["access"] = (
            "; ".join(texts)
            if texts
            else "no subway on this option"
            if not subway
            else "could not be checked"
        )
        return bad

    bad = {id(o): check(o) for o in options}
    good = [o for o in options if not bad[id(o)]]
    # Options with stairs are left out when a step-free one exists.
    return good or sorted(options, key=lambda o: bad[id(o)])


# The tool --------------------------------------------------------------------------------------


def _typical_drive(a: dict, b: dict, t: datetime, arrive_by: bool) -> dict | None:
    d = drive(a, b, t)
    if d and arrive_by:
        # A deadline's trip starts earlier: the typical time for the hour it leaves.
        d = drive(a, b, t - timedelta(minutes=d["minutes"][2])) or d
    return d


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
    data: dict = {"from": a, "to": b, "when": t.isoformat(), "step_free": step_free}
    sources: list[dict] = []
    layers: list[dict] = []
    # Walking, biking and the two car lookups do not depend on transit: start them now, so they
    # run while the transit planner works instead of after it.
    gap = _metres(a, b)
    pool = ThreadPoolExecutor(max_workers=4)
    later: dict = {}
    for how, limit, label in (("WALK", 5000, "walk"), ("BIKE", 16000, "bike")):
        if mode == label or (mode == "both" and gap <= limit):
            later[label] = pool.submit(direct, a, b, how)
    if mode in ("both", "drive"):
        later["car"] = pool.submit(car_route, a, b, t)
        later["drive"] = pool.submit(_typical_drive, a, b, t, arrive_by)
    pool.shutdown(wait=False)

    def ready(name: str):
        try:
            return later[name].result(timeout=40)
        except Exception:  # noqa: BLE001 - that mode is just left out
            return None

    if mode in ("both", "transit"):
        token = _STEP_FREE.set(step_free)
        try:
            options, notes = transit(a, b, t, arrive_by)
            # Never offer a plan that should already have left. If nothing is left, plan from
            # now, and for a deadline say it can no longer be made.
            gone = _now().timestamp() - 120
            if options and any(o.get("leave_ts", gone + 1) < gone for o in options):
                still = [o for o in options if o.get("leave_ts", gone + 1) >= gone]
                if still:
                    options = still
                else:
                    try:
                        soon, more = transit(a, b, _now(), False)
                    except Exception:  # noqa: BLE001
                        soon, more = [], []
                    notes = (
                        [
                            f"Leaving now, no transit option arrives by {t:%-I:%M %p}: every "
                            "one would have had to leave already. The options below leave now "
                            "and arrive as soon as possible."
                        ]
                        if arrive_by
                        else ["Those departures have gone; the options below leave from now."]
                    ) + more
                    options = soon
        except Exception as e:  # noqa: BLE001 - the drive half can still answer
            options, notes = [], [f"Transit planner unavailable: {str(e)[:80]}"]
        finally:
            _STEP_FREE.reset(token)
        if options and step_free:
            options = _step_free_first(options)
        changes = {id(o): _line_changes(o, t) for o in options}
        if not step_free:  # for a step-free trip, the order by access stands
            options = _affected_last(options, arrive_by)
        data["transit"] = options
        late = _hours_away(options, t, arrive_by)
        if options and late:
            notes.append(
                "Every transit option found is hours away from the time asked. That usually "
                "means the planner could not link these exact points then, not that nothing "
                "runs: plan again from or to the nearest station before telling the reader "
                "there is no service or the deadline cannot be met."
            )
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
                tag = ", ".join(t for t, x in ((first, quick), ("fewest changes", easy)) if o is x)
                lines.append(
                    f"{i}.{f' [{tag}]' if tag else ''} {span} door to door, leave {o['leave']}, "
                    f"arrive {o.get('arrive', '?')}, {o['transfers']} transfer"
                    f"{'s' if o['transfers'] != 1 else ''}, {o['walk_minutes']} min walking: "
                    + "; ".join(o["steps"])
                    + (f". Fare: {_fare(o)}" if _fare(o) else "")
                )
                if step_free and o.get("access") is not None:
                    lines.append("   Step-free check: " + o["access"])
                for warn in changes[id(o)]:
                    lines.append("   " + warn)
            # Commuter rail is priced by zone, peak or off-peak: point to each agency's own fare
            # page instead of guessing a price.
            pages = {}
            for o in options:
                for x in o.get("lines") or []:
                    if x.get("mode") in ("train", "bus") and x.get("fare_url") and x.get("name"):
                        if x["name"] not in ("PATH", "AirTrain") and not _MTA.search(
                            x.get("agency", "")
                        ):
                            pages.setdefault(x["name"], x["fare_url"])
            for name, page in list(pages.items())[:3]:
                lines.append(f"{name} fares depend on zones and time of day: {page}")
                sources.append({"kind": "official", "title": f"{name} fares", "url": page})
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
    for label in ("walk", "bike"):
        if label not in later:
            continue
        r = ready(label)
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
        car = ready("car")
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
        d = ready("drive")
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
            # Say which one is missing, so one fare is never read as both.
            gone = [
                lbl
                for lbl, k in (("yellow taxi", "taxi_fare_usd"), ("Uber or Lyft", "app_fare_usd"))
                if not d.get(k)
            ]
            if fares and gone:
                fares += f" (no typical {' or '.join(gone)} fare for this pair)"
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


SOONER_S = 45 * 60

# One-way fares as of November 2026 (MTA from Jan 4 2026, PATH from May 4 2026, NYC Ferry
# from Oct 19 2026). Commuter rail is priced by zone and time, so it is named, not added.
# The agencies' own fare pages, for the rides priced by zone and time of day.
FARE_PAGES = {
    "LIRR": "https://www.mta.info/fares-tolls/lirr-metro-north",
    "Metro-North": "https://www.mta.info/fares-tolls/lirr-metro-north",
    "NJ Transit": "https://www.njtransit.com/fares",
}
FARES = {"local": 3.00, "express": 7.25, "path": 3.25, "nyc_ferry": 5.00, "airtrain": 8.75}
_EXPRESS = re.compile(r"^(?:BxM|BM|QM|SIM|X)\d", re.I)
_MTA = re.compile(r"\bMTA\b|New York City Transit|NYCT|MTA Bus", re.I)
_BUS_AGENCY = {"NJ TRANSIT BUS": "NJ Transit", "Greyhound-us": "Greyhound", "FlixBus-us": "FlixBus"}


def _fare(o: dict) -> str:
    """What one adult pays for an itinerary, as far as fixed fares go: "$3.00", or "$3.00 plus
    the LIRR fare" when a zone-priced train is in it. Empty when it cannot be said."""
    total, rail, local, path = 0.0, [], False, False
    legs = [x for x in o.get("lines") or [] if x.get("mode") != "walk"]
    names = {x.get("name") or "" for x in legs}
    nj_rail = "NJ Transit" in names
    for x in legs:
        mode, name = x.get("mode"), x.get("name") or ""
        if name == "AirTrain":
            # Newark's AirTrain fee is part of the NJ Transit ticket to the airport station.
            total += 0 if nj_rail else FARES["airtrain"]
        elif name == "PATH":
            if not path:  # changing PATH trains is free
                total += FARES["path"]
                path = True
        elif mode == "ferry":
            low = name.lower()
            if "staten" in low or "st george" in low or "whitehall" in low:
                continue
            if name == "NYC Ferry":
                total += FARES["nyc_ferry"]
            else:
                rail.append(name or "ferry")
        elif mode == "bus" and x.get("agency") and not _MTA.search(x["agency"]):
            # NJ Transit, Bee-Line, NICE, intercity coaches: their own fares.
            rail.append(_BUS_AGENCY.get(x["agency"], x["agency"]) + " bus")
        elif mode == "bus" and _EXPRESS.match(name):
            total += FARES["express"]  # includes the transfer to a local bus or the subway
            local = True
        elif mode in ("subway", "bus", "tram"):
            if not local:  # one fare, with free transfers between subway and local buses
                total += FARES["local"]
                local = True
        elif mode == "train":
            rail.append(name or "commuter rail")
    if not legs:
        return ""
    if not total and not rail:
        return "free"
    named = f"the {' and '.join(dict.fromkeys(rail))} fare"
    if not rail:
        return f"${total:.2f}"
    return f"${total:.2f} plus {named}" if total else f"plus {named}"


def _affected_last(options: list[dict], arrive_by: bool) -> list[dict]:
    """A ride a service change runs into goes after the ones it does not. But not behind a
    ride that gets there far later: the 12:31 AM train with a change on its line still comes
    before the 5:30 AM bus, since the first option is the one the page opens and draws."""
    clean = [o for o in options if not o.get("affected")]
    if not clean or len(clean) == len(options):
        return options
    first_clean = min(o["arrive_ts"] for o in clean)

    def later(o: dict) -> bool:
        if not o.get("affected"):
            return False
        return arrive_by or first_clean - o["arrive_ts"] <= SOONER_S

    return sorted(options, key=later)


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
                    "fare": _fare(o),
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
        "step_free": bool(data.get("step_free")),
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
        "yourself. Set `when` to the time the reader will travel. Plan the whole trip in one "
        "call from the real start to the real end: separate calls for each leg do not line up "
        "in time, and stitching them leaves people waiting.",
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


# Place names (stations, corners, landmarks, towns) live in their own module too.
# The PATH engine lives in its own module; imported last, since it reaches back here.
from .path_trains import (  # noqa: E402
    _path_options,
)
from .placenames import _norm, _words, locate  # noqa: E402
