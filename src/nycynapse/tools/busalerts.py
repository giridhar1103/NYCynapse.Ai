"""MTA bus alerts read against one ride.

A bus route is long and has several alerts on any day, most of them streets away from the part a
reader rides. The alerts say where in words ("stops on Columbus Ave from W 80th St to W 72nd St",
"stops below W 86th St"), so the corners they name are found on the street map and compared with
where the ride boards, goes and ends.
"""

import math
import re
import time

from . import corners
from .http import get_json

BUS_ALERTS = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/camsys%2Fbus-alerts.json"
# Schedules are in the trip planner already, and "expect delays" is on most routes all day.
_SKIP = re.compile(r"schedule|extra service|delay|reduced service", re.I)
_CUT = re.compile(r"suspend|no service|no .{2,20} service|not operat", re.I)
_ROUTE = re.compile(r"^(?:BXM?|BM?|M|QM?|SIM|S|X)\d{1,3}[A-Z]?$")

_NAME = (
    r"(?:(?:[EWNS]\.? )?(?:\d+(?:st|nd|rd|th)|[A-Z][\w'’.]*(?: [A-Z][\w'’.]*){0,3}) "
    r"(?:Street|St|Avenue|Ave?|Blvd|Road|Rd|Place|Pl|Pkwy|Drive|Dr|Sq|Lane|Ln|Ter|Ct|Tpke|Way|Plaza|Plz|"
    r"Slip|Hwy|Loop|Terrace|Concourse)\b"
    r"(?: (?:[NSEW]|North|South|East|West)\b)?"
    r"|Central Park (?:West|South|North)|Broadway|Bowery|Grand Concourse)"
)
_ALONG = re.compile(rf"({_NAME}) (?:from|between) ({_NAME}) (?:to|and) ({_NAME})")
_PAIR = re.compile(rf"({_NAME})(?:\s*/\s*| at | & )({_NAME})")
_PAST = re.compile(rf"(?:stops?|service) (below|above|south of|north of) ({_NAME})")
_INSTEAD = re.compile(r"[,;.]?\s*(?:please )?use the\b|\bfor service\b", re.I)
_WAY = {"north": (0, 1), "south": (0, -1), "east": (1, 0), "west": (-1, 0)}
# How close, in metres: a stop is at its corner, a street with two roadways is 30 wide.
AT_STOP, ON_WAY = 75, 130


def route(name: str | None) -> str | None:
    """The route the alerts use ("M14A-SBS" and "M14A+" are M14A), or None for another kind."""
    r = re.sub(r"(?:-?SBS|\+)$", "", (name or "").strip().upper().replace(" ", ""))
    return r if _ROUTE.match(r) else None


def alerts(routes: set[str], t0: float, t1: float) -> list[dict]:
    """Alerts on these routes in effect at some point of the ride."""
    from .live import _plain

    try:
        d = get_json(BUS_ALERTS, ttl_s=120)
    except Exception:  # noqa: BLE001 - the trip stands without them
        return []
    soon = t0 - time.time() < 90 * 60
    out = []
    for ent in d.get("entity", []):
        al = ent.get("alert", {})
        hit = {route(x.get("route_id")) for x in al.get("informed_entity") or []} & routes
        kind = (al.get("transit_realtime.mercury_alert") or {}).get("alert_type") or ""
        during = any(
            int(p.get("start") or 0) <= t1 and (t0 < int(p["end"]) if p.get("end") else soon)
            for p in al.get("active_period") or [{}]
        )
        head = _plain(al.get("header_text", {}).get("translation"))
        if hit and during and head and not _SKIP.search(kind):
            out.append({"routes": hit, "kind": kind, "head": head})
    return out


def _km(lat: float, lon: float) -> tuple[float, float]:
    return lon * 84_300, lat * 111_200


def _gap(p: tuple, a: tuple, b: tuple) -> float:
    """Metres from a point to the line between two others (lat, lon)."""
    (px, py), (ax, ay), (bx, by) = _km(*p), _km(*a), _km(*b)
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    k = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length)) if length else 0.0
    return math.hypot(px - ax - k * dx, py - ay - k * dy)


def _meet(a: str, b: str, ride: list[tuple]) -> tuple | None:
    """Where the two streets meet, nearest to the ride."""
    found = [(h["lat"], h["lon"]) for h in corners.find(a, b)]
    return min(found, key=lambda c: min(_gap(c, p, p) for p in ride), default=None)


def _where(head: str, ride: list[tuple]) -> list[tuple]:
    """The stretches an alert is about, as pairs of points. What it says to use instead is not
    part of it."""
    head = re.split(
        r"[,;.]?\s*(?:please )?use the\b|\bfor service\b", head, maxsplit=1, flags=re.I
    )[0]
    out = []
    for m in _ALONG.finditer(head):
        ends = [_meet(m.group(1), m.group(k), ride) for k in (2, 3)]
        if all(ends):
            out.append(tuple(ends))
    rest = _ALONG.sub(" ", head)
    pairs = [(m, _meet(m.group(1), m.group(2), ride)) for m in _PAIR.finditer(rest)]
    pairs = [(m, c) for m, c in pairs if c]
    if len(pairs) == 2 and re.search(r"\bbetween\b|\bfrom\b", rest[: pairs[0][0].start()]):
        out.append((pairs[0][1], pairs[1][1]))
    else:
        out += [(c, c) for _, c in pairs]
    return out


def _uptown(p: tuple) -> float:
    """Metres along Manhattan's avenues."""
    x, y = _km(*p)
    return y * math.cos(math.radians(29)) + x * math.sin(math.radians(29))


def _beyond(head: str, p: tuple) -> bool | None:
    """Whether a stop is on the closed side of "stops below W 86th St". None: cannot tell."""
    m = _PAST.search(head)
    if not m:
        return None
    for cross in ("Central Park West", "8th Ave", "5th Ave", "Broadway", "Madison Ave"):
        c = _meet(m.group(2), cross, [p])
        if c:
            up = _uptown(p) - _uptown(c)
            return up < -40 if m.group(1).lower() in ("below", "south of") else up > 40
    return None


def _other_way(head: str, ride: list[tuple]) -> bool:
    """Whether the alert is only about buses going the other way."""
    said = set(re.findall(r"\b(north|south|east|west)bound\b", head.lower()))
    if len(said) != 1 or "both directions" in head.lower():
        return False
    (ax, ay), (bx, by) = _km(*ride[0]), _km(*ride[-1])
    x, y = _WAY[said.pop()]
    along, total = (bx - ax) * x + (by - ay) * y, math.hypot(bx - ax, by - ay)
    # Manhattan's avenues lean: only a ride clearly headed the other way.
    return total > 100 and along < -0.5 * total


def check(leg: dict, alert: dict) -> str | None:
    """What an alert means for a ride: "closed" (it does not stop where the ride boards or ends),
    "affects", "passes" (a detour on the way, or it does not say where), or None for no matter."""
    ride = [(lat, lon) for lon, lat in leg.get("coords") or []]
    if len(ride) < 2 or _other_way(alert["head"], ride):
        return None
    cut = bool(_CUT.search(f"{alert['kind']} {alert['head']}"))
    ends = (ride[0], ride[-1])
    side = [_beyond(alert["head"], p) for p in ends]
    if any(side):
        return "closed"
    if side[0] is False and side[1] is False:
        return None
    where = _where(alert["head"], ride)
    if not where:
        return "affects" if cut else "passes"
    if any(_gap(p, a, b) < AT_STOP for p in ends for a, b in where):
        return "affects"
    # The drawn line has few points on a long straight street: compare with the line itself too.
    legs = list(zip(ride, ride[1:], strict=False))
    if any(
        _gap(p, a, b) < ON_WAY or _gap(a, *leg) < ON_WAY or _gap(b, *leg) < ON_WAY
        for a, b in where
        for leg in legs
        for p in leg
    ):
        return "affects" if cut else "passes"
    return None


def changes(option: dict, t0: float, t1: float) -> list[str]:
    """Lines for the trip about the buses it rides. Marks the option as affected, or with the stop
    its bus does not serve."""
    legs = [(route(lg.get("name")), lg) for lg in option.get("lines") or [] if _mta_bus(lg)]
    legs = [(r, lg) for r, lg in legs if r]
    if not legs:
        return []
    out = []
    for al in alerts({r for r, _ in legs}, t0, t1):
        for r, lg in legs:
            found = check(lg, al) if r in al["routes"] else None
            if not found:
                continue
            if found == "closed":
                ride = [(lat, lon) for lon, lat in lg["coords"]]
                stop = lg.get("from") if _beyond(al["head"], ride[0]) else lg.get("to")
                option["skipped"] = f"the {lg['name']} does not stop at {stop} then"
            if found != "passes":
                option["affected"] = True
            start = "Service change" if found == "passes" else "Affects this ride"
            line = f"{start} on the {lg['name']} bus at that time: {al['head'][:240]}"
            if line not in out:
                out.append(line)
    out.sort(key=lambda x: not x.startswith("Affects"))
    return out[:3]


def _mta_bus(leg: dict) -> bool:
    return leg.get("mode") == "bus" and bool(
        re.search(r"\bMTA\b|New York City", leg.get("agency") or "")
    )
