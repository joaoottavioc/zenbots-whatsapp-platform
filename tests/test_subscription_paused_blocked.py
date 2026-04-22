# tests/test_subscription_paused_blocked.py
"""
Tests that paused and cancelled subscriptions block the bot from responding
and send the unavailability message instead.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from app.whatsapp import _check_subscription


def _make_subscription(status: str, period_end_days_from_now: int = 30):
    """Create a mock Subscription with given status and future period end."""
    sub = MagicMock()
    sub.status = status
    sub.plan_type = "basic"
    sub.cancel_at_period_end = False
    sub.current_period_end = datetime.now(timezone.utc).replace(
        tzinfo=None
    ) + timedelta(days=period_end_days_from_now)
    return sub


def _make_bot():
    bot = MagicMock()
    bot.id = 1
    bot.user_id = 1
    bot.whatsapp_token = "TOKEN"
    bot.phone_number_id = "PHONE"
    return bot


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paused", "cancelled"])
async def test_paused_and_cancelled_block_immediately(status):
    """A paused/cancelled subscription must block the bot even with time remaining."""
    sub = _make_subscription(status, period_end_days_from_now=30)
    bot = _make_bot()
    session = AsyncMock()

    with (
        patch(
            "app.whatsapp.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=sub,
        ),
        patch(
            "app.whatsapp.send_whatsapp_message", new_callable=AsyncMock
        ) as mock_send,
        patch("app.whatsapp.decrypt_value", side_effect=lambda x: x),
    ):
        blocked = await _check_subscription(session, bot, "5511999990000")

        assert blocked is True
        mock_send.assert_called_once()
        sent_msg = mock_send.call_args[1]["message"]
        assert "manutenção" in sent_msg


@pytest.mark.asyncio
async def test_authorized_subscription_not_blocked():
    """An authorized subscription with time remaining must NOT block."""
    sub = _make_subscription("authorized", period_end_days_from_now=30)
    bot = _make_bot()
    session = AsyncMock()

    with (
        patch(
            "app.whatsapp.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=sub,
        ),
        patch(
            "app.whatsapp.crud.is_plan_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.whatsapp.send_whatsapp_message", new_callable=AsyncMock
        ) as mock_send,
    ):
        blocked = await _check_subscription(session, bot, "5511999990000")

        assert blocked is False
        mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_no_subscription_falls_through_to_free_plan():
    """A bot with no Subscription row must be allowed under the Free plan."""
    bot = _make_bot()
    session = AsyncMock()

    with (
        patch(
            "app.whatsapp.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.whatsapp.crud.is_plan_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.whatsapp.send_whatsapp_message", new_callable=AsyncMock
        ) as mock_send,
    ):
        blocked = await _check_subscription(session, bot, "5511999990000")

        assert blocked is False
        mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_no_subscription_blocks_when_free_plan_missing():
    """If the Free plan seed is missing/disabled, no-sub bots are blocked."""
    bot = _make_bot()
    session = AsyncMock()

    with (
        patch(
            "app.whatsapp.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.whatsapp.crud.is_plan_active",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.whatsapp.send_whatsapp_message", new_callable=AsyncMock
        ) as mock_send,
        patch("app.whatsapp.decrypt_value", side_effect=lambda x: x),
    ):
        blocked = await _check_subscription(session, bot, "5511999990000")

        assert blocked is True
        mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_pending_within_grace_period_not_blocked():
    """A pending subscription within the grace period should NOT block."""
    sub = _make_subscription("pending", period_end_days_from_now=1)
    bot = _make_bot()
    session = AsyncMock()

    with (
        patch(
            "app.whatsapp.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=sub,
        ),
        patch(
            "app.whatsapp.crud.is_plan_active",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.whatsapp.send_whatsapp_message", new_callable=AsyncMock
        ) as mock_send,
    ):
        blocked = await _check_subscription(session, bot, "5511999990000")

        assert blocked is False
        mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_pending_past_grace_period_blocked():
    """A pending subscription past the grace period must be blocked."""
    sub = _make_subscription("pending", period_end_days_from_now=-5)
    bot = _make_bot()
    session = AsyncMock()

    with (
        patch(
            "app.whatsapp.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=sub,
        ),
        patch(
            "app.whatsapp.send_whatsapp_message", new_callable=AsyncMock
        ) as mock_send,
        patch("app.whatsapp.decrypt_value", side_effect=lambda x: x),
    ):
        blocked = await _check_subscription(session, bot, "5511999990000")

        assert blocked is True
        mock_send.assert_called_once()
