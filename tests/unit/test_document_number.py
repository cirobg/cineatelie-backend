"""ADR-007's own worked examples, verbatim."""

import pytest

from cineatelie.shared_kernel.document_number import parse, render


def test_render_pads_to_the_configured_width() -> None:
    assert render("ORC", 4, 25) == "ORC-0025"


def test_render_widens_on_overflow_never_truncates() -> None:
    assert render("OS", 4, 1035) == "OS-1035"


def test_parse_full_form() -> None:
    assert parse("ORC-0025") == ("ORC", 25)


def test_parse_is_forgiving_of_case_and_separator() -> None:
    assert parse("orc 25") == ("ORC", 25)


def test_parse_bare_number_has_no_prefix() -> None:
    assert parse("25") == (None, 25)


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse("not a document number")
