"""Restaurant inspections, one line per restaurant.

The city's data has one row per violation and no current grade: a restaurant's grade is the one
given at its newest graded inspection, and a bad first visit has a score but no grade until the
next one. That is worked out here rather than left to a query.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from . import Tool, ToolResult, areas, failed
from .http import get_json
from .live import _behind, _refused, _socrata_headers
from .trips import locate

NY = ZoneInfo("America/New_York")
DATASET = "43nn-pn8j"
RADIUS_M = 500
MONTHS = 36  # a good restaurant is seen about once a year, some later
PESTS = {"04L": "mice", "04K": "rats", "04M": "roaches"}
PROBLEMS = ("any", "mice", "rats", "roaches", "low_grade", "closed")
_COLUMNS = (
    "camis, dba, boro, building, street, cuisine_description, inspection_date, "
    "inspection_type, action, violation_code, score, grade, latitude, longitude"
)


def _day(row: dict) -> str:
    return str(row.get("inspection_date") or "")[:10]


def _say(day: str) -> str:
    return datetime.fromisoformat(day).strftime("%b %-d, %Y")


def _summed(rows: list[dict], since: str) -> dict:
    """What is known about one restaurant from its rows, pests and closures from `since` on."""
    first = rows[0]
    visits: dict[str, dict] = {}
    for r in rows:
        day = _day(r)
        v = visits.setdefault(
            day,
            {
                "day": day,
                "score": None,
                "grade": None,
                "kind": "",
                "codes": set(),
                "action": "",
                "why": [],
            },
        )
        v["action"] += " " + (r.get("action") or "")
        kind = r.get("inspection_type") or ""
        # Task force visits and paperwork checks are not the inspections that lead to a grade.
        if kind.startswith(("Cycle", "Pre-permit (Operational)")):
            if r.get("score"):
                v["score"] = int(float(r["score"]))
            v["kind"] = next((k for k in ("reopening", "compliance") if k in kind.lower()), "")
            if r.get("grade") in ("A", "B", "C", "P", "Z"):
                v["grade"] = r["grade"]
        if r.get("violation_code"):
            v["codes"].add(r["violation_code"])
            if r.get("violation_description"):
                v["why"].append(r["violation_description"])
    seen = sorted(visits.values(), key=lambda v: v["day"], reverse=True)
    recent = [v for v in seen if v["day"] >= since]
    graded = next((v for v in seen if v["grade"]), None)
    newest = next((v for v in seen if v["score"] is not None), seen[0])
    shut = [v for v in recent if re.search(r"(?<!re-)closed by", v["action"], re.I)]
    reshut = [v for v in recent if "re-closed" in v["action"]]
    closed = max((v["day"] for v in shut + reshut), default=None)
    reopened = next((v["day"] for v in seen if "re-opened" in v["action"]), None)
    # Any ordinary visit after the closure also means it was open again.
    later = next((v["day"] for v in seen if closed and v["day"] > closed), None)
    return {
        "id": first["camis"],
        "name": (first.get("dba") or "").title().replace("'S", "'s"),
        "address": " ".join(
            x
            for x in ((first.get("building") or ""), (first.get("street") or "").title())
            if x.strip()
        ),
        "borough": first.get("boro") or "",
        "cuisine": first.get("cuisine_description") or "",
        "lat": float(first["latitude"]) if first.get("latitude") else None,
        "lon": float(first["longitude"]) if first.get("longitude") else None,
        "grade": graded["grade"] if graded else None,
        "grade_day": graded["day"] if graded else None,
        "grade_score": graded["score"] if graded else None,
        "newest": newest["day"],
        "newest_score": newest["score"],
        "newest_kind": newest["kind"],
        "newest_why": list(dict.fromkeys(newest["why"])),
        "visits": sum(v["score"] is not None for v in recent),
        "pests": {
            name: sorted(v["day"] for v in recent if code in v["codes"])
            for code, name in PESTS.items()
        },
        "closed": closed,
        "reopened": (
            reopened if reopened and closed and reopened >= closed else later if closed else None
        ),
        "pests_ever": {
            name: [v["day"] for v in seen if code in v["codes"]] for code, name in PESTS.items()
        },
        "history": [v for v in seen if v["score"] is not None][:4],
    }


def _line(r: dict) -> str:
    bits = []
    if r["grade"] in ("P", "Z"):
        bits.append(f"grade pending since {_say(r['grade_day'])}")
    elif r["grade"]:
        bits.append(f"grade {r['grade']} (score {r['grade_score']}, {_say(r['grade_day'])})")
    else:
        bits.append("no letter grade in the data")
    if r["newest"] != r["grade_day"] and r["newest_score"] is not None:
        kind = f"{r['newest_kind']} visit" if r["newest_kind"] else "inspection"
        bits.append(f"newest {kind} {_say(r['newest'])}, score {r['newest_score']}, not graded")
    for name, days in r["pests"].items():
        if days:
            bits.append(
                f"{name} found at {len(days)} of {r['visits'] or len(days)} inspections in the "
                f"last 12 months, last {_say(days[-1])}"
            )
    if r["closed"]:
        bits.append(
            f"ordered closed by the city {_say(r['closed'])}"
            + (f", open again by {_say(r['reopened'])}" if r["reopened"] else ", no reopening yet")
        )
    where = ", ".join(x for x in (r["address"], r["borough"]) if x)
    return f"{r['name']}, {where} ({r['cuisine']}): " + "; ".join(bits)


def _badness(r: dict) -> float:
    vermin = sum(len(d) for d in r["pests"].values())
    shut = 0 if not r["closed"] else 8 if r["reopened"] else 30
    newest = r["newest_score"] or 0
    return shut + {"C": 8, "B": 4}.get(r["grade"], 0) + 3 * vermin + newest / 10


def _wanted(problem: str, r: dict) -> bool:
    if problem == "low_grade":
        return r["grade"] in ("B", "C")
    if problem == "closed":
        return bool(r["closed"])
    if problem in PESTS.values():
        return bool(r["pests"][problem])
    return _badness(r) >= 4


def restaurant_inspections(
    where: str = "", name: str = "", problem: str = "any", cuisine: str = "", n: int = 10
) -> ToolResult:
    """Inspection results by restaurant: those named, or the worst in a neighborhood."""
    if problem not in PROBLEMS:
        return failed(f"unknown problem {problem!r}; choose from {', '.join(PROBLEMS)}")
    words = [
        w for w in re.findall(r"[A-Z0-9&]+", re.sub(r"['’]S\b", "", name.upper())) if w != "THE"
    ]
    if not words and not where.strip():
        return failed("name a restaurant, or say where: a neighborhood, ZIP code or place")
    now = datetime.now(NY)
    since = (now - timedelta(days=365)).strftime("%Y-%m-%d")
    clauses = [f"inspection_date >= '{(now - timedelta(days=31 * MONTHS)):%Y-%m-%d}'"]
    clauses += [f"upper(dba) like '%{w}%'" for w in words]
    for w in re.findall(r"[A-Z]+", cuisine.upper()):
        clauses.append(f"upper(cuisine_description) like '%{w}%'")
    about, item = "New York City", None
    if where.strip():
        drawn = areas.outline(where)
        if "error" not in drawn:
            clauses.append(f"within_polygon(location, '{drawn['wkt']}')")
            about, item = drawn["note"], drawn["item"]
        elif "borough" in drawn["error"] and (boro := _borough(where)):
            clauses.append(f"boro = '{boro}'")
            about = boro
            if not words:
                return failed(f"{boro} is too large for one list; name a neighborhood or ZIP code")
        elif "name the borough" in drawn["error"]:
            return failed(drawn["error"])
        else:
            p = locate(where)
            if not p:
                return failed(f"could not place {where!r}; name a neighborhood or an address")
            clauses.append(f"within_circle(location, {p['lat']:.5f}, {p['lon']:.5f}, {RADIUS_M})")
            about = f"within {RADIUS_M} m of {p['name']}"
    query = {
        # The text of each violation is only needed, and only small, for a named restaurant.
        "$select": _COLUMNS + (", violation_description" if words else ""),
        "$where": " AND ".join(clauses),
        "$limit": 30000,
    }

    def ask() -> list[dict]:
        return get_json(
            f"https://data.cityofnewyork.us/resource/{DATASET}.json",
            query,
            _socrata_headers(),
            ttl_s=3600,
            again=_behind,
            timeout=15,
        )

    try:
        try:
            rows = ask()
        except httpx.TransportError:  # it often answers the second time
            rows = ask()
    except httpx.HTTPStatusError as e:
        return failed(_refused(e))
    except httpx.TransportError:
        return failed("the city's data service did not answer in time")
    by_id: dict[str, list[dict]] = {}
    for row in rows:
        by_id.setdefault(row["camis"], []).append(row)
    found = [_summed(x, since) for x in by_id.values()]
    sources = [
        {
            "kind": "live",
            "title": "NYC Health Department restaurant inspections",
            "url": f"https://data.cityofnewyork.us/d/{DATASET}",
        }
    ]
    n = max(1, min(int(n or 10), 15))
    if words:
        shown = sorted(found, key=lambda r: r["newest"], reverse=True)[:n]
        lines = [f"Restaurants matching {name!r} ({about}): {len(found)}"]
    else:
        hits = [r for r in found if _wanted(problem, r)]
        by = (lambda r: r["closed"]) if problem == "closed" else _badness
        shown = sorted(hits, key=by, reverse=True)[:n]
        grades = {g: sum(r["grade"] == g for r in found) for g in "ABC"}
        none = len(found) - sum(grades.values())  # or pending
        what = {"any": "problems", "low_grade": "a B or C", "closed": "a closure"}.get(
            problem, problem
        )
        first = "newest" if problem == "closed" else "worst"
        lines = [
            f"{len(found)} restaurants inspected, area: {about}. Grades now: {grades['A']} A, "
            f"{grades['B']} B, {grades['C']} C, {none} pending or without one. "
            f"{len(hits)} with {what}"
            + (f", the {first} {len(shown)}:" if len(hits) > len(shown) else f", {first} first:")
        ]
    lines += [f"{i}. {_line(r)}" for i, r in enumerate(shown, 1)]
    if words:
        for r in shown[:3]:
            none = [p for p, days in r["pests_ever"].items() if not days]
            if none:
                lines.append(
                    f"   {r['name']}: no {', '.join(none)} cited at any visit since "
                    f"{_say(r['history'][-1]['day']) if r['history'] else 'the data starts'}"
                )
            for v in r["history"]:
                lines.append(
                    f"   {r['name']} {_say(v['day'])}"
                    + (f" ({v['kind']} visit)" if v["kind"] else "")
                    + f": score {v['score']}"
                    + (f", grade {v['grade']}" if v["grade"] in ("A", "B", "C") else ", not graded")
                    + (
                        f". {'; '.join(x[:110] for x in dict.fromkeys(v['why']))}"
                        if v["why"]
                        else ""
                    )
                )
    if not shown:
        lines.append("None.")
    lines.append(
        "Lower scores are better: 0 to 13 is an A, 14 to 27 a B, 28 or more a C. A first visit at "
        "14 or more is not graded until the next. The grade is the one from the newest graded "
        "inspection; names are as registered. The list is from inspections, which are one day each."
    )
    layers: list[dict] = []
    if item:
        layers.append({"type": "highlight", "label": "Restaurant inspections", "items": [item]})
    pts = [
        {"lat": r["lat"], "lon": r["lon"], "label": r["name"][:60], "detail": _line(r)[:160]}
        for r in shown
        if r["lat"] and r["lon"]
    ]
    if pts:
        layers.append({"type": "points", "label": "Restaurants", "points": pts})
    return ToolResult(
        summary="\n".join(lines),
        data={"found": len(found), "shown": [{**r, "history": None} for r in shown]},
        sources=sources,
        map=layers,
    )


def _borough(text: str) -> str | None:
    return next((b for b in areas.BOROUGHS if b.lower() in text.lower()), None)


TOOLS = [
    Tool(
        "restaurant_inspections",
        "live",
        "Health inspections of restaurants, one line each with the current grade, pests found and "
        "closures: the ones named (`name`), or the worst in a neighborhood, ZIP code or around a "
        "place (`where`), optionally of one `cuisine`. Use for 'is X clean', 'bad grades in', "
        "'mice', 'closed by the health department'. Set both `name` and `where` for one "
        "restaurant in a known area. Use open_data only for other counts.",
        {
            "type": "object",
            "properties": {
                "where": {"type": "string"},
                "name": {"type": "string"},
                "problem": {"type": "string", "enum": list(PROBLEMS)},
                "cuisine": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 15},
            },
        },
        restaurant_inspections,
    )
]
