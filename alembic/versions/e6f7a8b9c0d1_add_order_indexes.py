"""add indexes on order.bot_id, order.contact_id, order.status

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-03-04

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_order_bot_id", "order", ["bot_id"])
    op.create_index("ix_order_contact_id", "order", ["contact_id"])
    op.create_index("ix_order_status", "order", ["status"])


def downgrade() -> None:
    op.drop_index("ix_order_status", table_name="order")
    op.drop_index("ix_order_contact_id", table_name="order")
    op.drop_index("ix_order_bot_id", table_name="order")
