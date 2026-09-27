"""The uniform list-response envelope every list endpoint returns (ADR-014, backend §4)."""

from __future__ import annotations

import math
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100


class PageParams(BaseModel):
    """Validated query parameters shared by every list endpoint."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Page(BaseModel, Generic[T]):
    """`{ items, page, page_size, total, total_pages }` — one shape for every list."""

    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int

    @classmethod
    def of(cls, items: list[T], *, page: int, page_size: int, total: int) -> Page[T]:
        total_pages = math.ceil(total / page_size) if page_size and total else 0
        return cls(
            items=items, page=page, page_size=page_size, total=total, total_pages=total_pages
        )
