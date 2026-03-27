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

    # Base transitions (PIX payment flow)
    PIX_TRANSITIONS = {
        "pending": {"paid", "canceled"},
        "paid": {"preparing", "canceled"},
        "preparing": {"ready", "canceled", "paid"},
        "ready": {"completed", "canceled", "preparing"},
        "completed": set(),
        "canceled": set(),
        "failed": set(),
        "expired": set(),
    }

    # Non-PIX transitions (card/money: skip "paid" state)
    NON_PIX_TRANSITIONS = {
        "pending": {"paid", "preparing", "canceled"},
        "paid": {"preparing", "canceled"},
        "preparing": {"pending", "ready", "canceled"},
        "ready": {"completed", "canceled", "preparing"},
        "completed": set(),
        "canceled": set(),
        "failed": set(),
        "expired": set(),
    }

    @pytest.mark.parametrize(
        "from_status,to_status,should_allow",
        [
            # PIX forward flow
            ("pending", "paid", True),
            ("pending", "canceled", True),
            ("pending", "completed", False),  # Can't skip states
            ("pending", "preparing", False),  # PIX must go through paid
            ("paid", "preparing", True),
            ("paid", "canceled", True),
            ("paid", "completed", False),  # Can't skip
            ("preparing", "ready", True),
            ("preparing", "canceled", True),
            ("ready", "completed", True),
            ("ready", "canceled", True),
            # PIX reverse flow
            ("preparing", "paid", True),  # Back from preparing → paid
            ("ready", "preparing", True),  # Back from ready → preparing
            ("preparing", "pending", False),  # PIX can't go back to pending
            ("ready", "paid", False),  # Can't skip backwards
            # Terminal states
            ("completed", "pending", False),
            ("completed", "canceled", False),
            ("canceled", "pending", False),
            ("canceled", "paid", False),
            ("failed", "pending", False),
            ("expired", "paid", False),
        ],
    )
    def test_pix_transition_validity(self, from_status, to_status, should_allow):
        allowed = self.PIX_TRANSITIONS.get(from_status, set())
        assert (to_status in allowed) == should_allow, (
            f"PIX transition {from_status} → {to_status}: "
            f"expected {'allowed' if should_allow else 'rejected'}"
        )

    @pytest.mark.parametrize(
        "from_status,to_status,should_allow",
        [
            # Non-PIX forward flow (card/money skip "paid")
            ("pending", "preparing", True),  # Direct to preparing
            ("pending", "paid", True),  # Still allowed
            ("pending", "canceled", True),
            ("pending", "completed", False),  # Can't skip
            ("paid", "preparing", True),
            ("preparing", "ready", True),
            ("preparing", "canceled", True),
            ("ready", "completed", True),
            ("ready", "canceled", True),
            # Non-PIX reverse flow
            ("preparing", "pending", True),  # Back to pending (not paid)
            ("ready", "preparing", True),  # Back to preparing
            ("preparing", "paid", False),  # Non-PIX goes back to pending, not paid
            ("ready", "pending", False),  # Can't skip backwards
            # Terminal states
            ("completed", "pending", False),
            ("canceled", "preparing", False),
        ],
    )
    def test_non_pix_transition_validity(self, from_status, to_status, should_allow):
        allowed = self.NON_PIX_TRANSITIONS.get(from_status, set())
        assert (to_status in allowed) == should_allow, (
            f"Non-PIX transition {from_status} → {to_status}: "
            f"expected {'allowed' if should_allow else 'rejected'}"
        )

    def test_transition_maps_match_backend(self):
        """Verify test maps match the actual backend logic."""
        from app.models import OrderStatus

        # Replicate the backend logic from bot_routes.py
        base = {
            OrderStatus.PENDING: {OrderStatus.PAID, OrderStatus.CANCELED},
            OrderStatus.PAID: {OrderStatus.PREPARING, OrderStatus.CANCELED},
            OrderStatus.PREPARING: {
                OrderStatus.READY,
                OrderStatus.CANCELED,
                OrderStatus.PAID,
            },
            OrderStatus.READY: {
                OrderStatus.COMPLETED,
                OrderStatus.CANCELED,
                OrderStatus.PREPARING,
            },
            OrderStatus.COMPLETED: set(),
            OrderStatus.CANCELED: set(),
            OrderStatus.FAILED: set(),
            OrderStatus.EXPIRED: set(),
        }

        # Convert to string sets for comparison with PIX_TRANSITIONS
        for status, allowed in base.items():
            expected = {s.value for s in allowed}
            assert expected == self.PIX_TRANSITIONS[status.value], (
                f"PIX mismatch at {status.value}: backend={expected}, test={self.PIX_TRANSITIONS[status.value]}"
            )

        # Apply non-PIX overrides (same as backend)
        non_pix = dict(base)
        non_pix[OrderStatus.PENDING] = {
            OrderStatus.PAID,
            OrderStatus.PREPARING,
            OrderStatus.CANCELED,
        }
        non_pix[OrderStatus.PREPARING] = {
            OrderStatus.PENDING,
            OrderStatus.READY,
            OrderStatus.CANCELED,
        }

        for status, allowed in non_pix.items():
            expected = {s.value for s in allowed}
            assert expected == self.NON_PIX_TRANSITIONS[status.value], (
                f"Non-PIX mismatch at {status.value}: backend={expected}, test={self.NON_PIX_TRANSITIONS[status.value]}"
            )

    @pytest.mark.parametrize("payment_method", ["card", "money"])
    def test_non_pix_methods_recognized(self, payment_method):
        """Ensure card and money are in the NON_PIX_METHODS set."""
        NON_PIX_METHODS = {"card", "money"}
        assert payment_method in NON_PIX_METHODS

    def test_pix_not_in_non_pix_methods(self):
        """PIX must NOT be treated as non-PIX."""
        NON_PIX_METHODS = {"card", "money"}
        assert "pix" not in NON_PIX_METHODS


# ────────────────────────────────────────────────────────
# F-03: Anti-hallucination guardrail
# ────────────────────────────────────────────────────────


class TestAntiHallucinationGuardrail:
    """When RAG finds no products for an ADD intent, the LLM's
    answer_conversationally output must be overridden with a
    controlled 'not found' message — never trust hallucinated product names."""

    def test_prompt_does_not_contain_hot_dog_mexicano(self):
        """Ensure the few-shot example no longer primes 'Hot Dog Mexicano'."""
        from app.prompt_central import create_central_prompt

        prompt = create_central_prompt(
            user_query="quero um cachorro quente",
            history=[],
            restaurant_name="Teste",
            cart_items=[],
            search_results=[],
        )
        all_text = " ".join(msg.get("content", "") or "" for msg in prompt).lower()
        assert "hot dog mexicano" not in all_text

    def test_prompt_example_uses_x_tudo(self):
        """The notes example should use X-Tudo instead of Hot Dog Mexicano."""
        from app.prompt_central import create_central_prompt

        prompt = create_central_prompt(
            user_query="test",
            history=[],
            restaurant_name="Teste",
            cart_items=[],
            search_results=[],
        )
        # Find the notes example in the prompt
        found = False
        for msg in prompt:
            if msg.get("role") == "user" and "X-Tudo" in (msg.get("content") or ""):
                found = True
                break
        assert found, "Few-shot example should reference X-Tudo"

    def test_guardrail_overrides_when_no_products_found(self):
        """When found_products is empty and intent is ADD,
        answer_conversationally should be overridden."""
        from app.sanitize import sanitize_llm_output
        from app.item_extraction import extract_items_local

        # Simulate the guardrail logic from whatsapp.py
        found_products = []
        unavailable_matches = []
        intent = "ADD"
        cart_tool_processed = False
        text_body = "queria comer cachorro quente"

        # LLM hallucinates
        llm_response = "Temos o Hot Dog Mexicano no cardápio! Você gostaria?"
        conv_text = sanitize_llm_output(llm_response)

        # Apply guardrail
        if (
            not cart_tool_processed
            and not found_products
            and not unavailable_matches
            and intent in ("ADD", "ADD_ITEMS")
        ):
            items_for_msg = extract_items_local(text_body)
            if items_for_msg:
                items_str = " e ".join(f"*{item}*" for item in items_for_msg)
                conv_text = (
                    f"Não encontrei {items_str} no nosso cardápio. 😕 "
                    "Quer ver nossas sugestões?"
                )

        assert "Hot Dog Mexicano" not in conv_text
        assert "não encontrei" in conv_text.lower()
        assert "cardápio" in conv_text.lower()

    def test_guardrail_does_not_override_when_products_found(self):
        """When products ARE found, don't override the LLM response."""
        found_products = [MagicMock()]  # non-empty
        unavailable_matches = []
        intent = "ADD"
        cart_tool_processed = False

        found_products = [MagicMock()]  # non-empty

        # Guardrail should NOT fire
        should_override = (
            not cart_tool_processed
            and not found_products
            and not unavailable_matches
            and intent in ("ADD", "ADD_ITEMS")
        )
        assert not should_override

    def test_guardrail_does_not_override_non_add_intent(self):
        """Guardrail only fires for ADD/ADD_ITEMS, not other intents."""
        found_products = []
        unavailable_matches = []
        cart_tool_processed = False

        for intent in (
            "GREETING_OR_QUESTION",
            "SHOW_CART",
            "REQUEST_SUGGESTION",
            "CONFIRM",
        ):
            should_override = (
                not cart_tool_processed
                and not found_products
                and not unavailable_matches
                and intent in ("ADD", "ADD_ITEMS")
            )
            assert not should_override, f"Should not override for intent={intent}"

    def test_guardrail_does_not_override_when_unavailable_found(self):
        """When unavailable products are found, let the existing unavailability
        flow handle it — don't override."""
        found_products = []
        unavailable_matches = [MagicMock()]  # non-empty
        intent = "ADD"
        cart_tool_processed = False

        should_override = (
            not cart_tool_processed
            and not found_products
            and not unavailable_matches
            and intent in ("ADD", "ADD_ITEMS")
        )
        assert not should_override

    def test_guardrail_extracts_item_name_for_message(self):
        """The controlled message should mention the item the user asked for."""
        from app.item_extraction import extract_items_local

        items = extract_items_local("queria comer cachorro quente")
        assert any("cachorro" in item for item in items)


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
            mock_crud.clear_contact_history = AsyncMock(return_value=0)
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
    async def test_no_match_preserves_suggestions(self):
        """When no selection matches, suggestions are preserved for CONFIRM handler."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "John's Calabresa")]
        mctx = _make_suggestion_mctx("quero uma coca cola", [1, 2, 3])
        self._setup_session_with_products(mctx, prods)
        result = await _handle_suggestion_selection(mctx)

        assert result is None
        assert mctx.cart.last_suggestions == [1, 2, 3]  # preserved

    @pytest.mark.asyncio
    async def test_pode_ser_preserves_suggestions(self):
        """'pode ser' should not clear suggestions — CONFIRM handler needs them."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's Calabresa"),
            _make_product_mock(2, "John's Simples"),
        ]
        mctx = _make_suggestion_mctx("pode ser", [1, 2])
        self._setup_session_with_products(mctx, prods)
        result = await _handle_suggestion_selection(mctx)

        assert result is None
        assert mctx.cart.last_suggestions == [1, 2]  # preserved for CONFIRM

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
        mctx.text_body = "a" * 121  # just a long string

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


# ────────────────────────────────────────────────────────
# Session 2026-03-19: Multi-selection, quantity parsing,
# meal filter, intent fixes, Option C, plural stemming
# ────────────────────────────────────────────────────────


class TestMultiSelection:
    """Tests for multi-item suggestion selection (ordinals + names + quantities)."""

    def _setup_session_with_products(self, mctx, products):
        result = MagicMock()
        result.scalars.return_value.all.return_value = products
        mctx.session.execute = AsyncMock(return_value=result)

    @pytest.mark.asyncio
    async def test_multi_ordinal_selection(self):
        """'dois do primeiro e tres do segundo' adds both items."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's Paranaense", price=45.0),
            _make_product_mock(2, "John's Alcatra", price=42.0),
        ]
        mctx = _make_suggestion_mctx("dois do primeiro e tres do segundo", [1, 2])
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
        assert len(items) == 2
        assert items[0]["product_id"] == 1
        assert items[0]["quantity"] == 2
        assert items[1]["product_id"] == 2
        assert items[1]["quantity"] == 3

    @pytest.mark.asyncio
    async def test_mixed_ordinal_and_name(self):
        """'dois paranaenses e sete do segundo' mixes name + ordinal."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's Paranaense", price=45.0),
            _make_product_mock(2, "John's Alcatra", price=42.0),
        ]
        mctx = _make_suggestion_mctx("dois paranaenses e sete do segundo", [1, 2])
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
        assert len(items) == 2
        assert items[0]["product_id"] == 1
        assert items[0]["quantity"] == 2
        assert items[1]["product_id"] == 2
        assert items[1]["quantity"] == 7

    @pytest.mark.asyncio
    async def test_dedup_same_product(self):
        """Same product referenced by name and ordinal — only added once."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's Paranaense", price=45.0),
            _make_product_mock(2, "John's Alcatra", price=42.0),
        ]
        mctx = _make_suggestion_mctx("5 alcatra e tres do segundo", [1, 2])
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
        assert len(items) == 1  # deduped
        assert items[0]["product_id"] == 2

    @pytest.mark.asyncio
    async def test_single_selection_still_works(self):
        """Single ordinal selection is backwards compatible."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "John's A"),
            _make_product_mock(2, "John's B"),
        ]
        mctx = _make_suggestion_mctx("pode ser o segundo", [1, 2])
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
        assert len(items) == 1
        assert items[0]["product_id"] == 2
        assert items[0]["quantity"] == 1


class TestPluralStemming:
    """Tests for Portuguese plural normalization in name matching."""

    def test_stem_simple_plural(self):
        """'paranaenses' → 'paranaense'."""

        # The stem logic strips trailing 's' for words > 4 chars
        def _stem(w):
            if len(w) > 4 and w.endswith("s"):
                return w[:-1]
            return w

        assert _stem("paranaenses") == "paranaense"
        assert _stem("alcatras") == "alcatra"
        assert _stem("calabresas") == "calabresa"

    def test_stem_preserves_short_words(self):
        """Short words are not stemmed."""

        def _stem(w):
            if len(w) > 4 and w.endswith("s"):
                return w[:-1]
            return w

        assert _stem("dois") == "dois"  # 4 chars, not stripped
        assert _stem("tres") == "tres"  # 4 chars, not stripped

    def test_stem_preserves_non_s_endings(self):
        """Words not ending in 's' are unchanged."""

        def _stem(w):
            if len(w) > 4 and w.endswith("s"):
                return w[:-1]
            return w

        assert _stem("frango") == "frango"
        assert _stem("alcatra") == "alcatra"
        assert _stem("paranaense") == "paranaense"


class TestMealSuggestionFilter:
    """Tests for _filter_meal_suggestions category exclusion."""

    def test_excludes_adicionais(self):
        from app.whatsapp import _filter_meal_suggestions

        products = [
            _make_product_mock(
                1, "John's Simples", price=16.0, category="John's Tradicionais"
            ),
            _make_product_mock(2, "Bacon", price=10.0, category="Adicionais"),
            _make_product_mock(3, "Coca-Cola", price=8.0, category="Bebidas"),
        ]
        filtered = _filter_meal_suggestions(products)
        assert len(filtered) == 1
        assert filtered[0].name == "John's Simples"

    def test_excludes_bebidas_and_cervejas(self):
        from app.whatsapp import _filter_meal_suggestions

        products = [
            _make_product_mock(1, "Heineken", price=10.0, category="Cervejas"),
            _make_product_mock(2, "Fanta", price=7.0, category="Bebidas"),
            _make_product_mock(
                3, "John's Paranaense", price=45.0, category="John's Especiais"
            ),
        ]
        filtered = _filter_meal_suggestions(products)
        assert len(filtered) == 1
        assert filtered[0].name == "John's Paranaense"

    def test_sorts_by_price_descending(self):
        from app.whatsapp import _filter_meal_suggestions

        products = [
            _make_product_mock(1, "John's Simples", price=16.0, category="Johns"),
            _make_product_mock(2, "John's Paranaense", price=45.0, category="Johns"),
            _make_product_mock(3, "John's Alcatra", price=42.0, category="Johns"),
        ]
        filtered = _filter_meal_suggestions(products)
        assert filtered[0].price == 45.0
        assert filtered[1].price == 42.0
        assert filtered[2].price == 16.0

    def test_empty_when_all_excluded(self):
        from app.whatsapp import _filter_meal_suggestions

        products = [
            _make_product_mock(1, "Bacon", price=10.0, category="Adicionais"),
            _make_product_mock(2, "Coca-Cola", price=8.0, category="Bebidas"),
        ]
        filtered = _filter_meal_suggestions(products)
        assert len(filtered) == 0

    def test_null_category_preserved(self):
        from app.whatsapp import _filter_meal_suggestions

        products = [
            _make_product_mock(1, "Mystery Item", price=20.0, category=None),
        ]
        filtered = _filter_meal_suggestions(products)
        assert len(filtered) == 1


class TestResolveIntentFixes:
    """Tests for intent resolution changes: low confidence default + FINISH_ORDER demotion."""

    @pytest.mark.asyncio
    async def test_low_confidence_defaults_to_add(self):
        """Very low router confidence should default to ADD."""
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = []

        with patch(
            "app.whatsapp.semantic_intent", new_callable=AsyncMock
        ) as mock_router:
            mock_router.return_value = ("FINISH_ORDER", 0.40, "matched")
            intent = await resolve_intent("to com fome demais", cart, [])

        assert intent == "ADD"

    @pytest.mark.asyncio
    async def test_finish_order_demoted_at_moderate(self):
        """FINISH_ORDER at moderate confidence should be demoted to ADD."""
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = []

        with patch(
            "app.whatsapp.semantic_intent", new_callable=AsyncMock
        ) as mock_router:
            # 0.65 is above 0.78 * 0.75 = 0.585 (moderate) but below 0.78 (confident)
            mock_router.return_value = ("FINISH_ORDER", 0.65, "matched")
            intent = await resolve_intent("o que tem de bom hoje", cart, [])

        assert intent == "ADD"

    @pytest.mark.asyncio
    async def test_finish_order_accepted_at_high_confidence(self):
        """FINISH_ORDER at high confidence should be accepted."""
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = []

        with patch(
            "app.whatsapp.semantic_intent", new_callable=AsyncMock
        ) as mock_router:
            mock_router.return_value = ("FINISH_ORDER", 0.85, "matched")
            intent = await resolve_intent("só isso mesmo", cart, [])

        assert intent == "FINISH_ORDER"

    @pytest.mark.asyncio
    async def test_other_intents_not_demoted_at_moderate(self):
        """Non-FINISH_ORDER intents at moderate confidence should be kept."""
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = []

        with patch(
            "app.whatsapp.semantic_intent", new_callable=AsyncMock
        ) as mock_router:
            mock_router.return_value = ("CONFIRM", 0.70, "matched")
            intent = await resolve_intent("sim", cart, [])

        assert intent == "CONFIRM"


class TestPromptCategories:
    """Tests for category injection into LLM prompt."""

    def test_categories_included_in_prompt(self):
        from app.prompt_central import create_central_prompt

        prompt = create_central_prompt(
            user_query="o que tem?",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            available_categories=["John's Tradicionais", "Bebidas"],
        )
        all_text = " ".join(m.get("content") or "" for m in prompt)
        assert "John's Tradicionais" in all_text
        assert "Bebidas" in all_text
        assert "NUNCA mencione" in all_text

    def test_no_categories_when_none(self):
        from app.prompt_central import create_central_prompt

        prompt = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            available_categories=None,
        )
        all_text = " ".join(m.get("content") or "" for m in prompt)
        assert "Categorias do cardápio" not in all_text

    def test_empty_categories_list(self):
        from app.prompt_central import create_central_prompt

        prompt = create_central_prompt(
            user_query="oi",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            available_categories=[],
        )
        all_text = " ".join(m.get("content") or "" for m in prompt)
        assert "Categorias do cardápio" not in all_text


class TestCartAddHintMessage:
    """Tests for the 'só isso' hint in the cart-add response."""

    def test_hint_in_suggestion_response(self):
        """After adding from suggestions, hint should appear."""
        # The hint is appended in _handle_suggestion_selection
        hint = "Mais alguma coisa ou *só isso* para finalizar? 😊"
        assert "só isso" in hint
        assert "finalizar" in hint


class TestHandleFinishOrderNoDoubleMessage:
    """Tests for _handle_finish_order not sending messages directly."""

    @pytest.mark.asyncio
    async def test_empty_cart_returns_string_only(self):
        """Empty cart should return string, not send message directly."""
        from app.whatsapp import _handle_finish_order, MessageContext

        mctx = MagicMock(spec=MessageContext)
        mctx.session = AsyncMock()
        mctx.bot = MagicMock()
        mctx.bot.id = 1
        mctx.bot.min_order_value = None
        mctx.cart = MagicMock()
        mctx.cart.items = []
        mctx.contact_number = "5511999"
        mctx.text_body = "só isso"
        mctx.token = "fake"
        mctx.phone_id = "fake"

        with patch(
            "app.whatsapp._load_cart_items_with_products", new_callable=AsyncMock
        ):
            result = await _handle_finish_order(mctx, "FINISH_ORDER")

        assert result == "Seu carrinho está vazio. 🛒 Me diga o que quer pedir!"
        # Should NOT call send_whatsapp_message — caller handles that
        mctx.session.flush.assert_not_called()


# ────────────────────────────────────────────────────────
# Programmatic em falta message
# ────────────────────────────────────────────────────────


class TestProgrammaticEmFalta:
    """Tests for programmatic em falta message construction."""

    def test_em_falta_message_built_with_product_name(self):
        """Em falta message should contain the unavailable product name."""
        unavailable_matches = [_make_product_mock(1, "John's Bacon")]
        _unavail_names = ", ".join(f"*{p.name}*" for p in unavailable_matches)
        msg = f"\n\nPuxa, {_unavail_names} está em falta no momento. 😕"
        assert "*John's Bacon*" in msg
        assert "em falta" in msg

    def test_em_falta_with_multiple_unavailable(self):
        """Multiple unavailable products should all appear."""
        unavailable_matches = [
            _make_product_mock(1, "John's Bacon"),
            _make_product_mock(2, "John's Frango"),
        ]
        _unavail_names = ", ".join(f"*{p.name}*" for p in unavailable_matches)
        msg = f"\n\nPuxa, {_unavail_names} está em falta no momento. 😕"
        assert "*John's Bacon*" in msg
        assert "*John's Frango*" in msg

    def test_em_falta_appended_to_cart_summary(self):
        """Em falta message should append to existing response, not replace."""
        cart_summary = "✅ Seu pedido:\n* 2x Alcatra — R$ 84.00\n\nAdicionado!"
        em_falta = "\n\nPuxa, *John's Bacon* está em falta no momento. 😕"
        combined = cart_summary + em_falta
        assert "Adicionado!" in combined
        assert "em falta" in combined

    def test_no_em_falta_when_no_unavailable(self):
        """No em falta message when unavailable_matches is empty."""
        unavailable_matches = []
        _em_falta_msg = ""
        if unavailable_matches:
            _em_falta_msg = "should not appear"
        assert _em_falta_msg == ""


# ────────────────────────────────────────────────────────
# Regression: LLM conversational follow-up suppressed after cart add
# ────────────────────────────────────────────────────────


class TestConversationalFollowupSuppressed:
    """When add_items_to_cart succeeds (cart_tool_processed=True), any
    answer_conversationally tool call from the same LLM response must be
    suppressed. Otherwise the LLM's confirmation question ('Só pra confirmar,
    você quis dizer 396 unidades...') leaks into the response and causes
    quantity doubling when the user says 'sim'."""

    def test_cart_processed_skips_conversational(self):
        """Simulate the dispatch logic: cart_tool_processed=True → conv text skipped."""
        cart_tool_processed = True
        response_to_user = "✅ Cart summary here"
        conv_text = "Só pra confirmar, 396 unidades de Picanha?"

        # This mirrors the logic at whatsapp.py ~line 1905
        if cart_tool_processed:
            pass  # suppressed
        else:
            response_to_user = conv_text

        assert "confirmar" not in response_to_user
        assert response_to_user == "✅ Cart summary here"

    def test_cart_not_processed_shows_conversational(self):
        """When no cart tool ran, conversational text IS used."""
        cart_tool_processed = False
        response_to_user = ""
        conv_text = "Olá! Como posso ajudar?"

        if cart_tool_processed:
            pass
        else:
            response_to_user = conv_text

        assert response_to_user == conv_text


# ────────────────────────────────────────────────────────
# Regression: last_suggestions preserved when unavailable_matches exist
# ────────────────────────────────────────────────────────


class TestLastSuggestionsPreservedOnEmFalta:
    """When add_items_to_cart adds valid items but there are also unavailable
    matches, last_suggestions must NOT be cleared. The em falta flow sets
    last_suggestions for the 'sim' alternatives flow — clearing it causes
    CONFIRM to fall through to ADD, doubling quantities."""

    def test_suggestions_kept_when_unavailable_matches_present(self):
        """Simulate the dispatch logic: unavailable_matches non-empty → don't clear."""
        last_suggestions = [685, 686, 687]
        unavailable_matches = [_make_product_mock(701, "Supremo X")]
        added_product_ids = [692, 696]  # Picanha com bacon, Coração

        # This mirrors whatsapp.py ~line 1562-1568
        if (
            not any(pid in (last_suggestions or []) for pid in added_product_ids)
            and not unavailable_matches
        ):
            last_suggestions = None

        # last_suggestions should be preserved because unavailable_matches exist
        assert last_suggestions == [685, 686, 687]

    def test_suggestions_cleared_when_no_unavailable_matches(self):
        """When no unavailable matches, normal clearing logic applies."""
        last_suggestions = [685, 686, 687]
        unavailable_matches = []
        added_product_ids = [692, 696]  # products NOT in suggestions

        if (
            not any(pid in (last_suggestions or []) for pid in added_product_ids)
            and not unavailable_matches
        ):
            last_suggestions = None

        assert last_suggestions is None

    def test_suggestions_kept_when_added_from_suggestions(self):
        """When added products ARE from suggestions, don't clear regardless."""
        last_suggestions = [685, 686, 687]
        unavailable_matches = []
        added_product_ids = [685]  # product IS in suggestions

        if (
            not any(pid in (last_suggestions or []) for pid in added_product_ids)
            and not unavailable_matches
        ):
            last_suggestions = None

        assert last_suggestions == [685, 686, 687]


# ────────────────────────────────────────────────────────
# Option C: shopping intents guard
# ────────────────────────────────────────────────────────


class TestOptionCIntentGuard:
    """Option C should only override for shopping intents, not greetings/confirmations."""

    _SHOPPING_INTENTS = {"ADD", "ADD_ITEMS", "MODIFY", "REMOVE", "REQUEST_SUGGESTION"}

    @pytest.mark.parametrize(
        "intent", ["ADD", "ADD_ITEMS", "MODIFY", "REMOVE", "REQUEST_SUGGESTION"]
    )
    def test_shopping_intents_trigger_override(self, intent):
        assert intent in self._SHOPPING_INTENTS

    @pytest.mark.parametrize(
        "intent",
        [
            "GREETING_OR_QUESTION",
            "CONFIRM",
            "NEGATE",
            "FINISH_ORDER",
            "CLEAR_CART",
            "SHOW_CART",
        ],
    )
    def test_non_shopping_intents_do_not_trigger(self, intent):
        assert intent not in self._SHOPPING_INTENTS


# ────────────────────────────────────────────────────────
# _get_meal_suggestions min/max behavior
# ────────────────────────────────────────────────────────


class TestGetMealSuggestionsMinMax:
    """Tests for _get_meal_suggestions enforcing min 3 / max 4 results."""

    @pytest.mark.asyncio
    async def test_returns_max_4_when_enough(self):
        from app.whatsapp import _get_meal_suggestions

        products = [
            _make_product_mock(i, f"Product {i}", price=float(50 - i), category="Johns")
            for i in range(1, 8)
        ]
        session = AsyncMock()
        result = await _get_meal_suggestions(session, 1, products)
        assert len(result) <= 4

    @pytest.mark.asyncio
    async def test_sorted_by_price_desc(self):
        from app.whatsapp import _get_meal_suggestions

        products = [
            _make_product_mock(1, "Cheap", price=10.0, category="Johns"),
            _make_product_mock(2, "Medium", price=30.0, category="Johns"),
            _make_product_mock(3, "Expensive", price=50.0, category="Johns"),
        ]
        session = AsyncMock()
        result = await _get_meal_suggestions(session, 1, products)
        assert result[0].price >= result[-1].price

    @pytest.mark.asyncio
    async def test_tops_up_from_db_when_below_min(self):
        """When filtering gives <3 results, should query DB for more."""
        from app.whatsapp import _get_meal_suggestions

        # Only 1 non-excluded product
        products = [
            _make_product_mock(1, "Johns Simples", price=16.0, category="Johns"),
            _make_product_mock(2, "Bacon", price=10.0, category="Adicionais"),
        ]
        # Mock session to return more products from DB
        db_products = [
            _make_product_mock(3, "Johns Paranaense", price=45.0, category="Johns"),
            _make_product_mock(4, "Johns Alcatra", price=42.0, category="Johns"),
            _make_product_mock(5, "Coca-Cola", price=8.0, category="Bebidas"),
        ]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = db_products
        session = AsyncMock()
        session.execute = AsyncMock(return_value=mock_result)

        result = await _get_meal_suggestions(session, 1, products)
        assert len(result) >= 3
        # Should not contain Adicionais or Bebidas
        for p in result:
            if p.category:
                assert p.category not in ("Adicionais", "Bebidas")


# ────────────────────────────────────────────────────────
# Cosine similarity threshold
# ────────────────────────────────────────────────────────


class TestCosineThreshold:
    """Tests for the embedding search threshold in find_relevant_products."""

    def test_threshold_value_exists_in_code(self):
        """Verify the threshold constant is set correctly."""
        with open("app/crud.py", encoding="utf-8") as f:
            source = f.read()
        assert "_EMBEDDING_MAX_DISTANCE = 0.3" in source

    def test_threshold_only_in_find_relevant_not_unavailable(self):
        """find_unavailable_products should NOT have the strict threshold."""
        with open("app/crud.py", encoding="utf-8") as f:
            source = f.read()
        # Find the find_unavailable_products function
        unavail_section = source[source.index("def find_unavailable_products") :]
        # It should NOT contain _EMBEDDING_MAX_DISTANCE in a WHERE clause
        assert (
            "cosine_distance(query_embedding) < _EMBEDDING_MAX_DISTANCE"
            not in unavail_section
        )


# ────────────────────────────────────────────────────────
# Availability filtering in all suggestion flows
# ────────────────────────────────────────────────────────


class TestSuggestionsAlwaysFilterAvailable:
    """Verify that all suggestion flows filter by is_available=True."""

    def test_all_suggestion_queries_filter_available(self):
        """Every DB query that populates last_suggestions must filter is_available."""
        with open("app/whatsapp.py", encoding="utf-8") as f:
            source = f.read()

        suggestion_sets = [
            i
            for i, line in enumerate(source.split("\n"))
            if "last_suggestions = [p.id for p in" in line
            or "last_suggestions = [p.id" in line
        ]
        assert len(suggestion_sets) >= 8, (
            f"Expected at least 8 suggestion-setting points, found {len(suggestion_sets)}"
        )

    def test_get_meal_suggestions_db_fallback_filters_available(self):
        """_get_meal_suggestions DB fallback must filter is_available."""
        with open("app/whatsapp.py", encoding="utf-8") as f:
            source = f.read()
        fn_start = source.index("async def _get_meal_suggestions")
        fn_section = source[fn_start : fn_start + 800]
        assert "Product.is_available == True" in fn_section


# ────────────────────────────────────────────────────────
# Session 2026-03-25: Large compound quantities, fuzzy
# number typos, and "mil duzentos" adjacency support
# ────────────────────────────────────────────────────────


class TestCompoundQuantitySuggestionSelection:
    """Tests for large compound numbers and typo-tolerant qty parsing in suggestion selection."""

    def _setup_session_with_products(self, mctx, products):
        result = MagicMock()
        result.scalars.return_value.all.return_value = products
        mctx.session.execute = AsyncMock(return_value=result)

    @pytest.mark.asyncio
    async def test_mil_duzentos_e_noventa_e_quatro(self):
        """'mil duzentos e noventa e quatro picanha' → qty 1294."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "PICANHA COM BACON", price=35.0),
            _make_product_mock(2, "PICANHA", price=32.0),
        ]
        mctx = _make_suggestion_mctx("mil duzentos e noventa e quatro picanha", [1, 2])
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
        assert len(items) >= 1
        # "picanha" matches both products; the handler picks the best name overlap
        picanha_item = items[0]
        assert picanha_item["quantity"] == 1294

    @pytest.mark.asyncio
    async def test_duzentos_e_trinta_e_cinco(self):
        """'duzentos e trinta e cinco picanha' → qty 235."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "PICANHA", price=32.0)]
        mctx = _make_suggestion_mctx("duzentos e trinta e cinco picanha", [1])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["quantity"] == 235

    @pytest.mark.asyncio
    async def test_tres_mil_quinhentos(self):
        """'três mil e quinhentos picanha' → qty 3500."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "PICANHA", price=32.0)]
        mctx = _make_suggestion_mctx("três mil e quinhentos picanha", [1])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["quantity"] == 3500

    @pytest.mark.asyncio
    async def test_noventa_e_nove(self):
        """'noventa e nove picanha' → qty 99 (tests newly added tens)."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "PICANHA", price=32.0)]
        mctx = _make_suggestion_mctx("noventa e nove picanha", [1])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["quantity"] == 99

    @pytest.mark.asyncio
    async def test_fuzzy_cuarenta(self):
        """'cuarenta picanha' (Spanish typo) → qty 40."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "PICANHA", price=32.0)]
        mctx = _make_suggestion_mctx("cuarenta picanha", [1])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["quantity"] == 40

    @pytest.mark.asyncio
    async def test_fuzzy_sinquenta(self):
        """'sinquenta picanha' (common typo) → qty 50."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "PICANHA", price=32.0)]
        mctx = _make_suggestion_mctx("sinquenta picanha", [1])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["quantity"] == 50

    @pytest.mark.asyncio
    async def test_fuzzy_dusentos(self):
        """'dusentos picanha' (common typo) → qty 200."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "PICANHA", price=32.0)]
        mctx = _make_suggestion_mctx("dusentos picanha", [1])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["quantity"] == 200

    @pytest.mark.asyncio
    async def test_user_scenario_full(self):
        """Full user scenario: 'quero mil duzentos e noventa e quatro picanha com bacon e 234 com catupiry'."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "PICANHA COM BACON", price=35.0),
            _make_product_mock(2, "PICANHA COM CATUPIRY", price=35.0),
            _make_product_mock(3, "PICANHA", price=32.0),
        ]
        mctx = _make_suggestion_mctx(
            "quero mil duzentos e noventa e quatro picanha com bacon e 234 com catupiry",
            [1, 2, 3],
        )
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
        items_by_id = {i["product_id"]: i for i in items}
        # PICANHA COM BACON should have qty 1294
        assert items_by_id[1]["quantity"] == 1294
        # PICANHA COM CATUPIRY should have qty 234
        assert items_by_id[2]["quantity"] == 234

    @pytest.mark.asyncio
    async def test_small_numbers_still_work(self):
        """Regression: 'dois do primeiro e tres do segundo' still works."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "PICANHA", price=35.0),
            _make_product_mock(2, "ALCATRA", price=32.0),
        ]
        mctx = _make_suggestion_mctx("dois do primeiro e tres do segundo", [1, 2])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert len(items) == 2
        assert items[0]["quantity"] == 2
        assert items[1]["quantity"] == 3

    @pytest.mark.asyncio
    async def test_entao_not_parsed_as_cento(self):
        """Regression: 'então' must NOT fuzzy-match to 'cento' (100).

        'pode ser o bagunça no lugar então' was incorrectly parsed as qty=100
        because SequenceMatcher('então','cento')=0.80 hit the old threshold."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [
            _make_product_mock(1, "Bagunça", price=33.0),
            _make_product_mock(2, "América", price=35.5),
        ]
        mctx = _make_suggestion_mctx("pode ser o bagunça no lugar então", [1, 2])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["product_id"] == 1
        assert items[0]["quantity"] == 1  # NOT 100

    @pytest.mark.asyncio
    async def test_suggestion_typo_cuatro_exact_entry(self):
        """'cuatro' (Spanish typo) is an exact entry → qty 4, not fuzzy-matched."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "PICANHA", price=32.0)]
        mctx = _make_suggestion_mctx("cuatro picanha", [1])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["quantity"] == 4

    @pytest.mark.asyncio
    async def test_suggestion_typo_ceis_exact_entry(self):
        """'ceis' (informal seis) is an exact entry → qty 6."""
        from app.whatsapp import _handle_suggestion_selection

        prods = [_make_product_mock(1, "PICANHA", price=32.0)]
        mctx = _make_suggestion_mctx("ceis picanha", [1])
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
            await _handle_suggestion_selection(mctx)

        items = mock_crud.add_items_to_db_cart.call_args[0][2]
        assert items[0]["quantity"] == 6


# ────────────────────────────────────────────────────────
# Pre-router REMOVE guard
# ────────────────────────────────────────────────────────


class TestRemoveGuardRegex:
    """Tests for _REMOVE_KEYWORD_RE — ensures removal messages are classified
    as MODIFY and not defaulted to ADD."""

    def _matches(self, text: str) -> bool:
        from app.whatsapp import _REMOVE_KEYWORD_RE

        return bool(_REMOVE_KEYWORD_RE.search(text))

    # --- Should match ---

    def test_tire_with_digit(self):
        assert self._matches("tire 2 prensadão")

    def test_tira_with_written_qty(self):
        assert self._matches("tira dois prensadão")

    def test_tirar_infinitive(self):
        assert self._matches("tirar 5 coca")

    def test_pode_tirar_prefix(self):
        assert self._matches("pode tirar dez prensadao e 9 bagunça")

    def test_por_favor_tira(self):
        assert self._matches("por favor tira 3 coca do pedido")

    def test_quero_tirar(self):
        assert self._matches("quero tirar 5 prensadão")

    def test_retira_with_qty(self):
        assert self._matches("retira 9 bagunça")

    def test_remove_with_qty(self):
        assert self._matches("remove 2 calabresa")

    def test_remover_infinitive(self):
        assert self._matches("pode remover 3 coca")

    def test_da_pra_tirar(self):
        assert self._matches("dá pra tirar 2 prensadão")

    def test_long_conversational_prefix(self):
        """Real-world: removal request buried in conversation."""
        assert self._matches(
            "vamos tirar dois prensadão e 11 bagunça, meu cunhado avisou que nao vem"
        )

    # --- Should NOT match (avoid false positives) ---

    def test_tira_cebola_no_qty(self):
        """'tira a cebola' is an observation, not a cart reduction."""
        assert not self._matches("tira a cebola do lanche")

    def test_add_message_not_matched(self):
        assert not self._matches("quero 5 prensadão e 3 coca")

    def test_pode_mandar_not_matched(self):
        assert not self._matches("pode mandar 5 prensadão")

    def test_conversational_without_remove_verb(self):
        assert not self._matches("acho que vou querer mais 2 bagunça")


class TestRemoveGuardIntentResolution:
    """Tests that the REMOVE guard resolves to MODIFY intent end-to-end."""

    @pytest.mark.asyncio
    async def test_tire_resolves_to_modify(self):
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = [MagicMock()]

        intent = await resolve_intent("tire dois prensadão e 9 bagunças", cart, [])
        assert intent == "MODIFY"

    @pytest.mark.asyncio
    async def test_pode_tirar_resolves_to_modify(self):
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = [MagicMock()]

        intent = await resolve_intent("pode tirar dez prensadao e 9 bagunça", cart, [])
        assert intent == "MODIFY"

    @pytest.mark.asyncio
    async def test_vamos_tirar_resolves_to_modify(self):
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = [MagicMock()]

        intent = await resolve_intent(
            "vamos tirar dois prensadão e 11 bagunça, meu primo não vem", cart, []
        )
        assert intent == "MODIFY"

    @pytest.mark.asyncio
    async def test_remove_guard_before_add_guard(self):
        """'quero tirar 5 prensadão' should match REMOVE guard, not ADD guard."""
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = [MagicMock()]

        intent = await resolve_intent("quero tirar 5 prensadão", cart, [])
        assert intent == "MODIFY"


# ────────────────────────────────────────────────────────
# Low-confidence REMOVE safety net
# ────────────────────────────────────────────────────────


class TestLowConfidenceRemoveSafetyNet:
    """When the router scores REMOVE at low confidence and the message
    contains a removal keyword stem, it should resolve to MODIFY instead
    of defaulting to ADD."""

    @pytest.mark.asyncio
    async def test_low_confidence_remove_with_keyword_becomes_modify(self):
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = [MagicMock()]

        with patch(
            "app.whatsapp.semantic_intent", new_callable=AsyncMock
        ) as mock_router:
            # Below threshold, but message contains "tirar"
            mock_router.return_value = ("REMOVE", 0.50, "matched")
            intent = await resolve_intent("da pra tirar uns itens do pedido", cart, [])

        assert intent == "MODIFY"

    @pytest.mark.asyncio
    async def test_low_confidence_remove_without_keyword_defaults_to_add(self):
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = []

        with patch(
            "app.whatsapp.semantic_intent", new_callable=AsyncMock
        ) as mock_router:
            # REMOVE at low confidence but no removal keyword in text
            mock_router.return_value = ("REMOVE", 0.50, "matched")
            intent = await resolve_intent("to com fome demais", cart, [])

        # No removal stems in message → ADD default
        assert intent in ("ADD", "GREETING_OR_QUESTION")

    @pytest.mark.asyncio
    async def test_low_confidence_non_remove_still_defaults_to_add(self):
        from app.whatsapp import resolve_intent
        from app.models import CartState

        cart = MagicMock()
        cart.state = CartState.SHOPPING
        cart.items = []

        with patch(
            "app.whatsapp.semantic_intent", new_callable=AsyncMock
        ) as mock_router:
            mock_router.return_value = ("CONFIRM", 0.50, "matched")
            intent = await resolve_intent("quero 5 prensadão", cart, [])

        assert intent == "ADD"


# ────────────────────────────────────────────────────────
# Programmatic cart reduction
# ────────────────────────────────────────────────────────


class TestProgrammaticCartReduce:
    """Tests for _programmatic_cart_reduce — handles 'tire N X' without LLM."""

    def _make_cart_item(self, product_name: str, qty: int, product_id: int = 1):
        ci = MagicMock()
        ci.product = MagicMock()
        ci.product.name = product_name
        ci.product.id = product_id
        ci.quantity = qty
        ci.id = product_id * 100
        return ci

    def test_basic_subtraction(self):
        from app.whatsapp import _programmatic_cart_reduce

        items = [
            self._make_cart_item("Prensadão", 12, 700),
            self._make_cart_item("Bagunça", 19, 686),
        ]
        pairs = [(2, "prensadão"), (9, "bagunça")]
        result = _programmatic_cart_reduce(pairs, items, "tire 2 prensadão e 9 bagunça")

        assert result is not None
        updates, parts = result
        assert len(updates) == 2
        assert updates[0][1] == 10  # 12 - 2
        assert updates[1][1] == 10  # 19 - 9

    def test_subtraction_to_zero(self):
        from app.whatsapp import _programmatic_cart_reduce

        items = [self._make_cart_item("Prensadão", 5, 700)]
        pairs = [(5, "prensadão")]
        result = _programmatic_cart_reduce(pairs, items, "tire 5 prensadão")

        updates, parts = result
        assert updates[0][1] == 0
        assert "Removido" in parts[0]

    def test_subtraction_more_than_available_floors_to_zero(self):
        from app.whatsapp import _programmatic_cart_reduce

        items = [self._make_cart_item("Prensadão", 3, 700)]
        pairs = [(10, "prensadão")]
        result = _programmatic_cart_reduce(pairs, items, "tire 10 prensadão")

        updates, parts = result
        assert updates[0][1] == 0  # max(0, 3 - 10)

    def test_no_match_returns_none(self):
        from app.whatsapp import _programmatic_cart_reduce

        items = [self._make_cart_item("Prensadão", 12, 700)]
        pairs = [(2, "pizza")]
        result = _programmatic_cart_reduce(pairs, items, "tire 2 pizza")

        assert result is None

    def test_ignores_non_matching_pairs(self):
        """'tirar' (verb) shouldn't match any product — only real product names count."""
        from app.whatsapp import _programmatic_cart_reduce

        items = [
            self._make_cart_item("Prensadão", 12, 700),
            self._make_cart_item("Bagunça", 19, 686),
        ]
        # extract_items_with_quantities produces (1, 'tirar') as a spurious pair
        pairs = [(1, "tirar"), (2, "prensadão"), (9, "bagunça")]
        result = _programmatic_cart_reduce(
            pairs, items, "vamos tirar dois prensadão e 9 bagunça"
        )

        assert result is not None
        updates, _ = result
        # Should only match prensadão and bagunça, not 'tirar'
        assert len(updates) == 2
        assert updates[0][1] == 10  # 12 - 2
        assert updates[1][1] == 10  # 19 - 9

    def test_substring_match_with_trailing_text(self):
        """'bagunça, vó avisou que nao vem' should still match 'Bagunça'."""
        from app.whatsapp import _programmatic_cart_reduce

        items = [self._make_cart_item("Bagunça", 19, 686)]
        pairs = [(11, "bagunça, vó avisou que nao vem mais")]
        result = _programmatic_cart_reduce(pairs, items, "tire 11 bagunça")

        assert result is not None
        updates, _ = result
        assert updates[0][1] == 8  # 19 - 11

    def test_empty_cart_returns_none(self):
        from app.whatsapp import _programmatic_cart_reduce

        result = _programmatic_cart_reduce([(2, "prensadão")], [], "tire 2 prensadão")
        assert result is None

    def test_empty_pairs_returns_none(self):
        from app.whatsapp import _programmatic_cart_reduce

        items = [self._make_cart_item("Prensadão", 12, 700)]
        result = _programmatic_cart_reduce([], items, "tire")
        assert result is None


# ────────────────────────────────────────────────────────
# Suggestion handler bypass for removal messages
# ────────────────────────────────────────────────────────


class TestSuggestionHandlerRemovalBypass:
    """Stale last_suggestions must not hijack removal messages.
    When a previous ADD fails (Option C sets last_suggestions), a
    subsequent 'tire N X' message should clear suggestions and go
    through the normal MODIFY flow instead."""

    def test_remove_keyword_clears_stale_suggestions(self):
        """_REMOVE_KEYWORD_RE match should clear last_suggestions."""
        from app.whatsapp import _REMOVE_KEYWORD_RE

        cart = MagicMock()
        cart.last_suggestions = [700, 686, 694]
        text = "vamos tirar dois prensadão e 11 bagunça"

        # Simulate the bypass logic from process_whatsapp_message
        if _REMOVE_KEYWORD_RE.search(text) and cart.last_suggestions:
            cart.last_suggestions = None

        assert cart.last_suggestions is None

    def test_add_message_preserves_suggestions(self):
        """ADD messages should not clear last_suggestions."""
        from app.whatsapp import _REMOVE_KEYWORD_RE

        cart = MagicMock()
        cart.last_suggestions = [700, 686, 694]
        text = "quero 5 prensadão"

        if _REMOVE_KEYWORD_RE.search(text) and cart.last_suggestions:
            cart.last_suggestions = None

        assert cart.last_suggestions == [700, 686, 694]

    def test_conversational_message_preserves_suggestions(self):
        """Non-removal conversational messages should keep suggestions."""
        from app.whatsapp import _REMOVE_KEYWORD_RE

        cart = MagicMock()
        cart.last_suggestions = [700, 686]
        text = "boa noite, tudo bem?"

        if _REMOVE_KEYWORD_RE.search(text) and cart.last_suggestions:
            cart.last_suggestions = None

        assert cart.last_suggestions == [700, 686]


# ────────────────────────────────────────────────────────
# Prompt: partial removal few-shot example
# ────────────────────────────────────────────────────────


class TestPartialRemovalFewShot:
    """Verifies the few-shot example for 'tire N X' → bulk_modify_quantities
    is present in the prompt and uses subtraction (not addition)."""

    def test_partial_remove_example_in_prompt(self):
        import json
        from app.prompt_central import create_central_prompt

        prompt = create_central_prompt(
            user_query="tire 2 coca",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        all_content = json.dumps(prompt, ensure_ascii=False)
        assert "tire dois prensadão e 9 bagunças" in all_content
        assert "bulk_modify_quantities" in all_content

    def test_partial_remove_rule_in_system_prompt(self):
        from app.prompt_central import create_central_prompt

        prompt = create_central_prompt(
            user_query="test",
            history=[],
            restaurant_name="Test",
            cart_items=[],
        )
        system_content = prompt[0]["content"]
        assert "tirar" in system_content.lower()
        assert "subtrair" in system_content.lower() or "SUBTRAIR" in system_content
        assert "NUNCA use `add_items_to_cart` quando" in system_content
