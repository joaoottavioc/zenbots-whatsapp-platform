"""Add AWAITING_CONTACT_PHONE to the cartstate enum

Revision ID: e5f6a7b8c9d0
Revises: b3c4d5e6f7a8
Create Date: 2026-05-20

Phase 2.4 (commit ad2ebef) added CartState.AWAITING_CONTACT_PHONE to
the Python enum but the matching ALTER TYPE was missing. The web
checkout flow worked up through customer-name capture, then crashed
when _handle_customer_name tried to UPDATE shoppingcart.state to a
value Postgres didn't know about:

  invalid input value for enum cartstate: "AWAITING_CONTACT_PHONE"

ALTER TYPE ... ADD VALUE is transactional in Postgres 12+, so this
runs inside the normal alembic transaction. Position the new value
BEFORE AWAITING_PAYMENT_METHOD to match the Python enum's declared
order (purely cosmetic — Postgres doesn't enforce ordering at the
type level, but enum_range() outputs in declaration order).

Downgrade is destructive (DROP TYPE + recreate without the value) and
would fail if any row still uses AWAITING_CONTACT_PHONE. Provided
defensively but not safe to run after web carts have been processed.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE cartstate ADD VALUE IF NOT EXISTS "
        "'AWAITING_CONTACT_PHONE' BEFORE 'AWAITING_PAYMENT_METHOD'"
    )


def downgrade() -> None:
    # PostgreSQL doesn't support ALTER TYPE ... DROP VALUE. Downgrade
    # requires recreating the enum without the value, which only works
    # if no row currently uses it. Left as a stub so alembic downgrade
    # doesn't error out; clean removal needs a manual SQL session.
    pass
