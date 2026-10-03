from nycynapse.pipeline.answer import unsupported_numbers


def test_rounded_numbers_are_supported():
    rows = [("Brooklyn", 12345.678, 0.4251)]
    assert unsupported_numbers("Brooklyn had 12,346 complaints, 42.5% of the total.", rows) == []


def test_thousands_and_millions_are_supported():
    rows = [(651246843,)]
    assert unsupported_numbers("About 651.2 million trips.", rows) == []


def test_invented_numbers_are_caught():
    rows = [("Queens", 100)]
    assert unsupported_numbers("Queens had 250 crashes.", rows) == ["250"]


def test_years_from_the_period_are_fine():
    assert unsupported_numbers("In June 2025 there were 5 alerts.", [(5,)], "June 2025") == []
