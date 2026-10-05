import copy

import pytest
from pydantic import ValidationError

from nycynapse.semantic.schema import Catalog, Metric
from nycynapse.semantic.validate import validate


def test_semantic_layer_matches_gold(catalog, manifest):
    problems = validate(catalog, manifest)
    assert problems == [], "\n".join(map(str, problems))


def test_every_workspace_has_models_and_every_model_a_home(catalog):
    homes = {m for w in catalog.workspaces for m in w.models}
    assert {m.name for m in catalog.models} == homes


def test_every_metric_has_a_unit_and_synonyms_are_unique(catalog):
    seen = {}
    for m in catalog.metrics:
        assert m.unit
        for s in [m.label.lower(), *(x.lower() for x in m.synonyms)]:
            assert seen.get(s, m.id) == m.id, f"{s} used by {seen[s]} and {m.id}"
            seen[s] = m.id


def _broken(catalog, **changes):
    data = copy.deepcopy(catalog.model_dump())
    for path, value in changes.items():
        target = data
        *keys, last = path.split(".")
        for k in keys:
            target = target[int(k)] if k.isdigit() else target[k]
        target[last] = value
    return Catalog(**data)


def test_unknown_column_is_reported(catalog, manifest):
    i = next(i for i, m in enumerate(catalog.models) if m.name == "service_requests")
    j = next(j for j, d in enumerate(catalog.models[i].dimensions) if d.name == "agency")
    broken = _broken(catalog, **{f"models.{i}.dimensions.{j}.column": "agencyy"})
    assert any("unknown column agencyy" in str(p) for p in validate(broken, manifest))


def test_bad_relationship_column_is_reported(catalog, manifest):
    i = next(i for i, r in enumerate(catalog.relationships) if r.id == "arrival_station")
    broken = _broken(
        catalog, **{f"relationships.{i}.condition": "{left}.station = {right}.station_id"}
    )
    assert any("unknown column l.station" in str(p) for p in validate(broken, manifest))


def test_metric_without_aggregate_is_reported(catalog, manifest):
    i = next(i for i, m in enumerate(catalog.metrics) if m.id == "request_count")
    broken = _broken(catalog, **{f"metrics.{i}.sources.0.expression": "hours_to_close"})
    assert any("no aggregate" in str(p) for p in validate(broken, manifest))


def test_semi_additive_metric_needs_a_rollup():
    with pytest.raises(ValidationError):
        Metric(
            id="x",
            label="x",
            description="x",
            unit="in",
            format="inches",
            additivity="semi_additive",
            sources=[{"model": "m", "expression": "sum(a)"}],
        )


def test_misspelled_key_is_rejected(catalog):
    data = catalog.model_dump()
    data["metrics"][0]["synonymns"] = ["typo"]
    with pytest.raises(ValidationError):
        Catalog(**data)


def test_aliases_point_at_real_dimensions(catalog, manifest):
    from nycynapse.semantic.validate import validate

    assert catalog.aliases, "aliases.yaml is loaded"
    assert not [p for p in validate(catalog, manifest) if "alias" in str(p)]
