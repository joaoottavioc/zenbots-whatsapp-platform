"""usage_events.bot_id nullable + ON DELETE SET NULL

Revision ID: p1q2r3s4t5u6
Revises: 8d88b7b6c633
Create Date: 2026-04-07 21:10:00.000000

Makes usage_events.bot_id nullable so events without a bot context (Cadastro
Mágico extraction, alias regeneration, ad-hoc CLI tools) can still be
recorded as "system / untracked" instead of being silently dropped.

Also changes the foreign key to ON DELETE SET NULL so deleting a bot (e.g.
the QA test cleanup that creates and tears down dozens of test bots per run)
preserves the cost history — the events become orphaned but readable.

Both ALTERs are non-blocking on PostgreSQL. Reversible.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "p1q2r3s4t5u6"
down_revision: Union[str, Sequence[str], None] = "8d88b7b6c633"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Drop the existing NO ACTION foreign key
    op.drop_constraint("usage_events_bot_id_fkey", "usage_events", type_="foreignkey")

    # 2. Make bot_id nullable
    op.alter_column("usage_events", "bot_id", nullable=True)

    # 3. Recreate the foreign key with ON DELETE SET NULL
    op.create_foreign_key(
        "usage_events_bot_id_fkey",
        "usage_events",
        "bot",
        ["bot_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # 1. Drop the SET NULL foreign key
    op.drop_constraint("usage_events_bot_id_fkey", "usage_events", type_="foreignkey")

    # 2. Backfill any NULL bot_ids before re-applying NOT NULL.
    # We can't safely guess a bot_id, so just delete orphaned rows.
    op.execute("DELETE FROM usage_events WHERE bot_id IS NULL")

    # 3. Restore NOT NULL
    op.alter_column("usage_events", "bot_id", nullable=False)

    # 4. Recreate the original NO ACTION foreign key
    op.create_foreign_key(
        "usage_events_bot_id_fkey",
        "usage_events",
        "bot",
        ["bot_id"],
        ["id"],
    )
