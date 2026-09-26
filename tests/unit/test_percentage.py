from decimal import Decimal

import pytest

from cineatelie.shared_kernel.percentage import Percentage


def test_refuses_float() -> None:
    with pytest.raises(TypeError):
        Percentage(35.0)  # type: ignore[arg-type]


def test_from_wire_round_trips() -> None:
    assert Percentage.from_wire("6.12").to_wire() == "6.1200"


def test_card_fee_table_values_are_exact() -> None:
    # ADR-013's card fee table: 6.12 %, 14.20 % must not drift after a round trip.
    for value in ("3.15", "4.79", "6.12", "7.42", "8.80", "12.50", "14.20"):
        assert Percentage.from_wire(value).to_wire() == f"{Decimal(value):.4f}"


def test_as_fraction() -> None:
    assert Percentage.from_wire("35.0000").as_fraction() == Decimal("0.35")


def test_round_uses_half_up() -> None:
    assert Percentage.round(Decimal("35.00005")).to_wire() == "35.0001"


def test_refuses_more_than_four_decimal_places() -> None:
    with pytest.raises(ValueError):
        Percentage(Decimal("35.00001"))


def test_ordering() -> None:
    assert Percentage.from_wire("5") < Percentage.from_wire("10")
