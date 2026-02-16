"""add canceled lowercase

Revision ID: 28efae713454
Revises: 3b170a01b5d2
Create Date: 2026-02-05 17:25:19.974734

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '28efae713454'
down_revision: Union[str, Sequence[str], None] = '62d505720a99'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
