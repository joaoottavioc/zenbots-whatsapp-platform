# tests/test_local_extraction_integration.py
"""
Integration tests for T2-1: local extraction replaces LLM extraction on hot path.

Validates:
- _handle_shopping_intent uses extract_items_local (not LLM) for initial search
- LLM extract_potential_items is only called when local extraction + RAG finds nothing
- Error message paths also use local extraction
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.whatsapp import _handle_shopping_intent, MessageContext
from app.models import CartState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_product(pid, name, price=10.0):
    p = MagicMock()
    p.id = pid
    p.name = name
    p.price = price
    p.description = f"Delicious {name}"
    return p


def _make_ctx(text_body="quero 2 pizzas"):
    session = AsyncMock()
    session.refresh = AsyncMock()
    session.execute = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    session.execute.return_value = result_mock

    bot = MagicMock()
    bot.id = 1
    bot.restaurant_name = "Test"
    bot.delivery_fee = 5.0
    bot.min_order_value = 0.0

    cart = MagicMock()
    cart.id = 100
    cart.items = []
    cart.last_suggestions = None
    cart.state = CartState.SHOPPING

    contact = MagicMock()
    contact.id = 10

    return MessageContext(
        session=session,
        bot=bot,
        contact=contact,
        cart=cart,
        contact_number="5511999",
        text_body=text_body,
        token="fake-token",
        phone_id="fake-phone",
    )


PATCH_CRUD = "app.whatsapp.crud"
PATCH_GET_AI = "app.whatsapp.get_ai_decision"
PATCH_EXTRACT_LLM = "app.whatsapp.extract_potential_items"
PATCH_EXTRACT_LOCAL = "app.whatsapp.extract_items_local"
PATCH_CREATE_PROMPT = "app.whatsapp.create_central_prompt"


def _make_ai_msg_answer_conversationally(text="Ok!"):
    """Make AI response that uses answer_conversationally (not add_items)."""
    ai_msg = MagicMock()
    tool_call = MagicMock()
    tool_call.function.name = "answer_conversationally"
    tool_call.function.arguments = json.dumps({"response": text})
    ai_msg.tool_calls = [tool_call]
    return ai_msg


class TestLocalExtractionHotPath:
    @pytest.mark.asyncio
    async def test_local_extraction_called_for_search(self):
        """extract_items_local is called for the RAG search, not LLM extraction."""
        mctx = _make_ctx("quero 2 pizzas")
        pizza = _make_product(1, "Pizza Margherita")

        # Use answer_conversationally to avoid the add_items_to_cart flow
        ai_msg = _make_ai_msg_answer_conversationally("Adicionei a pizza!")

        with (
            patch(PATCH_CRUD) as crud_mock,
            patch(PATCH_GET_AI, AsyncMock(return_value=ai_msg)),
            patch(PATCH_EXTRACT_LOCAL, return_value=["pizzas"]) as local_mock,
            patch(PATCH_EXTRACT_LLM, AsyncMock(return_value=["pizza"])) as llm_mock,
            patch(PATCH_CREATE_PROMPT, return_value=[]),
        ):
            crud_mock.get_history_for_contact = AsyncMock(return_value=[])
            crud_mock.find_relevant_products = AsyncMock(return_value=[pizza])

            await _handle_shopping_intent(mctx, "ADD")

        # Local extraction called for the search
        local_mock.assert_called_with("quero 2 pizzas")
        # LLM extraction NOT called when RAG found products
        llm_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_llm_fallback_when_rag_finds_nothing(self):
        """extract_potential_items is called as fallback when local + RAG finds nothing."""
        mctx = _make_ctx("me vê um oeuf poché")
        oeuf = _make_product(5, "Oeuf Poché")

        ai_msg = _make_ai_msg_answer_conversationally("Adicionei!")

        with (
            patch(PATCH_CRUD) as crud_mock,
            patch(PATCH_GET_AI, AsyncMock(return_value=ai_msg)),
            patch(PATCH_EXTRACT_LOCAL, return_value=["oeuf poché"]) as local_mock,
            patch(
                PATCH_EXTRACT_LLM, AsyncMock(return_value=["oeuf poché"])
            ) as llm_mock,
            patch(PATCH_CREATE_PROMPT, return_value=[]),
        ):
            crud_mock.get_history_for_contact = AsyncMock(return_value=[])
            # First call (local extraction) returns nothing, second call (LLM) returns product
            crud_mock.find_relevant_products = AsyncMock(side_effect=[[], [oeuf]])

            await _handle_shopping_intent(mctx, "ADD")

        local_mock.assert_called_once()
        llm_mock.assert_awaited_once()  # LLM called as fallback
        # find_relevant_products called twice: once with local terms, once with LLM terms
        assert crud_mock.find_relevant_products.call_count == 2

    @pytest.mark.asyncio
    async def test_no_llm_fallback_when_rag_has_results(self):
        """When local extraction + RAG finds products, no LLM fallback needed."""
        mctx = _make_ctx("quero pizza e coca")
        products = [_make_product(1, "Pizza"), _make_product(2, "Coca-Cola")]

        ai_msg = _make_ai_msg_answer_conversationally("Adicionei!")

        with (
            patch(PATCH_CRUD) as crud_mock,
            patch(PATCH_GET_AI, AsyncMock(return_value=ai_msg)),
            patch(PATCH_EXTRACT_LOCAL, return_value=["pizza", "coca"]) as local_mock,
            patch(
                PATCH_EXTRACT_LLM, AsyncMock(return_value=["pizza", "coca"])
            ) as llm_mock,
            patch(PATCH_CREATE_PROMPT, return_value=[]),
        ):
            crud_mock.get_history_for_contact = AsyncMock(return_value=[])
            crud_mock.find_relevant_products = AsyncMock(return_value=products)

            await _handle_shopping_intent(mctx, "ADD")

        local_mock.assert_called_once()
        llm_mock.assert_not_awaited()
        # find_relevant_products called only once (no LLM fallback needed)
        crud_mock.find_relevant_products.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_not_called_when_llm_returns_empty(self):
        """When LLM extraction returns empty, no second RAG call happens."""
        mctx = _make_ctx("xyzzy nonsense")

        ai_msg = _make_ai_msg_answer_conversationally("Não entendi")

        with (
            patch(PATCH_CRUD) as crud_mock,
            patch(PATCH_GET_AI, AsyncMock(return_value=ai_msg)),
            patch(PATCH_EXTRACT_LOCAL, return_value=["xyzzy nonsense"]) as local_mock,
            patch(PATCH_EXTRACT_LLM, AsyncMock(return_value=[])) as llm_mock,
            patch(PATCH_CREATE_PROMPT, return_value=[]),
        ):
            crud_mock.get_history_for_contact = AsyncMock(return_value=[])
            crud_mock.find_relevant_products = AsyncMock(return_value=[])

            await _handle_shopping_intent(mctx, "ADD")

        local_mock.assert_called_once()
        llm_mock.assert_awaited_once()  # LLM called since RAG found nothing
        # find_relevant_products called only once (LLM returned empty, no second RAG call)
        crud_mock.find_relevant_products.assert_called_once()


class TestErrorMessageUsesLocalExtraction:
    @pytest.mark.asyncio
    async def test_empty_items_arg_uses_local_extraction(self):
        """When AI returns empty items in add_items_to_cart, error message uses local extraction."""
        mctx = _make_ctx("quero bolo de fubá")

        ai_msg = MagicMock()
        tool_call = MagicMock()
        tool_call.function.name = "add_items_to_cart"
        tool_call.function.arguments = json.dumps({"items": []})
        ai_msg.tool_calls = [tool_call]

        with (
            patch(PATCH_CRUD) as crud_mock,
            patch(PATCH_GET_AI, AsyncMock(return_value=ai_msg)),
            patch(PATCH_EXTRACT_LOCAL, return_value=["bolo fubá"]),
            patch(PATCH_EXTRACT_LLM, AsyncMock(return_value=["bolo de fubá"])),
            patch(PATCH_CREATE_PROMPT, return_value=[]),
            # Bypass Pydantic validation so we reach the empty-items branch
            patch("app.tool_arg_schemas.TOOL_VALIDATORS", {}),
        ):
            crud_mock.get_history_for_contact = AsyncMock(return_value=[])
            crud_mock.find_relevant_products = AsyncMock(return_value=[])

            result = await _handle_shopping_intent(mctx, "ADD")

        # Error message should contain the locally extracted item name
        assert "bolo fubá" in result
        assert "cardápio" in result  # standard error message
