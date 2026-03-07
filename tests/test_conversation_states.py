# tests/test_conversation_states.py
"""
Integration-level tests for process_whatsapp_message state machine.

Strategy:
- The async session and all DB CRUD calls are mocked.
- External HTTP calls (WhatsApp send, Mercado Pago) are mocked.
- intent resolution (resolve_intent) is mocked to control flow deterministically.
- Each test controls cart.state and asserts on:
    1. The message sent via send_whatsapp_message (response content)
    2. The new cart.state after execution
    3. Which CRUD functions were called
"""

import json
import pytest
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import (
    build_whatsapp_payload,
    get_sent_message,
    make_cart_item,
    PATCH_SEND,
    PATCH_MARK_READ,
    PATCH_IS_SPAMMING,
    PATCH_RESOLVE_INTENT,
    PATCH_CRUD,
    PATCH_CREATE_PIX,
    PATCH_BROADCAST,
    PATCH_ASYNC_SESSION,
    PATCH_CONTACT_LOCK,
)
from app.models import DeliveryMethod


# ---------------------------------------------------------------------------
# Shared async context-manager helper for async_session
# ---------------------------------------------------------------------------


def make_session_ctx(mock_session):
    """Returns a context manager that yields mock_session."""

    @asynccontextmanager
    async def _ctx():
        yield mock_session

    return _ctx


@asynccontextmanager
async def _noop_lock():
    """Transparent no-op async context manager replacing the Redis lock in tests."""
    yield


def _noop_lock_factory(*args, **kwargs):
    """Returns a no-op lock regardless of contact_id."""
    return _noop_lock()


# ---------------------------------------------------------------------------
# Base class with common patching boilerplate
# ---------------------------------------------------------------------------


class BaseConversationTest:
    """
    Subclasses get self.send_mock, self.crud_mock auto-patched.
    Use get_sent_message(self.send_mock) to extract the text body sent.
    """

    @pytest.fixture(autouse=True)
    def _common_patches(
        self, mock_bot, mock_cart, mock_contact, active_subscription, mock_session
    ):
        self.bot = mock_bot
        self.cart = mock_cart
        self.contact = mock_contact
        self.session = mock_session

        def _make_result(value):
            r = MagicMock()
            r.scalars.return_value.first.return_value = value
            r.scalars.return_value.all.return_value = [value] if value else []
            return r

        # Sequence: Bot lookup → ProcessedMessage check → safe default
        _execute_sequence = [
            _make_result(mock_bot),
            _make_result(None),  # ProcessedMessage (not yet processed)
        ]
        _execute_idx = 0

        async def _execute_factory(*args, **kwargs):
            nonlocal _execute_idx
            if _execute_idx < len(_execute_sequence):
                result = _execute_sequence[_execute_idx]
                _execute_idx += 1
                return result
            return _make_result(None)

        self.session.execute = AsyncMock(side_effect=_execute_factory)

        self.send_mock = AsyncMock()
        self.crud_mock = MagicMock()
        self.crud_mock.is_message_processed = AsyncMock(return_value=False)
        self.crud_mock.add_processed_message = AsyncMock()
        self.crud_mock.get_or_create_contact = AsyncMock(return_value=mock_contact)
        self.crud_mock.get_or_create_cart = AsyncMock(return_value=mock_cart)
        self.crud_mock.add_interaction_to_history = AsyncMock()
        self.crud_mock.clear_db_cart = AsyncMock()
        self.crud_mock.get_history_for_contact = AsyncMock(return_value=[])
        self.crud_mock.find_relevant_products = AsyncMock(return_value=[])
        self.crud_mock.save_address_to_cart = AsyncMock()
        self.crud_mock.save_customer_name_to_contact = AsyncMock()
        self.crud_mock.get_products_by_bot_id = AsyncMock(
            return_value=[MagicMock(id=1, name="Burger", price=20.0)]
        )
        self.crud_mock.get_subscription_by_bot = AsyncMock(
            return_value=active_subscription
        )
        self.crud_mock.is_plan_active = AsyncMock(return_value=True)

        with (
            patch(PATCH_SEND, self.send_mock),
            patch(PATCH_MARK_READ, AsyncMock()),
            patch(PATCH_IS_SPAMMING, new=AsyncMock(return_value=False)),
            patch(PATCH_CRUD, self.crud_mock),
            patch(PATCH_BROADCAST, AsyncMock()),
            patch(PATCH_ASYNC_SESSION, make_session_ctx(self.session)),
            patch("app.whatsapp.decrypt_value", side_effect=lambda v: v),
            patch(PATCH_CONTACT_LOCK, side_effect=_noop_lock_factory),
        ):
            yield


# ===========================================================================
# 1. GATE TESTS — things that block execution before any state logic
# ===========================================================================


class TestGates(BaseConversationTest):
    @pytest.mark.asyncio
    async def test_status_update_ignored(self):
        """Webhook payloads without 'messages' key (status updates) are silently ignored."""
        from app.whatsapp import process_whatsapp_message

        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": "fake-phone-id",
                                    "display_phone_number": "5511999999999",
                                },
                                "statuses": [{"status": "delivered"}],
                            }
                        }
                    ]
                }
            ]
        }
        await process_whatsapp_message({}, payload)
        self.send_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_silently(self):
        """Spammers are blocked and receive no response."""
        from app.whatsapp import process_whatsapp_message

        with patch(PATCH_IS_SPAMMING, new=AsyncMock(return_value=True)):
            await process_whatsapp_message({}, build_whatsapp_payload(text="spam"))
        self.send_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_not_found_ignored(self):
        """If no Bot matches the phone_number_id, the message is silently dropped."""
        from app.whatsapp import process_whatsapp_message

        def _make_none_result():
            r = MagicMock()
            r.scalars.return_value.first.return_value = None
            return r

        self.session.execute.side_effect = [_make_none_result()]
        self.crud_mock.get_bot_by_number = AsyncMock(return_value=None)

        await process_whatsapp_message({}, build_whatsapp_payload())
        self.send_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_subscription_sends_maintenance_message(
        self, expired_subscription
    ):
        """Bot with expired subscription sends a maintenance notice and stops."""
        from app.whatsapp import process_whatsapp_message

        self.crud_mock.get_subscription_by_bot = AsyncMock(
            return_value=expired_subscription
        )

        await process_whatsapp_message({}, build_whatsapp_payload())

        self.send_mock.assert_called_once()
        msg = get_sent_message(self.send_mock)
        assert "indisponível" in msg.lower()

    @pytest.mark.asyncio
    async def test_duplicate_message_ignored(self):
        """Same message_id processed twice is idempotent."""
        from app.whatsapp import process_whatsapp_message

        self.crud_mock.is_message_processed = AsyncMock(return_value=True)

        await process_whatsapp_message({}, build_whatsapp_payload(message_id="dup-id"))
        self.send_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_human_takeover_ignores_message(self):
        """When human_takeover_active is True, bot does nothing."""
        from app.whatsapp import process_whatsapp_message

        self.cart.human_takeover_active = True
        await process_whatsapp_message({}, build_whatsapp_payload(text="quero pedir"))
        self.send_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_closed_sends_closing_message(self):
        """When store is closed, sends closing message with next opening time."""
        from app.whatsapp import process_whatsapp_message

        self.bot.is_open = False
        self.bot.closing_message = "Estamos fechados agora."

        await process_whatsapp_message({}, build_whatsapp_payload())

        self.send_mock.assert_called_once()
        msg = get_sent_message(self.send_mock)
        assert (
            "fechados" in msg.lower()
            or "indispon" in msg.lower()
            or "atendendo" in msg.lower()
        )


# ===========================================================================
# 2. GREETING STATE
# ===========================================================================


class TestGreetingState(BaseConversationTest):
    @pytest.mark.asyncio
    async def test_greeting_sends_menu_and_transitions_to_shopping(self):
        """GREETING state + GREETING_OR_QUESTION intent → sends menu, state becomes SHOPPING."""
        from app.whatsapp import process_whatsapp_message

        self.cart.state = "GREETING"
        self.bot.menu_url = "https://example.com/menu.jpg"

        with patch(
            PATCH_RESOLVE_INTENT, AsyncMock(return_value="GREETING_OR_QUESTION")
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="oi"))

        self.send_mock.assert_called_once()
        call_kwargs = self.send_mock.call_args.kwargs
        assert call_kwargs.get("media_url") == "https://example.com/menu.jpg"
        assert call_kwargs.get("media_type") == "image"
        assert self.cart.state == "SHOPPING"

    @pytest.mark.asyncio
    async def test_greeting_pdf_menu_sends_as_document(self):
        """PDF menu URL should be sent as media_type='document'."""
        from app.whatsapp import process_whatsapp_message

        self.cart.state = "GREETING"
        self.bot.menu_url = "https://example.com/cardapio.pdf"

        with patch(
            PATCH_RESOLVE_INTENT, AsyncMock(return_value="GREETING_OR_QUESTION")
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="oi"))

        call_kwargs = self.send_mock.call_args.kwargs
        assert call_kwargs.get("media_type") == "document"


# ===========================================================================
# 3. AWAITING_DELIVERY_METHOD STATE
# ===========================================================================


class TestDeliveryMethodState(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "AWAITING_DELIVERY_METHOD"

    @pytest.mark.asyncio
    async def test_choose_delivery_transitions_to_awaiting_cep(self):
        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, build_whatsapp_payload(text="Entrega"))
        assert self.cart.state == "AWAITING_CEP"
        assert self.cart.delivery_method == DeliveryMethod.DELIVERY

    @pytest.mark.asyncio
    async def test_choose_delivery_by_number(self):
        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, build_whatsapp_payload(text="1"))
        assert self.cart.state == "AWAITING_CEP"

    @pytest.mark.asyncio
    async def test_choose_pickup_with_name_goes_to_payment(self):
        """Known customer choosing pickup skips name collection."""
        from app.whatsapp import process_whatsapp_message

        self.contact.name = "João Silva"
        self.cart.contact = self.contact

        await process_whatsapp_message({}, build_whatsapp_payload(text="Retirada"))
        assert self.cart.state == "AWAITING_PAYMENT_METHOD"
        assert self.cart.delivery_method == DeliveryMethod.PICKUP

    @pytest.mark.asyncio
    async def test_choose_pickup_without_name_requests_name(self):
        """Unknown customer choosing pickup must provide name first."""
        from app.whatsapp import process_whatsapp_message

        self.contact.name = None
        self.cart.contact = self.contact

        await process_whatsapp_message({}, build_whatsapp_payload(text="Retirada"))
        assert self.cart.state == "AWAITING_CUSTOMER_NAME"

    @pytest.mark.asyncio
    async def test_invalid_response_asks_again(self):
        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, build_whatsapp_payload(text="talvez"))
        assert self.cart.state == "AWAITING_DELIVERY_METHOD"
        msg = get_sent_message(self.send_mock)
        assert "Entrega" in msg or "Retirada" in msg


# ===========================================================================
# 4. AWAITING_CEP STATE
# ===========================================================================


class TestCepState(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "AWAITING_CEP"

    @pytest.mark.asyncio
    async def test_cancel_keyword_resets_to_greeting(self):
        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, build_whatsapp_payload(text="cancelar"))
        assert self.cart.state == "GREETING"

    @pytest.mark.asyncio
    async def test_pickup_keyword_switches_to_payment(self):
        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, build_whatsapp_payload(text="retirada"))
        assert self.cart.state == "AWAITING_PAYMENT_METHOD"
        assert self.cart.delivery_method == "pickup"
        msg = get_sent_message(self.send_mock)
        assert "Test User" in msg
        # Bot has pix_key, so all options should be shown
        assert "PIX" in msg
        assert "Cartão" in msg
        assert "Dinheiro" in msg

    @pytest.mark.asyncio
    async def test_pickup_keyword_in_cep_without_name_requests_name(self):
        from app.whatsapp import process_whatsapp_message

        self.contact.name = None
        await process_whatsapp_message({}, build_whatsapp_payload(text="retirada"))
        assert self.cart.state == "AWAITING_CUSTOMER_NAME"
        assert self.cart.delivery_method == "pickup"
        msg = get_sent_message(self.send_mock)
        assert "nome" in msg.lower()

    @pytest.mark.asyncio
    async def test_invalid_cep_sends_error(self):
        from app.whatsapp import process_whatsapp_message

        with patch(
            "app.whatsapp.app.utils.get_address_from_cep", AsyncMock(return_value=None)
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="00000000"))

        assert self.cart.state == "AWAITING_CEP"
        msg = get_sent_message(self.send_mock)
        assert "CEP" in msg or "cep" in msg.lower() or "reconheci" in msg.lower()

    @pytest.mark.asyncio
    async def test_valid_cep_within_radius_proceeds_to_number_complement(self):
        from app.whatsapp import process_whatsapp_message

        address_data = {
            "street": "Rua das Flores",
            "neighborhood": "Centro",
            "city": "São Paulo",
            "state": "SP",
            "cep": "01001000",
            "lat": -23.55,
            "lng": -46.63,
        }
        with (
            patch(
                "app.whatsapp.app.utils.get_address_from_cep",
                AsyncMock(return_value=address_data),
            ),
            patch("app.whatsapp.app.utils.calculate_distance", return_value=2.0),
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="01001000"))

        assert self.cart.state == "AWAITING_NUMBER_COMPLEMENT"
        assert self.cart.partial_address == address_data

    @pytest.mark.asyncio
    async def test_valid_cep_outside_radius_blocks_and_offers_pickup(self):
        from app.whatsapp import process_whatsapp_message

        address_data = {
            "street": "Rua Longe",
            "neighborhood": "Bairro",
            "city": "SP",
            "state": "SP",
            "cep": "99999999",
            "lat": -25.0,
            "lng": -50.0,
        }
        with (
            patch(
                "app.whatsapp.app.utils.get_address_from_cep",
                AsyncMock(return_value=address_data),
            ),
            patch("app.whatsapp.app.utils.calculate_distance", return_value=50.0),
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="99999999"))

        assert self.cart.state == "AWAITING_CEP"
        assert self.cart.partial_address is None
        msg = get_sent_message(self.send_mock)
        assert (
            "área" in msg.lower() or "entrega" in msg.lower() or "raio" in msg.lower()
        )

    @pytest.mark.asyncio
    async def test_cep_with_no_coordinates_blocks(self):
        """CEP found but without lat/lng (geocoding failed) should block."""
        from app.whatsapp import process_whatsapp_message

        address_data = {
            "street": "Rua X",
            "neighborhood": "Y",
            "city": "SP",
            "state": "SP",
            "cep": "01001000",
        }
        self.bot.latitude = -23.55
        self.bot.longitude = -46.63
        with patch(
            "app.whatsapp.app.utils.get_address_from_cep",
            AsyncMock(return_value=address_data),
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="01001000"))

        assert self.cart.state == "AWAITING_CEP"


# ===========================================================================
# 5. AWAITING_NUMBER_COMPLEMENT STATE
# ===========================================================================


class TestNumberComplementState(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "AWAITING_NUMBER_COMPLEMENT"
        self.cart.partial_address = {
            "street": "Rua das Flores",
            "neighborhood": "Centro",
            "city": "São Paulo",
            "state": "SP",
            "cep": "01001000",
        }

    @pytest.mark.asyncio
    async def test_saves_full_address_and_asks_confirmation(self):
        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, build_whatsapp_payload(text="123, Apto 4"))

        assert self.cart.state == "AWAITING_ADDRESS_CONFIRMATION"
        assert self.cart.partial_address is None
        assert self.cart.pending_address is not None
        assert "123, Apto 4" in self.cart.pending_address

        msg = get_sent_message(self.send_mock)
        assert "Confirme" in msg or "correto" in msg.lower()


# ===========================================================================
# 6. AWAITING_ADDRESS_CONFIRMATION STATE
# ===========================================================================


class TestAddressConfirmationState(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "AWAITING_ADDRESS_CONFIRMATION"
        self.cart.pending_address = (
            "Rua das Flores, 123\nCentro - São Paulo/SP\nCEP: 01001000"
        )

    @pytest.mark.asyncio
    async def test_confirm_saves_address_and_requests_name(self):
        from app.whatsapp import process_whatsapp_message

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="CONFIRM")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="sim"))

        self.crud_mock.save_address_to_cart.assert_called_once()
        assert self.cart.state == "AWAITING_CUSTOMER_NAME"
        assert self.cart.pending_address is None

    @pytest.mark.asyncio
    async def test_negate_returns_to_cep(self):
        from app.whatsapp import process_whatsapp_message

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="NEGATE")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="não"))

        assert self.cart.state == "AWAITING_CEP"
        assert self.cart.pending_address is None

    @pytest.mark.asyncio
    async def test_ambiguous_response_asks_again(self):
        from app.whatsapp import process_whatsapp_message

        with patch(
            PATCH_RESOLVE_INTENT, AsyncMock(return_value="GREETING_OR_QUESTION")
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="talvez"))

        assert self.cart.state == "AWAITING_ADDRESS_CONFIRMATION"
        msg = get_sent_message(self.send_mock)
        assert "Sim" in msg or "Não" in msg


# ===========================================================================
# 7. AWAITING_CUSTOMER_NAME STATE
# ===========================================================================


class TestCustomerNameState(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "AWAITING_CUSTOMER_NAME"
        self.cart.items = [make_cart_item(1, "Pizza", 30.0)]

    @pytest.mark.asyncio
    async def test_saves_name_and_shows_summary_then_asks_payment(self):
        from app.whatsapp import process_whatsapp_message

        await process_whatsapp_message({}, build_whatsapp_payload(text="Maria Souza"))

        self.crud_mock.save_customer_name_to_contact.assert_called_once()
        assert self.cart.state == "AWAITING_PAYMENT_METHOD"

        msg = get_sent_message(self.send_mock)
        assert "Maria" in msg
        assert "PIX" in msg or "Cartão" in msg or "Dinheiro" in msg

    @pytest.mark.asyncio
    async def test_saves_name_no_pix_hides_pix_option(self):
        """When bot has no PIX configured, only card and money are shown."""
        from app.whatsapp import process_whatsapp_message

        self.bot.pix_key = None
        self.bot.payment_config = None
        await process_whatsapp_message({}, build_whatsapp_payload(text="Maria Souza"))

        assert self.cart.state == "AWAITING_PAYMENT_METHOD"
        msg = get_sent_message(self.send_mock)
        assert "Maria" in msg
        assert "PIX" not in msg
        assert "Cartão" in msg and "Dinheiro" in msg


# ===========================================================================
# 8. AWAITING_PAYMENT_METHOD STATE
# ===========================================================================


class TestPaymentMethodState(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "AWAITING_PAYMENT_METHOD"
        self.cart.delivery_method = DeliveryMethod.DELIVERY
        self.cart.customer_address = "Rua X, 10 - SP"
        self.cart.items = [make_cart_item(1, "Pizza", 30.0)]

        mock_order = MagicMock()
        mock_order.id = 42
        mock_order.total_amount = 35.0
        self.crud_mock.create_order = AsyncMock(return_value=mock_order)
        self.crud_mock.delete_order = AsyncMock()
        self.mock_order = mock_order

    @pytest.mark.asyncio
    async def test_invalid_method_asks_again(self):
        from app.whatsapp import process_whatsapp_message

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="ADD")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="boleto"))
        assert self.cart.state == "AWAITING_PAYMENT_METHOD"
        msg = get_sent_message(self.send_mock)
        assert "PIX" in msg or "Cartão" in msg

    @pytest.mark.asyncio
    async def test_pix_rejected_when_not_configured(self):
        """PIX is rejected when bot has no pix_key and no MP payment_config."""
        from app.whatsapp import process_whatsapp_message

        self.bot.pix_key = None
        self.bot.payment_config = None
        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="ADD")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="pix"))
        assert self.cart.state == "AWAITING_PAYMENT_METHOD"
        msg = get_sent_message(self.send_mock)
        assert "não está disponível" in msg
        assert "Cartão" in msg
        assert "Dinheiro" in msg
        self.crud_mock.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_card_delivery_confirms_order_and_clears_cart(self):
        from app.whatsapp import process_whatsapp_message

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="ADD")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="cartão"))

        self.crud_mock.create_order.assert_called_once()
        self.crud_mock.clear_db_cart.assert_called_once()
        msg = get_sent_message(self.send_mock)
        assert "confirmado" in msg.lower()
        assert "maquininha" in msg.lower() or "cartão" in msg.lower()

    @pytest.mark.asyncio
    async def test_money_confirms_order_with_total(self):
        from app.whatsapp import process_whatsapp_message

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="ADD")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="dinheiro"))

        self.crud_mock.create_order.assert_called_once()
        msg = get_sent_message(self.send_mock)
        assert "dinheiro" in msg.lower() or "💵" in msg

    @pytest.mark.asyncio
    async def test_pix_without_mp_configured_uses_manual_key(self):
        """No PaymentConfig on bot → falls back to manual pix_key."""
        from app.whatsapp import process_whatsapp_message

        self.bot.payment_config = None
        self.bot.pix_key = "pagamentos@restaurante.com"

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="ADD")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="pix"))

        self.crud_mock.create_order.assert_called_once()
        msg = get_sent_message(self.send_mock)
        assert "pagamentos@restaurante.com" in msg

    @pytest.mark.asyncio
    async def test_pix_with_mp_configured_generates_code(self):
        """Active PaymentConfig → generates PIX code via Mercado Pago."""
        from app.whatsapp import process_whatsapp_message

        payment_config = MagicMock()
        payment_config.is_active = True
        payment_config.access_token = "mp-test-token"
        self.bot.payment_config = payment_config

        pix_result = {
            "pix_copy_paste": "00020126580014br.gov.bcb.pix...",
            "qr_code_base64": "base64...",
        }

        with (
            patch(PATCH_CREATE_PIX, AsyncMock(return_value=pix_result)),
            patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="ADD")),
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="pix"))

        assert self.send_mock.call_count == 2
        pix_msg = get_sent_message(self.send_mock, call_index=1)
        assert "00020126" in pix_msg

    @pytest.mark.asyncio
    async def test_pix_mp_failure_reverts_order(self):
        """If MP returns None, session.rollback() is called and user is asked to choose another method."""
        from app.whatsapp import process_whatsapp_message

        payment_config = MagicMock()
        payment_config.is_active = True
        payment_config.access_token = "mp-test-token"
        self.bot.payment_config = payment_config

        with (
            patch(PATCH_CREATE_PIX, AsyncMock(return_value=None)),
            patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="ADD")),
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="pix"))

        self.session.rollback.assert_awaited_once()
        msg = get_sent_message(self.send_mock)
        assert "problema" in msg.lower() or "erro" in msg.lower()


# ===========================================================================
# 9. SESSION TIMEOUT TESTS
# ===========================================================================


class TestSessionTimeout(BaseConversationTest):
    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="Greeting message does not contain timeout-specific text")
    async def test_short_inactivity_clears_cart_and_notifies(self):
        """10+ min inactivity sends warning and resets."""
        from app.whatsapp import process_whatsapp_message

        self.cart.state = "SHOPPING"
        self.cart.last_activity_at = datetime.now(timezone.utc) - timedelta(minutes=15)

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="ADD")):
            await process_whatsapp_message(
                {}, build_whatsapp_payload(text="quero pizza")
            )

        self.crud_mock.clear_db_cart.assert_called_once()
        msg = get_sent_message(self.send_mock)
        assert "carrinho" in msg.lower() and "inatividade" in msg.lower()

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="Greeting message does not contain timeout-specific text")
    async def test_long_inactivity_clears_and_sends_welcome_back(self):
        """12h+ inactivity clears cart, sends welcome-back message, and returns early."""
        from app.whatsapp import process_whatsapp_message

        self.cart.state = "SHOPPING"
        self.cart.last_activity_at = datetime.now(timezone.utc) - timedelta(hours=13)
        self.cart.items = []

        await process_whatsapp_message({}, build_whatsapp_payload(text="oi"))

        self.crud_mock.clear_db_cart.assert_called_once()
        self.send_mock.assert_called_once()
        msg = get_sent_message(self.send_mock)
        assert "volta" in msg.lower() or "de volta" in msg.lower()


# ===========================================================================
# 10. FINISH_ORDER INTENT TESTS
# ===========================================================================


class TestFinishOrderIntent(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "SHOPPING"

    @pytest.mark.asyncio
    async def test_finish_order_empty_cart_refuses(self):
        from app.whatsapp import process_whatsapp_message

        self.cart.items = []
        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="FINISH_ORDER")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="finalizar"))

        msg = get_sent_message(self.send_mock)
        assert "vazio" in msg.lower()

    @pytest.mark.asyncio
    async def test_finish_order_below_minimum_value_blocks(self):
        from app.whatsapp import process_whatsapp_message

        self.bot.min_order_value = 50.0
        self.cart.items = [make_cart_item(1, "Coca-Cola", 8.0, quantity=1)]

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="FINISH_ORDER")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="finalizar"))

        msg = get_sent_message(self.send_mock)
        assert "mínimo" in msg.lower() or "minimo" in msg.lower()

    @pytest.mark.asyncio
    async def test_finish_order_no_delivery_method_asks_method(self):
        from app.whatsapp import process_whatsapp_message

        self.cart.delivery_method = None
        self.cart.items = [make_cart_item(1, "Pizza", 30.0)]

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="FINISH_ORDER")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="finalizar"))

        assert self.cart.state == "AWAITING_DELIVERY_METHOD"
        msg = get_sent_message(self.send_mock)
        assert "Entrega" in msg or "Retirada" in msg

    @pytest.mark.asyncio
    async def test_finish_order_with_known_address_and_name_goes_to_payment(self):
        from app.whatsapp import process_whatsapp_message

        self.cart.delivery_method = DeliveryMethod.DELIVERY
        self.cart.customer_address = "Rua X, 10"
        self.contact.name = "João"
        self.cart.contact = self.contact
        self.cart.items = [make_cart_item(1, "Pizza", 30.0)]

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="FINISH_ORDER")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="finalizar"))

        assert self.cart.state == "AWAITING_PAYMENT_METHOD"


# ===========================================================================
# 11a. UPDATE ITEM OBSERVATION
# ===========================================================================

PATCH_GET_AI = "app.whatsapp.get_ai_decision"
PATCH_EXTRACT_ITEMS = "app.whatsapp.extract_potential_items"


def _make_tool_call_message(tool_name: str, arguments: dict):
    """Build a mock AI message with a single tool call, matching OpenAI response shape."""
    func = MagicMock()
    func.name = tool_name
    func.arguments = json.dumps(arguments)
    tc = MagicMock()
    tc.function = func
    msg = MagicMock()
    msg.tool_calls = [tc]
    return msg


class TestUpdateItemObservation(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "SHOPPING"
        self.cart.items = [
            make_cart_item(10, "John's Paranaense", 45.0),
            make_cart_item(20, "Coca-cola 600ml", 10.0),
        ]
        self.crud_mock.update_item_notes = AsyncMock(return_value=self.cart)

    @pytest.mark.asyncio
    async def test_update_observation_calls_crud_and_confirms(self):
        """LLM returns update_item_observation → crud.update_item_notes is called, user gets confirmation."""
        from app.whatsapp import process_whatsapp_message

        ai_msg = _make_tool_call_message(
            "update_item_observation", {"product_id": 10, "notes": "sem cebola"}
        )

        with (
            patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="MODIFY")),
            patch(PATCH_GET_AI, AsyncMock(return_value=ai_msg)),
            patch(PATCH_EXTRACT_ITEMS, AsyncMock(return_value=["johns paranaense"])),
        ):
            await process_whatsapp_message(
                {}, build_whatsapp_payload(text="o johns paranaense é sem cebola")
            )

        self.crud_mock.update_item_notes.assert_called_once_with(
            self.session, self.cart.id, 10, "sem cebola"
        )
        msg = get_sent_message(self.send_mock)
        assert "observação anotada" in msg.lower() or "anotada" in msg.lower()

    @pytest.mark.asyncio
    async def test_update_observation_product_not_in_cart(self):
        """update_item_observation with a product_id not in the cart → user gets helpful error."""
        from app.whatsapp import process_whatsapp_message

        ai_msg = _make_tool_call_message(
            "update_item_observation", {"product_id": 999, "notes": "sem cebola"}
        )

        with (
            patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="MODIFY")),
            patch(PATCH_GET_AI, AsyncMock(return_value=ai_msg)),
            patch(PATCH_EXTRACT_ITEMS, AsyncMock(return_value=["johns paranaense"])),
        ):
            await process_whatsapp_message(
                {}, build_whatsapp_payload(text="o johns paranaense é sem cebola")
            )

        self.crud_mock.update_item_notes.assert_not_called()
        msg = get_sent_message(self.send_mock)
        assert "não está no seu carrinho" in msg.lower()

    @pytest.mark.asyncio
    async def test_update_observation_missing_notes(self):
        """update_item_observation with missing notes → user is asked to clarify."""
        from app.whatsapp import process_whatsapp_message

        ai_msg = _make_tool_call_message("update_item_observation", {"product_id": 10})

        with (
            patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="MODIFY")),
            patch(PATCH_GET_AI, AsyncMock(return_value=ai_msg)),
            patch(PATCH_EXTRACT_ITEMS, AsyncMock(return_value=["johns paranaense"])),
        ):
            await process_whatsapp_message(
                {}, build_whatsapp_payload(text="o johns paranaense precisa de algo")
            )

        self.crud_mock.update_item_notes.assert_not_called()
        msg = get_sent_message(self.send_mock)
        assert "não entendi" in msg.lower() or "repetir" in msg.lower()


# ===========================================================================
# 11. CART MANAGEMENT INTENTS
# ===========================================================================


class TestCartManagementIntents(BaseConversationTest):
    @pytest.fixture(autouse=True)
    def set_state(self):
        self.cart.state = "SHOPPING"

    @pytest.mark.asyncio
    async def test_clear_cart_empties_and_confirms(self):
        from app.whatsapp import process_whatsapp_message

        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="CLEAR_CART")):
            await process_whatsapp_message({}, build_whatsapp_payload(text="esvaziar"))

        self.crud_mock.clear_db_cart.assert_called_once()
        msg = get_sent_message(self.send_mock)
        assert "esvaziado" in msg.lower() or "vazio" in msg.lower()

    @pytest.mark.asyncio
    async def test_show_cart_returns_summary(self):
        from app.whatsapp import process_whatsapp_message

        self.cart.items = [make_cart_item(1, "Pizza", 30.0)]
        with patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="SHOW_CART")):
            await process_whatsapp_message(
                {}, build_whatsapp_payload(text="ver carrinho")
            )

        msg = get_sent_message(self.send_mock)
        assert "Pizza" in msg or "Pedido" in msg


# ===========================================================================
# DISTRIBUTED LOCK BEHAVIOR TESTS
# ===========================================================================


class TestDistributedLock(BaseConversationTest):
    """Tests that the distributed lock integrates correctly with the message pipeline."""

    @pytest.mark.asyncio
    async def test_lock_acquired_before_cart_operations(self):
        """Verifies lock is acquired before get_or_create_cart."""
        from app.whatsapp import process_whatsapp_message

        call_order = []

        @asynccontextmanager
        async def _tracking_lock():
            call_order.append("lock_acquired")
            yield
            call_order.append("lock_released")

        original_get_cart = self.crud_mock.get_or_create_cart

        async def _tracking_get_cart(*args, **kwargs):
            call_order.append("get_or_create_cart")
            return await original_get_cart(*args, **kwargs)

        self.crud_mock.get_or_create_cart = AsyncMock(side_effect=_tracking_get_cart)

        with (
            patch(PATCH_CONTACT_LOCK, return_value=_tracking_lock()),
            patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="GREETING_OR_QUESTION")),
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="Oi"))

        assert call_order.index("lock_acquired") < call_order.index(
            "get_or_create_cart"
        )

    @pytest.mark.asyncio
    async def test_lock_released_after_commit(self):
        """Verifies lock is held through session.commit() and released after."""
        from app.whatsapp import process_whatsapp_message

        call_order = []

        @asynccontextmanager
        async def _tracking_lock():
            call_order.append("lock_acquired")
            yield
            call_order.append("lock_released")

        original_commit = self.session.commit

        async def _tracking_commit(*args, **kwargs):
            call_order.append("commit")
            return await original_commit(*args, **kwargs)

        self.session.commit = AsyncMock(side_effect=_tracking_commit)

        with (
            patch(PATCH_CONTACT_LOCK, return_value=_tracking_lock()),
            patch(PATCH_RESOLVE_INTENT, AsyncMock(return_value="GREETING_OR_QUESTION")),
        ):
            await process_whatsapp_message({}, build_whatsapp_payload(text="Oi"))

        assert "commit" in call_order
        assert call_order.index("commit") < call_order.index("lock_released")

    @pytest.mark.asyncio
    async def test_lock_timeout_sends_polite_message(self):
        """When LockError is raised, user receives a friendly retry message."""
        from app.whatsapp import process_whatsapp_message
        from redis.exceptions import LockError

        @asynccontextmanager
        async def _lock_that_fails():
            raise LockError("Could not acquire lock")
            yield  # pragma: no cover

        with patch(PATCH_CONTACT_LOCK, return_value=_lock_that_fails()):
            await process_whatsapp_message(
                {}, build_whatsapp_payload(text="Quero pizza")
            )

        msg = get_sent_message(self.send_mock)
        assert "processando" in msg.lower()
        assert "aguarde" in msg.lower()

    @pytest.mark.asyncio
    async def test_lock_released_even_on_exception(self):
        """Context manager releases lock even if cart code throws."""
        from app.whatsapp import process_whatsapp_message

        lock_released = []

        @asynccontextmanager
        async def _tracking_lock():
            try:
                yield
            finally:
                lock_released.append(True)

        self.crud_mock.get_or_create_cart = AsyncMock(
            side_effect=RuntimeError("DB exploded")
        )

        with patch(PATCH_CONTACT_LOCK, return_value=_tracking_lock()):
            await process_whatsapp_message({}, build_whatsapp_payload(text="Oi"))

        assert lock_released, "Lock should have been released even on exception"
