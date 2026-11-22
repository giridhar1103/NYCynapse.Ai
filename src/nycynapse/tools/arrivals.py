"""The next trains at a subway station, live from the MTA's real-time feeds (GTFS-realtime), by
line and direction, with how many minutes away and where each train is headed."""

from __future__ import annotations

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
    "nyct%2Fgtfs-ace": "A C E H FS",
    "nyct%2Fgtfs-bdfm": "B D F M",
    "nyct%2Fgtfs-g": "G",
    "nyct%2Fgtfs-jz": "J Z",
    "nyct%2Fgtfs-nqrw": "N Q R W",
    "nyct%2Fgtfs-l": "L",
    "nyct%2Fgtfs-si": "SI",
}.items():
    for r in routes.split():
        FEED_OF[r] = key
NEAR_M = 250


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
    return {"S": "GS", "SIR": "SI", "6X": "6", "7X": "7", "FX": "F"}.get(r, r)


def next_trains(station: str, line: str | None = None, n: int = 3) -> ToolResult:
    """The next trains at a subway station (all its lines, or one), by direction."""
    from .access import _stations
    from .trips import _metres, locate

    p = locate(station)
    if not p:
        return failed(f"could not place {station!r}; name a subway station")
    try:
        stations = _stations()
    except Exception as e:  # noqa: BLE001
        return failed(f"the MTA station list did not load: {str(e)[:80]}")
    names = {s.get("gtfs_stop_id"): s.get("stop_name") for s in stations}
    here = []
    for s in stations:
        try:
            d = _metres(p, {"lat": float(s["gtfs_latitude"]), "lon": float(s["gtfs_longitude"])})
        except (KeyError, TypeError, ValueError):
            continue
        if d <= NEAR_M:
            here.append((d, s))
    if not here:
        return failed(f"no subway station within {NEAR_M} m of {p['name']}")
    want = _route(line.upper()) if line else None
    ids = {s["gtfs_stop_id"] for _, s in here}
    routes = {_route(r) for _, s in here for r in (s.get("daytime_routes") or "").split()}
    if want:
        routes &= {want}
        if not routes:
            served = ", ".join(
                sorted({r for _, s in here for r in (s.get("daytime_routes") or "").split()})
            )
            return failed(
                f"the {line} does not stop at {here[0][1]['stop_name']} (it has {served})"
            )
    now = time.time()
    found: dict[tuple[str, str], list[tuple[float, str]]] = {}
    asof = None
    for key in sorted({FEED_OF[r] for r in routes if r in FEED_OF}):
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
            if want and rid != want:
                continue
            stops = list(tu.stop_time_update)
            if not stops:
                continue
            last = names.get(stops[-1].stop_id[:-1], stops[-1].stop_id[:-1])
            for st in stops:
                if st.stop_id[:-1] in ids:
                    t = (
                        (st.arrival.time or st.departure.time)
                        if st.HasField("arrival") or st.HasField("departure")
                        else 0
                    )
                    if t and now - 30 <= t <= now + 2 * 3600:  # beyond two hours is a guess
                        found.setdefault((rid, last), []).append((t, st.stop_id[-1]))
    where = here[0][1]["stop_name"]
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
        lines.append(f"- {rid} toward {last}: {words}")
        rows.append({"line": rid, "toward": last, "minutes": mins, "times": clocks})
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
                "label": "Station",
                "points": [
                    {
                        "lat": float(here[0][1]["gtfs_latitude"]),
                        "lon": float(here[0][1]["gtfs_longitude"]),
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
