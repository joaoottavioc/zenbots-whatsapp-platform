"""add allows_bot_usage to plan

Revision ID: i0d1e2f3g4h5
Revises: h9c0d1e2f3g4
Create Date: 2026-03-07

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "i0d1e2f3g4h5"
down_revision = "h9c0d1e2f3g4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "plan",
        sa.Column(
            "allows_bot_usage",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("plan", "allows_bot_usage")
