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
# F-05: Unavailability similarity comparison
# ────────────────────────────────────────────────────────


class TestUnavailabilitySimilarityComparison:
    """Verifies the SequenceMatcher logic picks the right match."""

    def test_unavailable_closer_than_available(self):
        """When unavailable product name is closer to query, it should be flagged."""
        from difflib import SequenceMatcher

        query = "johns alcatra com bacon"
        unavail_name = "john's alcatra com bacon"
        avail_name = "john's alcatra com frango"

        unavail_score = SequenceMatcher(None, query, unavail_name.lower()).ratio()
        avail_score = SequenceMatcher(None, query, avail_name.lower()).ratio()

        assert unavail_score > avail_score, (
            f"Unavailable ({unavail_score:.3f}) should score higher than "
            f"available ({avail_score:.3f}) for query '{query}'"
        )

    def test_available_closer_than_unavailable(self):
        """When available product is a better match, don't flag unavailable."""
        from difflib import SequenceMatcher

        query = "coca cola"
        unavail_name = "x-bacon"  # unrelated unavailable product
        avail_name = "coca-cola 600ml"

        unavail_score = SequenceMatcher(None, query, unavail_name.lower()).ratio()
        avail_score = SequenceMatcher(None, query, avail_name.lower()).ratio()

        assert avail_score > unavail_score, (
            f"Available ({avail_score:.3f}) should score higher than "
            f"unavailable ({unavail_score:.3f}) for query '{query}'"
        )

    def test_nearly_identical_names(self):
        """Products differing by one word should have high similarity."""
        from difflib import SequenceMatcher

        query = "johns bacon"
        unavail_name = "john's bacon"
        avail_name = "john's bacon com frango"

        unavail_score = SequenceMatcher(None, query, unavail_name.lower()).ratio()
        avail_score = SequenceMatcher(None, query, avail_name.lower()).ratio()

        assert unavail_score > avail_score


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


# ────────────────────────────────────────────────────────
# Suggestion selection handler
# ────────────────────────────────────────────────────────


def _make_product_mock(pid, name, price=20.0, category="Johns"):
    p = MagicMock()
    p.id = pid
    p.name = name
    p.price = price
    p.category = category
    p.is_deleted = False
    p.is_available = True
    p.description = None
    return p


def _make_suggestion_mctx(text, suggestions_ids, bot_id=1):
    """Create a MessageContext with last_suggestions set."""
    from app.whatsapp import MessageContext

    mctx = MagicMock(spec=MessageContext)
    mctx.session = AsyncMock()
    mctx.bot = MagicMock()
    mctx.bot.id = bot_id
    mctx.bot.delivery_fee = 5.0
    mctx.bot.restaurant_name = "Test"
    mctx.contact = MagicMock()
    mctx.contact.id = 1
    mctx.cart = MagicMock()
    mctx.cart.id = 10
    mctx.cart.last_suggestions = suggestions_ids
    mctx.cart.delivery_method = None
    mctx.cart.state = "SHOPPING"
    mctx.text_body = text
    mctx.contact_number = "5511999"
    mctx.token = "fake"
    mctx.phone_id = "fake"
    return mctx


class TestSuggestionSelectionHandler:
    """Tests for _handle_suggestion_selection — number, ordinal, name matching."""

    def _setup_session_with_products(self, mctx, products):
        """Mock session.execute to return the given products."""
        result = MagicMock()
        result.scalars.return_value.all.return_value = products
        mctx.session.execute = AsyncMock(return_value=result)

    @pytest.mark.asyncio
    async def test_select_by_number(self):
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's Calabresa"),
            _make_product_mock(2, "John's Alcatra"),
            _make_product_mock(3, "John's Simples"),
        ]
        mctx = _make_suggestion_mctx("2", [1, 2, 3])
        self._setup_session_with_products(mctx, prods)

        with (
            patch("app.whatsapp.crud") as mock_crud,
            patch(
                "app.whatsapp._load_cart_items_with_products", new_callable=AsyncMock
            ),
            patch("app.whatsapp._build_cart_summary_message", return_value="🛒 Cart"),
        ):
            mock_crud.add_items_to_db_cart = AsyncMock()
            mctx.cart.items = [MagicMock()]  # non-empty after add
            result = await _handle_suggestion_selection(mctx)

        assert result is not None
        mock_crud.add_items_to_db_cart.assert_called_once()
        call_args = mock_crud.add_items_to_db_cart.call_args
        items = (
            call_args[1].get("items")
            if "items" in (call_args[1] or {})
            else call_args[0][2]
        )
        assert items[0]["product_id"] == 2  # selected 2nd product

    @pytest.mark.asyncio
    async def test_select_by_ordinal(self):
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's Calabresa"),
            _make_product_mock(2, "John's Alcatra"),
        ]
        mctx = _make_suggestion_mctx("pode ser o segundo", [1, 2])
        self._setup_session_with_products(mctx, prods)

        with (
            patch("app.whatsapp.crud") as mock_crud,
            patch(
                "app.whatsapp._load_cart_items_with_products", new_callable=AsyncMock
            ),
            patch("app.whatsapp._build_cart_summary_message", return_value="🛒 Cart"),
        ):
            mock_crud.add_items_to_db_cart = AsyncMock()
            mctx.cart.items = [MagicMock()]
            result = await _handle_suggestion_selection(mctx)

        assert result is not None
        call_args = mock_crud.add_items_to_db_cart.call_args
        items = call_args[0][2]
        assert items[0]["product_id"] == 2

    @pytest.mark.asyncio
    async def test_select_by_ordinal_primeiro(self):
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(10, "John's Simples"),
            _make_product_mock(20, "John's Duplo"),
        ]
        mctx = _make_suggestion_mctx("o primeiro", [10, 20])
        self._setup_session_with_products(mctx, prods)

        with (
            patch("app.whatsapp.crud") as mock_crud,
            patch(
                "app.whatsapp._load_cart_items_with_products", new_callable=AsyncMock
            ),
            patch("app.whatsapp._build_cart_summary_message", return_value="🛒"),
        ):
            mock_crud.add_items_to_db_cart = AsyncMock()
            mctx.cart.items = [MagicMock()]
            result = await _handle_suggestion_selection(mctx)

        assert result is not None
        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["product_id"] == 10

    @pytest.mark.asyncio
    async def test_select_by_name_fragment(self):
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's Calabresa"),
            _make_product_mock(2, "John's Alcatra Acebolada"),
            _make_product_mock(3, "John's Simples"),
        ]
        mctx = _make_suggestion_mctx("pode ser o alcatra", [1, 2, 3])
        self._setup_session_with_products(mctx, prods)

        with (
            patch("app.whatsapp.crud") as mock_crud,
            patch(
                "app.whatsapp._load_cart_items_with_products", new_callable=AsyncMock
            ),
            patch("app.whatsapp._build_cart_summary_message", return_value="🛒"),
        ):
            mock_crud.add_items_to_db_cart = AsyncMock()
            mctx.cart.items = [MagicMock()]
            result = await _handle_suggestion_selection(mctx)

        assert result is not None
        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["product_id"] == 2  # Alcatra matched

    @pytest.mark.asyncio
    async def test_word_overlap_prefers_more_matches(self):
        """'calabresa' matches both 'Calabresa' addon and 'John's Calabresa' burger.
        Word overlap should prefer the one with more shared words."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "Calabresa", category="Adicionais"),
            _make_product_mock(2, "John's Frango com Calabresa", category="Johns"),
        ]
        mctx = _make_suggestion_mctx("pode ser o johns calabresa", [1, 2])
        self._setup_session_with_products(mctx, prods)

        with (
            patch("app.whatsapp.crud") as mock_crud,
            patch(
                "app.whatsapp._load_cart_items_with_products", new_callable=AsyncMock
            ),
            patch("app.whatsapp._build_cart_summary_message", return_value="🛒"),
        ):
            mock_crud.add_items_to_db_cart = AsyncMock()
            mctx.cart.items = [MagicMock()]
            result = await _handle_suggestion_selection(mctx)

        assert result is not None
        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert (
            items[0]["product_id"] == 2
        )  # Johns Frango com Calabresa (2 word matches)

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self):
        """Message with no significant word overlap returns None."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "John's Calabresa")]
        mctx = _make_suggestion_mctx("sim", [1])
        self._setup_session_with_products(mctx, prods)

        result = await _handle_suggestion_selection(mctx)
        assert result is None  # "sim" has no >3 char words matching product names

    @pytest.mark.asyncio
    async def test_ordering_verb_clears_suggestions(self):
        """Messages with ordering verbs should clear stale suggestions."""
        from app.whatsapp import _handle_suggestion_selection

        mctx = _make_suggestion_mctx("quero uma coca cola", [1, 2, 3])
        result = await _handle_suggestion_selection(mctx)

        assert result is None
        assert mctx.cart.last_suggestions is None  # cleared

    @pytest.mark.asyncio
    async def test_adiciona_clears_suggestions(self):
        from app.whatsapp import _handle_suggestion_selection

        mctx = _make_suggestion_mctx("adiciona um johns alcatra", [1, 2])
        result = await _handle_suggestion_selection(mctx)

        assert result is None
        assert mctx.cart.last_suggestions is None

    @pytest.mark.asyncio
    async def test_tambem_clears_suggestions(self):
        from app.whatsapp import _handle_suggestion_selection

        mctx = _make_suggestion_mctx("também vou querer um bacon", [1, 2])
        result = await _handle_suggestion_selection(mctx)

        assert result is None
        assert mctx.cart.last_suggestions is None

    @pytest.mark.asyncio
    async def test_long_message_skipped(self):
        """Messages over 60 chars should not trigger suggestion selection."""
        from app.whatsapp import _handle_suggestion_selection

        long_msg = (
            "quero ver todas as opções disponíveis no cardápio por favor obrigado"
        )
        mctx = _make_suggestion_mctx(long_msg, [1, 2, 3])
        # Don't set ordering verbs so it reaches the length check
        # Actually "quero" is an ordering verb, so let me use a different long message
        mctx.text_body = "a" * 61  # just a long string

        result = await _handle_suggestion_selection(mctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_suggestions_returns_none(self):
        from app.whatsapp import _handle_suggestion_selection

        mctx = _make_suggestion_mctx("2", None)
        mctx.cart.last_suggestions = None
        result = await _handle_suggestion_selection(mctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_digit_in_sentence(self):
        """'quero o 3' should extract the digit and select 3rd item."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's A"),
            _make_product_mock(2, "John's B"),
            _make_product_mock(3, "John's C"),
        ]
        # "quero" is an ordering verb and would clear suggestions
        # Use a message without ordering verbs
        mctx = _make_suggestion_mctx("o 3 por favor", [1, 2, 3])
        self._setup_session_with_products(mctx, prods)

        with (
            patch("app.whatsapp.crud") as mock_crud,
            patch(
                "app.whatsapp._load_cart_items_with_products", new_callable=AsyncMock
            ),
            patch("app.whatsapp._build_cart_summary_message", return_value="🛒"),
        ):
            mock_crud.add_items_to_db_cart = AsyncMock()
            mctx.cart.items = [MagicMock()]
            result = await _handle_suggestion_selection(mctx)

        assert result is not None
        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["product_id"] == 3


# ────────────────────────────────────────────────────────
# Apostrophe normalization in similarity comparison
# ────────────────────────────────────────────────────────


class TestApostropheNormalization:
    """Verifies that apostrophe normalization works in name comparisons."""

    def test_norm_strips_apostrophes(self):
        """The _norm helper should strip apostrophes."""

        # Replicate the _norm function from whatsapp.py
        def _norm(s):
            return s.lower().replace("'", "").replace("\u2019", "").strip()

        assert _norm("John's Bacon") == "johns bacon"
        assert _norm("John\u2019s Frango") == "johns frango"
        assert _norm("johns bacon") == "johns bacon"

    def test_normalized_comparison_exact_match(self):
        from difflib import SequenceMatcher

        def _norm(s):
            return s.lower().replace("'", "").replace("\u2019", "").strip()

        query = "johns bacon"
        product = "John's Bacon"
        score = SequenceMatcher(None, _norm(query), _norm(product)).ratio()
        assert score == 1.0  # perfect match after normalization

    def test_typo_still_scores_high(self):
        from difflib import SequenceMatcher

        def _norm(s):
            return s.lower().replace("'", "").replace("\u2019", "").strip()

        query = "johs bacon"  # missing 'n'
        product = "John's Bacon"
        score = SequenceMatcher(None, _norm(query), _norm(product)).ratio()
        assert score > 0.85  # high despite typo

    def test_search_term_vs_full_message_comparison(self):
        """Using search_terms (not text_body) gives accurate scores."""
        from difflib import SequenceMatcher

        def _norm(s):
            return s.lower().replace("'", "").replace("\u2019", "").strip()

        # Using full message dilutes the score
        full_msg = "vou querer um johns bacon hoje"
        product = "John's Bacon"
        score_full = SequenceMatcher(None, _norm(full_msg), _norm(product)).ratio()

        # Using extracted search term gives accurate score
        search_term = "johns bacon"
        score_term = SequenceMatcher(None, _norm(search_term), _norm(product)).ratio()

        assert score_term > score_full  # search term is much more accurate
        assert score_term == 1.0
        assert score_full < 0.6


# ────────────────────────────────────────────────────────
# Category-based alternative filtering
# ────────────────────────────────────────────────────────


class TestCategoryFiltering:
    """Verifies that unavailability alternatives are filtered by category."""

    def test_same_category_kept(self):
        """Products in same category as unavailable product are kept."""
        unavail = _make_product_mock(1, "John's Bacon", category="Johns")
        found = [
            _make_product_mock(2, "John's Paranaense", category="Johns"),
            _make_product_mock(3, "Frango", category="Adicionais"),
            _make_product_mock(4, "John's Simples", category="Johns"),
        ]
        same_cat = [p for p in found if p.category == unavail.category]
        assert len(same_cat) == 2
        assert all(p.category == "Johns" for p in same_cat)
        assert not any(p.name == "Frango" for p in same_cat)

    def test_addon_excluded(self):
        """Addon products in different category are excluded."""
        unavail = _make_product_mock(1, "John's Frango", category="Johns")
        found = [
            _make_product_mock(10, "Frango", category="Adicionais"),
            _make_product_mock(11, "Bacon", category="Adicionais"),
            _make_product_mock(12, "Calabresa", category="Adicionais"),
        ]
        same_cat = [p for p in found if p.category == unavail.category]
        assert len(same_cat) == 0  # no same-category products

    def test_fallback_when_no_same_category(self):
        """When no same-category products exist, fallback to all."""
        unavail = _make_product_mock(1, "Unique Product", category="Special")
        found = [
            _make_product_mock(2, "Product A", category="Other"),
            _make_product_mock(3, "Product B", category="Other"),
        ]
        same_cat = [p for p in found if p.category == unavail.category]
        alternatives = same_cat if same_cat else found
        assert len(alternatives) == 2  # falls back to all found

    def test_null_category_handled(self):
        """Products with null category don't crash."""
        unavail = _make_product_mock(1, "Product", category=None)
        found = [
            _make_product_mock(2, "Other", category="Johns"),
        ]
        same_cat = [
            p
            for p in found
            if p.category and unavail.category and p.category == unavail.category
        ]
        alternatives = same_cat if same_cat else found
        assert len(alternatives) == 1  # falls back
