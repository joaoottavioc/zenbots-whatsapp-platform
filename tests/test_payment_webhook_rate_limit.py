# tests/test_payment_webhook_rate_limit.py
"""
Tests for rate limiting on the payment webhook endpoint.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_payment_webhook_returns_429_when_rate_limited():
    """handle_payment_notification returns 429 when rate limited."""
    with patch(
        "app.whatsapp.is_rate_limited", new_callable=AsyncMock, return_value=True
    ):
        from app.whatsapp import handle_payment_notification

        mock_request = MagicMock()
        response = await handle_payment_notification(order_id=42, request=mock_request)

        assert response.status_code == 429
        assert b"rate_limited" in response.body


@pytest.mark.asyncio
async def test_payment_webhook_proceeds_when_not_rate_limited():
    """handle_payment_notification proceeds past rate limit when not limited."""
    mock_request = AsyncMock()
    mock_request.json = AsyncMock(
        return_value={"type": "payment", "data": {"id": "12345"}}
    )
    mock_request.query_params = {"token": "valid_token"}
    mock_request.headers = {}

    with (
        patch(
            "app.whatsapp.is_rate_limited", new_callable=AsyncMock, return_value=False
        ),
        patch("app.whatsapp.require_mp_signature", new_callable=AsyncMock),
        patch("app.whatsapp.async_session") as mock_session_factory,
    ):
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None  # order not found
        mock_session.execute = AsyncMock(return_value=mock_result)

        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory.return_value = mock_ctx

        from app.whatsapp import handle_payment_notification

        response = await handle_payment_notification(order_id=999, request=mock_request)

        # Should get 404 (order not found), not 429
        assert response.status_code == 404
