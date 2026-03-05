"""add is_email_verified column to user table

Revision ID: g2h3i4j5k6l7
Revises: f7a8b9c0d1e2
Create Date: 2026-03-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'g2h3i4j5k6l7'
down_revision: Union[str, Sequence[str], None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('user', sa.Column('is_email_verified', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    # Grandfather all existing users as verified
    op.execute("UPDATE \"user\" SET is_email_verified = true")


def downgrade() -> None:
    op.drop_column('user', 'is_email_verified')
