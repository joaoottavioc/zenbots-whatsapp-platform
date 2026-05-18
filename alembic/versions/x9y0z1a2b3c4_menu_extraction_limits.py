"""Pricing: Cadastro Mágico per-tier extraction limits

Revision ID: x9y0z1a2b3c4
Revises: w8x9y0z1a2b3
Create Date: 2026-04-24

Adds per-tier limits for the Cadastro Mágico (menu extraction) feature:

  - Free: 3 extractions/mês, images + text only, max 3 images per call.
  - Pro / Founder: 5 extractions/mês, any file type, unlimited images.
  - Enterprise: unlimited extractions, unlimited images.

Schema changes:
  plan:
    + max_menu_extractions_per_month  (int, null = unlimited)
    + allows_pdf_extraction           (bool, default false)
    + max_images_per_extraction       (int, null = unlimited)
  bot_monthly_usage:
    + menu_extractions                (int, default 0 — month-keyed counter)

The counter is incremented at upload reception (not worker success) so
failed extractions still count against quota. Admin refund path is
out-of-scope for this migration.

The 10-image global cap in bot_routes.py (MAX_IMAGES_PER_REQUEST) is
preserved as a universal technical guard — vision TPM limits mean that
ceiling is a pipeline property, not a pricing lever. Free is tighter
(3) because that's a pricing decision; Pro/Founder still top out at
the technical ceiling. The `plan.max_images_per_extraction` column
only overrides downward, never past the global cap.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "x9y0z1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "w8x9y0z1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── plan: Cadastro Mágico limits ──────────────────────────────────
    op.add_column(
        "plan",
        sa.Column("max_menu_extractions_per_month", sa.Integer(), nullable=True),
    )
    op.add_column(
        "plan",
        sa.Column(
            "allows_pdf_extraction",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "plan",
        sa.Column("max_images_per_extraction", sa.Integer(), nullable=True),
    )

    # ── bot_monthly_usage: monthly extraction counter ─────────────────
    op.add_column(
        "bot_monthly_usage",
        sa.Column(
            "menu_extractions",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # ── Seed limits on the canonical plans ────────────────────────────
    # Free: 3/mês, images+text only, 3 images max.
    op.execute(
        """
        UPDATE plan SET
            max_menu_extractions_per_month = 3,
            allows_pdf_extraction = false,
            max_images_per_extraction = 3
         WHERE key = 'free'
        """
    )
    # Pro monthly / annual / Founder: 5/mês, any file type, unlimited images.
    op.execute(
        """
        UPDATE plan SET
            max_menu_extractions_per_month = 5,
            allows_pdf_extraction = true,
            max_images_per_extraction = NULL
         WHERE key IN ('pro_monthly', 'pro_annual', 'founder')
        """
    )


def downgrade() -> None:
    op.drop_column("bot_monthly_usage", "menu_extractions")
    op.drop_column("plan", "max_images_per_extraction")
    op.drop_column("plan", "allows_pdf_extraction")
    op.drop_column("plan", "max_menu_extractions_per_month")
