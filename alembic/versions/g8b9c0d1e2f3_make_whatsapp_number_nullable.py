"""make whatsapp_number nullable

Revision ID: g8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-03-06

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "g8b9c0d1e2f3"
down_revision = "g2h3i4j5k6l7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("bot", "whatsapp_number", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    # Set any NULLs to empty string before making NOT NULL again
    op.execute("UPDATE bot SET whatsapp_number = '' WHERE whatsapp_number IS NULL")
    op.alter_column(
        "bot", "whatsapp_number", existing_type=sa.String(), nullable=False
    )
