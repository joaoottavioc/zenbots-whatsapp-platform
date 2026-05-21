"""Add trace_id to conversation_history

Revision ID: f6g7h8i9j0k1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-21

P4 of plan/portfolio_pivot.md. The behind-the-scenes conversation
viewer needs to map each ConversationHistory row to the UsageEvent
rows that produced it. `trace_id` already exists on usage_events
(see model/migration history); adding the same column to
conversation_history closes the join and lets the trace endpoint
attach intent/tools/tokens/cost/latency per message without fuzzy
time-window matching.

Backfill behavior: existing rows get NULL. The trace endpoint
gracefully renders "no trace data" for those — they predate the
instrumentation, so there's nothing to attach. New writes set the
column from the current_trace_id ContextVar.

Index on (bot_id, trace_id) makes the per-trace lookup O(log n) — the
dashboard joins by trace_id, scoped to a single bot, and there can be
~10-20 trace_ids per conversation page.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f6g7h8i9j0k1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversationhistory",
        sa.Column("trace_id", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_conversationhistory_bot_trace",
        "conversationhistory",
        ["bot_id", "trace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversationhistory_bot_trace",
        table_name="conversationhistory",
    )
    op.drop_column("conversationhistory", "trace_id")
