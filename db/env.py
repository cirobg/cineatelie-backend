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

`version_table_schema` is set explicitly to `cineatelie` (ADR-001's schema decision,
2026-09-27), so Alembic's own bookkeeping table is always `cineatelie.alembic_version`,
addressed by an explicit schema qualifier Alembic attaches itself — never resolved through
`search_path`. That distinction is load-bearing: the baseline migration's own SQL changes
the session's `search_path` while it runs (so its ~50 unqualified `CREATE TABLE` statements
land in `cineatelie`), and that change persists for the rest of the transaction — including
whatever Alembic does immediately after `upgrade()` returns. An implicitly-resolved
`alembic_version` found that out the hard way: created under one `search_path`, looked up
under another, in the same transaction — `psycopg.errors.UndefinedTable: relation
"alembic_version" does not exist` (confirmed by actually running this migration).

That still leaves one bootstrapping gap on a genuinely empty database: Alembic checks
`cineatelie.alembic_version` — creating it if absent — *before* calling any revision's
`upgrade()`, which is the only place `CREATE SCHEMA cineatelie` actually lives (the top of
the baseline). On the very first run, that check fails outright:
`psycopg.errors.InvalidSchemaName: schema "cineatelie" does not exist` (also confirmed by
running it). `run_migrations_online()` below creates the schema itself, before
`context.configure()`, purely to give Alembic's own bookkeeping somewhere to live — the
baseline's own `CREATE SCHEMA IF NOT EXISTS` still runs normally afterward and is not
redundant work removed, just an idempotent statement that now finds the schema already
there.
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


VERSION_TABLE_SCHEMA = "cineatelie"


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (`alembic upgrade head --sql`)."""
    context.configure(
        url=_migration_database_url(),
        target_metadata=target_metadata,
        version_table_schema=VERSION_TABLE_SCHEMA,
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
        # See module docstring: Alembic's own version-table bookkeeping needs this schema
        # to exist *before* context.configure() below, on a database where nothing has run
        # yet. Idempotent, and harmless once the baseline's own CREATE SCHEMA runs too.
        connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {VERSION_TABLE_SCHEMA}")
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=VERSION_TABLE_SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
