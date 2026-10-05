from nycynapse.evals.judge import grader_error_rates, needs_review


def result(match, gold_rows=5, expected="answerable"):
    return {
        "expected": expected,
        "executed": True,
        "sql": "select 1",
        "result_match": match,
        "gold_rows": gold_rows,
    }


def test_what_gets_reviewed():
    assert needs_review(result(False))
    assert needs_review(result(True, gold_rows=1))  # single value: could be a coincidence
    assert not needs_review(result(True, gold_rows=5))
    assert not needs_review(result(None, expected="unsupported"))


def test_error_rates():
    reviews = [
        {"comparison": False, "panel": "equivalent"},
        {"comparison": False, "panel": "not_equivalent"},
        {"comparison": False, "panel": "both_reasonable"},
        {"comparison": True, "panel": "not_equivalent"},
        {"comparison": True, "panel": "split"},
    ]
    r = grader_error_rates(reviews)
    assert r["mismatches_reviewed"] == 3 and r["mismatches_panel_says_right"] == 1
    assert r["mismatches_panel_says_either"] == 1
    assert r["single_value_matches_panel_says_wrong"] == 1 and r["split_or_unjudged"] == 1
