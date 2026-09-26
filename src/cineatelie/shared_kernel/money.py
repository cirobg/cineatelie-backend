"""`Money` — the one type every monetary value in this codebase must pass through (ADR-008).

Backed by `Decimal`, quantized to `NUMERIC(14,2)`'s two places, and deliberately unable to
be constructed from a `float`: a single stray `float(...)` in a pricing path is the specific
mistake ADR-008 calls out as silently reintroducing drift, so the type itself refuses it
rather than relying on a lint rule alone.

Rounding happens at exactly one place — `Money.round()` — and nowhere implicitly. Every
other constructor (`from_wire`, `zero`, the plain constructor) requires a value that is
already at most two decimal places; `Money.round()` is the explicit boundary a caller uses
when it has a raw multiplication or division result, exactly as ADR-008's pricing chain
names each rounding point rather than leaving it to be inferred (that chain itself —
BR-QUO-03 — is implemented where quotes are, in M5; what's here is the primitive it is
built on).

This module also carries `split_evenly` (BR-FIN-02): instalment splitting is a generic
"divide a total into N parts, no rounding drift" operation, not something specific to the
finance module's tables, and M0 is asked to have it tested now.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import total_ordering

_TWO_PLACES = Decimal("0.01")


@total_ordering
class Money:
    """An exact amount of Brazilian reais (BRL), always at most two decimal places."""

    __slots__ = ("_amount",)

    def __init__(self, amount: Decimal) -> None:
        if not isinstance(amount, Decimal):
            raise TypeError(
                "Money requires a Decimal (ADR-008) — use Money.from_wire() for a wire "
                "string, Money.round() for a raw multiplication/division result, or "
                "Money.zero()."
            )
        if amount.as_tuple().exponent < -2:
            raise ValueError(f"Money must not carry more than 2 decimal places, got {amount}")
        self._amount = amount.quantize(_TWO_PLACES)

    # --- construction ------------------------------------------------------------------

    @classmethod
    def zero(cls) -> Money:
        return cls(Decimal("0"))

    @classmethod
    def from_wire(cls, value: str) -> Money:
        """Parse the API's decimal-string wire format (`"1026.80"`) — never a JSON number
        (ADR-008). Raises `ValueError` on anything that isn't a valid decimal string."""
        try:
            return cls(Decimal(value))
        except InvalidOperation as exc:
            raise ValueError(f"not a valid decimal string: {value!r}") from exc

    @classmethod
    def round(cls, raw: Decimal) -> Money:
        """The one sanctioned rounding boundary: `ROUND_HALF_UP`, Brazilian commercial
        practice, applied to a raw (unrounded) `Decimal` — never to an intermediate that
        has already been rounded once."""
        if not isinstance(raw, Decimal):
            raise TypeError("Money.round() requires a Decimal, refuses float (ADR-008)")
        return cls(raw.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))

    # --- wire format ---------------------------------------------------------------------

    def to_wire(self) -> str:
        return f"{self._amount:.2f}"

    @property
    def amount(self) -> Decimal:
        return self._amount

    # --- arithmetic (exact: operands are already at most 2dp, so +/- never need rounding) -

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self._amount + other._amount)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self._amount - other._amount)

    def __neg__(self) -> Money:
        return Money(-self._amount)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Money) and self._amount == other._amount

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._amount < other._amount

    def __hash__(self) -> int:
        return hash(self._amount)

    def __repr__(self) -> str:
        return f"Money({self.to_wire()!r})"

    def __str__(self) -> str:
        return self.to_wire()


def split_evenly(total: Money, parts: int) -> list[Money]:
    """BR-FIN-02 — split `total` into `parts` instalments with no rounding drift.

    Each of the first `parts - 1` instalments is `round(total / parts, 2)`; the last
    absorbs whatever the rounding left over, so the parts always sum exactly to `total`.
    R$ 1 000,00 in 3x becomes 333,33 / 333,33 / 333,34.
    """
    if parts < 1:
        raise ValueError("parts must be at least 1")
    if parts == 1:
        return [total]

    per = Money.round(total.amount / Decimal(parts))
    installments = [per] * (parts - 1)
    last = Money(total.amount - per.amount * Decimal(parts - 1))
    installments.append(last)
    return installments
