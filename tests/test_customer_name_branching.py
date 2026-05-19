"""Customer-name handler branches by channel (plan/in_browser_bots.md Phase 2.4).

Pins that:
  - WhatsApp channel keeps the original behavior — saves name, transitions
    straight to AWAITING_PAYMENT_METHOD, shows payment prompt.
  - Web channel saves name, transitions to AWAITING_CONTACT_PHONE, asks
    for the customer's real phone.

The branching is what unblocks Phase 2.4 (phone collection); without it
web customers would skip the new state entirely.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import CartState
from app.whatsapp import _handle_customer_name


def _make_mctx(channel: str):
    cart = MagicMock()
    cart.state = CartState.AWAITING_CUSTOMER_NAME
    cart.contact_id = 7
    cart.items = []
    cart.last_activity_at = None

    session = AsyncMock()
    session.add = MagicMock()

    bot = MagicMock()
    bot.id = 1
    bot.delivery_fee = 0
    bot.pix_key = None
    bot.payment_config = None

    mctx = MagicMock()
    mctx.cart = cart
    mctx.session = session
    mctx.bot = bot
    mctx.contact = MagicMock()
    mctx.contact_number = "5511999999999" if channel == "whatsapp" else "web:sess-abc"
    mctx.text_body = "Maria Silva"
    mctx.channel = channel
    mctx.channel_metadata = {} if channel == "whatsapp" else {"session_id": "sess-abc"}
    mctx.reply = AsyncMock()
    return mctx


@pytest.mark.asyncio
async def test_whatsapp_goes_straight_to_payment():
    """WhatsApp customer's Contact.phone_number IS their real phone, so
    no AWAITING_CONTACT_PHONE detour — directly to payment prompt."""
    mctx = _make_mctx("whatsapp")
    with (
        patch("app.whatsapp.crud.save_customer_name_to_contact", new=AsyncMock()),
        patch("app.whatsapp.crud.add_interaction_to_history", new=AsyncMock()),
        patch("app.whatsapp._load_cart_items_with_products", new=AsyncMock()),
        patch("app.whatsapp._bot_has_pix", new=AsyncMock(return_value=True)),
        patch(
            "app.whatsapp._build_cart_summary_message", return_value="(cart summary)"
        ),
    ):
        response = await _handle_customer_name(mctx)

    assert mctx.cart.state == CartState.AWAITING_PAYMENT_METHOD
    assert "Perfeito" in response
    assert "Pix" in response or "Cartão" in response or "Dinheiro" in response


@pytest.mark.asyncio
async def test_web_transitions_to_awaiting_contact_phone():
    """Web customer has only a synth identity; ask for real phone next."""
    mctx = _make_mctx("web")
    with (
        patch("app.whatsapp.crud.save_customer_name_to_contact", new=AsyncMock()),
        patch("app.whatsapp.crud.add_interaction_to_history", new=AsyncMock()),
    ):
        response = await _handle_customer_name(mctx)

    assert mctx.cart.state == CartState.AWAITING_CONTACT_PHONE
    # No payment prompt yet — that's the next state.
    assert "Pix" not in response
    assert "Cartão" not in response
    # Asks for phone with DDD.
    assert "telefone" in response.lower()
    assert "DDD" in response or "DDD" in response.upper()
