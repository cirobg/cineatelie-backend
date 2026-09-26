"""ADR-002 — must match `app.uuid_generate_v7()` bit-for-bit: version nibble `0111`,
variant bits `10`, 48-bit millisecond timestamp prefix, time-ordered.
"""

import time
import uuid

from cineatelie.core.ids import uuid7


def test_version_is_7() -> None:
    assert uuid7().version == 7


def test_variant_is_ietf() -> None:
    assert uuid7().variant == uuid.RFC_4122


def test_is_unique_across_many_calls() -> None:
    generated = {uuid7() for _ in range(1000)}
    assert len(generated) == 1000


def test_sorts_chronologically() -> None:
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()
    assert first.bytes < second.bytes


def test_timestamp_prefix_matches_generation_time() -> None:
    before_ms = int(time.time() * 1000)
    generated = uuid7()
    after_ms = int(time.time() * 1000)

    embedded_ms = int.from_bytes(generated.bytes[0:6], byteorder="big")
    assert before_ms <= embedded_ms <= after_ms
