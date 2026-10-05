from datetime import datetime
from types import SimpleNamespace

from nycynapse.pipeline.graph import coverage_gap, local_now
from nycynapse.pipeline.timeparse import Window

RIDERSHIP = SimpleNamespace(
    label="Subway ridership", table="gold.fct_ridership", coverage=["subway_ridership_hourly"]
)


def ctx(start, end):
    return SimpleNamespace(
        models_for_tables=lambda tables: [RIDERSHIP],
        table_coverage={"subway_ridership_hourly": (start, end)},
    )


def test_window_outside_the_data_is_not_covered():
    c = ctx(datetime(2024, 1, 1), datetime(2026, 9, 25))
    gap = coverage_gap(
        c, ["gold.fct_ridership"], Window(datetime(2026, 10, 1), datetime(2026, 10, 2), "1 Oct")
    )
    assert gap.covered == 0 and "does not include 1 Oct" in gap.message


def test_partly_covered_window_says_how_much():
    c = ctx(datetime(2024, 1, 1), datetime(2026, 9, 25))
    week = Window(datetime(2026, 9, 21), datetime(2026, 9, 28), "last week")
    gap = coverage_gap(c, ["gold.fct_ridership"], week)
    assert 0.5 < gap.covered < 0.6 and "about 57%" in gap.message


def test_covered_window_has_no_gap():
    c = ctx(datetime(2024, 1, 1), datetime(2026, 9, 25))
    june = Window(datetime(2026, 6, 1), datetime(2026, 7, 1), "June 2026")
    assert coverage_gap(c, ["gold.fct_ridership"], june) is None


def test_the_future_is_not_missing_data():
    # "this week" asked on Thursday morning, with data up to Thursday
    c = ctx(datetime(2024, 1, 1), datetime(2026, 10, 1, 4))
    week = Window(datetime(2026, 9, 28), datetime(2026, 10, 5), "this week")
    assert coverage_gap(c, ["gold.fct_ridership"], week, datetime(2026, 10, 1, 6)) is None
    assert coverage_gap(c, ["gold.fct_ridership"], week).covered < 0.9


def test_local_now_is_new_york_time():
    assert local_now("2026-10-03T06:00:00-04:00") == datetime(2026, 10, 3, 6, 0)
    assert local_now("2026-10-03T10:00:00+00:00") == datetime(2026, 10, 3, 6, 0)
