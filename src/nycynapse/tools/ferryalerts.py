"""Ferry notices from 511NY read against one ride.

511NY carries the Staten Island Ferry's and NYC Ferry's own notices ("operates on a holiday
schedule on Thanksgiving Day, every 30 minutes", a shuttle on select days). Each has a point at
one of the route's landings, a span of dates, and sometimes days of the week and hours.
"""

import math
from datetime import datetime

NEAR_M = 700  # a landing is the leg's start or end
WHITEHALL = (40.7013, -74.0132)
NY_DAY_BITS = {6: 1, 0: 2, 1: 4, 2: 8, 3: 16, 4: 32, 5: 64}  # Python weekday to 511NY's bit


def _metres(a: tuple, b: tuple) -> float:
    dy = (a[0] - b[0]) * 111_200
    dx = (a[1] - b[1]) * 84_300
    return math.hypot(dx, dy)


def _ms(t: dict | None) -> float | None:
    return t["time"] / 1000 if t and t.get("time") else None


def _in_effect(e: dict, ts: float, tz) -> bool:
    details = e.get("details") or [{}]
    start = min((_ms(d.get("start-time")) or 0 for d in details), default=0)
    end = max((_ms(d.get("end-time")) or 0 for d in details), default=0)
    if (start and ts < start) or (end and ts > end):
        return False
    times = [r for d in details for r in d.get("recurrent-times") or []]
    if not times:
        return True
    at = datetime.fromtimestamp(ts, tz)
    minute = at.hour * 60 + at.minute
    for r in times:
        bits = ((r.get("period") or {}).get("days-of-week") or {}).get("bits")
        if bits and not bits & NY_DAY_BITS[at.weekday()]:
            continue
        for s in r.get("schedule-times") or [{}]:
            a, b = s.get("startTimeLT") or {}, s.get("stopTimeLT") or {}
            lo = a.get("hour", 0) * 60 + a.get("minute", 0)
            hi = b.get("hour", 23) * 60 + b.get("minute", 59)
            if lo <= minute <= hi:
                return True
    return False


def changes(option: dict, events: list[dict], tz) -> list[str]:
    """Lines for the trip about the ferries it rides."""
    out = []
    for lg in option.get("lines") or []:
        if lg.get("mode") != "ferry" or not lg.get("coords"):
            continue
        ends = [(lat, lon) for lon, lat in (lg["coords"][0], lg["coords"][-1])]
        ts = lg.get("depart_ts") or option.get("leave_ts")
        for e in events:
            text = (e.get("description") or "").split(" Comment:")[0]
            if "ferry route" not in text.lower():
                continue
            g = e.get("geometry") or {}
            if g.get("type") != "Point" or len(g.get("coordinates") or []) != 2:
                continue
            lon, lat = g["coordinates"]
            if min(_metres((lat, lon), p) for p in ends) > NEAR_M:
                continue
            # NYC Ferry's St. George route lands at St. George too: a Staten Island Ferry notice
            # (Whitehall to St. George) is only for a ride from or to Whitehall.
            if "whitehall" in text.lower() and min(_metres(WHITEHALL, p) for p in ends) > NEAR_M:
                continue
            if ts and not _in_effect(e, ts, tz):
                continue
            line = f"Service change on the {lg.get('name') or 'ferry'} at that time: {text[:300]}"
            if line not in out:
                out.append(line)
    return out[:2]
