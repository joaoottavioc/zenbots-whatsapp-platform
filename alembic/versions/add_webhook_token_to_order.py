"""add webhook_token to order

Revision ID: a1b2c3d4e5f6
Revises: 232a783d23cf
Create Date: 2026-02-25
"""
import secrets
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '232a783d23cf'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add column as nullable first
    op.add_column('order', sa.Column('webhook_token', sa.String(), nullable=True))
    op.create_index(op.f('ix_order_webhook_token'), 'order', ['webhook_token'])

    # Backfill existing rows with unique tokens
    conn = op.get_bind()
    orders = conn.execute(sa.text("SELECT id FROM \"order\" WHERE webhook_token IS NULL"))
    for row in orders:
        token = secrets.token_urlsafe(32)
        conn.execute(
            sa.text("UPDATE \"order\" SET webhook_token = :token WHERE id = :id"),
            {"token": token, "id": row[0]},
        )

    # Make column non-nullable after backfill
    op.alter_column('order', 'webhook_token', nullable=False, server_default=sa.text("''"))


def downgrade() -> None:
    op.drop_index(op.f('ix_order_webhook_token'), table_name='order')
    op.drop_column('order', 'webhook_token')
