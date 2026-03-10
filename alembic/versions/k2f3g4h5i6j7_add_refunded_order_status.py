"""Add 'refunded' value to orderstatus enum

Revision ID: k2f3g4h5i6j7
Revises: j1e2f3g4h5i6
Create Date: 2026-03-09
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "k2f3g4h5i6j7"
down_revision = "j1e2f3g4h5i6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'refunded'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values.
    # A full enum rebuild would be required; safe to leave as no-op.
    pass
