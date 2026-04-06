"""Add restaurant_image_url to bot

Revision ID: m4h5i6j7k8l9
Revises: l3g4h5i6j7k8
Create Date: 2026-03-19
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "m4h5i6j7k8l9"
down_revision = "l3g4h5i6j7k8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bot", sa.Column("restaurant_image_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("bot", "restaurant_image_url")
