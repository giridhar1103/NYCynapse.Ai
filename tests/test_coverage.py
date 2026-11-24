from datetime import datetime
from types import SimpleNamespace

from nycynapse.pipeline.graph import coverage_gap, local_now, newest_record
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


def test_a_note_when_the_last_day_is_not_whole():
    c = ctx(datetime(2024, 1, 1), datetime(2026, 11, 23, 15, 9))
    month = Window(datetime(2026, 11, 1), datetime(2026, 11, 24), "1 to 23 Nov")
    morning = datetime(2026, 11, 24, 6)
    assert coverage_gap(c, ["gold.fct_ridership"], month, morning) is None
    note = newest_record(c, ["gold.fct_ridership"], month, morning)
    assert "Mon Nov 23 2026, 3:09 PM" in note
    earlier = Window(datetime(2026, 11, 1), datetime(2026, 11, 23), "1 to 22 Nov")
    assert newest_record(c, ["gold.fct_ridership"], earlier, morning) is None


def test_a_neighborhood_is_the_citys_own_outlines():
    from nycynapse.tools import areas

    heights = areas.outline("Washington Heights")
    assert sorted(heights["names"]) == ["Washington Heights (North)", "Washington Heights (South)"]
    assert heights["wkt"].startswith("MULTIPOLYGON(((-73.9")
    assert areas.outline("Bed-Stuy")["names"] == areas.outline("bedstuy")["names"]
    assert "Old Astoria-Hallets Point" in areas.outline("Astoria")["names"]
    assert "South Williamsburg" in areas.outline("Williamsburg")["note"]
    # The same name in two boroughs is not guessed at.
    assert "name the borough" in areas.outline("Murray Hill")["error"]
    assert areas.outline("Murray Hill, Queens")["names"] == ["Murray Hill-Broadway Flushing"]
    assert areas.outline("ZIP 11102")["item"] == {
        "name": "ZIP 11102",
        "level": "zip",
        "key": "11102",
    }
    for text in ("Brooklyn", "Nolita", "10047", ""):
        assert "error" in areas.outline(text)


def test_every_outline_fits_in_one_query():
    from nycynapse.tools import areas

    for name, boro, _, parts in areas._hoods():
        for part in parts:
            o = areas.outline(f"{part}, {boro}")
            assert len(o["wkt"]) <= areas.MAX_OUTLINE, (name, part)
    for z in areas._zips():
        assert len(areas.outline(z)["wkt"]) <= areas.MAX_OUTLINE, z


def test_open_data_counts_inside_an_area(monkeypatch):
    from nycynapse.tools import live

    asked = []

    def fake(url, params=None, *a, **k):
        asked.append(params or {})
        return [{"count": "7"}]

    monkeypatch.setattr(live, "get_json", fake)
    r = live.open_data(
        "crime", "count(*)", "law_cat_cd = 'FELONY' or law_cat_cd = 'X'", area="Bushwick"
    )
    where = asked[0]["$where"]
    assert where.startswith(
        "(law_cat_cd = 'FELONY' or law_cat_cd = 'X') AND within_polygon(lat_lon"
    )
    assert "Bushwick (East)" in r.summary and r.map[0]["items"][0]["level"] == "nta"
    assert not live.open_data("events", area="Bushwick").ok
    assert not live.open_data("311", "count(*)", area="Atlantis").ok
