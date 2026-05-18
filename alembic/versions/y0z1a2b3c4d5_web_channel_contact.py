"""Web channel: add Contact.channel and Contact.contact_phone

Revision ID: y0z1a2b3c4d5
Revises: x9y0z1a2b3c4
Create Date: 2026-05-18

Phase 1.1 of plan/in_browser_bots.md — additive schema changes that
enable the web-widget channel without altering any WhatsApp behavior.

Schema changes:
  contact:
    + channel        (str(16), not null, default 'whatsapp')
    + contact_phone  (str(32), nullable — the human phone, separate
                      from `phone_number` which is the identity key)
    + index on (bot_id, channel) for the KDS "show web orders" filter

Backfill:
  - Every existing contact is a WhatsApp contact; channel := 'whatsapp'.
  - For backward compat we copy phone_number → contact_phone so the
    "human phone" column is populated for every WhatsApp row from day
    one. Web rows (created after Phase 2 ships) leave contact_phone
    NULL until the customer provides it at checkout.

`contact.phone_number` stays NOT NULL — the identity-key column. Web
contacts will have phone_number = "web:{session_id}" (A1b in the plan).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "y0z1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "x9y0z1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── contact.channel ────────────────────────────────────────────────
    op.add_column(
        "contact",
        sa.Column(
            "channel",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'whatsapp'"),
        ),
    )

    # ── contact.contact_phone ──────────────────────────────────────────
    op.add_column(
        "contact",
        sa.Column("contact_phone", sa.String(length=32), nullable=True),
    )

    # ── Backfill: copy phone_number → contact_phone for WhatsApp rows ──
    # All existing rows are WhatsApp contacts (web channel doesn't exist
    # yet), so a blanket copy is safe.
    op.execute("UPDATE contact SET contact_phone = phone_number")

    # ── Index for the KDS "show channel" filter ────────────────────────
    op.create_index(
        "ix_contact_bot_id_channel",
        "contact",
        ["bot_id", "channel"],
    )


def downgrade() -> None:
    op.drop_index("ix_contact_bot_id_channel", table_name="contact")
    op.drop_column("contact", "contact_phone")
    op.drop_column("contact", "channel")
