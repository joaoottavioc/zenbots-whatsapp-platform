"""UsageEvent.channel for per-channel cost attribution

Revision ID: a2b3c4d5e6f7
Revises: z1a2b3c4d5e6
Create Date: 2026-05-19

Phase 5.4 of plan/in_browser_bots.md. Adds a `channel` column to
usage_events so the cost dashboard can split per-bot spend by channel
(whatsapp vs web). Nullable so the existing rows backfill cleanly —
historical events predate the web channel and don't have a meaningful
value; the dashboard interprets NULL as "unknown / pre-Phase-5".

The channel value is set by the LLM call sites in app/monitoring.py
when they have it in scope (most do, via the request-scoped ContextVar).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "z1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_events",
        sa.Column("channel", sa.String(length=16), nullable=True),
    )
    # Composite index for the per-channel cost query that the dashboard
    # will run (GROUP BY bot_id, channel WHERE created_at > ...).
    op.create_index(
        "ix_usage_events_bot_channel",
        "usage_events",
        ["bot_id", "channel"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_bot_channel", table_name="usage_events")
    op.drop_column("usage_events", "channel")
