"""Bot.slug for the customer-facing widget URL

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-05-19

Phase 4 (partial) of plan/in_browser_bots.md — replaces the ugly
`/widget?bot_id=2362` customer URL with a slug like
`/sabor-da-serra-zenbot`. The slug is owner-customizable in the
dashboard (Phase 4 UI work) and falls back to an auto-generated
kebab-case form of restaurant_name on bot creation.

Schema changes:
  bot:
    + slug (str(80), nullable initially, unique once backfilled)
    + index for the customer-facing lookup hot path

Backfill strategy:
  - For every existing row: slugify(restaurant_name or 'bot') +
    '-zenbot'. Collisions get -2, -3, etc. suffixes.
  - Then ALTER COLUMN to NOT NULL once every row has a value.

Two-step (add nullable → backfill → enforce NOT NULL + unique) so
the migration is reversible per-step and we don't have to write a
giant SQL UPDATE inside the migration body — the backfill runs in
Python where regex + collision handling is much clearer.
"""

import re
import unicodedata
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Slugify (duplicate of app.utils logic, inline here so the migration
#     doesn't import from app code — alembic should stay self-contained). ──


def _slugify(name: str) -> str:
    """Strip accents, lowercase, replace non-alphanumeric with hyphens."""
    if not name:
        return "bot"
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return hyphenated or "bot"


def upgrade() -> None:
    # Step 1: add nullable column
    op.add_column("bot", sa.Column("slug", sa.String(length=80), nullable=True))

    # Step 2: backfill every existing row
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, restaurant_name FROM bot ORDER BY id")
    ).fetchall()

    used_slugs: set[str] = set()
    for bot_id, restaurant_name in rows:
        base = _slugify(restaurant_name or f"bot-{bot_id}") + "-zenbot"
        candidate = base
        counter = 2
        while candidate in used_slugs:
            candidate = f"{base}-{counter}"
            counter += 1
        used_slugs.add(candidate)
        conn.execute(
            sa.text("UPDATE bot SET slug = :slug WHERE id = :id"),
            {"slug": candidate, "id": bot_id},
        )

    # Step 3: enforce NOT NULL + uniqueness now that every row has a value
    op.alter_column("bot", "slug", nullable=False)
    op.create_unique_constraint("uq_bot_slug", "bot", ["slug"])
    op.create_index("ix_bot_slug", "bot", ["slug"])


def downgrade() -> None:
    op.drop_index("ix_bot_slug", table_name="bot")
    op.drop_constraint("uq_bot_slug", "bot", type_="unique")
    op.drop_column("bot", "slug")
