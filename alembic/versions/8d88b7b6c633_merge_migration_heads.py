"""merge migration heads

Revision ID: 8d88b7b6c633
Revises: m4h5i6j7k8l9, n5i6j7k8l9m0
Create Date: 2026-04-06 20:57:45.831508

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8d88b7b6c633'
down_revision: Union[str, Sequence[str], None] = ('m4h5i6j7k8l9', 'n5i6j7k8l9m0')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
