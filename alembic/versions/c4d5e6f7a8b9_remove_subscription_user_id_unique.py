"""remove subscription user_id unique constraint

Revision ID: c4d5e6f7a8b9
Revises: b7f8e9a1c2d3
Create Date: 2026-03-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b7f8e9a1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _find_unique_constraint_name(connection, table, column):
    """Find the actual constraint name for a unique constraint on the given column."""
    inspector = sa.inspect(connection)
    for constraint in inspector.get_unique_constraints(table):
        if constraint['column_names'] == [column]:
            return constraint['name']
    return None


def upgrade() -> None:
    conn = op.get_bind()
    constraint_name = _find_unique_constraint_name(conn, 'subscription', 'user_id')
    if constraint_name:
        op.drop_constraint(constraint_name, 'subscription', type_='unique')


def downgrade() -> None:
    op.create_unique_constraint(
        'uq_subscription_user_id', 'subscription', ['user_id']
    )
