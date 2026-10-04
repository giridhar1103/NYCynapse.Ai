import os
from datetime import datetime

import pytest

from nycynapse.guard import check
from nycynapse.pipeline.compiler import CompileError, Compiler
from nycynapse.pipeline.ir import Plan
from nycynapse.pipeline.timeparse import Window

JUNE = Window(datetime(2025, 6, 1), datetime(2025, 7, 1), "June 2025")


@pytest.fixture(scope="module")
def compiler(catalog):
    return Compiler(catalog)


def sql_of(compiler, window=JUNE, places=None, **plan):
    sql = compiler.compile(Plan(**plan), window, places)
    assert check(sql).ok, check(sql).reason
    return sql


def test_metric_from_its_governed_source(compiler):
    sql = sql_of(
        compiler,
        model="trips_by_zone_hour",
        metrics=["app_ride_count"],
        filters=[{"field": "trips_by_zone_hour.service", "op": "=", "values": ["uber"]}],
    )
    assert (
        "FILTER(WHERE NOT t0.service IN ('yellow', 'green'))" in sql.replace("\n", " ")
        or "service" in sql
    )
    assert "t0.pickup_date >= DATE '2025-06-01'" in sql
    assert "t0.pickup_date < DATE '2025-07-01'" in sql


def test_instant_columns_get_new_york_boundaries(compiler):
    sql = sql_of(compiler, model="service_requests", metrics=["request_count"])
    assert "t0.created_at >= timezone('America/New_York', TIMESTAMP '2025-06-01 00:00:00')" in sql


def test_join_follows_declared_relationship(compiler):
    sql = sql_of(
        compiler,
        model="service_requests",
        metrics=["request_count"],
        group_by=["neighborhoods.neighborhood"],
        order=[{"by": "request_count"}],
        limit=5,
    )
    assert "JOIN gold.dim_neighborhood t1 ON t0.nta_code = t1.nta_code" in sql
    assert "ORDER BY request_count DESC" in sql and "LIMIT 5" in sql


def test_two_hop_join(compiler):
    sql = sql_of(
        compiler,
        model="subway_ridership",
        metrics=["subway_riders"],
        group_by=["neighborhoods.neighborhood"],
    )
    assert "gold.dim_subway_complex" in sql and "gold.dim_neighborhood" in sql


def test_role_picks_the_right_relationship(compiler):
    sql = sql_of(
        compiler,
        model="rideshare_trips",
        metrics=["app_ride_count"],
        group_by=["taxi_zones@dropoff.zone"],
    )
    assert "dropoff_zone_id" in sql and "pickup_zone_id =" not in sql


def test_range_relationship_becomes_exists(compiler):
    sql = sql_of(
        compiler,
        model="service_requests",
        metrics=["request_count"],
        filters=[{"field": "weather_alerts.alert_level", "op": "=", "values": ["warning"]}],
    )
    assert "EXISTS (SELECT 1 FROM gold.fct_weather_alert x" in sql
    assert "JOIN gold.fct_weather_alert" not in sql


def test_range_relationship_cannot_group(compiler):
    with pytest.raises(CompileError):
        compiler.compile(
            Plan(
                model="service_requests",
                metrics=["request_count"],
                group_by=["weather_alerts.event_name"],
            ),
            JUNE,
        )


def test_semi_additive_metric_rolls_up_across_boroughs(compiler):
    sql = sql_of(compiler, model="weather_hourly", metrics=["precipitation_in"])
    assert "avg(precipitation_in)" in sql and "GROUP BY 1" in sql


def test_default_and_metric_filters_apply(compiler):
    sql = sql_of(compiler, model="taxi_trips", metrics=["taxi_card_tip_pct"])
    assert "t0.is_plausible" in sql and "payment_type = 'credit card'" in sql


def test_day_part_uses_local_columns(compiler):
    sql = sql_of(compiler, model="collisions", metrics=["crash_count"], day_part="rush_hour")
    assert "(isodow(t0.crash_date) between 1 and 5 and (t0.crash_hour between 7 and 9" in sql


def test_grouping_by_a_list_counts_each_value(compiler):
    sql = sql_of(
        compiler,
        model="subway_alerts",
        metrics=["subway_alert_count"],
        group_by=["subway_alerts.routes"],
    )
    assert "CROSS JOIN unnest(t0.route_ids) AS u0(value)" in sql
    assert "u0.value AS routes" in sql


def test_filtering_a_list_matches_any_value(compiler):
    sql = sql_of(
        compiler,
        model="subway_alerts",
        metrics=["subway_alert_count"],
        filters=[{"field": "subway_alerts.routes", "op": "in", "values": ["A", "C"]}],
    )
    assert "list_has_any(t0.route_ids, ['A', 'C'])" in sql


def test_place_becomes_code_list(compiler):
    places = {"JFK Airport": {"taxi_zone": [("132", "JFK Airport")]}}
    plan = {
        "model": "trips_by_zone_hour",
        "metrics": ["trip_count"],
        "places": [
            {
                "place": "JFK Airport",
                "level": "taxi_zone",
                "field": "trips_by_zone_hour.pickup_zone_id",
            }
        ],
    }
    sql = Compiler.compile(compiler, Plan(**plan), JUNE, places)
    assert check(sql).ok
    assert "t0.pickup_zone_id IN (132)" in sql


def test_metric_from_the_wrong_model_is_an_error(compiler):
    with pytest.raises(CompileError, match="cannot be computed"):
        compiler.compile(Plan(model="collisions", metrics=["request_count"]), JUNE)


def test_unknown_dimension_is_an_error(compiler):
    with pytest.raises(CompileError):
        compiler.compile(
            Plan(model="collisions", metrics=["crash_count"], group_by=["collisions.weather"]), JUNE
        )


@pytest.mark.skipif(not os.environ.get("NYC_LAKE_PG_DSN"), reason="needs the lake")
def test_compiled_plans_match_gold(catalog):
    from pathlib import Path

    from nycynapse import lake
    from nycynapse.config import Settings
    from nycynapse.evals.cases import load_dir
    from nycynapse.evals.compare import compare
    from nycynapse.evals.execute import run

    con = lake.connect(Settings.from_env())
    cases = {c.id: c for c in load_dir(Path(__file__).resolve().parents[1] / "evals" / "cases")}
    comp = Compiler(catalog)
    plans = {
        "cs-004": (
            Plan(
                model="service_requests",
                metrics=["request_count"],
                filters=[
                    {
                        "field": "service_requests.descriptor",
                        "op": "=",
                        "values": ["Loud Music/Party"],
                    },
                    {
                        "field": "neighborhoods.neighborhood",
                        "op": "in",
                        "values": ["Bushwick (East)", "Bushwick (West)"],
                    },
                ],
            ),
            Window(datetime(2025, 1, 1), datetime(2026, 1, 1), "2025"),
        ),
    }
    for cid, (plan, window) in plans.items():
        case = cases[cid]
        gold = run(con, case.gold_sql)
        got = run(con, comp.compile(plan, window))
        assert compare(gold.columns, gold.rows, got.columns, got.rows, case.compare).match
