"""Add unique constraint on Contact (bot_id, phone_number)

Revision ID: j1e2f3g4h5i6
Revises: i0d1e2f3g4h5
Create Date: 2026-03-09
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "j1e2f3g4h5i6"
down_revision = "i0d1e2f3g4h5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Deduplicate contacts before adding the constraint.
    # For each (bot_id, phone_number) group, keep the contact with the lowest id
    # and reassign orders, conversation history, and carts to it.
    op.execute("""
        WITH duplicates AS (
            SELECT id, bot_id, phone_number,
                   ROW_NUMBER() OVER (PARTITION BY bot_id, phone_number ORDER BY id) AS rn
            FROM contact
        ),
        keeper AS (
            SELECT bot_id, phone_number, MIN(id) AS keep_id
            FROM contact
            GROUP BY bot_id, phone_number
            HAVING COUNT(*) > 1
        )
        UPDATE "order" SET contact_id = k.keep_id
        FROM keeper k
        JOIN contact c ON c.bot_id = k.bot_id AND c.phone_number = k.phone_number AND c.id != k.keep_id
        WHERE "order".contact_id = c.id
    """)

    op.execute("""
        WITH keeper AS (
            SELECT bot_id, phone_number, MIN(id) AS keep_id
            FROM contact
            GROUP BY bot_id, phone_number
            HAVING COUNT(*) > 1
        )
        UPDATE conversationhistory SET contact_id = k.keep_id
        FROM keeper k
        JOIN contact c ON c.bot_id = k.bot_id AND c.phone_number = k.phone_number AND c.id != k.keep_id
        WHERE conversationhistory.contact_id = c.id
    """)

    # Delete duplicate shopping carts (keep the one linked to the surviving contact)
    op.execute("""
        WITH keeper AS (
            SELECT bot_id, phone_number, MIN(id) AS keep_id
            FROM contact
            GROUP BY bot_id, phone_number
            HAVING COUNT(*) > 1
        )
        DELETE FROM shoppingcart
        WHERE contact_id IN (
            SELECT c.id FROM keeper k
            JOIN contact c ON c.bot_id = k.bot_id AND c.phone_number = k.phone_number AND c.id != k.keep_id
        )
    """)

    # Now delete the duplicate contact records
    op.execute("""
        WITH duplicates AS (
            SELECT id,
                   ROW_NUMBER() OVER (PARTITION BY bot_id, phone_number ORDER BY id) AS rn
            FROM contact
        )
        DELETE FROM contact WHERE id IN (
            SELECT id FROM duplicates WHERE rn > 1
        )
    """)

    op.create_unique_constraint("uq_contact_bot_phone", "contact", ["bot_id", "phone_number"])


def downgrade() -> None:
    op.drop_constraint("uq_contact_bot_phone", "contact", type_="unique")
