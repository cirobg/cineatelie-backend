"""The application's one async database engine (ADR-003, ADR-004).

Connects as `app_user` (`DATABASE_URL`) — never `app_migrator`, never a raw connection
outside this module. `platform/db` is the *only* sanctioned way to reach the database;
a use case that opens its own connection bypasses every guardrail here (the timeouts, the
pool ceiling) and, per ADR-001, RLS itself if it somehow connects as anything other than
`app_user`.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from cineatelie.core.config import Settings

_engine: AsyncEngine | None = None


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the engine. Called once at process start; see `get_engine()` for the
    process-wide singleton every use case actually depends on."""
    return create_async_engine(
        settings.database_url,
        poolclass=AsyncAdaptedQueuePool,
        pool_size=settings.db_pool_max_size,
        max_overflow=0,  # the ceiling is the ceiling — ADR-003's guardrail against one
        # instance quietly opening more connections than the pool config admits to.
        pool_timeout=settings.db_pool_acquire_timeout_s,
        # Connection-level defaults survive a pool checkout/checkin cycle, unlike a plain
        # SET issued per-request — belt and braces alongside the role-level ALTER ROLE
        # defaults the baseline already sets (db/baseline/00001_baseline.sql, section 16).
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.db_statement_timeout_ms),
                "lock_timeout": str(settings.db_lock_timeout_ms),
            }
        },
    )


def get_engine(settings: Settings) -> AsyncEngine:
    """Process-wide singleton. `create_engine()` is exposed separately for tests that
    need a fresh engine (e.g. against a different database per test session)."""
    global _engine
    if _engine is None:
        _engine = create_engine(settings)
    return _engine


async def dispose_engine() -> None:
    """Close every pooled connection. Call once, on process shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
