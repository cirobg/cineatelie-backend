"""ADR-008; database spec BR-FIN-02. Every monetary defect in this product will trace back
to `Money` or `split_evenly` (implementation plan §5, highest-risk item #3) — this is why
they are tested before anything else exists to depend on them.
"""

from decimal import Decimal

import pytest

from cineatelie.shared_kernel.money import Money, split_evenly


class TestConstruction:
    def test_refuses_float(self) -> None:
        with pytest.raises(TypeError):
            Money(10.5)  # type: ignore[arg-type]

    def test_round_refuses_float(self) -> None:
        with pytest.raises(TypeError):
            Money.round(10.5)  # type: ignore[arg-type]

    def test_refuses_more_than_two_decimal_places(self) -> None:
        with pytest.raises(ValueError):
            Money(Decimal("10.505"))

    def test_zero(self) -> None:
        assert Money.zero().to_wire() == "0.00"

    def test_quantizes_a_whole_number(self) -> None:
        assert Money(Decimal("10")).to_wire() == "10.00"


class TestWireFormat:
    def test_from_wire_round_trips(self) -> None:
        assert Money.from_wire("1026.80").to_wire() == "1026.80"

    def test_from_wire_rejects_garbage(self) -> None:
        with pytest.raises(ValueError):
            Money.from_wire("not a number")


class TestRounding:
    def test_round_half_up_not_bankers_rounding(self) -> None:
        # Python's Decimal default is ROUND_HALF_EVEN; ADR-008 explicitly overrides it.
        # 0.125 at 2 places is the classic case where the two roundings disagree.
        assert Money.round(Decimal("0.125")).to_wire() == "0.13"
        assert Money.round(Decimal("2.675")).to_wire() == "2.68"


class TestArithmetic:
    def test_add(self) -> None:
        assert Money.from_wire("10.00") + Money.from_wire("5.50") == Money.from_wire("15.50")

    def test_subtract(self) -> None:
        assert Money.from_wire("10.00") - Money.from_wire("5.50") == Money.from_wire("4.50")

    def test_ordering(self) -> None:
        assert Money.from_wire("5.00") < Money.from_wire("10.00")
        assert Money.from_wire("10.00") > Money.from_wire("5.00")

    def test_equality_and_hash(self) -> None:
        a = Money.from_wire("10.00")
        b = Money.from_wire("10.00")
        assert a == b
        assert hash(a) == hash(b)


class TestSplitEvenly:
    """BR-FIN-02 — the verification anchor from the database spec, verbatim."""

    def test_one_thousand_in_three_installments(self) -> None:
        result = split_evenly(Money.from_wire("1000.00"), 3)
        assert [m.to_wire() for m in result] == ["333.33", "333.33", "333.34"]

    def test_installments_always_sum_to_the_total(self) -> None:
        total = Money.from_wire("1283.50")
        for parts in range(1, 13):
            installments = split_evenly(total, parts)
            summed = sum(installments, Money.zero())
            assert summed == total, f"parts={parts} summed to {summed}, expected {total}"

    def test_evenly_divisible_total(self) -> None:
        result = split_evenly(Money.from_wire("300.00"), 3)
        assert [m.to_wire() for m in result] == ["100.00", "100.00", "100.00"]

    def test_single_installment_returns_the_whole_total(self) -> None:
        total = Money.from_wire("42.42")
        assert split_evenly(total, 1) == [total]

    def test_rejects_zero_or_negative_parts(self) -> None:
        with pytest.raises(ValueError):
            split_evenly(Money.from_wire("10.00"), 0)
