"""Pure slug derivation (ADR-004: `domain/` imports nothing from I/O). Uniquifying a
candidate against existing rows is an `application/` concern (`provisioning.py`) — it needs
the database, this does not.

Must stay compatible with `tenants_slug_format CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$')`
(db/baseline/00001_baseline.sql): 3-50 chars, lowercase ascii/digits/hyphens, never starting
or ending with a hyphen.
"""

from __future__ import annotations

import re
import unicodedata

_MAX_LENGTH = 50
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9-]+")
_REPEATED_HYPHENS = re.compile(r"-{2,}")


def slugify(text: str) -> str:
    """`"Ateliê da Maria!"` -> `"atelie-da-maria"`. Falls back to `"atelie"` if nothing
    alphanumeric survives (e.g. an emoji-only name), since the CHECK constraint requires at
    least 3 characters and `application/`'s uniquifying suffix still needs a base to append
    to."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower().strip()
    hyphenated = _NON_SLUG_CHARS.sub("-", lowered)
    collapsed = _REPEATED_HYPHENS.sub("-", hyphenated).strip("-")
    truncated = collapsed[:_MAX_LENGTH].strip("-")
    return truncated if len(truncated) >= 3 else "atelie"


def with_suffix(base: str, attempt: int) -> str:
    """`attempt` 0 returns `base` unchanged; 1+ appends `-2`, `-3`, ... (attempt 1 -> "-2",
    matching "the second one taken" rather than a confusing "-1" on the very first retry).
    Truncates `base` first so the result still respects the 50-char ceiling."""
    if attempt == 0:
        return base
    suffix = f"-{attempt + 1}"
    return f"{base[: _MAX_LENGTH - len(suffix)]}{suffix}"
