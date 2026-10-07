import json

from nycynapse.llm.client import LLMResult
from nycynapse.pipeline import plan as plan_mod


def fake(reply):
    return lambda *a, **k: LLMResult(json.dumps(reply), "m", "p", 1, 1, 1, 0.0)


def test_missing_data_declines(monkeypatch):
    monkeypatch.setattr(
        plan_mod,
        "complete",
        fake({"model": "none", "reason": "no crime data", "decline_kind": "data_missing"}),
    )
    p = plan_mod.make_plan("q", "2026-10-03T06:00:00-04:00", "ctx")
    assert p.declined == "no crime data" and not p.fallback


def test_a_plan_it_cannot_express_falls_back_to_sql(monkeypatch):
    monkeypatch.setattr(
        plan_mod,
        "complete",
        fake(
            {
                "model": "none",
                "reason": "ratio of two subjects",
                "decline_kind": "plan_cannot_express",
            }
        ),
    )
    p = plan_mod.make_plan("q", "2026-10-03T06:00:00-04:00", "ctx")
    assert p.fallback and p.declined is None and p.error is None


def test_aggregates_are_part_of_a_plan(monkeypatch):
    monkeypatch.setattr(
        plan_mod,
        "complete",
        fake(
            {
                "model": "collision_people",
                "aggregates": [{"fn": "avg", "field": "collision_people.age"}],
            }
        ),
    )
    p = plan_mod.make_plan("q", "2026-10-03T06:00:00-04:00", "ctx")
    assert p.plan.aggregates[0].name == "avg_age"


def test_nulls_from_strict_output_fall_back_to_defaults(monkeypatch):
    monkeypatch.setattr(
        plan_mod,
        "complete",
        fake(
            {
                "model": "service_requests",
                "metrics": ["request_count"],
                "filters": None,
                "group_by": None,
                "limit": None,
                "reason": None,
                "decline_kind": None,
            }
        ),
    )
    p = plan_mod.make_plan("q", "2026-10-03T06:00:00-04:00", "ctx")
    assert p.plan.filters == [] and p.plan.limit is None


def test_strict_schema_for_openai():
    from nycynapse.llm.client import strict_schema

    s = strict_schema(plan_mod.plan_schema())
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    assert "having_min" not in s["properties"]  # free-form map
    assert "null" in s["properties"]["limit"]["anyOf"][-1]["type"] or "null" in str(
        s["properties"]["limit"]
    )
