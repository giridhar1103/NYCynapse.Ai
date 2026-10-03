from pathlib import Path

from nycynapse.evals.cases import load_dir
from nycynapse.evals.harness import split_of
from nycynapse.guard import check

CASES = Path(__file__).resolve().parents[1] / "evals" / "cases"


def test_cases_load_and_have_unique_ids():
    cases = load_dir(CASES)
    assert len(cases) >= 100


def test_gold_sql_passes_the_guard():
    for c in load_dir(CASES):
        if c.gold_sql:
            r = check(c.gold_sql)
            assert r.ok, f"{c.id}: {r.reason}"


def test_public_cases_hold_no_holdout_split():
    assert all(split_of(c) != "holdout" for c in load_dir(CASES))


def test_paraphrase_groups_share_a_split():
    by_group = {}
    for c in load_dir(CASES):
        if c.group:
            by_group.setdefault(c.group, set()).add(split_of(c))
    assert all(len(s) == 1 for s in by_group.values())
