"""Turn a time expression the model extracted into an exact New York time window.

The model only labels what was said ("last month", "the last 7 days", "June 2025"). This code
decides what that means relative to the moment the question was asked, so the same words always
give the same window and the rule can be tested.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

NY = ZoneInfo("America/New_York")
Unit = Literal["hour", "day", "week", "month", "quarter", "year"]


class TimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["all", "now", "date", "month", "year", "between", "since", "relative", "last_n"]
    date: str | None = None  # date, since
    start: str | None = None  # between, inclusive
    end: str | None = None  # between, inclusive
    year: int | None = None
    month: int | None = None
    unit: Unit | None = None
    offset: int | None = None  # relative: 0 this unit so far, -1 the previous whole unit
    n: int | None = None  # last_n


@dataclass(frozen=True)
class Window:
    start: datetime | None  # local New York time, inclusive
    end: datetime | None  # local New York time, exclusive
    label: str
    is_now: bool = False

    def iso(self) -> tuple[str | None, str | None]:
        f = "%Y-%m-%d %H:%M:%S"
        return (
            self.start.strftime(f) if self.start else None,
            self.end.strftime(f) if self.end else None,
        )


def _local(as_of: str) -> datetime:
    return datetime.fromisoformat(as_of).astimezone(NY).replace(tzinfo=None)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _unit_start(now: datetime, unit: Unit) -> datetime:
    d = now.date()
    if unit == "hour":
        return now.replace(minute=0, second=0, microsecond=0)
    if unit == "day":
        start = d
    elif unit == "week":
        start = d - timedelta(days=d.weekday())
    elif unit == "month":
        start = _month_start(d)
    elif unit == "quarter":
        start = date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    else:
        start = date(d.year, 1, 1)
    return datetime.combine(start, datetime.min.time())


def _shift(start: datetime, unit: Unit, n: int) -> datetime:
    if unit == "hour":
        return start + timedelta(hours=n)
    if unit == "day":
        return start + timedelta(days=n)
    if unit == "week":
        return start + timedelta(weeks=n)
    months = {"month": 1, "quarter": 3, "year": 12}[unit] * n
    return datetime.combine(_add_months(start.date(), months), datetime.min.time())


def resolve(spec: TimeSpec, as_of: str) -> Window:
    now = _local(as_of)
    k = spec.kind
    if k == "all":
        return Window(None, None, "all time")
    if k == "now":
        return Window(None, now, "right now", is_now=True)
    if k == "date":
        d = datetime.fromisoformat(spec.date)
        return Window(d, d + timedelta(days=1), d.strftime("%-d %B %Y"))
    if k == "month":
        s = datetime(spec.year, spec.month, 1)
        return Window(s, _shift(s, "month", 1), s.strftime("%B %Y"))
    if k == "year":
        s = datetime(spec.year, 1, 1)
        return Window(s, datetime(spec.year + 1, 1, 1), str(spec.year))
    if k == "between":
        s, e = datetime.fromisoformat(spec.start), datetime.fromisoformat(spec.end)
        return Window(s, e + timedelta(days=1), f"{s:%-d %b %Y} to {e:%-d %b %Y}")
    if k == "since":
        s = datetime.fromisoformat(spec.date)
        return Window(s, now, f"since {s:%-d %b %Y}")
    if k == "last_n":
        return Window(now - _delta(spec.unit, spec.n), now, f"the last {spec.n} {spec.unit}s")
    if k == "relative":
        start = _unit_start(now, spec.unit)
        if spec.offset == 0:
            return Window(start, now, f"this {spec.unit} so far")
        s = _shift(start, spec.unit, spec.offset)
        e = _shift(s, spec.unit, 1)
        word = (
            "yesterday"
            if spec.unit == "day" and spec.offset == -1
            else (f"last {spec.unit}" if spec.offset == -1 else f"{-spec.offset} {spec.unit}s ago")
        )
        return Window(s, e, word)
    raise ValueError(f"unknown time kind {k}")


def _delta(unit: Unit, n: int) -> timedelta:
    if unit in ("month", "quarter", "year"):
        days = {"month": 30, "quarter": 91, "year": 365}[unit] * n
        return timedelta(days=days)
    return {"hour": timedelta(hours=n), "day": timedelta(days=n), "week": timedelta(weeks=n)}[unit]
