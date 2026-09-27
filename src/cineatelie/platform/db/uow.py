"""The `UnitOfWork` — one per use case, the only place a transaction is opened (ADR-004:
"the use-case layer has exactly one `async with uow:` block").

This is also where ADR-001's tenant GUC is set, which makes it the single most
safety-critical piece of code in this backend: get this wrong and one pooled connection
can leak one tenant's context into the next request (implementation plan §5, risk #1).
"""

from __future__ import annotations

import uuid
from types import TracebackType

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SET_TENANT_CONTEXT = text(
    "SELECT set_config('app.current_tenant_id', :tenant_id, true), "
    "set_config('app.current_user_id', :user_id, true)"
)


class UnitOfWork:
    """`async with UnitOfWork(session_factory, tenant_id=..., user_id=...) as uow:` opens a
    session, begins a transaction, and sets the tenant/user GUCs as the first statement in
    it — before any use-case code runs. Commits on clean exit, rolls back on exception.

    `tenant_id` is `None` for a platform-level job (`job_queue.tenant_id IS NULL` — the
    keep-alive ping, a platform-scoped sweep); `user_id` is `None` where no human is acting
    (the same platform jobs, or a request authenticated but not yet resolved to a specific
    person). Either way the GUC is set explicitly to "unset" rather than left alone, so this
    transaction never depends on whatever a previous transaction on this pooled connection
    happened to leave behind — even though `SET LOCAL` semantics (via `set_config`'s third
    argument) already guarantee that on their own. Belt and braces, deliberately.

    `set_config()`, not a literal `SET LOCAL ... = :value`: Postgres's `SET` command does not
    accept a bound parameter at all (the same restriction `ALTER ROLE ... PASSWORD` has,
    confirmed the hard way elsewhere in this project) — `set_config()` is an ordinary
    function call and binds normally. The empty string, not SQL `NULL`, is how "unset" is
    represented: `cineatelie.current_tenant_id()`'s own definition is
    `NULLIF(current_setting(...), '')::uuid`, so passing `''` is what that function already
    expects to mean "no tenant," not a guess.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        tenant_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._tenant_id = tenant_id
        self._user_id = user_id
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> UnitOfWork:
        self.session = self._session_factory()
        await self.session.execute(
            _SET_TENANT_CONTEXT,
            {
                "tenant_id": str(self._tenant_id) if self._tenant_id is not None else "",
                "user_id": str(self._user_id) if self._user_id is not None else "",
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self.session is not None, "UnitOfWork.__aexit__ called without __aenter__"
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        finally:
            await self.session.close()
            self.session = None
