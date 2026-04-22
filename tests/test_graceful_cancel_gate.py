"""Self-serve cancellation keeps paid plan active until period end.

When a customer cancels via POST /billing/cancel, the gate must NOT block
them immediately — they've paid through current_period_end. Only after
that date passes should the bot fall through to the Free plan.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.whatsapp import _check_subscription


def _make_sub(
    *,
    status: str,
    cancel_at_period_end: bool,
    period_end_days_from_now: int,
    plan_type: str = "pro_monthly",
):
    sub = MagicMock()
    sub.status = status
    sub.plan_type = plan_type
    sub.cancel_at_period_end = cancel_at_period_end
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
async def test_graceful_cancel_within_paid_period_not_blocked():
    """cancel_at_period_end=True + period still in future → bot stays live."""
    sub = _make_sub(
        status="authorized", cancel_at_period_end=True, period_end_days_from_now=5
    )
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
async def test_graceful_cancel_after_period_falls_through_to_free():
    """cancel_at_period_end=True + period already passed → Free baseline.

    Bot should still respond (Free.allows_bot_usage=True) rather than
    hitting the old hard-block path.
    """
    sub = _make_sub(
        status="cancelled", cancel_at_period_end=True, period_end_days_from_now=-2
    )
    bot = _make_bot()
    session = AsyncMock()

    plan_active_mock = AsyncMock()
    plan_active_mock.return_value = True

    with (
        patch(
            "app.whatsapp.crud.get_subscription_by_bot",
            new_callable=AsyncMock,
            return_value=sub,
        ),
        patch(
            "app.whatsapp.crud.is_plan_active",
            plan_active_mock,
        ),
        patch(
            "app.whatsapp.send_whatsapp_message", new_callable=AsyncMock
        ) as mock_send,
    ):
        blocked = await _check_subscription(session, bot, "5511999990000")

    assert blocked is False
    mock_send.assert_not_called()
    # The Free plan (not the paid plan_type) must be consulted once the
    # grace period is over.
    plan_active_mock.assert_awaited_with(session, "free")


@pytest.mark.asyncio
async def test_hard_cancel_without_cancel_at_period_end_still_blocks():
    """A MP-initiated cancellation (status=cancelled, flag=false) still blocks.

    Regression guard: we must not have accidentally softened this path
    when we added the graceful flow.
    """
    sub = _make_sub(
        status="cancelled", cancel_at_period_end=False, period_end_days_from_now=30
    )
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
