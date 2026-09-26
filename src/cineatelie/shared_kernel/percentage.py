"""`Percentage` — the `NUMERIC(7,4)` counterpart to `Money` (ADR-008).

Same discipline as `Money`: backed by `Decimal`, refuses `float`, and rounds only at an
explicit boundary (`Percentage.round()`), never implicitly. Four decimal places, so a
6.12 % card fee or a 14.20 % margin round-trips exactly.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import total_ordering

_FOUR_PLACES = Decimal("0.0001")
_ONE_HUNDRED = Decimal("100")


@total_ordering
class Percentage:
    """A percentage value, e.g. `35.0000` meaning 35 %. Always at most four decimal places."""

    __slots__ = ("_value",)

    def __init__(self, value: Decimal) -> None:
        if not isinstance(value, Decimal):
            raise TypeError(
                "Percentage requires a Decimal (ADR-008) — use Percentage.from_wire() for "
                "a wire string or Percentage.round() for a raw division result."
            )
        if value.as_tuple().exponent < -4:
            raise ValueError(f"Percentage must not carry more than 4 decimal places, got {value}")
        self._value = value.quantize(_FOUR_PLACES)

    @classmethod
    def zero(cls) -> Percentage:
        return cls(Decimal("0"))

    @classmethod
    def from_wire(cls, value: str) -> Percentage:
        try:
            return cls(Decimal(value))
        except InvalidOperation as exc:
            raise ValueError(f"not a valid decimal string: {value!r}") from exc

    @classmethod
    def round(cls, raw: Decimal) -> Percentage:
        if not isinstance(raw, Decimal):
            raise TypeError("Percentage.round() requires a Decimal, refuses float (ADR-008)")
        return cls(raw.quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP))

    def to_wire(self) -> str:
        return f"{self._value:.4f}"

    @property
    def value(self) -> Decimal:
        """The percentage itself, e.g. `Decimal("35.0000")` for 35 %."""
        return self._value

    def as_fraction(self) -> Decimal:
        """`35.0000` -> `Decimal("0.35")`, for multiplying directly into an amount."""
        return self._value / _ONE_HUNDRED

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Percentage) and self._value == other._value

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Percentage):
            return NotImplemented
        return self._value < other._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return f"Percentage({self.to_wire()!r})"

    def __str__(self) -> str:
        return self.to_wire()
