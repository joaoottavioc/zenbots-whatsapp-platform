"""Web widget enabled by default for all bots

Revision ID: 9057c98f2e9e
Revises: f6g7h8i9j0k1
Create Date: 2026-08-17

Follow-up to z1a2b3c4d5e6 (web widget config). The widget was
opt-in-only, gated behind a per-bot flag nobody had a reason to flip
except manually in the DB — in practice only one dev bot ever had it
enabled. Flips the default so every bot ships with the web widget on;
owners can still turn it off via PUT /bots/{id}. Production safety is
unchanged: an empty web_widget_allowed_origins list still rejects every
browser Origin in prod (see chat_routes._check_origin) — enabling the
flag alone does not open CORS.

Schema changes:
  bot:
    web_widget_enabled  server_default false -> true

Data backfill:
  bot: existing rows with web_widget_enabled = false are flipped to
  true, so pre-existing bots get the same default as new ones.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "9057c98f2e9e"
down_revision: Union[str, Sequence[str], None] = "f6g7h8i9j0k1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "bot",
        "web_widget_enabled",
        server_default=sa.text("true"),
    )
    op.execute(
        "UPDATE bot SET web_widget_enabled = true WHERE web_widget_enabled = false"
    )


def downgrade() -> None:
    op.alter_column(
        "bot",
        "web_widget_enabled",
        server_default=sa.text("false"),
    )
    # Data backfill is intentionally not reverted — the original
    # per-bot values weren't recorded anywhere to restore.
