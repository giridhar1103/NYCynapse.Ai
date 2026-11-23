"""The next trains at a subway station, live from the MTA's real-time feeds (GTFS-realtime), by
line and direction, with how many minutes away and where each train is headed."""

from __future__ import annotations

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import Tool, ToolResult, failed
from .http import _client, cached

NY = ZoneInfo("America/New_York")
BASE = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/"
FEED_OF = {}
for key, routes in {
    "nyct%2Fgtfs": "1 2 3 4 5 6 7 GS",
    "nyct%2Fgtfs-ace": "A C E H",
    "nyct%2Fgtfs-bdfm": "B D F M FS",
    "nyct%2Fgtfs-g": "G",
    "nyct%2Fgtfs-jz": "J Z",
    "nyct%2Fgtfs-nqrw": "N Q R W",
    "nyct%2Fgtfs-l": "L",
    "nyct%2Fgtfs-si": "SI",
}.items():
    for r in routes.split():
        FEED_OF[r] = key
NEAR_M = 600
BORO = {"M": "Manhattan", "Bk": "Brooklyn", "Q": "Queens", "Bx": "the Bronx", "SI": "Staten Island"}
_BORO_WORD = re.compile(r"\b(manhattan|brooklyn|queens|bronx|staten island)\b", re.I)
_WORDS = {
    "street": "st",
    "avenue": "av",
    "ave": "av",
    "square": "sq",
    "place": "pl",
    "road": "rd",
    "boulevard": "blvd",
    "center": "ctr",
    "parkway": "pkwy",
    "park": "pk",
    "heights": "hts",
}


def _norm(text: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split()
    words = [re.sub(r"^(\d+)(?:st|nd|rd|th)$", r"\1", w) for w in words]
    drop = {"station", "subway", "stop", "the", "train", "mta"}
    return " ".join(_WORDS.get(w, w) for w in words if w not in drop)


def _lines(s: dict) -> set[str]:
    """The feed's route ids at one stop. Three shuttles share the letter S."""
    stop = s.get("gtfs_stop_id") or ""
    shuttle = "GS" if stop.startswith("9") else "H" if stop.startswith("H") else "FS"
    return {shuttle if r == "S" else _route(r) for r in (s.get("daytime_routes") or "").split()}


def _letter(route: str) -> str:
    return {"GS": "S", "FS": "S", "H": "S", "SI": "SIR"}.get(route, route)


def _named(text: str, stations: list[dict]) -> list[dict]:
    """Stations with this name: the whole name, else one side of a name like Times Sq-42 St,
    else a name starting with it."""
    boro = _BORO_WORD.search(text)
    plain = re.sub(r"\s+(?:in|,)?\s*$", "", _BORO_WORD.sub("", text)) if boro else text
    q = _norm(plain.replace(" in ", " ") if boro else plain)
    if not q:
        return []
    if boro:
        near = [
            s for s in stations if boro.group(1).lower() in BORO.get(s.get("borough"), "").lower()
        ]
        stations = near or stations
    rows = [
        (s, _norm(s["stop_name"]), [_norm(x) for x in s["stop_name"].split("-")]) for s in stations
    ]
    return [s for s, whole, _ in rows if whole == q] + [
        s for s, whole, sides in rows if whole != q and q in sides
    ] or (
        [s for s, whole, sides in rows if any(x.startswith(q + " ") for x in [whole, *sides])]
        if len(q) >= 4
        else []
    )


def _feed(key: str):
    from google.transit import gtfs_realtime_pb2

    def make():
        r = _client.get(BASE + key, timeout=10)
        r.raise_for_status()
        msg = gtfs_realtime_pb2.FeedMessage()
        msg.ParseFromString(r.content)
        return msg

    return cached(("gtfs-rt", key), 25, make)


def _route(r: str) -> str:
    return {"S": "GS", "SIR": "SI", "SS": "SI", "5X": "5", "6X": "6", "7X": "7", "FX": "F"}.get(
        r, r
    )


def next_trains(station: str, line: str | None = None, n: int = 3) -> ToolResult:
    """The next trains at a subway station (all its lines, or one), by direction."""
    from .access import _stations
    from .trips import _metres, locate

    try:
        stations = _stations()
    except Exception as e:  # noqa: BLE001
        return failed(f"the MTA station list did not load: {str(e)[:80]}")
    names = {s.get("gtfs_stop_id"): s.get("stop_name") for s in stations}
    names.setdefault("H19", "Broad Channel")  # the shuttle's own platform there
    ask = (line or "").strip().upper()
    ask = "S" if ask in ("SHUTTLE", "S TRAIN") else re.sub(r"\s*(?:TRAIN|LINE)$", "", ask)
    wants = {"GS", "FS", "H"} if ask == "S" else {_route(ask)} if ask else set()

    groups: dict[str, list[dict]] = {}
    for s in stations:
        groups.setdefault(str(s.get("complex_id") or s.get("gtfs_stop_id")), []).append(s)
    of = {s["gtfs_stop_id"]: k for k, v in groups.items() for s in v}

    def serves(key: str) -> set[str]:
        return set().union(*(_lines(s) for s in groups[key]))

    keys = list(dict.fromkeys(of[s["gtfs_stop_id"]] for s in _named(station, stations)))
    title = {of[s["gtfs_stop_id"]]: s["stop_name"] for s in reversed(_named(station, stations))}
    if not keys:
        # Not a station's name: the closest station to that place, or the closest with that line.
        p = locate(station)
        if not p:
            return failed(f"could not place {station!r}; name a subway station")
        near: dict[str, float] = {}
        for s in stations:
            try:
                at = {"lat": float(s["gtfs_latitude"]), "lon": float(s["gtfs_longitude"])}
            except (KeyError, TypeError, ValueError):
                continue
            k = of[s["gtfs_stop_id"]]
            near[k] = min(near.get(k, 1e9), _metres(p, at))
        close = sorted((k for k, d in near.items() if d <= NEAR_M), key=near.get)
        if not close:
            return failed(f"no subway station within {NEAR_M} m of {p['name']}")
        keys = [k for k in close if wants & serves(k)][:1] or close[:1]
    if wants and any(wants & serves(k) for k in keys):
        keys = [k for k in keys if wants & serves(k)]
    if len(keys) > 3:
        said = "; ".join(
            f"{groups[k][0]['stop_name']} in {BORO.get(groups[k][0].get('borough'), 'the city')} "
            f"({', '.join(sorted({_letter(r) for r in serves(k)}))})"
            for k in keys[:12]
        )
        return failed(
            f"several stations are called {station!r}: {said}. Ask with a line or borough"
        )
    here = [s for k in keys for s in groups[k]]
    ids = {s["gtfs_stop_id"] for s in here}
    routes = set().union(*(serves(k) for k in keys))
    usual = bool(not wants or wants & routes)
    # A line that does not normally stop here may at night or when rerouted, so its feed decides.
    look = (wants & routes or wants) if wants else routes
    now = time.time()
    found: dict[tuple[str, str], list[tuple[float, str]]] = {}
    asof = None
    for key in sorted({FEED_OF[r] for r in look if r in FEED_OF}):
        try:
            feed = _feed(key)
        except Exception:  # noqa: BLE001 - other lines may still answer
            continue
        asof = max(asof or 0, feed.header.timestamp)
        for ent in feed.entity:
            if not ent.HasField("trip_update"):
                continue
            tu = ent.trip_update
            rid = _route(tu.trip.route_id)
            if wants and rid not in wants:
                continue
            stops = list(tu.stop_time_update)
            if not stops:
                continue
            last = names.get(stops[-1].stop_id[:-1], stops[-1].stop_id[:-1])
            for st in stops[:-1]:  # a train ending here cannot be boarded
                if st.stop_id[:-1] in ids:
                    t = (
                        (st.arrival.time or st.departure.time)
                        if st.HasField("arrival") or st.HasField("departure")
                        else 0
                    )
                    if t and now - 30 <= t <= now + 2 * 3600:  # beyond two hours is a guess
                        found.setdefault((rid, last), []).append((t, st.stop_id[-1]))
    where = " and ".join(dict.fromkeys(title.get(k, groups[k][0]["stop_name"]) for k in keys))
    if not usual and not found:
        served = ", ".join(sorted({_letter(r) for r in routes}))
        return failed(f"the {ask} does not stop at {where} (it has {served})")
    clock = datetime.fromtimestamp(asof or now, NY)
    if not found:
        return ToolResult(
            summary=f"No trains are showing in the MTA's live feed for {where}"
            + (f" on the {line}" if line else "")
            + f" right now ({clock:%-I:%M %p}). Service may not be running, or the feed may be "
            "missing that line for the moment.",
            data={"station": where, "trains": []},
            sources=[_SOURCE],
        )
    lines = [f"Next trains at {where}, live from the MTA feed as of {clock:%-I:%M %p}:"]
    rows = []
    for (rid, last), times in sorted(found.items(), key=lambda kv: (kv[0][0], min(kv[1])[0])):
        times.sort()
        soon = times[: max(1, min(int(n or 3), 5))]
        mins = [max(0, round((t - now) / 60)) for t, _ in soon]
        clocks = [datetime.fromtimestamp(t, NY).strftime("%-I:%M") for t, _ in soon]
        words = ", ".join(
            ("now" if m == 0 else f"{m} min") + f" ({c})" for m, c in zip(mins, clocks, strict=True)
        )
        lines.append(f"- {_letter(rid)} toward {last}: {words}")
        rows.append({"line": _letter(rid), "toward": last, "minutes": mins, "times": clocks})
    lines.append(
        "Times are the MTA's predictions and can change; trains not yet in the feed do not show."
    )
    return ToolResult(
        summary="\n".join(lines),
        data={"station": where, "trains": rows, "as_of": clock.isoformat()},
        sources=[{**_SOURCE, "updated_at": clock.isoformat()}],
        map=[
            {
                "type": "points",
                "label": where,
                "points": [
                    {
                        "lat": float(here[0]["gtfs_latitude"]),
                        "lon": float(here[0]["gtfs_longitude"]),
                        "label": where,
                    }
                ],
            }
        ],
    )


_SOURCE = {"kind": "live", "title": "MTA subway real-time feeds", "url": "https://api.mta.info/"}

TOOLS = [
    Tool(
        "next_trains",
        "live",
        "The next trains at a subway station right now, by line and direction (where each is "
        "headed), with minutes away and clock times, live from the MTA's real-time feeds. Use for "
        "'when's the next train', 'how long until the L', 'is the 7 coming'.",
        {
            "type": "object",
            "properties": {
                "station": {"type": "string", "description": "station name or a place next to it"},
                "line": {"type": "string", "description": "one line, like L, 7, A"},
                "n": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["station"],
        },
        next_trains,
    )
]
