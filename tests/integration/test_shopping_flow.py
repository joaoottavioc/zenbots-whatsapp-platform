# tests/integration/test_shopping_flow.py
"""
Tier 2: Mock-LLM integration tests for the shopping flow.

Tests the full pipeline: user message → extraction → prompt construction →
mocked LLM tool call → cart mutation. Exercises the orchestration logic
that sits between extraction and cart state.

Requires Docker DB (like other integration tests). The LLM is mocked
to return deterministic tool calls.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.item_extraction import extract_items_local
from app.prompt_central import create_central_prompt
from app.crud import find_relevant_products, find_unavailable_products


def _fake_product(id, name, category="Lanches", description="desc"):
    """Lightweight fake product for prompt tests that don't need DB."""
    p = MagicMock()
    p.id = id
    p.name = name
    p.category = category
    p.description = description
    return p


@pytest.mark.integration
class TestPromptConstruction:
    """Verify prompt content by running extraction → prompt chain."""

    @pytest.mark.asyncio
    async def test_structured_query_in_user_message(
        self, db_session, test_bot, test_products
    ):
        """Structured rewrite appears as user query in prompt."""
        from app.item_extraction import (
            extract_items_with_quantities,
            rewrite_as_structured_order,
        )

        text = "um classic burger dois spicy wings"
        pairs = extract_items_with_quantities(text)
        structured = rewrite_as_structured_order(pairs)
        assert structured is not None

        products = await find_relevant_products(
            db_session, test_bot["bot_id"], extract_items_local(text)
        )

        prompt = create_central_prompt(
            user_query=structured,
            history=[],
            restaurant_name="Test",
            cart_items=[],
            search_results=products,
        )

        # The user message should contain the structured rewrite
        user_msgs = [m for m in prompt if m["role"] == "user"]
        assert any("1x classic burger" in m["content"].lower() for m in user_msgs)
        assert any("2x spicy wings" in m["content"].lower() for m in user_msgs)

    @pytest.mark.asyncio
    async def test_full_menu_in_dynamic_system(
        self, db_session, test_bot, test_products
    ):
        """All available products appear in the dynamic system message."""
        from sqlmodel import select
        from app.models import Product

        result = await db_session.execute(
            select(Product).where(
                Product.bot_id == test_bot["bot_id"],
                Product.is_available.is_(True),
                Product.is_deleted.is_(False),
            )
        )
        all_products = list(result.scalars().all())

        prompt = create_central_prompt(
            user_query="quero tudo",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            search_results=all_products,
        )

        all_content = " ".join(m.get("content") or "" for m in prompt)
        for p in all_products:
            assert f"(ID: {p.id})" in all_content, (
                f"{p.name} (ID:{p.id}) missing from prompt"
            )

    @pytest.mark.asyncio
    async def test_unavailable_with_variant_annotation(
        self, db_session, test_bot, test_products
    ):
        """Unavailable product shows 'NÃO substitua' with variant names."""
        unavail = await find_unavailable_products(
            db_session, test_bot["bot_id"], ["picanha com catupiry"]
        )
        if not unavail:
            pytest.skip("PICANHA COM CATUPIRY not found as unavailable")

        # Build variant map (same logic as whatsapp.py)
        from sqlmodel import select
        from app.models import Product

        result = await db_session.execute(
            select(Product).where(
                Product.bot_id == test_bot["bot_id"],
                Product.is_available.is_(True),
                Product.is_deleted.is_(False),
            )
        )
        full_menu = list(result.scalars().all())

        variant_map = {}
        for up in unavail:
            root = None
            for w in up.name.split():
                if len(w) > 2:
                    root = w.lower()
                    break
            if root:
                variants = [
                    p.name
                    for p in full_menu
                    if p.name.lower().startswith(root) and p.id != up.id
                ]
                if variants:
                    variant_map[up.name] = variants

        prompt = create_central_prompt(
            user_query="quero picanha",
            history=[],
            restaurant_name="Test",
            cart_items=[],
            search_results=full_menu,
            unavailable_products=unavail,
            variant_map=variant_map,
        )

        all_content = " ".join(m.get("content") or "" for m in prompt)
        assert "NÃO substitua por:" in all_content
        # PICANHA and PICANHA COM BACON should be listed as variants
        assert "PICANHA" in all_content

    @pytest.mark.asyncio
    async def test_cart_items_in_context(self, db_session, test_bot, test_products):
        """Pre-existing cart items appear in the prompt's cart section."""
        cart_items = [
            {
                "product_id": test_products["CLASSIC BURGER"],
                "name": "CLASSIC BURGER",
                "quantity": 3,
            },
        ]

        prompt = create_central_prompt(
            user_query="mais alguma coisa",
            history=[],
            restaurant_name="Test",
            cart_items=cart_items,
            search_results=[],
        )

        all_content = " ".join(m.get("content") or "" for m in prompt)
        assert "3x CLASSIC BURGER" in all_content
        assert f"ID: {test_products['CLASSIC BURGER']}" in all_content


@pytest.mark.integration
class TestToolDispatchToCart:
    """Mock LLM response and verify cart mutations."""

    def _make_tool_call(self, name, arguments):
        """Create a mock tool call matching OpenAI's format."""
        tc = MagicMock()
        tc.id = "call_test123"
        tc.function = MagicMock()
        tc.function.name = name
        tc.function.arguments = (
            json.dumps(arguments) if isinstance(arguments, dict) else arguments
        )
        return tc

    def _make_ai_message(self, tool_calls=None, content=None):
        """Create a mock AI message matching OpenAI ChatCompletionMessage."""
        msg = MagicMock()
        msg.content = content
        msg.tool_calls = tool_calls or []
        return msg

    @pytest.mark.asyncio
    async def test_add_items_creates_cart_entries(
        self, db_session, test_bot, test_products
    ):
        """Mocked add_items_to_cart tool call creates cart entries."""
        from app.models import (
            Contact,
            ShoppingCart,
            CartItem,
            CartState,
            Product,
        )
        from app.whatsapp import MessageContext, _handle_shopping_intent
        from sqlmodel import select

        # Create contact + cart
        contact = Contact(
            bot_id=test_bot["bot_id"],
            phone_number="5500088880001",
        )
        db_session.add(contact)
        await db_session.flush()

        cart = ShoppingCart(
            contact_id=contact.id,
            state=CartState.SHOPPING,
        )
        db_session.add(cart)
        await db_session.flush()
        await db_session.refresh(cart)

        # Load bot
        from app.models import Bot

        bot = await db_session.get(Bot, test_bot["bot_id"])

        burger_id = test_products["CLASSIC BURGER"]
        wings_id = test_products["SPICY WINGS"]

        mock_ai = self._make_ai_message(
            tool_calls=[
                self._make_tool_call(
                    "add_items_to_cart",
                    {
                        "items": [
                            {"product_id": burger_id, "quantity": 2},
                            {"product_id": wings_id, "quantity": 5},
                        ]
                    },
                )
            ]
        )

        mctx = MessageContext(
            session=db_session,
            bot=bot,
            contact=contact,
            cart=cart,
            contact_number="5500088880001",
            text_body="dois classic burger e cinco spicy wings",
            token="fake-token",
            phone_id="fake-phone-id",
        )

        with patch(
            "app.whatsapp.get_ai_decision",
            AsyncMock(return_value=mock_ai),
        ):
            result = await _handle_shopping_intent(mctx, "ADD")

        assert result is not None

        # Verify cart items
        items_r = await db_session.execute(
            select(CartItem, Product)
            .join(Product, CartItem.product_id == Product.id)
            .where(CartItem.cart_id == cart.id)
        )
        items = {p.name: ci.quantity for ci, p in items_r}
        assert items.get("CLASSIC BURGER") == 2
        assert items.get("SPICY WINGS") == 5

    @pytest.mark.asyncio
    async def test_hallucinated_id_handled_gracefully(
        self, db_session, test_bot, test_products
    ):
        """Non-existent product ID doesn't crash — logged as error."""
        from app.models import (
            Contact,
            ShoppingCart,
            CartItem,
            CartState,
        )
        from app.whatsapp import MessageContext, _handle_shopping_intent
        from app.models import Bot
        from sqlmodel import select

        contact = Contact(
            bot_id=test_bot["bot_id"],
            phone_number="5500088880002",
        )
        db_session.add(contact)
        await db_session.flush()

        cart = ShoppingCart(
            contact_id=contact.id,
            state=CartState.SHOPPING,
        )
        db_session.add(cart)
        await db_session.flush()
        await db_session.refresh(cart)

        bot = await db_session.get(Bot, test_bot["bot_id"])

        mock_ai = self._make_ai_message(
            tool_calls=[
                self._make_tool_call(
                    "add_items_to_cart",
                    {"items": [{"product_id": 99999, "quantity": 1}]},
                )
            ]
        )

        mctx = MessageContext(
            session=db_session,
            bot=bot,
            contact=contact,
            cart=cart,
            contact_number="5500088880002",
            text_body="um xyz inexistente",
            token="fake-token",
            phone_id="fake-phone-id",
        )

        with patch(
            "app.whatsapp.get_ai_decision",
            AsyncMock(return_value=mock_ai),
        ):
            # Should not crash
            await _handle_shopping_intent(mctx, "ADD")

        # Cart should be empty (hallucinated ID skipped)
        items_r = await db_session.execute(
            select(CartItem).where(CartItem.cart_id == cart.id)
        )
        assert len(list(items_r.scalars().all())) == 0

    @pytest.mark.asyncio
    async def test_variant_removed_after_dispatch(
        self, db_session, test_bot, test_products
    ):
        """Post-process removes auto-substituted variant from cart."""
        from app.models import (
            Contact,
            ShoppingCart,
            CartItem,
            CartState,
            Product,
        )
        from app.whatsapp import MessageContext, _handle_shopping_intent
        from app.models import Bot
        from sqlmodel import select

        contact = Contact(
            bot_id=test_bot["bot_id"],
            phone_number="5500088880003",
        )
        db_session.add(contact)
        await db_session.flush()

        cart = ShoppingCart(
            contact_id=contact.id,
            state=CartState.SHOPPING,
        )
        db_session.add(cart)
        await db_session.flush()
        await db_session.refresh(cart)

        bot = await db_session.get(Bot, test_bot["bot_id"])

        # The LLM auto-substitutes PICANHA COM CATUPIRY (unavail) →
        # PICANHA COM BACON (avail variant)
        bacon_id = test_products["PICANHA COM BACON"]
        burger_id = test_products["CLASSIC BURGER"]

        mock_ai = self._make_ai_message(
            tool_calls=[
                self._make_tool_call(
                    "add_items_to_cart",
                    {
                        "items": [
                            {"product_id": bacon_id, "quantity": 1},
                            {"product_id": burger_id, "quantity": 3},
                        ]
                    },
                )
            ]
        )

        mctx = MessageContext(
            session=db_session,
            bot=bot,
            contact=contact,
            cart=cart,
            contact_number="5500088880003",
            text_body="um picanha com catupiry e tres classic burger",
            token="fake-token",
            phone_id="fake-phone-id",
        )

        with patch(
            "app.whatsapp.get_ai_decision",
            AsyncMock(return_value=mock_ai),
        ):
            await _handle_shopping_intent(mctx, "ADD")

        # Verify: CLASSIC BURGER should remain, PICANHA COM BACON removed
        items_r = await db_session.execute(
            select(CartItem, Product)
            .join(Product, CartItem.product_id == Product.id)
            .where(CartItem.cart_id == cart.id)
        )
        items = {p.name: ci.quantity for ci, p in items_r}
        assert items.get("CLASSIC BURGER") == 3
        # Variant should be removed by post-process
        assert "PICANHA COM BACON" not in items

    @pytest.mark.asyncio
    async def test_conversational_tool_no_cart_change(
        self, db_session, test_bot, test_products
    ):
        """answer_conversationally tool doesn't modify cart."""
        from app.models import (
            Contact,
            ShoppingCart,
            CartItem,
            CartState,
        )
        from app.whatsapp import MessageContext, _handle_shopping_intent
        from app.models import Bot
        from sqlmodel import select

        contact = Contact(
            bot_id=test_bot["bot_id"],
            phone_number="5500088880004",
        )
        db_session.add(contact)
        await db_session.flush()

        cart = ShoppingCart(
            contact_id=contact.id,
            state=CartState.SHOPPING,
        )
        db_session.add(cart)
        await db_session.flush()
        await db_session.refresh(cart)

        bot = await db_session.get(Bot, test_bot["bot_id"])

        mock_ai = self._make_ai_message(
            tool_calls=[
                self._make_tool_call(
                    "answer_conversationally",
                    {"response_to_user": "Temos várias opções!"},
                )
            ]
        )

        mctx = MessageContext(
            session=db_session,
            bot=bot,
            contact=contact,
            cart=cart,
            contact_number="5500088880004",
            text_body="o que vocês tem de bom?",
            token="fake-token",
            phone_id="fake-phone-id",
        )

        with patch(
            "app.whatsapp.get_ai_decision",
            AsyncMock(return_value=mock_ai),
        ):
            await _handle_shopping_intent(mctx, "ADD")

        # Cart should be empty
        items_r = await db_session.execute(
            select(CartItem).where(CartItem.cart_id == cart.id)
        )
        assert len(list(items_r.scalars().all())) == 0
