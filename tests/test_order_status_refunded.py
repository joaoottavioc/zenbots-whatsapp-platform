"""Tests for D13: OrderStatus enum includes REFUNDED state.

Verifies that the OrderStatus enum has a 'refunded' value for
tracking Mercado Pago chargebacks and refunds.
"""

from app.models import OrderStatus


class TestOrderStatusRefunded:
    def test_refunded_value_exists(self):
        """OrderStatus must have a REFUNDED member."""
        assert hasattr(OrderStatus, "REFUNDED")
        assert OrderStatus.REFUNDED.value == "refunded"

    def test_refunded_is_valid_string_enum(self):
        """REFUNDED must work as a string (str enum)."""
        assert OrderStatus.REFUNDED == "refunded"
        assert str(OrderStatus.REFUNDED) == "OrderStatus.REFUNDED"

    def test_all_expected_statuses_present(self):
        """All expected order lifecycle states must be present."""
        expected = {
            "pending",
            "paid",
            "failed",
            "expired",
            "preparing",
            "ready",
            "completed",
            "canceled",
            "refunded",
        }
        actual = {s.value for s in OrderStatus}
        assert actual == expected

    def test_refunded_can_be_constructed_from_string(self):
        """OrderStatus('refunded') must return REFUNDED."""
        assert OrderStatus("refunded") is OrderStatus.REFUNDED
