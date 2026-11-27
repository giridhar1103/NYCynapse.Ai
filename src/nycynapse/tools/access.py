"""Step-free access at subway stations: which stations the MTA lists as accessible (from its
station list on data.ny.gov) and which elevators are out of service right now (from its
elevator and escalator feed). Trip plans use this to say, station by station, whether a
route works without stairs."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import Tool, ToolResult, failed
from .http import get_json

NY = ZoneInfo("America/New_York")
STATIONS = "https://data.ny.gov/resource/39hk-dx4f.json"
OUTAGES = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fnyct_ene.json"
EQUIPMENT = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fnyct_ene_equipments.json"
NEAR_M = 250
LATE = timedelta(hours=12)  # repairs often end after the time given


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("street", "st"))


def _stations() -> list[dict]:
    return list(get_json(STATIONS, {"$limit": 600}, ttl_s=24 * 3600) or [])


def _outages(at: datetime | None = None) -> list[dict]:
    """Elevators out now, or at the time of a trip: planned outages that will have begun count,
    and the ones expected back by then do not."""
    rows = [r for r in get_json(OUTAGES, ttl_s=300) or [] if r.get("equipmenttype") == "EL"]
    if at is None:
        return [r for r in rows if r.get("isupcomingoutage") != "Y"]
    out = []
    for r in rows:
        planned = r.get("isupcomingoutage") == "Y"
        begins, ends = _time(r.get("outagedate")), _time(r.get("estimatedreturntoservice"))
        if planned and (begins is None or at < begins):
            continue
        if ends and at >= ends + (timedelta(0) if planned else LATE):
            continue
        out.append(r)
    return out


def _time(s: str | None) -> datetime | None:
    try:
        return datetime.strptime(s or "", "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=NY)
    except ValueError:
        return None


def _equipment() -> dict[str, tuple[str, set[str]]]:
    """Each elevator's station complex and the lines it serves. At 161 St the one to the 4 does
    not matter to the D; the complex catches names written another way ("Westchester
    Sq-East Tremont Av", "8th Ave") and transfer elevators listed under the other platform."""
    try:
        rows = get_json(EQUIPMENT, ttl_s=24 * 3600) or []
    except Exception:  # noqa: BLE001 - then every elevator at the station counts
        return {}
    return {
        r.get("equipmentno"): (
            str(r.get("stationcomplexid") or ""),
            set(re.findall(r"[A-Z0-9]+", r.get("linesservedbyelevator") or "")),
        )
        for r in rows
    }


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat1 - lat2) * 111_320
    dx = (lon1 - lon2) * 111_320 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def station(
    name: str, lat: float, lon: float, line: str | None = None, at: datetime | None = None
) -> dict | None:
    """The MTA's access record for the subway platform nearest a point (the line's own
    platform when it is given), with any elevator to it that is out, now or at a time."""
    near = [
        (s, _metres(lat, lon, float(s["gtfs_latitude"]), float(s["gtfs_longitude"])))
        for s in _stations()
        if s.get("gtfs_latitude")
    ]
    near = [x for x in near if x[1] <= NEAR_M]
    if not near:
        return None
    if line:
        own = [x for x in near if line in (x[0].get("daytime_routes") or "").split()]
        near = own or near
    s = min(near, key=lambda x: x[1])[0]
    ada = s.get("ada")  # 1 full, 2 partial, 0 none
    notes = s.get("ada_notes") if s.get("ada_notes") not in (None, "NaN", "") else None
    outages_known = True
    try:
        equipment = _equipment() if line else {}
        complex_id = str(s.get("complex_id") or "")

        def here(o: dict) -> bool:
            cx, lines = equipment.get(o.get("equipment"), ("", set()))
            if lines and line not in lines:
                return False
            if line and cx and cx == complex_id:
                return True
            return _norm(o.get("station")) == _norm(s["stop_name"]) and bool(
                set((o.get("trainno") or "").replace("/", " ").split())
                & set((s.get("daytime_routes") or "").split())
            )

        out = [o for o in (_outages(at) if at else _outages()) if here(o)]
    except Exception:  # noqa: BLE001 - the station list alone still answers, but not fully
        out, outages_known = [], False
    return {
        "id": s.get("gtfs_stop_id") or f"{s['stop_name']}|{s.get('daytime_routes')}",
        "outages_known": outages_known,
        "when": "at the time of the trip" if at else "now",
        "name": s["stop_name"],
        "routes": s.get("daytime_routes"),
        "step_free": {"1": "yes", "2": "partly"}.get(str(ada), "no"),
        "notes": notes,
        "out": [
            f"elevator {o.get('equipment')} ({o.get('serving')}) out"
            + (
                f" until about {_when(o['estimatedreturntoservice'])}"
                if o.get("estimatedreturntoservice")
                else ""
            )
            for o in out[:3]
        ],
    }


def _when(s: str) -> str:
    try:
        return datetime.strptime(s, "%m/%d/%Y %I:%M:%S %p").strftime("%a %b %-d %-I:%M %p")
    except ValueError:
        return s


def describe(rec: dict) -> str:
    word = {"yes": "step-free", "partly": "partly step-free", "no": "not step-free (stairs)"}
    text = f"{rec['name']} ({rec['routes']}): {word[rec['step_free']]}"
    if rec["notes"]:
        text += f", {rec['notes']}"
    if rec["out"]:
        text += f"; {rec.get('when', 'now')}: " + "; ".join(rec["out"])
    if not rec.get("outages_known", True):
        text += "; elevator status could not be checked"
    return text


# --------------------------------------------------------------------------------------- the tool


def _elevators(complex_id: str) -> list[dict]:
    rows = get_json(EQUIPMENT, ttl_s=24 * 3600) or []
    return [
        r
        for r in rows
        if str(r.get("stationcomplexid") or "") == complex_id
        and r.get("equipmenttype") == "EL"
        and r.get("isactive") == "Y"
    ]


def complex_report(lat: float, lon: float) -> dict | None:
    """Step-free access at the station complex nearest a point: whether each platform is
    accessible, where the elevators go, and which are out now or planned to be."""
    near = [
        (s, _metres(lat, lon, float(s["gtfs_latitude"]), float(s["gtfs_longitude"])))
        for s in _stations()
        if s.get("gtfs_latitude")
    ]
    near = [x for x in near if x[1] <= NEAR_M]
    if not near:
        return None
    first = min(near, key=lambda x: x[1])[0]
    cx = str(first.get("complex_id") or "")
    platforms = [s for s in _stations() if str(s.get("complex_id") or "") == cx] or [first]
    word = {"1": "step-free", "2": "partly step-free"}
    lines = []
    for s in platforms:
        kind = word.get(str(s.get("ada")), "not step-free (stairs only)")
        text = f"{s['stop_name']} ({s.get('daytime_routes')}): {kind}"
        notes = s.get("ada_notes")
        if notes not in (None, "NaN", ""):
            text += f", {notes}"
        lines.append(text)
    try:
        lifts = _elevators(cx)
        rows = [r for r in get_json(OUTAGES, ttl_s=300) or [] if r.get("equipmenttype") == "EL"]
        known = True
    except Exception:  # noqa: BLE001 - the station list alone still answers, but not fully
        lifts, rows, known = [], [], False
    mine = {r.get("equipmentno") for r in lifts}
    now = {
        r.get("equipment"): r
        for r in rows
        if r.get("equipment") in mine and r.get("isupcomingoutage") != "Y"
    }
    later = {
        r.get("equipment"): r
        for r in rows
        if r.get("equipment") in mine and r.get("isupcomingoutage") == "Y"
    }
    lift_lines = []
    for r in sorted(
        lifts,
        key=lambda r: (
            r.get("ADA") != "Y",
            "street" not in (r.get("shortdescription") or "").lower(),
        ),
    ):
        no = r.get("equipmentno")
        text = f"{no} {r.get('serving')}"
        if r.get("ADA") != "Y":
            text += " (not an accessible route)"
        if no in now:
            o = now[no]
            text += ": OUT NOW"
            if o.get("estimatedreturntoservice"):
                text += f", back about {_when(o['estimatedreturntoservice'])}"
            if r.get("alternativeroute"):
                text += f". MTA's way around it: {r['alternativeroute'][:400]}"
        elif known:
            text += ": working now"
        if no in later:
            o = later[no]
            text += f"; planned outage from {_when(o.get('outagedate') or '')}"
            if o.get("estimatedreturntoservice"):
                text += f" to {_when(o['estimatedreturntoservice'])}"
        lift_lines.append(text)
    return {
        "name": first["stop_name"],
        "complex_id": cx,
        "platforms": lines,
        "elevators": lift_lines,
        "outages_known": known,
        "out_now": sorted(now),
    }


def station_access(station: str) -> ToolResult:
    from datetime import datetime as _dt

    from .trips import locate

    p = locate(station)
    if not p:
        return failed(f"could not place {station!r}; name the subway station")
    rep = complex_report(p["lat"], p["lon"])
    if not rep:
        return failed(f"no subway station within {NEAR_M} m of {p['name']}")
    asof = _dt.now(NY)
    parts = [
        f"Step-free access at {rep['name']} (MTA station list and elevator feed, "
        f"{asof:%-I:%M %p %a %b %-d}):"
    ]
    parts += [f"- {x}" for x in rep["platforms"]]
    if rep["elevators"]:
        parts.append("Elevators:")
        parts += [f"- {x}" for x in rep["elevators"]]
    elif rep["outages_known"]:
        parts.append("No elevators listed at this station.")
    if not rep["outages_known"]:
        parts.append("Elevator status could not be checked right now.")
    elif rep["elevators"] and not rep["out_now"]:
        parts.append("No elevator here is out right now.")
    return ToolResult(
        summary="\n".join(parts),
        data=rep,
        sources=[
            {
                "kind": "live",
                "title": "MTA elevator and escalator status",
                "url": "https://www.mta.info/elevator-escalator-status",
            },
            {
                "kind": "live",
                "title": "MTA subway stations (data.ny.gov)",
                "url": "https://data.ny.gov/d/39hk-dx4f",
            },
        ],
    )


TOOLS = [
    Tool(
        "station_access",
        "live",
        "Step-free access at one subway station: whether each platform is accessible, where each "
        "elevator goes (which street corner, which platform), and which elevators are out right "
        "now or have a planned outage, with the MTA's way around one that is out. Use for "
        "strollers, wheelchairs, luggage, 'is X accessible', 'where is the elevator at X'.",
        {
            "type": "object",
            "properties": {"station": {"type": "string", "description": "subway station name"}},
            "required": ["station"],
        },
        station_access,
    )
]
