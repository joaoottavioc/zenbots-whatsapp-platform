"""Phase 1 features: bot ETA/owner phone/cancel window + contact memory

Revision ID: l3g4h5i6j7k8
Revises: k2f3g4h5i6j7
Create Date: 2026-03-18
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "l3g4h5i6j7k8"
down_revision = "k2f3g4h5i6j7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # F-07: ETA fields on Bot
    op.add_column(
        "bot",
        sa.Column("default_delivery_time_minutes", sa.Integer(), nullable=True),
    )
    op.add_column(
        "bot",
        sa.Column("default_pickup_time_minutes", sa.Integer(), nullable=True),
    )

    # F-09: Owner notification phone on Bot
    op.add_column(
        "bot",
        sa.Column("owner_notification_phone", sa.String(length=20), nullable=True),
    )

    # F-17: Cancellation window on Bot
    op.add_column(
        "bot",
        sa.Column(
            "cancellation_window_minutes",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
    )

    # F-01: Customer memory on Contact
    op.add_column(
        "contact",
        sa.Column("default_address_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "contact",
        sa.Column("last_order_date", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contact", "last_order_date")
    op.drop_column("contact", "default_address_json")
    op.drop_column("bot", "cancellation_window_minutes")
    op.drop_column("bot", "owner_notification_phone")
    op.drop_column("bot", "default_pickup_time_minutes")
    op.drop_column("bot", "default_delivery_time_minutes")
