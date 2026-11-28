"""Opening hours as mapped in OpenStreetMap, read into days and clock times.

Museum and zoo sites often refuse the guide, so their hours come from the map first. Only the
common forms of the opening_hours tag are read (weekdays, "off", the nth weekday of a month, fixed
dates, months, public holidays); anything else is given as written and not worked out.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import ToolResult, failed, osm

NY = ZoneInfo("America/New_York")
PAGE = "https://www.openstreetmap.org/{}/{}"
_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MON = "|".join(_MONTHS)
_DAY = "|".join(_DAYS)
_TIME = r"\d{1,2}:\d{2}-\d{1,2}:\d{2}"
_RULE = re.compile(
    rf"^(?P<months>(?:(?:{_MON})(?:\s+\d{{1,2}})?(?:-(?:(?:{_MON})\s+)?\d{{1,2}}|-(?:{_MON}))?"
    rf"(?:,(?:{_MON})(?:\s+\d{{1,2}})?)*))?\s*"
    rf"(?P<days>(?:(?:{_DAY}|PH)(?:\[-?\d\])?(?:-(?:{_DAY}))?)(?:,(?:(?:{_DAY}|PH)(?:\[-?\d\])?"
    rf"(?:-(?:{_DAY}))?))*)?\s*"
    rf"(?P<times>{_TIME}(?:,{_TIME})*|off|closed|24/7)?$"
)


def _nth(d: date, n: int) -> bool:
    """d is the nth weekday of its kind in its month (n < 0 counts from the end)."""
    if n > 0:
        return (d.day - 1) // 7 + 1 == n
    return (d + timedelta(days=-7 * n)).month != d.month and (
        d + timedelta(days=-7 * (n + 1))
    ).month == d.month


def _weekday_of(year: int, month: int, wd: int, n: int) -> date:
    first = date(year, month, 1)
    if n > 0:
        return first + timedelta(days=(wd - first.weekday()) % 7 + 7 * (n - 1))
    last = (date(year + month // 12, month % 12 + 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - wd) % 7 + 7 * (-n - 1))


def _observed(d: date) -> date:
    return (
        d - timedelta(days=1)
        if d.weekday() == 5
        else d + timedelta(days=1)
        if d.weekday() == 6
        else d
    )


def holiday(d: date) -> str | None:
    """The federal holiday on d, by its observed day."""
    y = d.year
    fixed = {
        "New Year's Day": date(y, 1, 1),
        "Juneteenth": date(y, 6, 19),
        "Independence Day": date(y, 7, 4),
        "Veterans Day": date(y, 11, 11),
        "Christmas Day": date(y, 12, 25),
    }
    for name, day in fixed.items():
        if d in (day, _observed(day)):
            return name
    moving = {
        "Martin Luther King Jr. Day": _weekday_of(y, 1, 0, 3),
        "Presidents Day": _weekday_of(y, 2, 0, 3),
        "Memorial Day": _weekday_of(y, 5, 0, -1),
        "Labor Day": _weekday_of(y, 9, 0, 1),
        "Columbus Day": _weekday_of(y, 10, 0, 2),
        "Thanksgiving": _weekday_of(y, 11, 3, 4),
    }
    return next((name for name, day in moving.items() if day == d), None)


def _months_match(sel: str, d: date) -> bool:
    for part in sel.split(","):
        m = re.fullmatch(
            rf"({_MON})(?:\s+(\d{{1,2}}))?(?:-(?:({_MON})\s+)?(\d{{1,2}})|-({_MON}))?", part.strip()
        )
        if not m:
            return False
        m1, d1, m2, d2, m3 = m.groups()
        a = _MONTHS.index(m1) + 1
        if m3:  # Jun-Aug
            b = _MONTHS.index(m3) + 1
            if (a <= d.month <= b) if a <= b else (d.month >= a or d.month <= b):
                return True
        elif d1 and d2:  # Dec 24-26, Dec 24-Jan 02
            b = _MONTHS.index(m2) + 1 if m2 else a
            lo, hi = (a, int(d1)), (b, int(d2))
            here = (d.month, d.day)
            if (lo <= here <= hi) if lo <= hi else (here >= lo or here <= hi):
                return True
        elif d1:  # Dec 25
            if (d.month, d.day) == (a, int(d1)):
                return True
        elif d.month == a:  # Nov
            return True
    return False


def _days_match(sel: str, d: date) -> bool:
    for part in sel.split(","):
        m = re.fullmatch(rf"({_DAY}|PH)(?:\[(-?\d)\])?(?:-({_DAY}))?", part)
        a, n, b = m.groups()
        if a == "PH":
            if holiday(d):
                return True
            continue
        i, j = _DAYS.index(a), _DAYS.index(b) if b else _DAYS.index(a)
        inside = (i <= d.weekday() <= j) if i <= j else (d.weekday() >= i or d.weekday() <= j)
        if inside and (not n or _nth(d, int(n))):
            return True
    return False


def _clock(t: str) -> str:
    h, m = (int(x) for x in t.split(":"))
    if h >= 24:
        h -= 24
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _said(times: str) -> str:
    if times in ("off", "closed"):
        return "closed"
    if times == "24/7":
        return "open all day"
    spans = []
    for span in times.split(","):
        a, b = span.split("-")
        spans.append(f"{_clock(a)} to {_clock(b)}")
    return ", ".join(spans)


def parse(tag: str) -> list[tuple[str, str, str]] | None:
    """[(months, days, times)] for each rule, or None when a rule is not a form read here."""
    rules = []
    # "Mo-Fr 09:00-17:00, Sa 10:00-14:00": a comma before a day starts another rule.
    parts = re.split(rf"\s*;\s*|,\s+(?=(?:{_DAY}|{_MON}|PH)\b)", tag)
    for raw in parts:
        raw = raw.strip()
        if not raw:
            continue
        m = _RULE.match(raw)
        if not m or not any(m.groups()) or not m["times"]:
            return None
        rules.append((m["months"] or "", m["days"] or "", m["times"]))
    return rules or None


def on(rules: list[tuple[str, str, str]], d: date) -> str | None:
    """What the rules say for d: the last rule that matches wins, as in the tag's own rules.
    None when no rule names the day (not mapped as open)."""
    said = None
    for months, days, times in rules:
        if months and not _months_match(months, d):
            continue
        if days and not _days_match(days, d):
            continue
        said = _said(times)
    return said


def _dated(rules: list[tuple[str, str, str]], d: date) -> bool:
    """A rule made for this one day (public holidays, a date, the nth weekday of a month)
    names it; a season like Sep-May does not count."""
    for months, days, _ in rules:
        if months and not _months_match(months, d):
            continue
        if days and not _days_match(days, d):
            continue
        if "PH" in days or re.search(r"\d", months) or "[" in days:
            return True
    return False


def _words(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower())) - {"the", "of", "and", "nyc", "new", "york"}


def opening_hours(place: str, date_from: str | None = None) -> ToolResult:
    """Opening hours of a museum, zoo, garden or venue for seven days, from OpenStreetMap."""
    place = (place or "").strip()[:100]
    if not place:
        return failed("no place given")
    hit = osm.tagged(re.sub(r"^\s*the\s+", "", place, flags=re.I))
    name_only = re.sub(r"^\s*the\s+", "", place.split(",")[0], flags=re.I).strip()
    if not hit and "," in place and name_only:
        # "Vessel, Hudson Yards" finds nothing, as the area is not part of its address; the
        # name alone finds it, and the name check below still guards against a wrong match.
        hit = osm.tagged(name_only)
    if not hit:
        return failed(f"OpenStreetMap had no match for {place!r} now; use the place's own site")
    name = hit.get("name") or ""
    if not _words(place.split(",")[0]) & _words(name):
        return failed(f"OpenStreetMap's match for {place!r} was {name!r}, not the place")
    tags = hit.get("extratags") or {}
    tag = tags.get("opening_hours")
    page = PAGE.format(hit.get("osm_type", "node"), hit.get("osm_id", ""))
    if not tag:
        return failed(f"{name} has no opening hours on OpenStreetMap; use its own site")
    try:
        start = date.fromisoformat(date_from) if date_from else datetime.now(NY).date()
    except ValueError:
        start = datetime.now(NY).date()
    rules = parse(tag)
    lines, days = [], []
    for k in range(7):
        d = start + timedelta(days=k)
        said = on(rules, d) if rules else None
        label = f"{d:%A, %B} {d.day}"
        shown = said or "not listed as open"
        if holiday(d):
            label += f" ({holiday(d)})"
            if rules and said != "closed" and not _dated(rules, d):
                said, shown = None, "no holiday hours on the map, many places close: check its site"
        if rules:
            lines.append(f"{label}: {shown}")
        days.append({"date": d.isoformat(), "hours": said})
    checked = tags.get("check_date:opening_hours") or tags.get("check_date")
    note = (
        "As mapped in OpenStreetMap by volunteers"
        + (f", checked {checked}" if checked else ", date of the last check not given")
        + ": may be out of date, and special closings are listed only when mapped. Say that the "
        "place's own site has the final word."
    )
    if rules:
        summary = f"{name} opening hours (tag: {tag}):\n" + "\n".join(lines) + "\n" + note
    else:
        summary = (
            f"{name} opening hours as written in OpenStreetMap, not worked out by day: {tag}\n"
            + note
        )
    return ToolResult(
        summary=summary,
        data={
            "name": name,
            "tag": tag,
            "days": days if rules else [],
            "website": tags.get("website"),
        },
        sources=[{"title": f"OpenStreetMap: {name}", "url": page}],
    )
