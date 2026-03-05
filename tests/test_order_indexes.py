# tests/test_order_indexes.py
"""Tests that Order model fields have proper indexes defined in SQLModel."""

from app.models import Order


class TestOrderIndexes:
    def _column_has_index(self, column_name: str) -> bool:
        """Check if a column is indexed via SQLAlchemy table inspection."""
        table = Order.__table__
        col = table.columns[column_name]
        # Check direct column index flag
        if col.index:
            return True
        # Check table-level indexes
        for idx in table.indexes:
            if column_name in [c.name for c in idx.columns]:
                return True
        return False

    def test_bot_id_has_index(self):
        assert self._column_has_index("bot_id"), "Order.bot_id should have index=True"

    def test_contact_id_has_index(self):
        assert self._column_has_index("contact_id"), "Order.contact_id should have index=True"

    def test_status_has_index(self):
        assert self._column_has_index("status"), "Order.status should have index=True"
