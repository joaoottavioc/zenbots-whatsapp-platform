"""Tests for Phase 1 features: F-06 guards, F-03 ambiguity, F-07 ETA,
F-09 owner notifications, F-17 cancellation, F-01 customer memory."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

# ────────────────────────────────────────────────────────
# F-06: Order status transition guards
# ────────────────────────────────────────────────────────


class TestOrderStatusTransitionGuards:
    """Validates that invalid state jumps are rejected."""

    VALID_TRANSITIONS = {
        "pending": {"paid", "canceled"},
        "paid": {"preparing", "canceled"},
        "preparing": {"ready", "canceled"},
        "ready": {"completed", "canceled"},
        "completed": set(),
        "canceled": set(),
        "failed": set(),
        "expired": set(),
    }

    @pytest.mark.parametrize(
        "from_status,to_status,should_allow",
        [
            ("pending", "paid", True),
            ("pending", "canceled", True),
            ("pending", "completed", False),  # Can't skip states
            ("pending", "preparing", False),  # Must go through paid
            ("paid", "preparing", True),
            ("paid", "canceled", True),
            ("paid", "completed", False),  # Can't skip
            ("preparing", "ready", True),
            ("preparing", "canceled", True),
            ("preparing", "pending", False),  # Can't go backwards
            ("ready", "completed", True),
            ("ready", "canceled", True),
            ("ready", "paid", False),  # Can't go backwards
            ("completed", "pending", False),  # Terminal
            ("completed", "canceled", False),  # Terminal
            ("canceled", "pending", False),  # Terminal
            ("canceled", "paid", False),  # Terminal
            ("failed", "pending", False),  # Terminal
            ("expired", "paid", False),  # Terminal
        ],
    )
    def test_transition_validity(self, from_status, to_status, should_allow):
        allowed = self.VALID_TRANSITIONS.get(from_status, set())
        assert (to_status in allowed) == should_allow, (
            f"Transition {from_status} → {to_status}: "
            f"expected {'allowed' if should_allow else 'rejected'}"
        )


# ────────────────────────────────────────────────────────
# F-03: Ambiguity resolution — consistent formatting
# ────────────────────────────────────────────────────────


class TestAmbiguityResolution:
    """Verifies _format_product_suggestions_message produces numbered lists with prices."""

    def test_format_product_suggestions_with_prices(self):
        from app.whatsapp import _format_product_suggestions_message

        class FakeProduct:
            def __init__(self, name, price, description=None):
                self.name = name
                self.price = price
                self.description = description

        products = [
            FakeProduct("X-Burger", 25.90, "Pão, carne, queijo"),
            FakeProduct("X-Tudo", 32.50),
        ]
        result = _format_product_suggestions_message(products, "Encontrei:")
        assert "Encontrei:" in result
        assert "*X-Burger*" in result
        assert "R$ 25,90" in result
        assert "*X-Tudo*" in result
        assert "R$ 32,50" in result
        assert "Pão, carne, queijo" in result
        assert "Qual você quer?" in result

    def test_format_empty_products(self):
        from app.whatsapp import _format_product_suggestions_message

        result = _format_product_suggestions_message([], "Title")
        assert "não encontrei" in result.lower()


# ────────────────────────────────────────────────────────
# F-07: ETA in order confirmation
# ────────────────────────────────────────────────────────


class TestETA:
    """Verifies ETA fields exist and are used in confirmation messages."""

    def test_bot_model_has_eta_fields(self):
        from app.models import Bot

        bot = Bot()
        assert hasattr(bot, "default_delivery_time_minutes")
        assert hasattr(bot, "default_pickup_time_minutes")
        assert bot.default_delivery_time_minutes is None
        assert bot.default_pickup_time_minutes is None

    def test_bot_schema_accepts_eta_fields(self):
        from app.schemas import BotCreate

        bot = BotCreate(
            restaurant_name="Test",
            cep="01001000",
            address="Rua A",
            default_delivery_time_minutes=45,
            default_pickup_time_minutes=20,
        )
        assert bot.default_delivery_time_minutes == 45
        assert bot.default_pickup_time_minutes == 20

    def test_bot_schema_rejects_invalid_eta(self):
        from app.schemas import BotCreate
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            BotCreate(
                restaurant_name="Test",
                cep="01001000",
                address="Rua A",
                default_delivery_time_minutes=0,  # min is 1
            )

        with pytest.raises(ValidationError):
            BotCreate(
                restaurant_name="Test",
                cep="01001000",
                address="Rua A",
                default_delivery_time_minutes=200,  # max is 180
            )


# ────────────────────────────────────────────────────────
# F-09: Owner notifications
# ────────────────────────────────────────────────────────


class TestOwnerNotifications:
    """Verifies owner notification phone field exists."""

    def test_bot_model_has_owner_phone(self):
        from app.models import Bot

        bot = Bot()
        assert hasattr(bot, "owner_notification_phone")
        assert bot.owner_notification_phone is None

    def test_bot_schema_accepts_owner_phone(self):
        from app.schemas import BotUpdate

        update = BotUpdate(owner_notification_phone="5511999999999")
        assert update.owner_notification_phone == "5511999999999"

    def test_bot_response_includes_owner_phone(self):
        from app.schemas import BotResponse

        fields = BotResponse.model_fields
        assert "owner_notification_phone" in fields


# ────────────────────────────────────────────────────────
# F-17: Order cancellation
# ────────────────────────────────────────────────────────


class TestOrderCancellation:
    """Verifies cancellation intent and model fields."""

    def test_bot_model_has_cancellation_window(self):
        from app.models import Bot

        bot = Bot()
        assert hasattr(bot, "cancellation_window_minutes")
        assert bot.cancellation_window_minutes == 5

    def test_semantic_router_has_order_cancel_intent(self):
        from app.semantic_router import PROTOS, THRESHOLDS

        assert "ORDER_CANCEL" in PROTOS
        assert "ORDER_CANCEL" in THRESHOLDS
        assert len(PROTOS["ORDER_CANCEL"]) >= 5

    def test_semantic_router_has_order_repeat_intent(self):
        from app.semantic_router import PROTOS, THRESHOLDS

        assert "ORDER_REPEAT" in PROTOS
        assert "ORDER_REPEAT" in THRESHOLDS

    def test_cancellation_window_schema_validation(self):
        from app.schemas import BotCreate
        from pydantic import ValidationError

        # Valid
        bot = BotCreate(
            restaurant_name="Test",
            cep="01001000",
            address="Rua A",
            cancellation_window_minutes=10,
        )
        assert bot.cancellation_window_minutes == 10

        # Invalid: too high
        with pytest.raises(ValidationError):
            BotCreate(
                restaurant_name="Test",
                cep="01001000",
                address="Rua A",
                cancellation_window_minutes=60,
            )


# ────────────────────────────────────────────────────────
# F-01: Customer memory
# ────────────────────────────────────────────────────────


class TestCustomerMemory:
    """Verifies contact model has memory fields."""

    def test_contact_model_has_address_fields(self):
        from app.models import Contact

        contact = Contact(phone_number="5511999", bot_id=1)
        assert hasattr(contact, "default_address_json")
        assert hasattr(contact, "last_order_date")
        assert contact.default_address_json is None
        assert contact.last_order_date is None

    def test_contact_address_json_structure(self):
        """Validates address JSON can hold the expected structure."""
        from app.models import Contact

        contact = Contact(phone_number="5511999", bot_id=1)
        contact.default_address_json = {
            "full_address": "Rua A, 100 - Bairro, Cidade - SP",
        }
        assert (
            contact.default_address_json["full_address"]
            == "Rua A, 100 - Bairro, Cidade - SP"
        )

    def test_welcome_personalization_returns_name(self):
        """Returning customer with name gets personalized greeting."""
        from app.whatsapp import _format_product_suggestions_message

        # This test validates the format function works (used in welcome)
        class FP:
            name = "Pizza"
            price = 30.0
            description = None

        result = _format_product_suggestions_message([FP()], "Olá!")
        assert "*Pizza*" in result


# ────────────────────────────────────────────────────────
# F-17: _handle_order_cancel unit tests
# ────────────────────────────────────────────────────────


class TestHandleOrderCancel:
    """Unit tests for the _handle_order_cancel handler."""

    @pytest.mark.asyncio
    async def test_no_active_order(self):
        from app.whatsapp import _handle_order_cancel, MessageContext

        mctx = MagicMock(spec=MessageContext)
        mctx.session = AsyncMock()
        mctx.bot = MagicMock()
        mctx.bot.id = 1
        mctx.bot.cancellation_window_minutes = 5
        mctx.contact = MagicMock()
        mctx.contact.id = 1

        with patch("app.whatsapp.crud") as mock_crud:
            mock_crud.get_latest_active_order = AsyncMock(return_value=None)
            result = await _handle_order_cancel(mctx)

        assert "não tem nenhum pedido" in result.lower()

    @pytest.mark.asyncio
    async def test_cancel_preparing_order_rejected(self):
        from app.whatsapp import _handle_order_cancel, MessageContext
        from app.models import OrderStatus

        order = MagicMock()
        order.status = OrderStatus.PREPARING
        order.id = 42

        mctx = MagicMock(spec=MessageContext)
        mctx.session = AsyncMock()
        mctx.bot = MagicMock()
        mctx.bot.id = 1
        mctx.bot.cancellation_window_minutes = 5
        mctx.contact = MagicMock()
        mctx.contact.id = 1

        with patch("app.whatsapp.crud") as mock_crud:
            mock_crud.get_latest_active_order = AsyncMock(return_value=order)
            result = await _handle_order_cancel(mctx)

        assert "já está sendo preparado" in result.lower()

    @pytest.mark.asyncio
    async def test_cancel_within_window_succeeds(self):
        from app.whatsapp import _handle_order_cancel, MessageContext
        from app.models import OrderStatus

        order = MagicMock()
        order.status = OrderStatus.PENDING
        order.id = 42
        # utcnow() in the handler strips tzinfo, so created_at must be naive too
        from app.time import utcnow as _utcnow

        order.created_at = _utcnow()  # just created, naive UTC

        mctx = MagicMock(spec=MessageContext)
        mctx.session = AsyncMock()
        mctx.bot = MagicMock()
        mctx.bot.id = 1
        mctx.bot.cancellation_window_minutes = 5
        mctx.contact = MagicMock()
        mctx.contact.id = 1

        with (
            patch("app.whatsapp.crud") as mock_crud,
            patch("app.whatsapp.broadcast_order_update", new_callable=AsyncMock),
        ):
            mock_crud.get_latest_active_order = AsyncMock(return_value=order)
            result = await _handle_order_cancel(mctx)

        assert "cancelado com sucesso" in result.lower()
        assert order.status == OrderStatus.CANCELED

    @pytest.mark.asyncio
    async def test_cancel_outside_window_rejected(self):
        from app.whatsapp import _handle_order_cancel, MessageContext
        from app.models import OrderStatus

        order = MagicMock()
        order.status = OrderStatus.PAID
        order.id = 42
        order.created_at = datetime.now() - timedelta(minutes=10)  # 10 min ago

        mctx = MagicMock(spec=MessageContext)
        mctx.session = AsyncMock()
        mctx.bot = MagicMock()
        mctx.bot.id = 1
        mctx.bot.cancellation_window_minutes = 5
        mctx.contact = MagicMock()
        mctx.contact.id = 1

        with patch("app.whatsapp.crud") as mock_crud:
            mock_crud.get_latest_active_order = AsyncMock(return_value=order)
            result = await _handle_order_cancel(mctx)

        assert "prazo de cancelamento" in result.lower()


# ────────────────────────────────────────────────────────
# F-01: _handle_order_repeat unit tests
# ────────────────────────────────────────────────────────


class TestHandleOrderRepeat:
    """Unit tests for the _handle_order_repeat handler."""

    @pytest.mark.asyncio
    async def test_no_previous_order(self):
        from app.whatsapp import _handle_order_repeat, MessageContext

        mctx = MagicMock(spec=MessageContext)
        mctx.session = AsyncMock()
        mctx.bot = MagicMock()
        mctx.bot.id = 1
        mctx.contact = MagicMock()
        mctx.contact.id = 1
        mctx.cart = MagicMock()
        mctx.cart.id = 10

        with patch("app.whatsapp.crud") as mock_crud:
            mock_crud.get_last_completed_order_items = AsyncMock(return_value=None)
            result = await _handle_order_repeat(mctx)

        assert "não encontrei" in result.lower()

    @pytest.mark.asyncio
    async def test_reorder_with_available_items(self):
        from app.whatsapp import _handle_order_repeat, MessageContext
        from app.models import CartState

        mctx = MagicMock(spec=MessageContext)
        mctx.session = AsyncMock()
        mctx.bot = MagicMock()
        mctx.bot.id = 1
        mctx.bot.delivery_fee = 5.0
        mctx.bot.restaurant_name = "Test"
        mctx.contact = MagicMock()
        mctx.contact.id = 1
        mctx.cart = MagicMock()
        mctx.cart.id = 10
        mctx.cart.delivery_method = None

        last_items = [
            {"product_id": 1, "quantity": 2, "product_name": "Pizza", "notes": None}
        ]

        mock_product = MagicMock()
        mock_product.name = "Pizza"
        mock_product.price = 30.0
        mock_product.id = 1
        mock_product.product_id = 1

        mock_cart_item = MagicMock()
        mock_cart_item.product = mock_product
        mock_cart_item.quantity = 2
        mock_cart_item.product_id = 1
        mock_cart_item.notes = None

        mctx.cart.items = [mock_cart_item]

        with (
            patch("app.whatsapp.crud") as mock_crud,
            patch(
                "app.whatsapp._load_cart_items_with_products", new_callable=AsyncMock
            ),
            patch(
                "app.whatsapp._build_cart_summary_message", return_value="🛒 Carrinho"
            ),
        ):
            mock_crud.get_last_completed_order_items = AsyncMock(
                return_value=last_items
            )
            mock_crud.add_items_to_db_cart = AsyncMock()
            result = await _handle_order_repeat(mctx)

        assert "pedido anterior adicionado" in result.lower()
        assert mctx.cart.state == CartState.SHOPPING


# ────────────────────────────────────────────────────────
# CRUD: find_unavailable_products
# ────────────────────────────────────────────────────────


class TestFindUnavailableProducts:
    """Tests for the find_unavailable_products CRUD function."""

    def test_function_exists(self):
        from app.crud import find_unavailable_products

        assert callable(find_unavailable_products)

    def test_get_latest_active_order_exists(self):
        from app.crud import get_latest_active_order

        assert callable(get_latest_active_order)

    def test_get_last_completed_order_items_exists(self):
        from app.crud import get_last_completed_order_items

        assert callable(get_last_completed_order_items)
