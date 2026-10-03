"""Add fn_resolve_user_identity, which the baseline defines but Supabase dev never received.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03

The baseline (`db/baseline/00001_baseline.sql`, section 15) defines `fn_resolve_user_identity`,
but the Supabase dev database had `0001` applied before that definition existed. Alembic
records a revision once, so editing the baseline cannot reach that database. This forward
migration adds the function to every database that is still missing it. The statements are
the same as the baseline's, and `CREATE OR REPLACE` makes the migration a no-op on a fresh
database, where the baseline already created the function.

Forward-only, per the baseline's own rule for every revision after `0001`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_RESOLVE_USER_IDENTITY_SQL = """
CREATE OR REPLACE FUNCTION cineatelie.fn_resolve_user_identity(
    p_provider text, p_provider_subject text)
RETURNS uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = cineatelie, extensions
AS $fn$
    SELECT user_id
      FROM cineatelie.user_identities
     WHERE provider = p_provider
       AND provider_subject = p_provider_subject;
$fn$;

REVOKE EXECUTE ON FUNCTION cineatelie.fn_resolve_user_identity(text, text) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION cineatelie.fn_resolve_user_identity(text, text) TO app_user;
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_RESOLVE_USER_IDENTITY_SQL)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(
        "DROP FUNCTION IF EXISTS cineatelie.fn_resolve_user_identity(text, text)"
    )
