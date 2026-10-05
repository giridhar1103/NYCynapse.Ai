from nycynapse.evals import stats


def case(i, ok, expected="answerable", predicted="answerable", group=None):
    return {
        "id": f"c{i}",
        "group": group,
        "expected": expected,
        "predicted": predicted,
        "classification_ok": expected == predicted,
        "result_match": ok if expected == "answerable" else None,
    }


def test_interval_contains_the_estimate_and_narrows_with_more_cases():
    small = [case(i, i % 4 != 0) for i in range(20)]
    large = [case(i, i % 4 != 0) for i in range(400)]
    a, b = stats.bootstrap(small, stats.accuracy), stats.bootstrap(large, stats.accuracy)
    assert a.value == b.value == 0.75
    assert a.low < 0.75 < a.high and b.low < 0.75 < b.high
    assert (b.high - b.low) < (a.high - a.low)


def test_paraphrase_groups_resample_together():
    # Ten groups of five identical paraphrases behave like ten cases, not fifty.
    grouped = [
        case(i, g % 2 == 0, group=f"g{g}") for g in range(10) for i in range(g * 5, g * 5 + 5)
    ]
    single = [case(i, i % 2 == 0) for i in range(50)]
    wide = stats.bootstrap(grouped, stats.accuracy)
    narrow = stats.bootstrap(single, stats.accuracy)
    assert (wide.high - wide.low) > (narrow.high - narrow.low)


def test_mcnemar():
    assert stats.mcnemar_exact(0, 0) == 1.0
    assert abs(stats.mcnemar_exact(1, 9) - 0.021484375) < 1e-9
    assert stats.mcnemar_exact(5, 5) == 1.0


def test_paired_difference():
    a = [case(i, i < 6) for i in range(10)]
    b = [case(i, i < 9) for i in range(10)]
    p = stats.paired(a, b)
    assert p["only_b_right"] == 3 and p["only_a_right"] == 0
    assert abs(p["difference"]["value"] - 0.3) < 1e-9


def test_two_axes_for_declining():
    rows = [
        case(1, True),
        case(2, False),
        case(3, None, expected="answerable", predicted="unsupported"),
        case(4, None, expected="unsupported", predicted="answerable"),
        case(5, None, expected="unsupported", predicted="unsupported"),
    ]
    assert stats.answered_unanswerable(rows) == 0.5
    assert stats.wrong_when_answered(rows) == 0.5
    assert abs(stats.declined_answerable(rows) - 1 / 3) < 1e-9


def test_repeats():
    run1 = [case(1, True), case(2, True), case(3, False)]
    run2 = [case(1, True), case(2, False), case(3, False)]
    r = stats.repeats([run1, run2])
    assert r["right_every_time"] == 1 / 3 and r["right_at_least_once"] == 2 / 3
    assert r["flipped"] == 1
