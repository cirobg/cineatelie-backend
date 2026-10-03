"""Make the four views run with the caller's rights, so RLS applies to them.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03

A view runs with its owner's privileges unless it says otherwise. Two things follow:

1. On Supabase dev, the views were handed to `app_migrator` (the 2026-10-03 ownership handover).
   `subscriptions` has forced RLS with a policy for `app_user` only, so the owner saw no rows and
   `v_active_subscription` returned nothing, which made every business route answer 402.
2. In general, an owner that bypasses RLS lets a view return every tenant's rows to `app_user`,
   which is a cross-tenant leak as soon as a query forgets its filter.

`security_invoker = true` (PostgreSQL 15+) runs the view as the caller. RLS then applies with the
tenant GUC the caller set, which is the isolation model the baseline intends. The owner stays
`app_migrator`, so no admin change is needed. Forward-only: downgrading would reopen the leak.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_VIEWS = (
    "v_active_subscription",
    "v_material_availability",
    "v_rtw_availability",
    "v_tenant_storage_usage",
)


def upgrade() -> None:
    for view in _VIEWS:
        op.get_bind().exec_driver_sql(f"ALTER VIEW cineatelie.{view} SET (security_invoker = true)")


def downgrade() -> None:
    raise RuntimeError(
        "Forward-only: removing security_invoker would let these views bypass RLS again "
        "(see this revision's docstring)."
    )
