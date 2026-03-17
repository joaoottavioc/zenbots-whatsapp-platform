"""Tests for L1: Multi-intent / multi-tool dispatch.

Verifies that when the LLM returns multiple tool_calls (e.g., add_items +
answer_conversationally), ALL tool calls are processed — not just the first.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_tool_call(name, args):
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    return tc


def _make_ai_message(tool_calls):
    msg = MagicMock()
    msg.tool_calls = tool_calls
    return msg


@pytest.mark.asyncio
async def test_cart_tool_followed_by_conversational_tool():
    """After add_items_to_cart, a following answer_conversationally should
    be processed and appended to the response."""
    from app.whatsapp import _handle_shopping_intent, MessageContext
    from app.models import CartState

    # Build tool calls: add + answer
    tool_calls = [
        _make_tool_call(
            "add_items_to_cart", {"items": [{"product_id": 1, "quantity": 1}]}
        ),
        _make_tool_call(
            "answer_conversationally", {"response_text": "Temos sobremesas também!"}
        ),
    ]
    ai_message = _make_ai_message(tool_calls)

    # Mock context
    cart = MagicMock()
    cart.id = 1
    cart.state = CartState.SHOPPING
    cart.items = [MagicMock(product_id=1, quantity=1, product=MagicMock(name="Pizza"))]
    cart.last_suggestions = None
    cart.last_activity_at = None
    cart.pix_only = False

    bot = MagicMock()
    bot.id = 10
    bot.restaurant_name = "Test Pizzaria"
    bot.delivery_fee = 5.0
    bot.min_order_amount = 0
    bot.pix_only = False

    session = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()

    mock_product = MagicMock(id=1, name="Pizza", price=30.0)

    # After adding item, cart has items
    async def mock_refresh(obj, attribute_names=None):
        if attribute_names and "items" in attribute_names:
            obj.items = [
                MagicMock(
                    product_id=1,
                    quantity=1,
                    product=mock_product,
                )
            ]

    session.refresh = mock_refresh

    # Mock session.execute for _load_cart_items_with_products product query
    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [mock_product]
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    session.execute = AsyncMock(return_value=mock_result)

    mctx = MessageContext(
        session=session,
        bot=bot,
        contact=MagicMock(),
        cart=cart,
        contact_number="5511999",
        text_body="quero uma pizza e veja as sobremesas",
        token="tok",
        phone_id="pid",
    )

    contact = MagicMock()
    contact.id = 100
    mctx.contact = contact

    with (
        patch("app.whatsapp.extract_potential_items", return_value=["pizza"]),
        patch(
            "app.whatsapp.crud.find_relevant_products",
            return_value=[
                MagicMock(id=1, name="Pizza", price=30.0, is_available=True, bot_id=10)
            ],
        ),
        patch(
            "app.whatsapp.crud.get_history_for_contact",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("app.whatsapp.create_central_prompt", return_value=[]),
        patch("app.whatsapp.get_ai_decision", return_value=ai_message),
        patch("app.whatsapp.crud.add_items_to_db_cart", new_callable=AsyncMock),
        patch("app.whatsapp.send_whatsapp_message", new_callable=AsyncMock),
        patch("app.whatsapp.crud.add_interaction_to_history", new_callable=AsyncMock),
        patch("app.whatsapp.sanitize_llm_output", side_effect=lambda x: x),
        patch(
            "app.whatsapp._build_cart_summary_message",
            return_value="🛒 Carrinho: 1x Pizza",
        ),
        patch("app.whatsapp.clear_pending"),
    ):
        result = await _handle_shopping_intent(mctx, "ADD")

    # The result should contain the conversational response (appended after cart action)
    assert result is not None
    assert "Temos sobremesas também!" in result


@pytest.mark.asyncio
async def test_second_cart_tool_is_skipped():
    """If LLM returns two cart-modifying tools, only the first should execute."""
    from app.whatsapp import _handle_shopping_intent, MessageContext
    from app.models import CartState

    tool_calls = [
        _make_tool_call(
            "add_items_to_cart", {"items": [{"product_id": 1, "quantity": 1}]}
        ),
        _make_tool_call("remove_items_from_cart", {"product_ids": [2]}),
    ]
    ai_message = _make_ai_message(tool_calls)

    cart = MagicMock()
    cart.id = 1
    cart.state = CartState.SHOPPING
    cart.items = [MagicMock(product_id=1, quantity=1, product=MagicMock(name="Pizza"))]
    cart.last_suggestions = None
    cart.pix_only = False

    bot = MagicMock()
    bot.id = 10
    bot.restaurant_name = "Test"
    bot.delivery_fee = 5.0
    bot.min_order_amount = 0
    bot.pix_only = False

    session = AsyncMock()
    session.flush = AsyncMock()
    session.add = MagicMock()

    mock_product = MagicMock(id=1, name="Pizza", price=30.0)

    async def mock_refresh(obj, attribute_names=None):
        if attribute_names and "items" in attribute_names:
            obj.items = [
                MagicMock(
                    product_id=1,
                    quantity=1,
                    product=mock_product,
                )
            ]

    session.refresh = mock_refresh

    mock_scalars = MagicMock()
    mock_scalars.all.return_value = [mock_product]
    mock_result = MagicMock()
    mock_result.scalars.return_value = mock_scalars
    session.execute = AsyncMock(return_value=mock_result)

    mctx = MessageContext(
        session=session,
        bot=bot,
        contact=MagicMock(),
        cart=cart,
        contact_number="5511999",
        text_body="add pizza remove soda",
        token="tok",
        phone_id="pid",
    )

    contact = MagicMock()
    contact.id = 100
    mctx.contact = contact

    with (
        patch("app.whatsapp.extract_potential_items", return_value=["pizza"]),
        patch(
            "app.whatsapp.crud.find_relevant_products",
            return_value=[
                MagicMock(id=1, name="Pizza", price=30.0, is_available=True, bot_id=10)
            ],
        ),
        patch(
            "app.whatsapp.crud.get_history_for_contact",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch("app.whatsapp.create_central_prompt", return_value=[]),
        patch("app.whatsapp.get_ai_decision", return_value=ai_message),
        patch(
            "app.whatsapp.crud.add_items_to_db_cart", new_callable=AsyncMock
        ) as mock_add,
        patch(
            "app.whatsapp.crud.modify_item_quantity_in_db_cart", new_callable=AsyncMock
        ) as mock_remove,
        patch("app.whatsapp.send_whatsapp_message", new_callable=AsyncMock),
        patch("app.whatsapp.crud.add_interaction_to_history", new_callable=AsyncMock),
        patch("app.whatsapp._build_cart_summary_message", return_value="🛒 Carrinho"),
        patch("app.whatsapp.clear_pending"),
    ):
        await _handle_shopping_intent(mctx, "ADD")

    # add was called but remove was NOT (second cart tool skipped)
    mock_add.assert_called_once()
    mock_remove.assert_not_called()
