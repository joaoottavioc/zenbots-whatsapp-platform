# tests/test_cancel_expired_pix.py
"""
Tests for the cancel_expired_pix_orders function and its cron configuration.

Covers:
- Expired PIX orders are canceled
- No unnecessary commits when nothing to cancel
- WorkerSettings has cron_jobs configured
"""
import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from app.models import OrderStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session() -> AsyncMock:
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    session.execute.return_value = result_mock
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


def _make_order(order_id: int, status=OrderStatus.PENDING, payment_method: str = "pix"):
    order = MagicMock()
    order.id = order_id
    order.status = status
    order.payment_method = payment_method
    return order


# ===========================================================================
# TestCancelExpiredPixOrders
# ===========================================================================

class TestCancelExpiredPixOrders:

    async def test_cancels_expired_pix_orders(self):
        """Pending PIX orders older than 15 minutes are canceled."""
        from app.crud import cancel_expired_pix_orders

        session = _make_session()
        order1 = _make_order(order_id=1)
        order2 = _make_order(order_id=2)
        session.execute.return_value.scalars.return_value.all.return_value = [order1, order2]

        count = await cancel_expired_pix_orders(session)

        assert count == 2
        assert order1.status == OrderStatus.CANCELED
        assert order2.status == OrderStatus.CANCELED
        session.commit.assert_awaited_once()

    async def test_no_commit_when_nothing_to_cancel(self):
        """When there are no expired orders, no commit is issued."""
        from app.crud import cancel_expired_pix_orders

        session = _make_session()
        session.execute.return_value.scalars.return_value.all.return_value = []

        count = await cancel_expired_pix_orders(session)

        assert count == 0
        session.commit.assert_not_awaited()

    def test_cron_jobs_configured(self):
        """WorkerSettings must have cron_jobs with at least one entry."""
        from app.worker import WorkerSettings

        assert hasattr(WorkerSettings, "cron_jobs"), "WorkerSettings missing cron_jobs"
        assert len(WorkerSettings.cron_jobs) >= 1, "cron_jobs should have at least one cron job"


# ===========================================================================
# TestOrderStatusEnumUsed
# ===========================================================================

class TestOrderStatusEnumUsed:

    def test_cancel_query_uses_enum_not_string(self):
        """Ensure the query uses OrderStatus.PENDING, not a raw string."""
        import inspect
        from app import crud
        source = inspect.getsource(crud.cancel_expired_pix_orders)
        assert 'OrderStatus.PENDING' in source, (
            "cancel_expired_pix_orders should use OrderStatus.PENDING, not a raw string"
        )
        assert '"PENDING"' not in source, (
            "cancel_expired_pix_orders should NOT use string \"PENDING\""
        )
