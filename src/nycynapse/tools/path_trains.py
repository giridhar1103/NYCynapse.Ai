"""PATH trips. The trip planner does not carry PATH, so they are built here: station order
and ride times between stations from PATH's schedule data, next trains from PATH's own live
feed, and the line pattern in effect at the time of travel (some lines run via Hoboken late at
night and on weekends). Shared helpers are reached through `trips` at call time, which keeps the
two modules free of an import cycle and lets tests swap trips' clock."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from . import trips as T
from .http import get_json

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
    return max(1, round(T._metres(a, b) * 1.3 / 80))


# The middle of the Hudson (lat, lon), south to north: a pier on the Manhattan side (the
# Intrepid) is outside the city's zones but east of it.
_HUDSON = [
    (40.69, -74.030),
    (40.72, -74.025),
    (40.75, -74.018),
    (40.78, -74.003),
    (40.82, -73.975),
    (40.86, -73.950),
    (40.90, -73.925),
]


def in_nj(p: dict) -> bool:
    if p["lon"] >= -73.995 and not (40.80 < p["lat"] < 40.92):
        return False
    if T.zone_of(p["lat"], p["lon"]) is not None:
        return False
    for (y0, x0), (y1, x1) in zip(_HUDSON, _HUDSON[1:], strict=False):
        if y0 <= p["lat"] <= y1:
            return p["lon"] < x0 + (x1 - x0) * (p["lat"] - y0) / (y1 - y0)
    return p["lon"] < -73.995


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
    live = _path_live() if abs((when - T._now()).total_seconds()) < 900 and not arrive_by else {}
    starts = sorted(PATH["stations"], key=lambda c: T._metres(a, _station(c)))[:3]
    ends = sorted(PATH["stations"], key=lambda c: T._metres(b, _station(c)))[:3]
    options = []
    for o in starts:
        so = _station(o)
        if T._metres(a, so) > WALK_MAX_M:
            continue
        for d in ends:
            sd = _station(d)
            if o == d:
                continue
            walk1 = _walk_min(a, so)
            best = _path_rides(o, d, when, live, walk1)
            if best and arrive_by:
                # The lines that run are those at boarding time, well before a deadline (6 AM
                # is when Hoboken-WTC starts; a 5:37 AM boarding needs the night pattern).
                board_at = when - timedelta(minutes=best[0] + _walk_min(sd, b) + 10)
                best = _path_rides(o, d, board_at, live, walk1)
            if not best:
                continue
            total, rides, wait_s = best
            # Without live times, allow for the wait: a few minutes by day, more at night,
            # and again at a change of trains.
            spare = 0 if wait_s is not None else (20 if when.hour < 6 else 10)
            change = (15 if when.hour < 6 else 7) if len(rides) > 1 else 0
            ride_min = sum(r[3] for r in rides) + change
            near_end = T._metres(sd, b) <= WALK_MAX_M
            tail = None
            # The rest of the way costs the trip planner a request or two, so only from the
            # PATH stations nearest each end: Hoboken to Summit had asked it twelve times.
            if not near_end and (o != starts[0] or d != ends[0]):
                continue
            if not near_end:
                # The rest of the way by the trip planner: from when PATH gets in, or, for a
                # deadline, the latest trip that still makes it.
                t_off = (
                    when
                    if arrive_by
                    else when + timedelta(minutes=walk1 + ride_min, seconds=wait_s or 0)
                )
                rest = T._plan(sd, b, t_off, arrive_by, n=2)
                if not rest:
                    continue
                tail = T._itinerary(rest[0], b["name"])
            walk2 = _walk_min(sd, b) if near_end else 0
            if arrive_by:
                # Work back from the deadline: the end of the trip, then the PATH ride, the
                # wait for it and the walk to the station.
                end = (
                    datetime.fromtimestamp(tail["leave_ts"], T.NY)
                    if tail
                    else when - timedelta(minutes=walk2)
                )
                off = end
                board = off - timedelta(minutes=ride_min)
                leave = board - timedelta(minutes=walk1 + spare)
            else:
                board = when + timedelta(minutes=walk1, seconds=wait_s or 0)
                off = board + timedelta(minutes=ride_min)
                # With a live train time, leave in time for it rather than now and wait half an
                # hour on the platform; without one, leave now and allow for the wait.
                leave = board - timedelta(minutes=walk1 + 1) if wait_s else when
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
                end = datetime.fromtimestamp(tail["arrive_ts"], T.NY)
            mins = round((end - leave).total_seconds() / 60)
            arrive_ts = end.timestamp() + (0 if arrive_by else spare * 60)
            options.append(
                {
                    "minutes": (
                        mins - (spare if arrive_by else 0),
                        mins + (0 if arrive_by else spare),
                    ),
                    "arrive_ts": arrive_ts,
                    "leave_ts": leave.timestamp(),
                    "leave": leave.strftime("%-I:%M %p"),
                    "arrive": end.strftime("%-I:%M %p"),
                    "transfers": sum(x["mode"] != "walk" for x in lines) - 1,
                    # Every walk, the ones in the onward subway ride too.
                    "walk_minutes": sum(x["minutes"] for x in lines if x["mode"] == "walk"),
                    "steps": steps,
                    "live": wait_s is not None,
                    "lines": lines,
                }
            )
    # Soonest arrival first, or for a deadline the latest that still makes it.
    ok = when.timestamp() + 60
    options.sort(
        key=lambda o: (o["arrive_ts"] > ok, -o["leave_ts"]) if arrive_by else (0, o["arrive_ts"])
    )
    seen, out = set(), []
    for o in options:
        if o["steps"][1] not in seen:
            seen.add(o["steps"][1])
            out.append(o)
    return out[:2]
