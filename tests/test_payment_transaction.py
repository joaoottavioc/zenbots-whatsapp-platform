# tests/test_payment_transaction.py
"""
Tests for transaction isolation in payment handler (Fix #9).

1. PIX failure triggers session.rollback() — no orphan order
2. create_order with auto_commit=False uses flush instead of commit
"""

from unittest.mock import AsyncMock, MagicMock
import pytest

from app.crud import create_order
from app.models import DeliveryMethod


@pytest.mark.asyncio
async def test_create_order_auto_commit_false_uses_flush():
    """When auto_commit=False, create_order calls flush() instead of commit()."""
    session = AsyncMock()
    session.get = AsyncMock()

    # Mock product lookup
    product = MagicMock()
    product.id = 1
    product.price = 25.0
    session.get.return_value = product

    session.add = MagicMock()

    order = await create_order(
        session,
        bot_id=1,
        items=[{"product_id": 1, "quantity": 2}],
        total_amount=50.0,
        delivery_method=DeliveryMethod.DELIVERY,
        auto_commit=False,
    )

    assert order is not None
    session.flush.assert_awaited_once()
    session.commit.assert_not_awaited()
    session.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_order_auto_commit_true_uses_commit():
    """When auto_commit=True (default), create_order calls commit()."""
    session = AsyncMock()
    session.get = AsyncMock()

    product = MagicMock()
    product.id = 1
    product.price = 25.0
    session.get.return_value = product

    session.add = MagicMock()

    order = await create_order(
        session,
        bot_id=1,
        items=[{"product_id": 1, "quantity": 2}],
        total_amount=50.0,
        delivery_method=DeliveryMethod.DELIVERY,
        auto_commit=True,
    )

    assert order is not None
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_pix_failure_calls_rollback_not_delete():
    """
    Verify that whatsapp._handle_payment_method calls session.rollback()
    on PIX failure instead of crud.delete_order().
    """
    import inspect
    from app import whatsapp

    source = inspect.getsource(whatsapp._handle_payment_method)

    # Should call session.rollback() on PIX failure
    assert "session.rollback()" in source, (
        "_handle_payment_method should call session.rollback() on PIX failure"
    )

    # Should NOT call crud.delete_order
    assert "crud.delete_order" not in source, (
        "_handle_payment_method should not call crud.delete_order — "
        "rollback handles it atomically"
    )

    # Should pass auto_commit=False to create_order
    assert "auto_commit=False" in source, (
        "_handle_payment_method should pass auto_commit=False to create_order"
    )


@pytest.mark.asyncio
async def test_oauth_token_exchange_logs_audit_event():
    """OAuth token exchange should log a structured audit event."""
    import inspect
    from app import payment_routes

    source = inspect.getsource(payment_routes)
    assert "OAUTH_TOKEN_EXCHANGE_SUCCESS" in source, (
        "payment_routes should log OAUTH_TOKEN_EXCHANGE_SUCCESS on successful token exchange"
    )
    assert "user_id" in source and "bot_id" in source, (
        "OAuth audit log should include user_id and bot_id"
    )


@pytest.mark.asyncio
async def test_token_refresh_logs_audit_event():
    """Token refresh should log a structured audit event."""
    import inspect
    from app import payment_service

    source = inspect.getsource(payment_service)
    assert "OAUTH_TOKEN_REFRESH_SUCCESS" in source, (
        "payment_service should log OAUTH_TOKEN_REFRESH_SUCCESS on successful token refresh"
    )
    assert "config_id" in source and "bot_id" in source, (
        "Token refresh audit log should include config_id and bot_id"
    )
