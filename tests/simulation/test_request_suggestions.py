"""
Simulation tests: REQUEST_SUGGESTION scenarios.

5 scenarios testing that suggestion requests NEVER add items to the cart
and that the bot responds with product suggestions. Covers informal
phrasing, abbreviations, indecisive customers, and ambiguous requests.

Run with: docker compose exec backend pytest tests/simulation/test_request_suggestions.py -v
"""

import pytest


def assert_cart_empty(cart_items):
    """Assert the cart has no items."""
    assert len(cart_items) == 0, (
        f"Cart should be empty but has {len(cart_items)} items: {cart_items}"
    )


def assert_has_suggestions(response: str):
    """Assert the response contains numbered product suggestions."""
    assert response is not None, "No response received"
    # Suggestions use numbered emoji (1️⃣, 2️⃣) or bold product names
    has_numbered = "1️⃣" in response or "1." in response
    has_bold_product = "*" in response and "R$" in response
    assert has_numbered or has_bold_product, (
        f"Response should contain product suggestions. Got: {response[:300]}"
    )


class TestRequestSuggestions:
    """5 scenarios ensuring suggestion requests show products without cart mutations."""

    @pytest.mark.asyncio
    async def test_informal_abbreviation(self, sim_context):
        """Informal slang: 'o que tem de bom ai?' should show suggestions, not add items."""
        await sim_context.send("oi")
        response = await sim_context.send("o que tem de bom ai?")

        cart = await sim_context.get_cart_items()
        assert_cart_empty(cart)
        assert_has_suggestions(response)

    @pytest.mark.asyncio
    async def test_indecisive_plea_for_help(self, sim_context):
        """Indecisive customer: 'to sem ideia do que pedir, me ajuda' — should guide, not add."""
        await sim_context.send("oi")
        response = await sim_context.send("to sem ideia do que pedir, me ajuda")

        cart = await sim_context.get_cart_items()
        assert_cart_empty(cart)
        assert response is not None, "Bot should respond, not stay silent"

    @pytest.mark.asyncio
    async def test_asking_for_popular_items(self, sim_context):
        """'quais são os mais pedidos?' — popularity question, not an order."""
        await sim_context.send("oi")
        response = await sim_context.send("quais são os mais pedidos?")

        cart = await sim_context.get_cart_items()
        assert_cart_empty(cart)
        assert response is not None, "Bot should respond, not stay silent"

    @pytest.mark.asyncio
    async def test_first_timer_asking_recommendation(self, sim_context):
        """'primeira vez aqui, o que recomenda?' — new customer asking for recs."""
        await sim_context.send("oi")
        response = await sim_context.send("primeira vez aqui, o que recomenda?")

        cart = await sim_context.get_cart_items()
        assert_cart_empty(cart)
        assert_has_suggestions(response)

    @pytest.mark.asyncio
    async def test_dicas_de_pedidos(self, sim_context):
        """'tem dicas de pedidos?' — must NOT trigger ORDER_REPEAT."""
        await sim_context.send("oi")
        response = await sim_context.send("tem dicas de pedidos?")

        cart = await sim_context.get_cart_items()
        assert_cart_empty(cart)
        assert_has_suggestions(response)
