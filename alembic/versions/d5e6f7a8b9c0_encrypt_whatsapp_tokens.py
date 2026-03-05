"""encrypt existing whatsapp_tokens at rest

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-03-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from app.encryption import encrypt_value, decrypt_value


# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, whatsapp_token FROM bot WHERE whatsapp_token IS NOT NULL AND whatsapp_token != ''")
    ).fetchall()

    for row in rows:
        bot_id, token = row
        encrypted = encrypt_value(token)
        conn.execute(
            sa.text("UPDATE bot SET whatsapp_token = :token WHERE id = :id"),
            {"token": encrypted, "id": bot_id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, whatsapp_token FROM bot WHERE whatsapp_token IS NOT NULL AND whatsapp_token != ''")
    ).fetchall()

    for row in rows:
        bot_id, token = row
        decrypted = decrypt_value(token)
        conn.execute(
            sa.text("UPDATE bot SET whatsapp_token = :token WHERE id = :id"),
            {"token": decrypted, "id": bot_id},
        )
