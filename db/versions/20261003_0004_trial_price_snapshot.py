"""Backfill the price snapshot on trial subscriptions created before provisioning copied it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-03

Provisioning inserted trial subscriptions without `price_amount` and `currency_code`, so the
"Meu plano" screen showed "—" for the price. The snapshot rule is BR-SUB-01: copy the plan's price
at sale time. A trial's plan price is 0 (BR-SUB-04, BR-SUB-06). The provisioning code now copies
it. This revision is the intended backfill, and it only touches trial rows with a null snapshot.

Known limit (found 2026-10-03): on Supabase, `app_migrator` owns `subscriptions` and that table has
FORCE row-level security with a policy for `app_user` only. The migrator therefore sees no tenant
rows, and this UPDATE changes nothing there. Locally it works, because the migrator is a superuser.
The existing Supabase trial row was fixed separately by an admin statement the owner approves.
Future data backfills on tenant tables need the same treatment, so they are not run through
migrations on Supabase.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        UPDATE cineatelie.subscriptions s
           SET price_amount = p.price_amount,
               currency_code = p.currency_code
          FROM cineatelie.plans p
         WHERE p.code = s.plan_code
           AND s.plan_code = 'trial'
           AND s.price_amount IS NULL
        """
    )


def downgrade() -> None:
    # Data backfill: nothing to reverse. Clearing the snapshot would reintroduce the "—" bug.
    pass
