"""Rendering and parsing of human-facing document codes (ADR-007).

The database stores only the integer `number`; `prefix` and `padding` live in
`document_counters`, per tenant. This module is the one place that turns a stored number
into `"ORC-0025"` and back — both the API response (`number` + `display_number`) and search
(which accepts `"ORC-0025"`, `"orc 25"`, or bare `"25"`) go through it.
"""

from __future__ import annotations

import re

_PARSE_PATTERN = re.compile(r"^\s*([A-Za-z]+)?[\s\-]*(\d+)\s*$")


def render(prefix: str, padding: int, number: int) -> str:
    """`render("ORC", 4, 25)` -> `"ORC-0025"`. A number wider than `padding` digits widens
    the output rather than being truncated: `render("OS", 4, 1035)` -> `"OS-1035"`."""
    return f"{prefix}-{number:0{padding}d}"


def parse(text: str) -> tuple[str | None, int]:
    """`"ORC-0025"`, `"orc 25"` and `"25"` all parse to a prefix (or `None`, for a bare
    number) and an `int`. The caller already knows which document type it is searching for
    when the prefix is absent — this function only extracts what the text actually says.

    Raises `ValueError` if `text` is not a recognisable document number.
    """
    match = _PARSE_PATTERN.match(text)
    if not match:
        raise ValueError(f"not a document number: {text!r}")
    prefix_raw, digits = match.groups()
    prefix = prefix_raw.upper() if prefix_raw else None
    return prefix, int(digits)
