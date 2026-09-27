"""UUIDv7 primary keys, generated application-side (ADR-002).

This must produce bit-for-bit the same shape as the database's own generator,
`cineatelie.uuid_generate_v7()` (db/baseline/00001_baseline.sql, section 0), so that a key minted
by the API, one minted by a migration's `DEFAULT`, and one minted by a seed script are
indistinguishable and equally sortable. Layout (RFC 9562):

    byte 0-5    unix_ts_ms, big-endian, 48 bits
    byte 6      high nibble = version (0111) | low nibble = random
    byte 7      random
    byte 8      top 2 bits = variant (10) | remaining 6 bits = random
    byte 9-15   random
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """A time-ordered, non-enumerable UUID. See module docstring for the exact layout."""
    unix_ts_ms = int(time.time() * 1000)
    ts_bytes = unix_ts_ms.to_bytes(6, byteorder="big")
    rand = bytearray(os.urandom(10))

    value = bytearray(ts_bytes) + rand
    value[6] = (value[6] & 0x0F) | 0x70  # version 7
    value[8] = (value[8] & 0x3F) | 0x80  # IETF variant

    return uuid.UUID(bytes=bytes(value))
