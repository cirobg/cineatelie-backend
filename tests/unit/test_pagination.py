import pytest
from pydantic import ValidationError

from cineatelie.core.pagination import MAX_PAGE_SIZE, Page, PageParams


def test_defaults() -> None:
    params = PageParams()
    assert params.page == 1
    assert params.page_size == 10
    assert params.offset == 0


def test_offset_calculation() -> None:
    assert PageParams(page=3, page_size=10).offset == 20


def test_rejects_page_below_one() -> None:
    with pytest.raises(ValidationError):
        PageParams(page=0)


def test_rejects_page_size_above_the_ceiling() -> None:
    with pytest.raises(ValidationError):
        PageParams(page_size=MAX_PAGE_SIZE + 1)


def test_envelope_shape() -> None:
    page: Page[int] = Page.of([1, 2, 3], page=1, page_size=10, total=25)
    assert page.model_dump() == {
        "items": [1, 2, 3],
        "page": 1,
        "page_size": 10,
        "total": 25,
        "total_pages": 3,
    }


def test_envelope_with_zero_results() -> None:
    page: Page[int] = Page.of([], page=1, page_size=10, total=0)
    assert page.total_pages == 0
