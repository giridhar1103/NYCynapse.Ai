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
    where = asked[-1]["$where"]
    assert "(law_cat_cd = 'FELONY' or law_cat_cd = 'X') AND within_polygon(lat_lon" in where
    assert "Bushwick (East)" in r.summary and r.map[0]["items"][0]["level"] == "nta"
    assert not live.open_data("events", area="Bushwick").ok
    assert not live.open_data("311", "count(*)", area="Atlantis").ok


def test_crime_counts_leave_out_earlier_years(monkeypatch):
    from nycynapse.tools import live

    asked = []

    def fake(url, params=None, *a, **k):
        asked.append(params or {})
        if "newest" in (params or {}).get("$select", ""):
            return [{"newest": "2026-09-30T00:00:00.000"}]
        return [{"count": "7"}]

    monkeypatch.setattr(live, "get_json", fake)
    r = live.open_data("crime", "count(*)", "ofns_desc = 'ROBBERY' or ofns_desc = 'ARSON'")
    assert asked[-1]["$where"] == (
        "(ofns_desc = 'ROBBERY' or ofns_desc = 'ARSON') AND cmplnt_fr_dt >= '2026-01-01T00:00:00'"
    )
    assert "up to Sep 30" in r.summary
    live.open_data("crime", "count(*)")
    assert asked[-1]["$where"] == "cmplnt_fr_dt >= '2026-01-01T00:00:00'"
    # A query that picks its own dates keeps them.
    own = "cmplnt_fr_dt >= '2026-05-01'"
    r = live.open_data("crime", "count(*)", own)
    assert {"$where": own} in [{"$where": a.get("$where")} for a in asked]
    assert "left out" not in r.summary


def test_a_query_the_city_refuses_says_why(monkeypatch):
    import httpx

    from nycynapse.tools import live

    def answer(code, **kw):
        def fake(url, *a, **k):
            resp = httpx.Response(code, request=httpx.Request("GET", url), **kw)
            raise httpx.HTTPStatusError("no", request=resp.request, response=resp)

        return fake

    message = "Could not parse within_polygon(location, 'MULTIPOLYGON(((1 2)))'): no such column x"
    monkeypatch.setattr(live, "get_json", answer(400, json={"message": message}))
    r = live.open_data("311", "count(*)", "x = 1")
    assert not r.ok and "no such column x" in r.error and "MULTIPOLYGON" not in r.error
    monkeypatch.setattr(live, "get_json", answer(503))
    assert "503" in live.open_data("311", "count(*)").error


def _visit(
    day, score=None, grade=None, code=None, kind="Cycle Inspection / Initial Inspection", **kw
):
    return {
        "camis": "1",
        "dba": "TEST'S KITCHEN",
        "boro": "Queens",
        "building": "1",
        "street": "MAIN STREET",
        "cuisine_description": "Thai",
        "inspection_date": f"{day}T00:00:00.000",
        "inspection_type": kind,
        "score": score,
        "grade": grade,
        "violation_code": code,
        "violation_description": "x" if code else None,
        "action": kw.get("action", "Violations were cited in the following area(s)."),
    }


def test_a_restaurants_grade_is_its_newest_graded_inspection():
    from nycynapse.tools import restaurants as R

    r = R._summed(
        [
            _visit("2026-09-10", "0", kind="Inter-Agency Task Force / Initial Inspection"),
            _visit("2026-09-01", "30", code="04L"),
            _visit("2026-09-01", "30", code="04K"),
            _visit("2026-09-01", kind="Administrative Miscellaneous / Initial Inspection"),
            _visit("2026-02-01", "12", "A", "10F", kind="Cycle Inspection / Re-inspection"),
            _visit("2024-01-01", "40", "C", "04M"),
        ],
        since="2025-11-24",
    )
    assert (r["grade"], r["grade_day"], r["grade_score"]) == ("A", "2026-02-01", 12)
    # A task force visit is not the inspection that leads to a grade.
    assert (r["newest"], r["newest_score"], r["visits"]) == ("2026-09-01", 30, 2)
    assert r["pests"] == {"mice": ["2026-09-01"], "rats": ["2026-09-01"], "roaches": []}
    line = R._line(r)
    assert "grade A (score 12, Feb 1, 2026)" in line and "score 30, not graded" in line
    assert line.startswith("Test's Kitchen, 1 Main Street, Queens (Thai)")


def test_a_closure_stands_until_a_later_visit():
    from nycynapse.tools import restaurants as R

    shut = _visit(
        "2026-10-07", "90", action="Establishment Closed by DOHMH. Violations were cited."
    )
    r = R._summed([shut], since="2025-11-24")
    assert r["closed"] == "2026-10-07" and not r["reopened"] and "no reopening yet" in R._line(r)
    back = _visit(
        "2026-10-12",
        "2",
        "P",
        kind="Cycle Inspection / Reopening Inspection",
        action="Establishment re-opened by DOHMH.",
    )
    r = R._summed([shut, back], since="2025-11-24")
    assert r["reopened"] == "2026-10-12" and "grade pending since Oct 12, 2026" in R._line(r)


def test_restaurant_names_are_matched_by_word(monkeypatch):
    from nycynapse.tools import restaurants as R

    asked = []
    monkeypatch.setattr(R, "get_json", lambda url, p, *a, **k: asked.append(p) or [])
    R.restaurant_inspections(name="Joe's Shanghai'; drop", where="Chinatown")
    where = asked[0]["$where"]
    assert "upper(dba) like '%JOE%'" in where and "like '%SHANGHAI%'" in where
    assert "';" not in where and "within_polygon" in where
    assert not R.restaurant_inspections().ok
    assert not R.restaurant_inspections(where="Queens").ok
    assert not R.restaurant_inspections(where="Chinatown", problem="dragons").ok


def test_a_dearer_train_two_minutes_later_is_not_first():
    from datetime import datetime

    from nycynapse.tools import trips as T

    when = datetime(2026, 11, 25, 5, 30, tzinfo=T.NY)
    end = when.timestamp()

    def option(leave, arrive, transfers, *lines):
        return {
            "leave_ts": end - 60 * leave,
            "arrive_ts": end - 60 * arrive,
            "minutes": (leave - arrive, leave - arrive),
            "transfers": transfers,
            "lines": [{"mode": m, "name": n} for m, n in lines],
        }

    rail = option(88, 3, 2, ("subway", "2"), ("train", "LIRR"), ("bus", "Q70-SBS"))
    plain = option(90, 13, 2, ("bus", "B41"), ("subway", "F"), ("bus", "Q70-SBS"))
    assert T._for_deadline([rail, plain], when, True) == [plain, rail]
    # Half an hour more at home is worth the fare.
    rail["leave_ts"] += 1800
    assert T._for_deadline([plain, rail], when, True)[0] is rail
    # A ride with no known price does not make the total look complete.
    amtrak = option(60, 10, 1, ("train", "Amtrak"), ("subway", "2"))
    assert T._fare(amtrak) == "$3.00 plus the Amtrak fare"
    assert T._dollars(amtrak) == 23.0


def test_inside_zone_one_the_terminal_tells_the_direction():
    from datetime import datetime

    from nycynapse.tools import railfares as R

    def at(h, m):
        return datetime(2026, 11, 25, h, m, tzinfo=R.NY).timestamp()

    assert R.fare("LIRR", "Forest Hills", "Penn Station", at(8, 20), at(8, 40)) == "$7.25"
    assert R.fare("LIRR", "Forest Hills", "Penn Station", at(11, 20), at(11, 40)) == "$5.25"
    assert R.fare("LIRR", "Penn Station", "Woodside", at(4, 45), at(4, 56)) == "$5.25"
    assert R.fare("LIRR", "Penn Station", "Woodside", at(17, 45), at(17, 56)) == "$7.25"
    assert " or " in R.fare("LIRR", "Woodside", "Forest Hills", at(17, 45), at(17, 55))


def test_the_staten_island_railway_is_the_subway_and_the_ferry_is_free():
    from nycynapse.tools import trips as T

    def leg(mode, agency, short):
        stop = {"name": "St George", "lat": 40.64, "lon": -74.07}
        return {
            "mode": mode,
            "agencyName": agency,
            "routeShortName": short,
            "from": stop,
            "to": stop,
            "duration": 600,
            "startTime": "2026-11-28T13:00:00Z",
            "endTime": "2026-11-28T13:10:00Z",
        }

    sir = leg("REGIONAL_RAIL", "MTA New York City Transit", "SIR")
    boat = leg("FERRY", "New York City Department of Transportation", "")
    assert T._leg_text(sir, "").startswith("SIR train")
    assert T._leg_text(boat, "").startswith("Staten Island Ferry from")
    lines = [T._line(sir, ""), T._line(boat, "")]
    assert [(x["mode"], x["name"]) for x in lines] == [
        ("subway", "SIR"),
        ("ferry", "Staten Island Ferry"),
    ]
    assert T._fare({"lines": lines}) == "$3.00"
    assert T._fare({"lines": lines[1:]}) == "free"
    lirr = leg("REGIONAL_RAIL", "Long Island Rail Road", "")
    assert T._line(lirr, "")["mode"] == "train"
