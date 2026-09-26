"""Calendar-date helpers with no business-module dependency.

`add_months_clamped` is BR-FIN-04: adding a month to 31 January must yield 28 or 29
February, never 3 March. Naive `date(y, m + i, d)` arithmetic (as the prototype does)
overflows past the end of a shorter month instead of clamping, which would silently move a
client's instalment into the wrong month — the exact failure this function exists to rule
out.
"""

from __future__ import annotations

import calendar
from datetime import date


def add_months_clamped(start: date, months: int) -> date:
    """Add `months` calendar months to `start`, clamping the day to the last valid day of
    the target month. `months` may be negative."""
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    last_day_of_target_month = calendar.monthrange(year, month)[1]
    day = min(start.day, last_day_of_target_month)
    return date(year, month, day)
