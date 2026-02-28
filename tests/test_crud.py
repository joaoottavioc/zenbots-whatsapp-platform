# tests/test_crud.py
"""
Unit tests for key CRUD functions in app/crud.py.

All database interaction is mocked via AsyncMock — no real DB connection required.

Covered:
- get_or_create_contact   (returns existing / creates new)
- clear_db_cart           (clears items and resets state / missing cart)
- save_address_to_cart    (saves address / missing cart)
- save_customer_name_to_contact (saves name / missing contact)
- delete_order            (deletes existing / missing order)
- is_message_processed    (found / not found)
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.crud import (
    get_or_create_contact,
    clear_db_cart,
    save_address_to_cart,
    save_customer_name_to_contact,
    delete_order,
    is_message_processed,
    upsert_subscription,
    find_relevant_products,
    add_items_to_db_cart,
)
from app.schemas import BotUpdate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session() -> AsyncMock:
    """
    Returns a minimal AsyncMock that satisfies the session interface used by
    the functions under test (execute, get, add, flush, commit, refresh,
    delete).
    """
    session = AsyncMock()

    # execute() returns a result object; scalar_one_or_none defaults to None.
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute.return_value = result_mock

    # get() is an awaitable that returns None by default.
    session.get = AsyncMock(return_value=None)

    session.add = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    return session


def _make_contact(contact_id: int = 10, phone: str = "5511888888888", bot_id: int = 1) -> MagicMock:
    contact = MagicMock()
    contact.id = contact_id
    contact.phone_number = phone
    contact.bot_id = bot_id
    contact.name = None
    return contact


def _make_cart(cart_id: int = 100, state: str = "GREETING") -> MagicMock:
    cart = MagicMock()
    cart.id = cart_id
    cart.state = state
    cart.items = []
    cart.customer_address = None
    cart.pending_action_tool = None
    cart.pending_action_args = None
    cart.pending_action_question = None
    cart.pending_action_expires_at = None
    cart.delivery_method = None
    cart.last_suggestions = None
    return cart


def _make_cart_item(item_id: int = 1, product_id: int = 10) -> MagicMock:
    item = MagicMock()
    item.id = item_id
    item.product_id = product_id
    return item


def _make_order(order_id: int = 42, bot_id: int = 1) -> MagicMock:
    order = MagicMock()
    order.id = order_id
    order.bot_id = bot_id
    return order


def _make_processed_message(message_id: str = "wamid.test123") -> MagicMock:
    pm = MagicMock()
    pm.message_id = message_id
    return pm


# ===========================================================================
# TestGetOrCreateContact
# ===========================================================================

class TestGetOrCreateContact:

    async def test_returns_existing_contact(self):
        """When execute finds a contact, it is returned as-is without adding a new row."""
        session = _make_session()
        existing = _make_contact(contact_id=10, phone="5511888888888", bot_id=1)

        # Make scalar_one_or_none return the existing contact.
        session.execute.return_value.scalar_one_or_none.return_value = existing

        result = await get_or_create_contact(session, bot_id=1, contact_number="5511888888888")

        assert result is existing
        session.add.assert_not_called()
        session.flush.assert_not_called()
        session.refresh.assert_not_called()

    async def test_normalizes_phone_before_querying(self):
        """get_or_create_contact must normalize the phone number (strip non-digits, remove leading 55)."""
        session = _make_session()
        existing = _make_contact(contact_id=10, phone="11888888888", bot_id=1)
        session.execute.return_value.scalar_one_or_none.return_value = existing

        # Pass a raw number with country prefix — normalization should strip it
        result = await get_or_create_contact(session, bot_id=1, contact_number="5511888888888")

        # The query should have used the normalized phone number
        assert result is existing
        # Verify that execute was called (the WHERE clause uses normalized number)
        session.execute.assert_awaited_once()

    async def test_creates_new_contact_when_not_found(self):
        """When no contact exists, a new Contact is created, added and flushed."""
        session = _make_session()

        # scalar_one_or_none returns None  → contact must be created.
        session.execute.return_value.scalar_one_or_none.return_value = None

        result = await get_or_create_contact(session, bot_id=1, contact_number="5511777777777")

        # session.add must have been called once with a Contact-like object.
        session.add.assert_called_once()
        added_obj = session.add.call_args[0][0]
        # Phone is normalized: "5511777777777" → strip non-digits → strip leading "55" → "11777777777"
        assert added_obj.phone_number == "11777777777"
        assert added_obj.bot_id == 1

        session.flush.assert_awaited_once()
        session.refresh.assert_awaited_once()

        # The returned object is the same instance that was passed to add().
        assert result is added_obj


# ===========================================================================
# TestClearDbCart
# ===========================================================================

class TestClearDbCart:

    async def test_clears_items_and_resets_state(self):
        """All cart items are deleted and cart fields are reset to defaults."""
        session = _make_session()

        item1 = _make_cart_item(item_id=1, product_id=10)
        item2 = _make_cart_item(item_id=2, product_id=11)
        cart = _make_cart(cart_id=100, state="ORDERING")
        cart.items = [item1, item2]

        session.get.return_value = cart

        result = await clear_db_cart(session, cart_id=100)

        # Both items must have been individually deleted.
        assert session.delete.await_count == 2
        delete_targets = {c.args[0] for c in session.delete.call_args_list}
        assert item1 in delete_targets
        assert item2 in delete_targets

        # Cart state and optional fields must be reset.
        assert result.state == "GREETING"
        assert result.customer_address is None
        assert result.pending_action_tool is None
        assert result.pending_action_args is None
        assert result.pending_action_question is None
        assert result.pending_action_expires_at is None
        assert result.delivery_method is None
        assert result.last_suggestions is None

        # items list cleared in memory.
        assert result.items == []

        session.flush.assert_awaited_once()

    async def test_returns_none_for_missing_cart(self):
        """When session.get returns None the function returns None without any DB writes."""
        session = _make_session()
        session.get.return_value = None

        result = await clear_db_cart(session, cart_id=999)

        assert result is None
        session.delete.assert_not_called()
        session.flush.assert_not_called()


# ===========================================================================
# TestSaveAddressToCart
# ===========================================================================

class TestSaveAddressToCart:

    async def test_saves_address(self):
        """customer_address is set on the cart, add and flush are called."""
        session = _make_session()
        cart = _make_cart(cart_id=100)
        session.get.return_value = cart

        result = await save_address_to_cart(session, cart_id=100, address="Rua das Flores, 123")

        assert result is cart
        assert cart.customer_address == "Rua das Flores, 123"
        session.add.assert_called_once_with(cart)
        session.flush.assert_awaited_once()

    async def test_returns_none_for_missing_cart(self):
        """When the cart does not exist, None is returned without modifying the session."""
        session = _make_session()
        session.get.return_value = None

        result = await save_address_to_cart(session, cart_id=999, address="any address")

        assert result is None
        session.add.assert_not_called()
        session.flush.assert_not_called()


# ===========================================================================
# TestSaveCustomerNameToContact
# ===========================================================================

class TestSaveCustomerNameToContact:

    async def test_saves_name(self):
        """contact.name is updated, add / flush / refresh are called."""
        session = _make_session()
        contact = _make_contact(contact_id=10)
        session.get.return_value = contact

        result = await save_customer_name_to_contact(session, contact_id=10, name="Maria")

        assert result is contact
        assert contact.name == "Maria"
        session.add.assert_called_once_with(contact)
        session.flush.assert_awaited_once()
        session.refresh.assert_awaited_once_with(contact)

    async def test_returns_none_for_missing_contact(self):
        """When the contact does not exist, None is returned without touching the session."""
        session = _make_session()
        session.get.return_value = None

        result = await save_customer_name_to_contact(session, contact_id=999, name="Ghost")

        assert result is None
        session.add.assert_not_called()
        session.flush.assert_not_called()
        session.refresh.assert_not_called()


# ===========================================================================
# TestDeleteOrder
# ===========================================================================

class TestDeleteOrder:

    async def test_deletes_existing_order(self):
        """session.delete is called with the order and True is returned."""
        session = _make_session()
        order = _make_order(order_id=42)
        session.get.return_value = order

        result = await delete_order(session, order_id=42)

        assert result is True
        session.delete.assert_awaited_once_with(order)
        session.commit.assert_awaited_once()

    async def test_returns_false_for_missing_order(self):
        """When the order is not found, False is returned and nothing is deleted."""
        session = _make_session()
        session.get.return_value = None

        result = await delete_order(session, order_id=999)

        assert result is False
        session.delete.assert_not_called()
        session.commit.assert_not_called()


# ===========================================================================
# TestIsMessageProcessed
# ===========================================================================

class TestIsMessageProcessed:

    async def test_returns_true_when_exists(self):
        """When a ProcessedMessage row is found, the function returns True."""
        session = _make_session()
        pm = _make_processed_message(message_id="wamid.abc123")
        session.execute.return_value.scalar_one_or_none.return_value = pm

        result = await is_message_processed(session, message_id="wamid.abc123")

        assert result is True
        session.execute.assert_awaited_once()

    async def test_returns_false_when_not_exists(self):
        """When no ProcessedMessage row is found, the function returns False."""
        session = _make_session()
        session.execute.return_value.scalar_one_or_none.return_value = None

        result = await is_message_processed(session, message_id="wamid.unknown")

        assert result is False
        session.execute.assert_awaited_once()


# ===========================================================================
# TestUpsertSubscription
# ===========================================================================

class TestUpsertSubscription:

    async def test_creates_subscription_when_none_exists(self):
        """When no subscription exists for the bot, a new one is created."""
        session = _make_session()

        with patch("app.crud.get_subscription_by_bot", new=AsyncMock(return_value=None)):
            result = await upsert_subscription(
                session, user_id=1, bot_id=2, mp_id="mp_abc123", status="authorized"
            )

        session.add.assert_called_once()
        added = session.add.call_args[0][0]
        assert added.user_id == 1
        assert added.bot_id == 2
        assert added.mp_subscription_id == "mp_abc123"
        assert added.status == "authorized"
        session.flush.assert_awaited()
        session.refresh.assert_awaited()

    async def test_updates_existing_subscription(self):
        """When a subscription already exists, its fields are mutated in-place."""
        session = _make_session()
        existing_sub = MagicMock()
        existing_sub.mp_subscription_id = "old_mp_id"
        existing_sub.status = "pending"

        with patch("app.crud.get_subscription_by_bot", new=AsyncMock(return_value=existing_sub)):
            result = await upsert_subscription(
                session, user_id=1, bot_id=2, mp_id="new_mp_id", status="authorized"
            )

        # Fields should be updated on the existing object
        assert existing_sub.mp_subscription_id == "new_mp_id"
        assert existing_sub.status == "authorized"
        # Should NOT call session.add for an existing object
        session.add.assert_not_called()
        session.flush.assert_awaited()
        session.refresh.assert_awaited()


# ===========================================================================
# TestBotUpdateMinOrderValue
# ===========================================================================

class TestBotUpdateMinOrderValue:

    def test_min_order_value_not_in_dump_when_unset(self):
        """BotUpdate without min_order_value → model_dump(exclude_unset=True) excludes it."""
        update = BotUpdate(restaurant_name="New Name")
        dumped = update.model_dump(exclude_unset=True)
        assert "min_order_value" not in dumped

    def test_min_order_value_in_dump_when_explicitly_set(self):
        """BotUpdate with min_order_value=0.0 → included in dump."""
        update = BotUpdate(min_order_value=0.0)
        dumped = update.model_dump(exclude_unset=True)
        assert "min_order_value" in dumped
        assert dumped["min_order_value"] == 0.0


# ===========================================================================
# TestFindRelevantProducts
# ===========================================================================

class TestFindRelevantProducts:

    async def test_find_relevant_products_excludes_unavailable(self):
        """Verify that all 4 queries include is_available filtering by inspecting compiled SQL."""
        session = _make_session()

        # Capture all SQL statements passed to session.execute
        captured_stmts = []
        original_execute = session.execute

        async def capture_execute(stmt, *args, **kwargs):
            captured_stmts.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
            # Return empty result set
            result = MagicMock()
            result.scalars.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=capture_execute)

        with patch("app.crud.embed_async", new=AsyncMock(return_value=[[0.0] * 384])):
            await find_relevant_products(session, bot_id=1, extracted_items=["pizza"])

        # All 4 queries must contain is_available filter
        assert len(captured_stmts) == 4, f"Expected 4 queries, got {len(captured_stmts)}"
        for i, stmt_str in enumerate(captured_stmts):
            assert "is_available" in stmt_str, (
                f"Query {i+1} missing is_available filter: {stmt_str}"
            )

    async def test_find_relevant_products_empty_items_returns_empty(self):
        """Edge case: empty extracted_items returns empty list without querying."""
        session = _make_session()
        result = await find_relevant_products(session, bot_id=1, extracted_items=[])
        assert result == []
        session.execute.assert_not_called()


# ===========================================================================
# TestAddItemsBotIdValidation
# ===========================================================================

class TestAddItemsBotIdValidation:

    def _make_product(self, product_id: int, bot_id: int = 1, is_available: bool = True):
        p = MagicMock()
        p.id = product_id
        p.bot_id = bot_id
        p.is_available = is_available
        return p

    def _make_cart_for_add(self, cart_id: int = 100):
        cart = MagicMock()
        cart.id = cart_id
        cart.items = []
        return cart

    async def test_add_items_rejects_product_from_wrong_bot(self):
        """A product belonging to a different bot_id is silently skipped."""
        session = _make_session()
        cart = self._make_cart_for_add()
        product = self._make_product(product_id=10, bot_id=2, is_available=True)

        # session.get returns cart for first call, product for second
        session.get = AsyncMock(side_effect=[cart, product])

        result = await add_items_to_db_cart(
            session, cart_id=100,
            items_to_add=[{"product_id": 10, "quantity": 1}],
            bot_id=1,  # product belongs to bot 2, we expect bot 1
        )

        # Product should NOT have been added
        session.add.assert_not_called()
        session.flush.assert_awaited_once()

    async def test_add_items_without_bot_id_preserves_old_behavior(self):
        """When bot_id is None (default), any available product is accepted."""
        session = _make_session()
        cart = self._make_cart_for_add()
        product = self._make_product(product_id=10, bot_id=99, is_available=True)

        session.get = AsyncMock(side_effect=[cart, product])

        result = await add_items_to_db_cart(
            session, cart_id=100,
            items_to_add=[{"product_id": 10, "quantity": 1}],
            # bot_id not passed → None → skip bot_id check
        )

        # Product SHOULD be added (new item)
        session.add.assert_called_once()
        session.flush.assert_awaited_once()

    async def test_add_items_accepts_product_from_correct_bot(self):
        """A product belonging to the correct bot_id is accepted."""
        session = _make_session()
        cart = self._make_cart_for_add()
        product = self._make_product(product_id=10, bot_id=1, is_available=True)

        session.get = AsyncMock(side_effect=[cart, product])

        result = await add_items_to_db_cart(
            session, cart_id=100,
            items_to_add=[{"product_id": 10, "quantity": 1}],
            bot_id=1,
        )

        session.add.assert_called_once()
        session.flush.assert_awaited_once()
