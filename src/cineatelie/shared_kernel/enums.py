"""Enums shared across modules, mirroring a database `CHECK` constraint value-for-value
(ADR-008: "mirroring every set as a Python `StrEnum` ... with a CI test asserting the two
stay in sync").

Only genuinely cross-module, platform-wide sets belong here. An enum used by one module
(quote status, order stage, finance entry status, ...) is defined in that module's own
`domain/` when the module is built — putting it here first would just move the drift risk
from "database vs. Python" to "database vs. shared_kernel vs. the module that actually
owns the concept".
"""

from __future__ import annotations

from enum import StrEnum


class DocumentType(StrEnum):
    """`document_counters.document_type` (ADR-007). Platform-wide because numbering is a
    cross-cutting concern, not owned by any single business module."""

    QUOTE = "quote"
    SERVICE_ORDER = "service_order"
    RECEIPT = "receipt"
    CONTRACT = "contract"
