"""BR-FIN-04 — the verification anchor from the database spec, verbatim: adding a month to
31 January must yield 28 or 29 February, never 3 March.
"""

from datetime import date

from cineatelie.core.dates import add_months_clamped


def test_end_of_january_clamps_to_end_of_february_non_leap_year() -> None:
    assert add_months_clamped(date(2025, 1, 31), 1) == date(2025, 2, 28)


def test_end_of_january_clamps_to_end_of_february_leap_year() -> None:
    assert add_months_clamped(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_naive_date_arithmetic_would_have_overflowed_into_march() -> None:
    # The prototype's `new Date(y, m + i, d)` overflows here; this is exactly the bug
    # BR-FIN-04 exists to rule out.
    result = add_months_clamped(date(2025, 1, 31), 1)
    assert result.month == 2
    assert result != date(2025, 3, 3)


def test_ordinary_month_addition_is_unaffected() -> None:
    assert add_months_clamped(date(2025, 3, 15), 1) == date(2025, 4, 15)


def test_year_rollover() -> None:
    assert add_months_clamped(date(2025, 12, 15), 1) == date(2026, 1, 15)


def test_multiple_months_crossing_a_short_february() -> None:
    assert add_months_clamped(date(2025, 12, 31), 2) == date(2026, 2, 28)


def test_negative_months() -> None:
    assert add_months_clamped(date(2026, 1, 15), -1) == date(2025, 12, 15)


def test_negative_months_across_year_boundary_with_clamping() -> None:
    assert add_months_clamped(date(2025, 3, 31), -1) == date(2025, 2, 28)


def test_twelve_card_installments_stay_on_the_fifteenth() -> None:
    # A twelve-installment card payment (ADR-008's own example) must never drift off its
    # anchor day just because it crosses a February.
    start = date(2025, 1, 15)
    for i in range(12):
        result = add_months_clamped(start, i)
        assert result.day == 15
