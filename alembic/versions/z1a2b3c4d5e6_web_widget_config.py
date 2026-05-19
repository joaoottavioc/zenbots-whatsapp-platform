"""Web widget config: Bot channel toggles + widget settings

Revision ID: z1a2b3c4d5e6
Revises: y0z1a2b3c4d5
Create Date: 2026-05-19

Phase 2.5 of plan/in_browser_bots.md — per-bot channel matrix.
Symmetric to Contact.channel from y0z1a2b3c4d5, but on the Bot side:
which channels does this bot accept messages on?

Schema changes:
  bot:
    + whatsapp_enabled            (bool, default true)
        Backward compatible: every existing bot was WhatsApp-only,
        so defaulting to true preserves current behavior.
    + web_widget_enabled          (bool, default false)
        Off by default — bots stay WhatsApp-only until the owner
        explicitly enables the widget from the dashboard.
    + web_widget_allowed_origins  (JSON list[str], default '[]')
        Per-bot CORS allowlist for the widget. Empty list = block all.
        Phase 4.1 in the plan, but landed here because the gate logic
        for the widget needs the column at ingress time.
    + web_widget_theme            (JSON dict, default '{}')
        UI customization (primary_color, position, welcome_message).
        Free-form to avoid migration churn on theme additions.
    + web_widget_offline_message  (str(500), nullable)
        Replaces the "store closed" text on the web channel — WhatsApp
        keeps its richer multi-line closing message (with menu image)
        because that's what restaurants are used to seeing in WA.

Per-bot channel matrix enforced by the application layer using
(whatsapp_enabled, web_widget_enabled). Not encoded as a CHECK
constraint because draft state (both false) is intentionally allowed
for staged onboarding.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "z1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "y0z1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Channel toggles ────────────────────────────────────────────────
    op.add_column(
        "bot",
        sa.Column(
            "whatsapp_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "bot",
        sa.Column(
            "web_widget_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── Widget config ──────────────────────────────────────────────────
    op.add_column(
        "bot",
        sa.Column(
            "web_widget_allowed_origins",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "bot",
        sa.Column(
            "web_widget_theme",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        "bot",
        sa.Column(
            "web_widget_offline_message",
            sa.String(length=500),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("bot", "web_widget_offline_message")
    op.drop_column("bot", "web_widget_theme")
    op.drop_column("bot", "web_widget_allowed_origins")
    op.drop_column("bot", "web_widget_enabled")
    op.drop_column("bot", "whatsapp_enabled")
