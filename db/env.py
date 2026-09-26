"""Alembic environment.

Runs synchronously, over `psycopg` — deliberately a different driver from the app's own
async `asyncpg` engine (ADR-004 picks async SQLAlchemy for the *application*; Alembic is a
CI/deploy-time step outside the request path, per ADR-003's own boundary, and psycopg gives
the most predictable support for handing the server a large, hand-written, dollar-quoted
SQL script verbatim — see `db/versions/20260926_0001_baseline.py`). Connects as `app_migrator`
(`MIGRATION_DATABASE_URL`), never as the application role (database spec §8.1).

`target_metadata` is `None` on purpose: there are no SQLAlchemy models yet (M0 has no
business modules). Once a module defines ORM models, this becomes `Base.metadata`, which is
what makes `alembic check` in CI able to catch drift on the columns/indexes it *can* see —
the subset autogenerate is trusted for at all (database spec §8.3, rule 5). Everything
autogenerate cannot see (RLS, triggers, functions, views, grants, generated columns, partial
and exclusion indexes, `CHECK` constraints) stays hand-written `op.execute()` regardless.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None  # see module docstring


def _migration_database_url() -> str:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "MIGRATION_DATABASE_URL is not set. Alembic connects as app_migrator, never as "
            "the application role — see db/README.md."
        )
    if "+asyncpg" in url:
        raise RuntimeError(
            "MIGRATION_DATABASE_URL must use a synchronous driver (postgresql+psycopg://), "
            "not +asyncpg — Alembic itself runs synchronously; only the running app is "
            "async (ADR-004)."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=_migration_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _migration_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
