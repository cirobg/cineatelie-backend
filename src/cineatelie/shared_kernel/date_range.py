"""`DateRange` — a start/end pair of business dates, half-open on the end (ADR-004).

Used wherever a window needs to be reasoned about as a single value rather than two
loose columns: a subscription's `starts_on`/`ends_on`, a quote's validity window. `end`
being `None` means "open-ended" (e.g. an active subscription with no renewal boundary yet
computed).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date | None = None

    def __post_init__(self) -> None:
        if self.end is not None and self.end < self.start:
            raise ValueError(f"end ({self.end}) precedes start ({self.start})")

    def contains(self, day: date) -> bool:
        """Whether `day` falls within `[start, end)` — `end` itself is excluded, matching
        how a window's day-after-closing convention reads elsewhere in this codebase."""
        if day < self.start:
            return False
        return self.end is None or day < self.end

    def overlaps(self, other: DateRange) -> bool:
        if self.end is not None and other.start >= self.end:
            return False
        if other.end is not None and self.start >= other.end:
            return False
        return True
