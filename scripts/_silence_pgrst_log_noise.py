"""One-off: Supabase's documented workaround for the `pg_pgrst_no_exposed_schemas` log
noise (seen when the project's Data API/PostgREST is disabled, which it deliberately is
here — ADR-009, no provider SDK). Run once against the admin connection; safe to re-run
(CREATE SCHEMA IF NOT EXISTS). Not part of the application schema/migrations: this configures
Supabase's own `authenticator` role, which we don't own or manage via Alembic.

Reverse with:
    ALTER ROLE authenticator RESET pgrst.db_schemas;
    NOTIFY pgrst;
"""

import asyncio
import os

import asyncpg


async def main() -> None:
    conn = await asyncpg.connect(
        host=os.environ["DEV_POSTGRES_HOST"],
        port=int(os.environ["DEV_POSTGRES_PORT"]),
        database=os.environ["DEV_POSTGRES_DATABASE"],
        user=os.environ["DEV_POSTGRES_USER"],
        password=os.environ["DEV_POSTGRES_PASS"],
        ssl="require",
    )
    try:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS pgrst_no_exposed_schemas")
        await conn.execute(
            "ALTER ROLE authenticator SET pgrst.db_schemas = 'pgrst_no_exposed_schemas'"
        )
        await conn.execute("NOTIFY pgrst")
        print("Done: authenticator.pgrst.db_schemas -> pgrst_no_exposed_schemas; pgrst notified.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
