"""Step-free access at subway stations: which stations the MTA lists as accessible (from its
station list on data.ny.gov) and which elevators are out of service right now (from its
elevator and escalator feed). Trip plans use this to say, station by station, whether a
route works without stairs."""

from __future__ import annotations

import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .http import get_json

NY = ZoneInfo("America/New_York")
STATIONS = "https://data.ny.gov/resource/39hk-dx4f.json"
OUTAGES = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fnyct_ene.json"
NEAR_M = 250


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower().replace("street", "st"))


def _stations() -> list[dict]:
    return list(get_json(STATIONS, {"$limit": 600}, ttl_s=24 * 3600) or [])


def _outages() -> list[dict]:
    rows = get_json(OUTAGES, ttl_s=300) or []
    return [r for r in rows if r.get("equipmenttype") == "EL" and r.get("isupcomingoutage") != "Y"]


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dy = (lat1 - lat2) * 111_320
    dx = (lon1 - lon2) * 111_320 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def station(name: str, lat: float, lon: float, line: str | None = None) -> dict | None:
    """The MTA's access record for the subway platform nearest a point (the line's own
    platform when it is given), with any elevator out there now."""
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
    try:
        out = [
            o
            for o in _outages()
            if _norm(o.get("station")) == _norm(s["stop_name"])
            and set((o.get("trainno") or "").replace("/", " ").split())
            & set((s.get("daytime_routes") or "").split())
        ]
    except Exception:  # noqa: BLE001 - the station list alone still answers
        out = []
    return {
        "name": s["stop_name"],
        "routes": s.get("daytime_routes"),
        "step_free": {"1": "yes", "2": "partly"}.get(str(ada), "no"),
        "notes": notes,
        "out": [
            f"elevator {o.get('equipment')} ({o.get('serving')}) out"
            + (f" until about {_when(o['estimatedreturntoservice'])}"
               if o.get("estimatedreturntoservice") else "")
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
        text += "; now: " + "; ".join(rec["out"])
    return text
