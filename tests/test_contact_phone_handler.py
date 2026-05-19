"""Web checkout phone-collection handler (plan/in_browser_bots.md Phase 2.4).

A new state `AWAITING_CONTACT_PHONE` sits between AWAITING_CUSTOMER_NAME
and AWAITING_PAYMENT_METHOD on the web channel only. The handler:

  1. Refuses to fire when cart.state isn't AWAITING_CONTACT_PHONE.
  2. Normalizes free-form phone input (digits-only, accepts +55 prefix).
  3. Rejects inputs outside the 10-13 digit Brazilian range.
  4. Saves the normalized phone to Contact.contact_phone (NOT
     Contact.phone_number — that's the identity key).
  5. Transitions to AWAITING_PAYMENT_METHOD and shows payment prompt.

The normalizer is exposed via _normalize_contact_phone for direct testing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import CartState
from app.whatsapp import _handle_contact_phone, _normalize_contact_phone


# ── Normalizer ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Mobile, 11 digits with DDD
        ("11912345678", "11912345678"),
        ("11 91234-5678", "11912345678"),
        ("(11) 91234-5678", "11912345678"),
        # Landline, 10 digits with DDD
        ("1133345678", "1133345678"),
        ("11 3334-5678", "1133345678"),
        # Country code stripped
        ("+55 11 91234-5678", "11912345678"),
        ("5511912345678", "11912345678"),
        # Country code with landline (12 digits)
        ("551133345678", "1133345678"),
    ],
)
def test_normalize_accepts_valid_brazilian_phones(raw, expected):
    assert _normalize_contact_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "123",  # too short
        "12345",  # too short
        "123456789",  # 9 digits — neither landline nor mobile
        "12345678901234",  # too long
        "abc",  # no digits
        "não sei",  # gibberish
    ],
)
def test_normalize_rejects_invalid(raw):
    assert _normalize_contact_phone(raw) is None


# ── Handler ──────────────────────────────────────────────────────────


def _make_mctx(cart_state, text_body, channel="web"):
    cart = MagicMock()
    cart.state = cart_state
    cart.contact_id = 7
    cart.last_activity_at = None
    cart.items = []

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
    mctx.contact_number = "web:sess-abc"
    mctx.text_body = text_body
    mctx.channel = channel
    mctx.channel_metadata = {"session_id": "sess-abc"}
    mctx.reply = AsyncMock()
    return mctx


@pytest.mark.asyncio
async def test_handler_skips_when_state_does_not_match():
    """Returning None signals the FSM dispatcher to try the next handler."""
    mctx = _make_mctx(CartState.AWAITING_CUSTOMER_NAME, "11912345678")
    assert await _handle_contact_phone(mctx) is None
    mctx.reply.assert_not_called()


@pytest.mark.asyncio
async def test_handler_rejects_invalid_phone_input():
    """Bad input keeps the state at AWAITING_CONTACT_PHONE and asks again
    — does NOT transition to AWAITING_PAYMENT_METHOD."""
    mctx = _make_mctx(CartState.AWAITING_CONTACT_PHONE, "não tenho")
    with patch(
        "app.whatsapp.crud.save_contact_phone_to_contact", new=AsyncMock()
    ) as mock_save:
        with patch("app.whatsapp.crud.add_interaction_to_history", new=AsyncMock()):
            response = await _handle_contact_phone(mctx)

    mock_save.assert_not_awaited()
    assert mctx.cart.state == CartState.AWAITING_CONTACT_PHONE  # unchanged
    assert "DDD" in response or "telefone" in response.lower()
    mctx.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_handler_saves_normalized_phone_and_advances_state():
    """Happy path: digit-extract the phone, save it to contact_phone,
    move to AWAITING_PAYMENT_METHOD, return payment prompt."""
    mctx = _make_mctx(CartState.AWAITING_CONTACT_PHONE, "(11) 91234-5678")

    with (
        patch(
            "app.whatsapp.crud.save_contact_phone_to_contact", new=AsyncMock()
        ) as mock_save,
        patch("app.whatsapp.crud.add_interaction_to_history", new=AsyncMock()),
        patch("app.whatsapp._load_cart_items_with_products", new=AsyncMock()),
        patch("app.whatsapp._bot_has_pix", new=AsyncMock(return_value=True)),
        patch(
            "app.whatsapp._build_cart_summary_message", return_value="(cart summary)"
        ),
    ):
        response = await _handle_contact_phone(mctx)

    # Phone saved normalized (digits only, no formatting).
    mock_save.assert_awaited_once_with(mctx.session, 7, "11912345678")
    assert mctx.cart.state == CartState.AWAITING_PAYMENT_METHOD
    # Response signals success + shows payment options.
    assert "Telefone salvo" in response
    assert "Pix" in response or "Cartão" in response or "Dinheiro" in response


@pytest.mark.asyncio
async def test_handler_strips_country_code_before_save():
    """+55 prefix is stripped so storage is canonical DDD+number."""
    mctx = _make_mctx(CartState.AWAITING_CONTACT_PHONE, "+55 11 91234-5678")
    with (
        patch(
            "app.whatsapp.crud.save_contact_phone_to_contact", new=AsyncMock()
        ) as mock_save,
        patch("app.whatsapp.crud.add_interaction_to_history", new=AsyncMock()),
        patch("app.whatsapp._load_cart_items_with_products", new=AsyncMock()),
        patch("app.whatsapp._bot_has_pix", new=AsyncMock(return_value=True)),
        patch(
            "app.whatsapp._build_cart_summary_message", return_value="(cart summary)"
        ),
    ):
        await _handle_contact_phone(mctx)

    saved_phone = mock_save.await_args.args[2]
    assert saved_phone == "11912345678"
