import pytest

from cineatelie.modules.identity.domain.slug import slugify, with_suffix

_SLUG_FORMAT = __import__("re").compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")


def test_lowercases_and_hyphenates() -> None:
    assert slugify("Ateliê da Maria") == "atelie-da-maria"


def test_strips_accents() -> None:
    assert slugify("Confecção São José") == "confeccao-sao-jose"


def test_collapses_repeated_separators() -> None:
    assert slugify("Ateliê   ---  Maria!!!") == "atelie-maria"


def test_trims_leading_and_trailing_hyphens() -> None:
    assert slugify("--Ateliê--") == "atelie"


def test_falls_back_when_nothing_alphanumeric_survives() -> None:
    assert slugify("👗👗👗") == "atelie"


def test_truncates_to_the_schema_ceiling() -> None:
    long_name = "Ateliê " + "a" * 60
    result = slugify(long_name)
    assert len(result) <= 50


@pytest.mark.parametrize(
    "name",
    ["Ateliê da Maria", "Confecção São José", "👗👗👗", "--Ateliê--", "Ateliê " + "a" * 60],
)
def test_every_slug_matches_the_database_check_constraint(name: str) -> None:
    assert _SLUG_FORMAT.match(slugify(name)), slugify(name)


def test_with_suffix_zero_is_unchanged() -> None:
    assert with_suffix("atelie-maria", 0) == "atelie-maria"


def test_with_suffix_starts_at_dash_two() -> None:
    assert with_suffix("atelie-maria", 1) == "atelie-maria-2"
    assert with_suffix("atelie-maria", 2) == "atelie-maria-3"


def test_with_suffix_keeps_the_result_within_the_length_ceiling() -> None:
    base = "a" * 50
    result = with_suffix(base, 1)
    assert len(result) <= 50
    assert result.endswith("-2")
