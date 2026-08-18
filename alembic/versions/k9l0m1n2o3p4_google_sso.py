"""add google_sub column to user, make hashed_password nullable

Revision ID: k9l0m1n2o3p4
Revises: 9057c98f2e9e
Create Date: 2026-08-18

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "k9l0m1n2o3p4"
down_revision: Union[str, Sequence[str], None] = "9057c98f2e9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("user", "hashed_password", existing_type=sa.String(), nullable=True)
    op.add_column("user", sa.Column("google_sub", sa.String(), nullable=True))
    op.create_index(op.f("ix_user_google_sub"), "user", ["google_sub"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_user_google_sub"), table_name="user")
    op.drop_column("user", "google_sub")
    op.alter_column(
        "user", "hashed_password", existing_type=sa.String(), nullable=False
    )
