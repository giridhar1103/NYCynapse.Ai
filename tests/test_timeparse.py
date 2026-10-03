from datetime import datetime

import pytest

from nycynapse.pipeline.timeparse import TimeSpec, resolve

AS_OF = "2026-10-03T06:00:00-04:00"  # a Saturday


@pytest.mark.parametrize(
    "spec,start,end",
    [
        ({"kind": "relative", "unit": "month", "offset": -1}, "2026-09-01", "2026-10-01"),
        ({"kind": "relative", "unit": "day", "offset": -1}, "2026-10-02", "2026-10-03"),
        ({"kind": "relative", "unit": "week", "offset": -1}, "2026-09-21", "2026-09-28"),
        ({"kind": "relative", "unit": "week", "offset": 0}, "2026-09-28", "2026-10-03 06:00"),
        ({"kind": "relative", "unit": "year", "offset": 0}, "2026-01-01", "2026-10-03 06:00"),
        ({"kind": "relative", "unit": "year", "offset": -1}, "2025-01-01", "2026-01-01"),
        ({"kind": "relative", "unit": "quarter", "offset": -1}, "2026-07-01", "2026-10-01"),
        ({"kind": "month", "year": 2025, "month": 6}, "2025-06-01", "2025-07-01"),
        ({"kind": "month", "year": 2025, "month": 12}, "2025-12-01", "2026-01-01"),
        ({"kind": "year", "year": 2024}, "2024-01-01", "2025-01-01"),
        ({"kind": "date", "date": "2025-07-04"}, "2025-07-04", "2025-07-05"),
        (
            {"kind": "between", "start": "2025-12-01", "end": "2026-02-28"},
            "2025-12-01",
            "2026-03-01",
        ),
        ({"kind": "last_n", "unit": "day", "n": 7}, "2026-09-26 06:00", "2026-10-03 06:00"),
        ({"kind": "since", "date": "2024-01-01"}, "2024-01-01", "2026-10-03 06:00"),
    ],
)
def test_windows(spec, start, end):
    w = resolve(TimeSpec(**spec), AS_OF)
    assert w.start == datetime.fromisoformat(start)
    assert w.end == datetime.fromisoformat(end)


def test_now_and_all():
    assert resolve(TimeSpec(kind="now"), AS_OF).is_now
    w = resolve(TimeSpec(kind="all"), AS_OF)
    assert w.start is None and w.end is None


def test_as_of_is_read_in_new_york_time():
    # 01:00 UTC on the 1st is still the previous evening in New York.
    w = resolve(TimeSpec(kind="relative", unit="month", offset=-1), "2026-10-01T01:00:00+00:00")
    assert w.start == datetime(2026, 8, 1)


def test_labels():
    assert resolve(TimeSpec(kind="relative", unit="day", offset=-1), AS_OF).label == "yesterday"
    assert resolve(TimeSpec(kind="month", year=2025, month=6), AS_OF).label == "June 2025"
