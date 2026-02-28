# tests/conftest.py
"""
Shared fixtures for ZenBots test suite.

Key design decisions:
- All DB-touching code is mocked via AsyncMock — no real DB required.
- External HTTP calls (WhatsApp, Mercado Pago) are patched at the call site.
- Fixtures return MagicMock objects that mimic SQLModel model instances.
"""
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from app.models import DeliveryMethod, OrderStatus, CartState


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Model fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.id = 1
    bot.user_id = 1
    bot.restaurant_name = "Test Restaurant"
    bot.whatsapp_token = "fake-token"
    bot.phone_number_id = "fake-phone-id"
    bot.whatsapp_number = "5511999999999"
    bot.menu_url = None
    bot.pix_key = "test@pix.com"
    bot.delivery_fee = 5.0
    bot.min_order_value = 0.0
    bot.is_open = True
    bot.closing_message = None
    bot.schedule = None
    bot.timezone = "America/Sao_Paulo"
    bot.latitude = -23.5505
    bot.longitude = -46.6333
    bot.max_delivery_radius = 10.0
    bot.payment_config = None
    return bot


@pytest.fixture
def mock_contact():
    contact = MagicMock()
    contact.id = 10
    contact.phone_number = "5511888888888"
    contact.name = "Test User"
    contact.bot_id = 1
    return contact


@pytest.fixture
def mock_cart(mock_contact):
    cart = MagicMock()
    cart.id = 100
    cart.state = CartState.GREETING
    cart.delivery_method = None
    cart.customer_address = None
    cart.pending_address = None
    cart.partial_address = None
    cart.items = []
    cart.last_suggestions = None
    cart.human_takeover_active = False
    cart.pix_only = False
    cart.last_activity_at = utcnow()
    cart.contact_id = mock_contact.id
    cart.contact = mock_contact
    # Pending action fields
    cart.pending_action_tool = None
    cart.pending_action_args = None
    cart.pending_action_question = None
    cart.pending_action_expires_at = None
    return cart


@pytest.fixture
def active_subscription():
    sub = MagicMock()
    sub.status = "authorized"
    sub.current_period_end = utcnow() + timedelta(days=30)
    return sub


@pytest.fixture
def expired_subscription():
    sub = MagicMock()
    sub.status = "cancelled"
    sub.current_period_end = utcnow() - timedelta(days=10)
    return sub


# ---------------------------------------------------------------------------
# Cart item / product helpers
# ---------------------------------------------------------------------------

def make_product(product_id: int, name: str, price: float, bot_id: int = 1):
    p = MagicMock()
    p.id = product_id
    p.name = name
    p.price = price
    p.bot_id = bot_id
    p.description = f"Description for {name}"
    p.is_available = True
    p.is_deleted = False
    return p


def make_cart_item(product_id: int, name: str, price: float, quantity: int = 1, notes: str = None):
    item = MagicMock()
    item.product_id = product_id
    item.quantity = quantity
    item.notes = notes
    item.product = make_product(product_id, name, price)
    return item


# ---------------------------------------------------------------------------
# WhatsApp webhook payload builder
# ---------------------------------------------------------------------------

def build_whatsapp_payload(
    from_number: str = "5511888888888",
    text: str = "Olá",
    message_id: str = "wamid.test123",
    phone_number_id: str = "fake-phone-id",
    display_phone: str = "5511999999999",
):
    """Builds a minimal WhatsApp Cloud API webhook payload."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "phone_number_id": phone_number_id,
                                "display_phone_number": display_phone,
                            },
                            "messages": [
                                {
                                    "from": from_number,
                                    "id": message_id,
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


# ---------------------------------------------------------------------------
# Async session mock
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_session():
    """A minimal AsyncMock that satisfies session.execute / scalars / add / commit."""
    session = AsyncMock()

    # Default: execute returns an object whose scalars().first() returns None
    result_mock = MagicMock()
    result_mock.scalars.return_value.first.return_value = None
    result_mock.scalars.return_value.all.return_value = []
    session.execute.return_value = result_mock

    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.refresh = AsyncMock()
    session.is_active = True
    return session


# ---------------------------------------------------------------------------
# Common patches (use as context managers or decorators in individual tests)
# ---------------------------------------------------------------------------

PATCH_SEND = "app.whatsapp.send_whatsapp_message"
PATCH_MARK_READ = "app.whatsapp.mark_message_as_read"
PATCH_IS_SPAMMING = "app.whatsapp.is_spamming"
PATCH_RESOLVE_INTENT = "app.whatsapp.resolve_intent"
PATCH_CRUD = "app.whatsapp.crud"
PATCH_GET_ADDRESS = "app.utils.get_address_from_cep"
PATCH_CALC_DIST = "app.utils.calculate_distance"
PATCH_CREATE_PIX = "app.whatsapp.create_pix_payment"
PATCH_BROADCAST = "app.whatsapp.broadcast_order_update"
PATCH_ASYNC_SESSION = "app.whatsapp.async_session"
PATCH_CONTACT_LOCK = "app.whatsapp.contact_lock"


# ---------------------------------------------------------------------------
# Message extraction helper
# ---------------------------------------------------------------------------

def get_sent_message(send_mock, call_index: int = -1) -> str:
    """
    Extract the text body from a send_whatsapp_message mock call.

    send_whatsapp_message(to, message, token=..., phone_id=...) is called
    with 'message' as the 2nd positional arg, not a kwarg, so we must
    check call_args.args[1] instead of call_args.kwargs["message"].
    """
    calls = send_mock.call_args_list
    if not calls:
        return ""
    call = calls[call_index]
    # Prefer explicit kwarg, fall back to 2nd positional arg
    return call.kwargs.get("message") or (call.args[1] if len(call.args) > 1 else "")
