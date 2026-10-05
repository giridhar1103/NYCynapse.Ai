import datetime as dt
from decimal import Decimal

from nycynapse.evals.cases import Compare
from nycynapse.evals.compare import compare


def c(**kw):
    return Compare(**kw)


def test_single_number_with_extra_label_column():
    v = compare(["n"], [(1234,)], ["label", "count"], [("trips", 1234)], c())
    assert v.match


def test_numbers_within_tolerance():
    assert compare(["x"], [(10.0,)], ["y"], [(10.004,)], c(tolerance=0.001)).match
    assert not compare(["x"], [(10.0,)], ["y"], [(10.2,)], c(tolerance=0.001)).match
    assert compare(["x"], [(Decimal("5.5"),)], ["y"], [(5.5,)], c()).match


def test_unordered_rows_and_renamed_columns():
    gold = [("Manhattan", 10), ("Bronx", 5)]
    cand = [(5, "BRONX"), (10, "manhattan")]
    assert compare(["borough", "n"], gold, ["n", "b"], cand, c()).match


def test_pairs_must_line_up_not_just_columns():
    gold = [("a", 1), ("b", 2)]
    cand = [("a", 2), ("b", 1)]
    assert not compare(["k", "v"], gold, ["k", "v"], cand, c()).match


def test_order_matters_for_rankings():
    gold = [("x", 3), ("y", 2)]
    assert compare(["k", "v"], gold, ["k", "v"], gold, c(order_matters=True)).match
    assert not compare(
        ["k", "v"], gold, ["k", "v"], list(reversed(gold)), c(order_matters=True)
    ).match


def test_top_n_compares_only_first_rows():
    gold = [("x", 3), ("y", 2)]
    cand = [("x", 3), ("y", 2), ("z", 1)]
    assert compare(["k", "v"], gold, ["k", "v"], cand, c(order_matters=True, rows=2)).match


def test_subset_only_needs_the_first_column():
    gold = [("Brooklyn", 101.2), ("Queens", 99.0)]
    cand = [("Queens",), ("Brooklyn",)]
    assert compare(["b", "t"], gold, ["b"], cand, c(columns="subset")).match


def test_dates_and_timestamps():
    d = dt.date(2025, 6, 1)
    ts = dt.datetime(2025, 6, 1, 4, tzinfo=dt.UTC)
    assert compare(["m", "t"], [(d, ts)], ["m", "t"], [(d, ts)], c()).match


def test_row_count_mismatch_is_reported():
    v = compare(["n"], [(1,), (2,)], ["n"], [(1,)], c())
    assert not v.match and "rows" in v.reason


def test_midnight_timestamp_equals_date():
    v = compare(["m"], [(dt.datetime(2024, 1, 1),)], ["m"], [(dt.date(2024, 1, 1),)], c())
    assert v.match


def test_share_as_percentage():
    assert compare(["s"], [(0.925,)], ["pct"], [(92.5,)], c()).match
    assert not compare(["s"], [(0.925,)], ["pct"], [(85.0,)], c()).match


def test_share_as_fraction_or_percent_either_way():
    spec = Compare(tolerance=0.005)
    assert compare(["share"], [(17.3,)], ["s"], [(0.173,)], spec).match
    assert compare(["share"], [(0.173,)], ["s"], [(17.3,)], spec).match
    assert not compare(["share"], [(0.173,)], ["s"], [(0.25,)], spec).match


def test_a_group_with_zero_equals_a_missing_group():
    spec = Compare()
    gold = [("Bronx", 10), ("Brooklyn", 20)]
    cand = [("Bronx", 10), ("Brooklyn", 20), ("Staten Island", 0)]
    assert compare(["b", "n"], gold, ["b", "n"], cand, spec).match
    assert not compare(["b", "n"], gold, ["b", "n"], [*cand[:2], ("Queens", 5)], spec).match
