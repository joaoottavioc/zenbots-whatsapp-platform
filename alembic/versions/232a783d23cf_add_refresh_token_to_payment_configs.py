"""add refresh_token to payment_configs

Revision ID: 232a783d23cf
Revises: bc818893059c
Create Date: 2026-02-25 15:12:50.452336

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '232a783d23cf'
down_revision: Union[str, Sequence[str], None] = 'bc818893059c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('payment_configs', sa.Column('refresh_token', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('payment_configs', sa.Column('token_expires_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('payment_configs', 'token_expires_at')
    op.drop_column('payment_configs', 'refresh_token')
