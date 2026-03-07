"""make joaoottavioc@gmail.com admin

Revision ID: h9c0d1e2f3g4
Revises: g8b9c0d1e2f3
Create Date: 2026-03-07

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "h9c0d1e2f3g4"
down_revision = "g8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE \"user\" SET is_admin = true WHERE email = 'joaoottavioc@gmail.com'"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE \"user\" SET is_admin = false WHERE email = 'joaoottavioc@gmail.com'"
        )
    )
