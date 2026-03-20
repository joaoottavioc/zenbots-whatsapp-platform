"""Add pg_trgm extension and GIN indexes for fuzzy search

Revision ID: n5i6j7k8l9m0
Revises: m4h5i6j7k8l9
Create Date: 2026-03-20
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "n5i6j7k8l9m0"
down_revision = "l3g4h5i6j7k8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_product_name_trgm "
        "ON product USING gin (name gin_trgm_ops);"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_product_keywords_trgm "
        "ON product USING gin (keywords gin_trgm_ops);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_product_keywords_trgm;")
    op.execute("DROP INDEX IF EXISTS ix_product_name_trgm;")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm;")
